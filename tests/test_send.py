import pytest

from server import filesystem


BOUNDARY = "frontera"


def multipart_body(path: str, filename: str, content: bytes) -> bytes:
    """Arma el cuerpo multiparte a mano.

    httpx omite el parametro filename cuando esta vacio, y entonces la parte
    deja de ser un archivo y FastAPI responde 422 sin llegar a save_file.
    """
    return (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="path"\r\n\r\n'
        f"{path}\r\n"
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{BOUNDARY}--\r\n".encode()


def test_sube_un_archivo_a_un_directorio_existente(client, storage):
    """AC-01: archivo valido + directorio existente -> exito y queda almacenado."""
    (storage / "universidad").mkdir()

    response = client.post(
        "/files/upload",
        data={"path": "/universidad"},
        files={"file": ("tarea.txt", b"contenido de la tarea")}
    )

    assert response.status_code == 200
    assert response.json() == {
        "message": "Archivo subido correctamente",
        "path": "/universidad/tarea.txt"
    }

    destino = storage / "universidad" / "tarea.txt"
    assert destino.read_bytes() == b"contenido de la tarea"


def test_directorio_destino_inexistente(client, storage):
    """AC-02: directorio destino que no existe -> 404 y no se guarda nada."""
    response = client.post(
        "/files/upload",
        data={"path": "/inexistente"},
        files={"file": ("tarea.txt", b"contenido de la tarea")}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "El directorio no existe"
    assert list(storage.iterdir()) == []


def test_ruta_fuera_del_storage_root(client, storage):
    """AC-03: ruta destino fuera de STORAGE_ROOT -> 403 y no se escribe fuera."""
    fuera = storage.parent / "fuera"
    fuera.mkdir()

    response = client.post(
        "/files/upload",
        data={"path": "/../fuera"},
        files={"file": ("tarea.txt", b"contenido de la tarea")}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Acceso fuera del sistema DFS no permitido"
    assert list(fuera.iterdir()) == []


def test_archivo_ya_existente_en_destino(client, storage):
    """AC-04: nombre ya ocupado -> 409 y el original queda intacto."""
    (storage / "universidad").mkdir()
    original = storage / "universidad" / "tarea.txt"
    original.write_bytes(b"contenido original")

    response = client.post(
        "/files/upload",
        data={"path": "/universidad"},
        files={"file": ("tarea.txt", b"contenido nuevo")}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "El archivo ya existe"
    assert original.read_bytes() == b"contenido original"


@pytest.mark.parametrize("nombre", ["", "   "])
def test_nombre_de_archivo_vacio(client, storage, nombre):
    """AC-07: nombre vacio o en blanco -> 400."""
    (storage / "universidad").mkdir()

    response = client.post(
        "/files/upload",
        content=multipart_body("/universidad", nombre, b"contenido de la tarea"),
        headers={"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "El archivo debe tener un nombre"
    assert list((storage / "universidad").iterdir()) == []


def test_archivo_por_encima_del_tope_no_deja_parcial(client, storage, monkeypatch):
    """AC-06: superar el tope -> 413 y ningun archivo parcial en storage."""
    monkeypatch.setattr(filesystem, "MAX_FILE_SIZE", 4 * 1024)
    (storage / "universidad").mkdir()

    response = client.post(
        "/files/upload",
        data={"path": "/universidad"},
        files={"file": ("tarea.txt", b"x" * (4 * 1024 + 1))}
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "El archivo supera el tamaño máximo permitido"
    assert list((storage / "universidad").iterdir()) == []


def test_archivo_en_el_tope_exacto(client, storage):
    """AC-05: 10 MB justos -> exito y el archivo queda completo."""
    (storage / "universidad").mkdir()
    contenido = b"x" * filesystem.MAX_FILE_SIZE

    response = client.post(
        "/files/upload",
        data={"path": "/universidad"},
        files={"file": ("tarea.bin", contenido)}
    )

    assert response.status_code == 200

    destino = storage / "universidad" / "tarea.bin"
    assert destino.stat().st_size == filesystem.MAX_FILE_SIZE


def test_archivo_un_byte_por_encima_del_tope(client, storage):
    """AC-06: 10 MB + 1 byte -> 413 y nada queda en storage."""
    (storage / "universidad").mkdir()
    contenido = b"x" * (filesystem.MAX_FILE_SIZE + 1)

    response = client.post(
        "/files/upload",
        data={"path": "/universidad"},
        files={"file": ("tarea.bin", contenido)}
    )

    assert response.status_code == 413
    assert list((storage / "universidad").iterdir()) == []


def test_nombre_de_archivo_que_escapa_del_storage_root(client, storage):
    """AC-03: el nombre del archivo tampoco puede sacar el destino de la raiz."""
    fuera = storage.parent / "fuera"
    fuera.mkdir()

    response = client.post(
        "/files/upload",
        data={"path": "/"},
        files={"file": ("../fuera/tarea.txt", b"contenido de la tarea")}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Acceso fuera del sistema DFS no permitido"
    assert list(fuera.iterdir()) == []
