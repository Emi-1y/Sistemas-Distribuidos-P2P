from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, Header, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server import blocks, config, membership, ring
from server.filesystem import (
    list_directory,
    create_directory,
    remove_directory,
    remove_file,
    save_file,
    get_file
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Un peer que no figura en su propio bootstrap se presenta y absorbe la
    # membresía. El que sí figura no emite ninguna petición.
    membership.announce()
    yield


app = FastAPI(
    title="DFSha Peer",
    description="Peer del sistema de archivos distribuido DFSha",
    version="0.2.0",
    lifespan=lifespan
)


class DirectoryRequest(BaseModel):
    path: str


class JoinRequest(BaseModel):
    peer_id: str
    address: str


@app.get("/")
def root():
    return {
        "service": "DFSha",
        "status": "running"
    }


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
        forwarded=x_forwarded_by is not None
    )


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


@app.get("/files")
def ls(path: str = "/"):
    return {
        "path": path,
        "items": list_directory(path)
    }


@app.post("/directories")
def mkdir(request: DirectoryRequest):
    return create_directory(request.path)


@app.delete("/directories")
def rmdir(path: str):
    return remove_directory(path)


@app.delete("/files")
def rm(path: str):
    return remove_file(path)


@app.post("/files/upload")
def send(path: str = Form(...), file: UploadFile = File(...)):
    return save_file(path, file)


@app.get("/files/download")
def receive(path: str):
    file_path = get_file(path)
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )
