import posixpath
from pathlib import Path

import requests


SERVER_URL = "http://127.0.0.1:8000"


def build_path(current_path: str, target: str) -> str:
    if target.startswith("/"):
        path = target
    else:
        path = posixpath.join(current_path, target)

    path = posixpath.normpath(path)

    if not path.startswith("/"):
        path = "/" + path

    return path


def print_error(response):
    try:
        detail = response.json().get("detail", "Error desconocido")
    except ValueError:
        detail = "Error desconocido"

    print(f"Error {response.status_code}: {detail}")


def ls(current_path: str):
    try:
        response = requests.get(
            f"{SERVER_URL}/files",
            params={"path": current_path}
        )

        if response.status_code == 200:
            data = response.json()
            items = data["items"]

            if not items:
                print("Directorio vacío")
                return

            for item in items:
                if item["type"] == "directory":
                    print(f"[DIR]  {item['name']}")
                else:
                    print(f"[FILE] {item['name']}")

        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def mkdir(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.post(
            f"{SERVER_URL}/directories",
            json={"path": path}
        )

        if response.status_code == 200:
            print("Directorio creado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def rmdir(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.delete(
            f"{SERVER_URL}/directories",
            params={"path": path}
        )

        if response.status_code == 200:
            print("Directorio eliminado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def rm(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.delete(
            f"{SERVER_URL}/files",
            params={"path": path}
        )

        if response.status_code == 200:
            print("Archivo eliminado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def send(current_path: str, filename: str):
    name = filename.strip()

    if not name:
        print("Error: el archivo debe tener un nombre")
        return

    local_file = Path(name)

    if not local_file.is_file():
        print(f"Error: el archivo local '{name}' no existe")
        return

    try:
        with local_file.open("rb") as handle:
            response = requests.post(
                f"{SERVER_URL}/files/upload",
                data={"path": current_path},
                files={"file": (local_file.name, handle)}
            )

        if response.status_code == 200:
            print("Archivo subido correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def receive(current_path: str, filename: str):
    name = filename.strip()

    if not name:
        print("Error: el archivo debe tener un nombre")
        return

    remote_path = build_path(current_path, name)
    local_file = Path(name)

    if local_file.exists():
        print(f"Error: ya existe un archivo local llamado '{name}'")
        return

    temp_file = local_file.with_name(local_file.name + ".part")

    try:
        response = requests.get(
            f"{SERVER_URL}/files/download",
            params={"path": remote_path},
            stream=True
        )

        if response.status_code != 200:
            print_error(response)
            return

        with temp_file.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                handle.write(chunk)

        temp_file.rename(local_file)
        print("Archivo descargado correctamente")

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")
        temp_file.unlink(missing_ok=True)


def change_directory(current_path: str, target: str) -> str:
    new_path = build_path(current_path, target)

    try:
        response = requests.get(
            f"{SERVER_URL}/files",
            params={"path": new_path}
        )

        if response.status_code == 200:
            return new_path

        print_error(response)
        return current_path

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")
        return current_path