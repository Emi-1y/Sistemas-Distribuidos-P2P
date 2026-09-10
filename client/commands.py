import os
import posixpath
from pathlib import Path

import requests


DEFAULT_PEER = "http://127.0.0.1:8000"
HEALTH_TIMEOUT = 2

# El peer con el que se esta hablando. Lo fija connect() al arrancar el REPL.
PEER_URL = None


def bootstrap_addresses():
    """Las direcciones de DFSHA_BOOTSTRAP, en orden.

    Se lee el mismo formato que en el servidor, `peer_id=url`, y se descarta el
    peer_id: el cliente no calcula el anillo, lo consulta. Una sola variable
    evita que un docker-compose mantenga dos listas que dicen lo mismo.
    """
    direcciones = []

    for trozo in os.environ.get("DFSHA_BOOTSTRAP", "").split(","):
        _, _, address = trozo.rpartition("=")
        address = address.strip().rstrip("/")

        if address:
            direcciones.append(address)

    return direcciones or [DEFAULT_PEER]


def connect():
    """El primer peer del bootstrap que responda /health, o None.

    Todos los peers son simetricos: vale cualquiera que este vivo. Conocer uno
    solo seria un punto unico de fallo.
    """
    global PEER_URL

    for address in bootstrap_addresses():
        try:
            respuesta = requests.get(
                f"{address}/health",
                timeout=HEALTH_TIMEOUT
            )

            if respuesta.status_code == 200:
                PEER_URL = address
                return address

        except requests.RequestException:
            continue

    return None


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
            f"{PEER_URL}/files",
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
            f"{PEER_URL}/directories",
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
            f"{PEER_URL}/directories",
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
            f"{PEER_URL}/files",
            params={"path": path}
        )

        if response.status_code == 200:
            print("Archivo eliminado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def send(current_path: str, filename: str):
    """Retirado en la SPEC-04, reconstruido en la SPEC-05.

    El endpoint monolitico que usaba escribia el archivo entero en el disco de
    un peer, y eso dejo de tener sentido cuando el espacio de nombres paso a
    estar repartido y los datos a ir en bloques.
    """
    print("send no esta disponible todavia: llega con la SPEC-05")


def receive(current_path: str, filename: str):
    """Retirado en la SPEC-04, reconstruido en la SPEC-05."""
    print("receive no esta disponible todavia: llega con la SPEC-05")


def change_directory(current_path: str, target: str) -> str:
    new_path = build_path(current_path, target)

    try:
        response = requests.get(
            f"{PEER_URL}/files",
            params={"path": new_path}
        )

        if response.status_code == 200:
            return new_path

        print_error(response)
        return current_path

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")
        return current_path