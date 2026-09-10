"""El espacio de nombres, repartido por el anillo.

Este módulo no sabe que existen otros peers: opera siempre sobre el contenido
del directorio que le toca, que asume local. Quién es el dueño de cada clave y
cómo se le habla es asunto de `routing`, y esa separación es la que permite
probar la semántica entera sin levantar nada.
"""

import json
import os
import posixpath
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from server import config, ring
from server.filesystem import resolve_path


# El plano de metadatos: un árbol propio, hermano del de bloques.
METADATA_ROOT = config.STORAGE_ROOT / "metadata"

# El factor de réplica, en un solo sitio. Subirlo a 3 no exige tocar nada más:
# `allocate` planea tres peers por bloque y `commit` exige tres confirmaciones.
REPLICAS = 1

METADATA_ROOT.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Rutas lógicas
# ---------------------------------------------------------------------------

def normalize(path: str) -> str:
    """La forma canónica de una ruta del usuario, o `403` si se sale.

    Dos escrituras de la misma ruta tienen que dar la misma clave: si
    `//universidad//tarea.pdf` y `/universidad/tarea.pdf` normalizaran distinto,
    acabarían en peers distintos y el archivo desaparecería al leerlo.

    El recorrido se hace a mano y no con `posixpath.normpath`, porque normpath
    **se traga** los `..` que suben por encima de la raíz: convierte `/..` en
    `/` en silencio, justo el caso que hay que rechazar.
    """
    crudo = (path or "").strip()

    if not crudo:
        raise HTTPException(
            status_code=403,
            detail="Acceso fuera del sistema DFS no permitido"
        )

    partes: list[str] = []

    for parte in crudo.split("/"):
        if parte in ("", "."):
            continue

        if parte == "..":
            if not partes:
                raise HTTPException(
                    status_code=403,
                    detail="Acceso fuera del sistema DFS no permitido"
                )

            partes.pop()
            continue

        partes.append(parte)

    return "/" + "/".join(partes)


def parent_of(path: str) -> str:
    ruta = normalize(path)

    if ruta == "/":
        return "/"

    return posixpath.dirname(ruta) or "/"


def name_of(path: str) -> str:
    ruta = normalize(path)

    return "" if ruta == "/" else posixpath.basename(ruta)


# ---------------------------------------------------------------------------
# El contenido de un directorio, en disco
# ---------------------------------------------------------------------------

def bucket_file(directory: str) -> Path:
    """El archivo del contenido, con el hash de la ruta como nombre.

    El nombre se sintetiza, igual que el de un bloque: una ruta lógica puede
    llevar caracteres que un nombre de archivo no admite. La ruta va dentro del
    JSON para poder mirar el disco y entender qué hay.
    """
    return resolve_path(METADATA_ROOT, f"{ring.stable_hash(directory):016x}.json")


def read_bucket(directory: str) -> dict | None:
    """El contenido del directorio, o `None` si ese directorio no existe.

    La raíz es la excepción: existe siempre, en todos los peers, sin que nadie
    la cree. Si tuviera que crearse, cada operación cargaría con el caso
    «todavía no hay raíz».
    """
    ruta = bucket_file(directory)

    if not ruta.is_file():
        return {"path": "/", "entries": {}} if directory == "/" else None

    return json.loads(ruta.read_text(encoding="utf-8"))


def write_bucket(bucket: dict) -> None:
    """Escritura atómica: a un temporal y después `os.replace`.

    Sin esto, un peer que muriera a mitad de un `allocate` dejaría el JSON
    truncado, y al arrancar no podría leer ese directorio: la subida de un
    archivo habría destruido los metadatos de todos sus hermanos.
    """
    destino = bucket_file(bucket["path"])
    temporal = destino.with_suffix(".tmp")

    temporal.write_text(
        json.dumps(bucket, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    os.replace(temporal, destino)


def ensure_bucket(directory: str) -> None:
    """La segunda escritura de `mkdir`. Idempotente."""
    ruta = normalize(directory)

    if read_bucket(ruta) is None:
        write_bucket({"path": ruta, "entries": {}})


def drop_bucket(directory: str) -> None:
    """El segundo borrado de `rmdir`.

    La ausencia de contenido se trata como directorio vacío: es lo que permite
    deshacer un `mkdir` que se quedó a medias con un comando que ya existe.
    """
    ruta = normalize(directory)
    bucket = read_bucket(ruta)

    if bucket is None:
        return

    if bucket["entries"]:
        raise HTTPException(
            status_code=400,
            detail="El directorio no está vacío"
        )

    bucket_file(ruta).unlink(missing_ok=True)


def _bucket_of_parent(path: str, missing: str) -> tuple[dict, str]:
    """El contenido del padre y el nombre, o `404` con el mensaje que toque."""
    padre = parent_of(path)
    bucket = read_bucket(padre)

    if bucket is None:
        raise HTTPException(status_code=404, detail=missing)

    return bucket, name_of(path)


# ---------------------------------------------------------------------------
# Directorios
# ---------------------------------------------------------------------------

def list_entries(directory: str) -> list[dict]:
    """Lo que cuelga del directorio: subdirectorios y archivos confirmados.

    Las entradas pendientes no salen. Hasta el commit, un archivo no existe.
    """
    ruta = normalize(directory)
    bucket = read_bucket(ruta)

    if bucket is None:
        raise HTTPException(
            status_code=404,
            detail="El directorio no existe"
        )

    items = []

    for nombre in sorted(bucket["entries"]):
        entrada = bucket["entries"][nombre]

        if entrada.get("type") == "directory":
            items.append({"name": nombre, "type": "directory", "size": None})

        elif entrada.get("state") == "committed":
            items.append(
                {"name": nombre, "type": "file", "size": entrada["size"]}
            )

    return items


def add_directory(path: str) -> dict:
    """La primera escritura de `mkdir`: la entrada hija en el padre.

    Va antes que el contenido propio porque **es lo que reserva el nombre**. Si
    fuera al revés y fallara, el nombre seguiría libre y un `allocate` podría
    crear un archivo con esa misma ruta.
    """
    ruta = normalize(path)

    if ruta == "/":
        raise HTTPException(
            status_code=409,
            detail="El directorio ya existe"
        )

    bucket, nombre = _bucket_of_parent(ruta, "El directorio no existe")

    if nombre in bucket["entries"]:
        raise HTTPException(
            status_code=409,
            detail="El directorio ya existe"
        )

    entrada = {"name": nombre, "type": "directory"}
    bucket["entries"][nombre] = entrada
    write_bucket(bucket)

    return entrada


def remove_directory(path: str) -> dict:
    ruta = normalize(path)

    if ruta == "/":
        raise HTTPException(
            status_code=403,
            detail="No se puede eliminar la raíz del DFS"
        )

    bucket, nombre = _bucket_of_parent(ruta, "El directorio no existe")
    entrada = bucket["entries"].get(nombre)

    if entrada is None:
        raise HTTPException(
            status_code=404,
            detail="El directorio no existe"
        )

    if entrada.get("type") != "directory":
        raise HTTPException(
            status_code=400,
            detail="La ruta no es un directorio"
        )

    del bucket["entries"][nombre]
    write_bucket(bucket)

    return entrada


# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------

def allocate(path: str, size: int) -> dict:
    """Reserva la ruta y devuelve el plan de bloques, en estado `pending`.

    El plan dice en qué peers *debería* quedar cada bloque. Es una intención:
    el almacén de bloques acepta lo que le manden sin comprobar si le tocaba,
    así que el hecho solo lo conocerá el cliente, y lo reportará en el commit.
    """
    if not (path or "").strip() or not isinstance(size, int) or size < 0:
        raise HTTPException(
            status_code=400,
            detail="Parámetros de asignación inválidos"
        )

    ruta = normalize(path)
    bucket, nombre = _bucket_of_parent(ruta, "El directorio no existe")

    if nombre in bucket["entries"]:
        raise HTTPException(
            status_code=409,
            detail="El archivo ya existe"
        )

    file_id = str(uuid.uuid4())
    block_size = config.BLOCK_SIZE

    bloques = []
    restante = size
    index = 0

    while restante > 0:
        trozo = min(block_size, restante)

        bloques.append({
            "index": index,
            "size": trozo,
            "checksum": None,
            "peers": ring.choose_nodes(f"{file_id}:{index}", REPLICAS)
        })

        restante -= trozo
        index += 1

    entrada = {
        "path": ruta,
        "file_id": file_id,
        "size": size,
        "block_size": block_size,
        "state": "pending",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "blocks": bloques
    }

    bucket["entries"][nombre] = entrada
    write_bucket(bucket)

    return entrada


def commit(path: str, file_id: str, blocks: list[dict]) -> dict:
    """Confirma la entrada con lo que el cliente reporta.

    Se guardan los peers **reportados**, no los planeados, porque el plan era
    una intención. Y por eso hay que validarlos: si el cliente pudiera escribir
    cualquier `peer_id`, la metadata seguiría mintiendo, solo que con más pasos.
    """
    ruta = normalize(path)
    bucket, nombre = _bucket_of_parent(ruta, "El archivo no existe")
    entrada = bucket["entries"].get(nombre)

    if (
        entrada is None
        or entrada.get("type") == "directory"
        or entrada.get("file_id") != file_id
    ):
        raise HTTPException(
            status_code=404,
            detail="El archivo no existe"
        )

    if entrada["state"] == "committed":
        raise HTTPException(
            status_code=409,
            detail="El archivo ya fue confirmado"
        )

    conocidos = {peer["peer_id"] for peer in ring.LOCAL.peers()}
    reportado = {}

    for bloque in blocks:
        peers = bloque.get("peers") or []

        if not set(peers) <= conocidos:
            raise HTTPException(
                status_code=422,
                detail="Confirmación de bloques inválida"
            )

        reportado[bloque["index"]] = bloque

    planeados = {bloque["index"] for bloque in entrada["blocks"]}

    if set(reportado) != planeados:
        raise HTTPException(
            status_code=422,
            detail="Confirmación de bloques incompleta"
        )

    for bloque in entrada["blocks"]:
        confirmado = reportado[bloque["index"]]

        # Peers **distintos**: repetir uno no es replicar.
        distintos = list(dict.fromkeys(confirmado["peers"]))

        if len(distintos) < REPLICAS:
            raise HTTPException(
                status_code=422,
                detail="Confirmación de bloques incompleta"
            )

        bloque["checksum"] = confirmado["checksum"]
        bloque["peers"] = distintos

    entrada["state"] = "committed"
    write_bucket(bucket)

    return entrada


def lookup(path: str) -> dict:
    """La entrada completa de un archivo confirmado."""
    ruta = normalize(path)
    bucket, nombre = _bucket_of_parent(ruta, "El archivo no existe")
    entrada = bucket["entries"].get(nombre)

    if (
        entrada is None
        or entrada.get("type") == "directory"
        or entrada.get("state") != "committed"
    ):
        raise HTTPException(
            status_code=404,
            detail="El archivo no existe"
        )

    return entrada


def remove_file(path: str) -> dict:
    """Quita la entrada y la devuelve, con sus bloques y sus peers.

    No avisa a los peers de los bloques: eso es red, y la red no entra en este
    módulo. Lo lanza el endpoint con lo que aquí se devuelve.
    """
    ruta = normalize(path)
    bucket, nombre = _bucket_of_parent(ruta, "El archivo no existe")
    entrada = bucket["entries"].get(nombre)

    if entrada is None:
        raise HTTPException(
            status_code=404,
            detail="El archivo no existe"
        )

    if entrada.get("type") == "directory":
        raise HTTPException(
            status_code=400,
            detail="La ruta no corresponde a un archivo"
        )

    del bucket["entries"][nombre]
    write_bucket(bucket)

    return entrada
