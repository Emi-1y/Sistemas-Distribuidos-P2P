from pathlib import Path
from fastapi import HTTPException


BASE_DIR = Path(__file__).resolve().parent
STORAGE_ROOT = (BASE_DIR / "storage").resolve()

MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB
CHUNK_SIZE = 1024 * 1024           # 1 MB

STORAGE_ROOT.mkdir(parents=True, exist_ok=True)


def resolve_path(remote_path: str) -> Path:
    clean_path = remote_path.lstrip("/")
    full_path = (STORAGE_ROOT / clean_path).resolve()

    if full_path != STORAGE_ROOT and STORAGE_ROOT not in full_path.parents:
        raise HTTPException(
            status_code=403,
            detail="Acceso fuera del sistema DFS no permitido"
        )

    return full_path


def list_directory(remote_path: str):
    path = resolve_path(remote_path)

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="El directorio no existe"
        )

    if not path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="La ruta no es un directorio"
        )

    items = []

    for item in path.iterdir():
        items.append({
            "name": item.name,
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None
        })

    return items


def create_directory(remote_path: str):
    path = resolve_path(remote_path)

    if path.exists():
        raise HTTPException(
            status_code=409,
            detail="El directorio ya existe"
        )

    path.mkdir(parents=True)

    return {
        "message": "Directorio creado correctamente",
        "path": remote_path
    }


def remove_directory(remote_path: str):
    path = resolve_path(remote_path)

    if path == STORAGE_ROOT:
        raise HTTPException(
            status_code=403,
            detail="No se puede eliminar la raíz del DFS"
        )

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="El directorio no existe"
        )

    if not path.is_dir():
        raise HTTPException(
            status_code=400,
            detail="La ruta no es un directorio"
        )

    try:
        path.rmdir()
    except OSError:
        raise HTTPException(
            status_code=400,
            detail="El directorio no está vacío"
        )

    return {
        "message": "Directorio eliminado correctamente"
    }


def save_file(remote_path: str, uploaded_file):
    filename = (uploaded_file.filename or "").strip()

    if not filename:
        raise HTTPException(
            status_code=400,
            detail="El archivo debe tener un nombre"
        )

    directory = resolve_path(remote_path)

    if not directory.is_dir():
        raise HTTPException(
            status_code=404,
            detail="El directorio no existe"
        )

    virtual_path = f"{remote_path.rstrip('/')}/{filename}"
    destination = resolve_path(virtual_path)

    if destination.exists():
        raise HTTPException(
            status_code=409,
            detail="El archivo ya existe"
        )

    written = 0

    try:
        with destination.open("wb") as buffer:
            while True:
                chunk = uploaded_file.file.read(CHUNK_SIZE)

                if not chunk:
                    break

                written += len(chunk)

                if written > MAX_FILE_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="El archivo supera el tamaño máximo permitido"
                    )

                buffer.write(chunk)

    except HTTPException:
        destination.unlink(missing_ok=True)
        raise

    return {
        "message": "Archivo subido correctamente",
        "path": virtual_path
    }


def remove_file(remote_path: str):
    path = resolve_path(remote_path)

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="El archivo no existe"
        )

    if not path.is_file():
        raise HTTPException(
            status_code=400,
            detail="La ruta no corresponde a un archivo"
        )

    path.unlink()

    return {
        "message": "Archivo eliminado correctamente"
    }


def get_file(remote_path: str) -> Path:
    path = resolve_path(remote_path)

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="El archivo no existe"
        )

    if not path.is_file():
        raise HTTPException(
            status_code=400,
            detail="La ruta no corresponde a un archivo"
        )

    return path