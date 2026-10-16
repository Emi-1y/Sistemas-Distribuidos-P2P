"""Reparación de réplicas de archivos confirmados.

Cuando un peer desaparece o una réplica se corrompe, los bloques de un archivo
pueden quedar por debajo del factor de réplica configurado. Este módulo
verifica el estado real de las réplicas y las reconstruye copiando bloques
entre peers.

El coordinador de la reparación es el dueño de la entrada en metadatos. Eso
mantiene la simetría del sistema: cualquier peer puede recibir la petición, y
si no es el dueño la reenvía.
"""

import hashlib

import httpx

from server import blocks, config, metadata, ring, security


TIMEOUT = 10.0


def _peer_get(url: str):
    kwargs = {"timeout": TIMEOUT, "headers": security.peer_headers()}
    if url.startswith("https"):
        kwargs["verify"] = security.httpx_verify()
    return httpx.get(url, **kwargs)


def _peer_head(url: str):
    kwargs = {"timeout": TIMEOUT, "headers": security.peer_headers()}
    if url.startswith("https"):
        kwargs["verify"] = security.httpx_verify()
    return httpx.head(url, **kwargs)


def _peer_put(url: str, data, headers):
    kwargs = {
        "timeout": TIMEOUT,
        "data": data,
        "headers": security.peer_headers(headers),
    }
    if url.startswith("https"):
        kwargs["verify"] = security.httpx_verify()
    return httpx.put(url, **kwargs)


def repair(path: str, user: str | None = None, internal: bool = False, excluded_peers: set[str] | None = None) -> dict:
    """Repara las réplicas de un archivo confirmado.

    Devuelve un resumen de los cambios realizados. Si el archivo ya tiene
    todas sus réplicas, no mueve ningún byte.

    Reparar es una operación sensible sobre el archivo: solo el propietario
    puede lanzarla. `internal=True` la marca como reparación del sistema (el
    heartbeat automático tras caerse un peer), que no tiene usuario y actúa en
    nombre del sistema para preservar la durabilidad.
    """
    # La entrada se lee sin filtro de lectura: la autorización de reparación es
    # de escritura, y se comprueba a continuación de forma explícita para dar
    # un 403 claro.
    entrada = metadata.get_entry(path)

    if not internal and not security.can_write(entrada.get("owner"), user):
        raise metadata.HTTPException(
            status_code=403,
            detail="No autorizado a reparar este archivo"
        )

    file_id = entrada["file_id"]
    replicas_esperadas = entrada.get("replication_factor", config.REPLICAS)
    cambios = []

    excluded_peers = excluded_peers or set()

    for bloque in entrada["blocks"]:
        index = bloque["index"]
        checksum = bloque["checksum"]
        peers_actuales = _peers_que_tienen_bloque(
            file_id, index, bloque["peers"]
        )
        faltantes = replicas_esperadas - len(peers_actuales)

        if faltantes <= 0:
            continue

        if not peers_actuales:
            raise metadata.HTTPException(
                status_code=422,
                detail=f"El bloque {index} no tiene ninguna réplica viva"
            )

        fuente = peers_actuales[0]
        nuevos = _elegir_nuevos_peers(
            file_id, index, peers_actuales, faltantes, excluded_peers
        )

        for peer_id in nuevos:
            _copiar_bloque(file_id, index, checksum, fuente, peer_id)
            peers_actuales.append(peer_id)
            cambios.append({"index": index, "from": fuente, "to": peer_id})

        metadata.update_block_peers(path, file_id, index, peers_actuales)

    return {
        "path": path,
        "file_id": file_id,
        "repaired": len(cambios) > 0,
        "replication_factor": replicas_esperadas,
        "changes": cambios
    }


def _peers_que_tienen_bloque(
    file_id: str,
    index: int,
    peers_planificados: list[str]
) -> list[str]:
    """Pregunta a los peers del plan si realmente tienen el bloque."""
    tienen = []

    for peer_id in peers_planificados:
        if peer_id == config.PEER_ID:
            if blocks.block_exists(file_id, index):
                tienen.append(peer_id)
        elif _peer_tiene_bloque(peer_id, file_id, index):
            tienen.append(peer_id)

    return tienen


def _peer_tiene_bloque(peer_id: str, file_id: str, index: int) -> bool:
    """HEAD /blocks/{file_id}/{index} al peer remoto."""
    try:
        address = ring.address_of(peer_id)
    except KeyError:
        return False

    try:
        respuesta = _peer_head(f"{address}/blocks/{file_id}/{index}")
        return respuesta.status_code == 200
    except httpx.HTTPError:
        return False


def _elegir_nuevos_peers(
    file_id: str,
    index: int,
    peers_existentes: list[str],
    cantidad: int,
    excluded_peers: set[str] | None = None,
) -> list[str]:
    """Elige peers del anillo que no tengan ya el bloque ni estén excluidos."""
    excluded_peers = excluded_peers or set()

    candidatos = ring.choose_nodes(
        f"{file_id}:{index}",
        len(ring.LOCAL.peers())
    )

    return [
        peer_id
        for peer_id in candidatos
        if (
            peer_id not in peers_existentes
            and peer_id not in excluded_peers
        )
    ][:cantidad]


def _copiar_bloque(
    file_id: str,
    index: int,
    checksum_esperado: str,
    fuente: str,
    destino: str
) -> None:
    """Descarga el bloque de la fuente y lo sube al destino."""
    try:
        address_fuente = ring.address_of(fuente)
        address_destino = ring.address_of(destino)
    except KeyError as exc:
        raise metadata.HTTPException(
            status_code=422,
            detail="Peer desconocido en el anillo"
        ) from exc

    try:
        respuesta = _peer_get(f"{address_fuente}/blocks/{file_id}/{index}")
        respuesta.raise_for_status()
    except httpx.HTTPError as exc:
        raise metadata.HTTPException(
            status_code=503,
            detail=f"No se pudo leer el bloque {index} de {fuente}"
        ) from exc

    datos = respuesta.content

    if hashlib.sha256(datos).hexdigest() != checksum_esperado:
        raise metadata.HTTPException(
            status_code=422,
            detail=f"El bloque {index} llegó corrupto desde {fuente}"
        )

    try:
        respuesta = _peer_put(
            f"{address_destino}/blocks/{file_id}/{index}",
            data=datos,
            headers={"X-Block-Checksum": checksum_esperado},
        )
        respuesta.raise_for_status()
    except httpx.HTTPError as exc:
        raise metadata.HTTPException(
            status_code=503,
            detail=f"No se pudo escribir el bloque {index} en {destino}"
        ) from exc


def repair_all(user: str | None = None) -> dict:
    """Repara todas las entradas confirmadas que le tocan a este peer.

    Devuelve un resumen agregado de los archivos tocados y las réplicas
    restauradas. Solo toca los archivos que `user` puede escribir; los de otros
    propietarios se saltan en silencio (el 403 por archivo no debe abortar el
    barrido entero).
    """
    archivos = metadata.list_all_files()
    reparados = 0
    total_cambios = 0
    detalles = []

    for entrada in archivos:
        if (
            security.user_auth_enabled()
            and not security.can_write(entrada.get("owner"), user)
        ):
            continue

        try:
            resultado = repair(entrada["path"], user=user)
        except metadata.HTTPException:
            # Un archivo que no se puede reparar no debe detener los demás.
            continue

        if resultado.get("repaired"):
            reparados += 1
            total_cambios += len(resultado.get("changes", []))
            detalles.append({
                "path": entrada["path"],
                "changes": len(resultado.get("changes", []))
            })

    return {
        "scanned": len(archivos),
        "repaired_files": reparados,
        "total_changes": total_cambios,
        "details": detalles
    }


def repair_for_peer(fallen_peer_id: str) -> dict:
    """Repara los archivos que tenían al menos un bloque en el peer caído.

    Se usa desde el heartbeat automático: cuando se detecta que un peer no
    responde, cada peer repara las entradas locales que podrían haber perdido
    una réplica.
    """
    archivos = metadata.list_all_files()
    reparados = 0
    total_cambios = 0

    for entrada in archivos:
        afectado = any(
            fallen_peer_id in bloque.get("peers", [])
            for bloque in entrada["blocks"]
        )

        if not afectado:
            continue

        try:
            # Reparación del sistema: sin usuario, actúa en nombre del sistema
            # para preservar la durabilidad tras caerse un peer.
            resultado = repair(entrada["path"], internal=True, excluded_peers={fallen_peer_id})
        except metadata.HTTPException:
            continue

        if resultado.get("repaired"):
            reparados += 1
            total_cambios += len(resultado.get("changes", []))

    return {
        "fallen_peer": fallen_peer_id,
        "repaired_files": reparados,
        "total_changes": total_cambios
    }
