from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.filesystem import (
    list_directory,
    create_directory,
    remove_directory,
    remove_file,
    save_file,
    get_file,
    resolve_path
)


app = FastAPI(
    title="DFSha Server",
    description="Servidor monolítico del sistema de archivos DFSha",
    version="0.1.0"
)


class DirectoryRequest(BaseModel):
    path: str


@app.get("/")
def root():
    return {
        "service": "DFSha",
        "status": "running"
    }


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
