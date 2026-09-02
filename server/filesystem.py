from pathlib import Path
from fastapi import HTTPException


BASE_DIR = Path(__file__).resolve().parent
STORAGE_ROOT = (BASE_DIR / "storage").resolve()

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