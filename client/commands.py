# FUNCIONES DE MANEJOR DE ARCHIVOS
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