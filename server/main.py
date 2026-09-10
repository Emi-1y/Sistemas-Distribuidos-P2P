from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server import blocks, config, membership, metadata, ring, routing


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Un peer que no figura en su propio bootstrap se presenta y absorbe la
    # membresía. El que sí figura no emite ninguna petición.
    membership.announce()
    yield


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
def get_ring():
    return {
        "ring_version": ring.LOCAL.version(),
        "peers": ring.LOCAL.peers()
    }


@app.post("/peers/join")
def join(
    request: JoinRequest,
    x_forwarded_by: str | None = Header(default=None)
):
    return membership.join(
        request.peer_id,
        request.address,
        forwarded=forwarded(x_forwarded_by)
    )


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
    x_block_checksum: str | None = Header(default=None)
):
    identifier, number = blocks.parse_identifiers(file_id, index)
    checksum = blocks.parse_checksum(x_block_checksum)

    return await blocks.save_block(
        identifier,
        number,
        request.stream(),
        checksum,
        declared_size=declared_size(request)
    )


@app.get("/blocks/{file_id}/{index}")
def get_block(file_id: str, index: str):
    identifier, number = blocks.parse_identifiers(file_id, index)
    path, checksum = blocks.read_block(identifier, number)

    return FileResponse(
        path=path,
        media_type="application/octet-stream",
        headers={"X-Block-Checksum": checksum}
    )


@app.delete("/blocks/{file_id}")
def delete_block(file_id: str):
    return blocks.delete_blocks(blocks.parse_file_id(file_id))


# ---------------------------------------------------------------------------
# Metadatos — SPEC-04
#
# Cada endpoint empieza igual: calcula su clave, y si no es suya la reenvía al
# dueño y devuelve lo que conteste. La lógica está en `metadata`, el reenvío en
# `routing`, y aquí solo el cableado.
# ---------------------------------------------------------------------------

@app.get("/files")
def ls(path: str = "/", x_forwarded_by: str | None = Header(default=None)):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        ruta, forwarded(x_forwarded_by), "GET", "/files", params={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    return {"path": ruta, "items": metadata.list_entries(ruta)}


@app.post("/directories")
def mkdir(
    request: DirectoryRequest,
    x_forwarded_by: str | None = Header(default=None)
):
    ruta = metadata.normalize(request.path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/directories", json={"path": ruta}
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
        local=lambda: metadata.ensure_bucket(ruta)
    )

    return {"message": "Directorio creado correctamente", "path": ruta}


@app.delete("/directories")
def rmdir(path: str, x_forwarded_by: str | None = Header(default=None)):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "DELETE", "/directories", params={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    # El contenido primero: si se quitara antes la entrada del padre, un
    # directorio no vacío se quedaría sin nombre y con su contenido colgando.
    routing.call_owner(
        ruta, "DELETE", "/directories/content", params={"path": ruta},
        local=lambda: metadata.drop_bucket(ruta)
    )

    metadata.remove_directory(ruta)

    return {"message": "Directorio eliminado correctamente"}


@app.delete("/files")
def rm(path: str, x_forwarded_by: str | None = Header(default=None)):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "DELETE", "/files", params={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    entrada = metadata.remove_file(ruta)

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
    x_forwarded_by: str | None = Header(default=None)
):
    # Los parámetros antes de normalizar: un `path` vacío da `400`, no el `403`
    # de la normalización, que significa otra cosa.
    metadata.check_allocate(request.path, request.size)
    ruta = metadata.normalize(request.path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "POST", "/files/allocate", json={"path": ruta, "size": request.size}
    )

    if ajeno is not None:
        return ajeno

    return metadata.allocate(ruta, request.size)


@app.post("/files/commit")
def commit(
    request: CommitRequest,
    x_forwarded_by: str | None = Header(default=None)
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
        }
    )

    if ajeno is not None:
        return ajeno

    return metadata.commit(ruta, request.file_id, confirmados)


@app.get("/files/lookup")
def lookup(path: str, x_forwarded_by: str | None = Header(default=None)):
    ruta = metadata.normalize(path)

    ajeno = routing.delegate(
        metadata.parent_of(ruta), forwarded(x_forwarded_by),
        "GET", "/files/lookup", params={"path": ruta}
    )

    if ajeno is not None:
        return ajeno

    return metadata.lookup(ruta)


# El contenido de un directorio: la segunda escritura de `mkdir` y el segundo
# borrado de `rmdir`. Los llama otro peer, no el cliente, pero se exponen igual
# que el resto — los peers son simétricos y no hay canal privado.

@app.post("/directories/content")
def create_content(
    request: DirectoryRequest,
    x_forwarded_by: str | None = Header(default=None)
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
    x_forwarded_by: str | None = Header(default=None)
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
