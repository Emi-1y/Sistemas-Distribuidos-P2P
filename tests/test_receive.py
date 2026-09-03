def test_descarga_un_archivo_existente(client, storage):
    """AC-01: archivo remoto existente -> 200 y contenido correcto."""
    (storage / "universidad").mkdir()
    (storage / "universidad" / "tarea.txt").write_bytes(b"contenido de la tarea")

    response = client.get(
        "/files/download",
        params={"path": "/universidad/tarea.txt"}
    )

    assert response.status_code == 200
    assert response.content == b"contenido de la tarea"


def test_archivo_remoto_inexistente(client, storage):
    """AC-02: archivo que no existe -> 404."""
    response = client.get(
        "/files/download",
        params={"path": "/universidad/tarea.txt"}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "El archivo no existe"


def test_ruta_fuera_del_storage_root(client, storage):
    """AC-03: ruta fuera de STORAGE_ROOT -> 403."""
    fuera = storage.parent / "fuera"
    fuera.mkdir()
    (fuera / "secreto.txt").write_bytes(b"dato sensible")

    response = client.get(
        "/files/download",
        params={"path": "/../fuera/secreto.txt"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Acceso fuera del sistema DFS no permitido"


def test_ruta_es_un_directorio(client, storage):
    """AC-04: la ruta apunta a un directorio, no a un archivo -> 400."""
    (storage / "universidad").mkdir()

    response = client.get(
        "/files/download",
        params={"path": "/universidad"}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "La ruta no corresponde a un archivo"