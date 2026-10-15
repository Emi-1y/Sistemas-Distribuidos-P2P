import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from server import (
    blocks,
    config,
    membership,
    metadata,
    repair,
    ring,
    routing,
    security,
)


HEARTBEAT_INTERVAL = 30.0


async def _heartbeat_loop():
    """Verifica periódicamente la salud de los peers y repara lo afectado.

    Corre en segundo plano durante la vida de la aplicación. Si detecta que
    un peer no responde, repara los archivos locales que tenían bloques en
    ese peer.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)

        # 1. Reconciliación de anillos: absorbe peers que otros conocen y
        #    este peer no tiene. Va antes del health check para tener la vista
        #    más completa posible antes de decidir quién está caído.
        try:
            await asyncio.to_thread(membership.reconcile_all)
        except Exception:
            pass

        # 2. Health check: detecta peers que no responden.
        try:
            caidos = membership.health_check_all()
        except Exception:
            continue

        # 3. Reparación de réplicas afectadas por los peers caídos.
        for peer_id in caidos:
            try:
                await asyncio.to_thread(repair.repair_for_peer, peer_id)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail-closed: con DFSHA_REQUIRE_AUTH activo, arrancar sin los secretos
    # obligatorios es un error inmediato en vez de una degradación silenciosa.
    security.verify_startup_config()

    # Un peer que no figura en su propio bootstrap se presenta y absorbe la
    # membresía. El que sí figura no emite ninguna petición.
    membership.announce()

    tarea = asyncio.create_task(_heartbeat_loop())

    try:
        yield
    finally:
        tarea.cancel()

        try:
            await tarea
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="DFSha Peer",
    description="Peer del sistema de archivos distribuido DFSha",
    version="0.3.0",
    lifespan=lifespan
)


class DirectoryRequest(BaseModel):
    path: str


class JoinRequest(BaseModel):
    peer_id: str
    address: str


class AllocateRequest(BaseModel):
    path: str
    size: int


class BlockConfirmation(BaseModel):
    index: int
    checksum: str
    peers: list[str] = []


class CommitRequest(BaseModel):
    path: str
    file_id: str
    blocks: list[BlockConfirmation] = []


def forwarded(x_forwarded_by: str | None) -> bool:
    return x_forwarded_by is not None


@app.get("/")
def root():
    return {
        "service": "DFSha",
        "status": "running"
    }


# ---------------------------------------------------------------------------
# Anillo y membresía — SPEC-02
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "peer_id": config.PEER_ID,
        "status": "ok",
        "ring_version": ring.LOCAL.version()
    }


@app.get("/ring")
def get_ring(_: str | None = Depends(security.require_user_or_peer)):
    # El cliente consulta /ring para conocer la topología antes de hablar
    # directamente con cada peer de datos; por eso exige identidad de cliente
    # (o X-Peer-Token en la reconciliación interna), pero NO es peer-only.
    return {
        "ring_version": ring.LOCAL.version(),
        "peers": ring.LOCAL.peers()
    }


@app.post("/peers/join")
def join(
    request: JoinRequest,
    x_forwarded_by: str | None = Header(default=None),
    _: None = Depends(security.require_peer_token),
):
    return membership.join(
        request.peer_id,
        request.address,
        forwarded=forwarded(x_forwarded_by)
    )


@app.post("/peers/reconcile")
def reconcile(_: None = Depends(security.require_peer_token)):
    """Fuerza la reconciliación del anillo con todos los peers conocidos.

    Útil cuando se sospecha que el anillo local está desactualizado.
    """
    actualizados = membership.reconcile_all()

    return {
        "ring_version": ring.LOCAL.version(),
        "updated_from": actualizados,
        "peers": ring.LOCAL.peers()
    }


# ---------------------------------------------------------------------------
# Bloques — SPEC-03
# ---------------------------------------------------------------------------

def declared_size(request: Request) -> int | None:
    """El Content-Length, o None si no vino o no es un numero."""
    value = request.headers.get("content-length")

    return int(value) if value is not None and value.isdigit() else None


@app.put("/blocks/{file_id}/{index}", status_code=201)
async def put_block(
    file_id: str,
    index: str,
    request: Request,
    x_block_checksum: str | None = Header(default=None),
    x_block_cap: str | None = Header(default=None),
    x_peer_token: str | None = Header(default=None),
):
    identifier, number = blocks.parse_identifiers(file_id, index)

    # Autorización de escritura: una capability `w` emitida por el dueño de la
    # metadata en `allocate`, o un X-Peer-Token válido (reparación interna).
    security.require_block_access(
        identifier, number, "w", x_block_cap, x_peer_token
    )

    checksum = blocks.parse_checksum(x_block_checksum)

    return await blocks.save_block(
        identifier,
        number,
        request.stream(),
        checksum,
        declared_size=declared_size(request)
    )


@app.get("/blocks/{file_id}/{index}")
def get_block(
    file_id: str,
    index: str,
    x_block_cap: str | None = Header(default=None),
    x_peer_token: str | None = Header(default=None),
):
    identifier, number = blocks.parse_identifiers(file_id, index)

    # Autorización de lectura: capability `r` emitida en `lookup`, o
    # X-Peer-Token interno (reparación).
    security.require_block_access(
        identifier, number, "r", x_block_cap, x_peer_token
    )

    try:
        datos, checksum = blocks.read_block(identifier, number)
    except security.ErrorCifrado as exc:
        # El bloque en disco no supera la verificación de integridad AES-GCM:
        # fue manipulado o la clave es incorrecta. Es un 422 de integridad, el
        # mismo espíritu que el checksum que no cuadra en la escritura.
        raise HTTPException(status_code=422, detail=str(exc))

    # Response con los bytes en claro en memoria: nunca un FileResponse del
    # criptograma ni un fichero temporal descifrado.
    return Response(
        content=datos,
        media_type="application/octet-stream",
        headers={"X-Block-Checksum": checksum}
    )


@app.head("/blocks/{file_id}/{index}")
def head_block(
    file_id: str,
    index: str,
    x_block_cap: str | None = Header(default=None),
    x_peer_token: str | None = Header(default=None),
):
    identifier, number = blocks.parse_identifiers(file_id, index)

    security.require_block_access(
        identifier, number, "r", x_block_cap, x_peer_token
    )

    if not blocks.block_exists(identifier, number):
        raise HTTPException(
            status_code=404,
            detail="El bloque no existe"
        )

    return {}


@app.delete("/blocks/{file_id}")
def delete_block(
    file_id: str,
    x_peer_token: str | None = Header(default=None),
):
    # El borrado de bloques solo lo originan los peers (durante el `rm` de un
    # archivo o la limpieza): es una operación interna, no de cliente.
    security.require_peer_token(x_peer_token)

    return blocks.delete_blocks(blocks.parse_file_id(file_id))


# ---------------------------------------------------------------------------
# Metadatos — SPEC-04
#
# Cada endpoint empieza igual: calcula su clave, y si no es suya la reenvía al
# dueño y devuelve lo que conteste. La lógica está en `metadata`, el reenvío en
# `routing`, y aquí solo el cableado.
# ---------------------------------------------------------------------------

@app.get("/files")
def ls(
    path: str = "/",
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        ruta, forwarded(x_forwarded_by), "GET", "/files",
        params={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    # `ls` respeta el modelo de acceso: solo lista las entradas que el usuario
    # puede ver (los archivos propios y los directorios).
    return {"path": ruta, "items": metadata.list_entries(ruta, user=user)}


@app.post("/directories")
def mkdir(
    request: DirectoryRequest,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(request.path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/directories", json={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    # Primero la entrada en el padre: es lo que reserva el nombre. Si fuera al
    # revés y fallara la segunda, el nombre seguiría libre y un `allocate`
    # podría crear un archivo con esa misma ruta.
    metadata.add_directory(ruta)

    # Después el contenido, en el dueño del propio directorio.
    routing.call_owner(
        ruta, "POST", "/directories/content", json={"path": ruta},
        user=user, local=lambda: metadata.ensure_bucket(ruta)
    )

    return {"message": "Directorio creado correctamente", "path": ruta}


@app.delete("/directories")
def rmdir(
    path: str,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "DELETE", "/directories", params={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    # El contenido primero: si se quitara antes la entrada del padre, un
    # directorio no vacío se quedaría sin nombre y con su contenido colgando.
    routing.call_owner(
        ruta, "DELETE", "/directories/content", params={"path": ruta},
        user=user, local=lambda: metadata.drop_bucket(ruta)
    )

    metadata.remove_directory(ruta)

    return {"message": "Directorio eliminado correctamente"}


@app.delete("/files")
def rm(
    path: str,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "DELETE", "/files", params={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    # Solo el propietario (o el sistema sin auth) puede borrar: la comprobación
    # vive en metadata.remove_file, que conoce el owner de la entrada.
    entrada = metadata.remove_file(ruta, user=user)

    # Los bloques después de la entrada: al revés quedaría un archivo que `ls`
    # muestra y que no se puede leer. Así lo que queda son bloques que nadie
    # referencia — ocupan disco, pero no mienten.
    destinos = sorted(
        {peer for bloque in entrada["blocks"] for peer in bloque["peers"]}
    )

    for peer_id in destinos:
        if peer_id == config.PEER_ID:
            blocks.delete_blocks(entrada["file_id"])
        else:
            routing.notify(
                peer_id, "DELETE", f"/blocks/{entrada['file_id']}"
            )

    return {"message": "Archivo eliminado correctamente"}


@app.post("/files/allocate")
def allocate(
    request: AllocateRequest,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    # Los parámetros antes de normalizar: un `path` vacío da `400`, no el `403`
    # de la normalización, que significa otra cosa.
    metadata.check_allocate(request.path, request.size)
    ruta = metadata.normalize(request.path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/files/allocate", json={"path": ruta, "size": request.size},
        user=user
    )

    if ajeno is not None:
        return ajeno

    # El dueño de la metadata registra al usuario como owner del archivo nuevo.
    entrada = metadata.allocate(ruta, request.size, owner=user)

    # Y, habiendo comprobado el permiso de escritura, emite una capability `w`
    # para que el cliente pueda hacer PUT de los bloques directamente contra
    # los peers de datos. Si las capabilities están apagadas, es None y no
    # viaja.
    cap = security.issue_capability(entrada["file_id"], "w")

    if cap is not None:
        entrada = {**entrada, "write_cap": cap}

    return entrada


@app.post("/files/commit")
def commit(
    request: CommitRequest,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(request.path)
    confirmados = [bloque.model_dump() for bloque in request.blocks]

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/files/commit",
        json={
            "path": ruta,
            "file_id": request.file_id,
            "blocks": confirmados
        },
        user=user
    )

    if ajeno is not None:
        return ajeno

    # Solo quien es owner de la entrada pendiente puede confirmarla.
    return metadata.commit(ruta, request.file_id, confirmados, user=user)


@app.get("/files/lookup")
def lookup(
    path: str,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "GET", "/files/lookup", params={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    # Solo un usuario autorizado puede resolver el archivo (y, por tanto,
    # descargarlo): la comprobación de lectura vive en metadata.lookup.
    entrada = metadata.lookup(ruta, user=user)

    # Comprobado el permiso de lectura, se emite una capability `r` para que el
    # cliente pueda hacer GET de los bloques directamente contra los peers.
    cap = security.issue_capability(entrada["file_id"], "r")

    if cap is not None:
        entrada = {**entrada, "read_cap": cap}

    return entrada


@app.post("/files/repair")
def repair_file(
    path: str,
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    """Repara las réplicas de un archivo confirmado.

    El peer dueño de la entrada verifica cuántas réplicas reales tiene cada
    bloque y copia los que faltan desde un peer vivo hacia peers nuevos. Solo
    el propietario del archivo puede lanzar la reparación.
    """
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/files/repair", params={"path": ruta}, user=user
    )

    if ajeno is not None:
        return ajeno

    return repair.repair(ruta, user=user)


@app.post("/files/repair-all")
def repair_all_files(
    x_forwarded_by: str | None = Header(default=None),
    user: str | None = Depends(security.require_user),
):
    """Repara todas las entradas confirmadas que le tocan a este peer.

    Opera sobre las entradas locales de este peer y solo repara las que el
    usuario autenticado puede escribir (en `repair.repair_all`). Exige
    identidad de cliente, no es un endpoint interno de peer.
    """
    if forwarded(x_forwarded_by):
        raise HTTPException(
            status_code=508,
            detail="Bucle de enrutamiento detectado"
        )

    return repair.repair_all(user=user)


# El contenido de un directorio: la segunda escritura de `mkdir` y el segundo
# borrado de `rmdir`. Los llama otro peer, no el cliente, pero se exponen igual
# que el resto — los peers son simétricos y no hay canal privado.

@app.post("/directories/content")
def create_content(
    request: DirectoryRequest,
    x_forwarded_by: str | None = Header(default=None),
    _: None = Depends(security.require_peer_token),
):
    ruta = metadata.normalize(request.path)

    ajeno = routing.delegate(
        ruta, forwarded(x_forwarded_by),
        "POST", "/directories/content", json={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    metadata.ensure_bucket(ruta)

    return {"path": ruta}


@app.delete("/directories/content")
def drop_content(
    path: str,
    x_forwarded_by: str | None = Header(default=None),
    _: None = Depends(security.require_peer_token),
):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        ruta, forwarded(x_forwarded_by),
        "DELETE", "/directories/content", params={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    metadata.drop_bucket(ruta)

    return {"path": ruta}
