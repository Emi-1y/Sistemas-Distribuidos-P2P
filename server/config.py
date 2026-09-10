"""Configuración de este peer, leída del entorno al importar.

Tres peers en la misma máquina necesitan raíces de datos distintas o se pisan
los bloques, y un docker-compose no puede editar código. Los valores por
defecto reproducen el monolito del hito 1: sin configurar nada, el sistema se
comporta como antes.
"""

import os
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent


def normalize_address(address: str) -> str:
    """Sin espacios ni barra final.

    `http://peer2:9002` y `http://peer2:9002/` son el mismo peer. Sin
    normalizar, un carácter dejaría un peer fuera del anillo con un 409 falso.
    """
    return address.strip().rstrip("/")


def is_valid_address(address: str) -> bool:
    partes = urlparse(normalize_address(address))

    return partes.scheme in ("http", "https") and bool(partes.netloc)


def parse_bootstrap(raw: str) -> list[tuple[str, str]]:
    """`peer1=http://peer1:9001,peer2=http://peer2:9002` a pares.

    Se ignoran las entradas vacías: la variable se escribe a mano en un
    docker-compose y acaba con espacios y comas de más.
    """
    entradas = []

    for trozo in raw.split(","):
        peer_id, _, address = trozo.partition("=")
        peer_id = peer_id.strip()
        address = normalize_address(address)

        if peer_id and address:
            entradas.append((peer_id, address))

    return entradas


PEER_ID = os.environ.get("DFSHA_PEER_ID", "peer1")

ADDRESS = normalize_address(
    os.environ.get("DFSHA_ADDRESS", "http://127.0.0.1:8000")
)

BOOTSTRAP = parse_bootstrap(os.environ.get("DFSHA_BOOTSTRAP", ""))

STORAGE_ROOT = Path(
    os.environ.get("DFSHA_STORAGE_ROOT", BASE_DIR / "storage")
).resolve()

BLOCK_SIZE = int(os.environ.get("DFSHA_BLOCK_SIZE", 4 * 1024 * 1024))

VNODES = int(os.environ.get("DFSHA_VNODES", 128))
