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

REPLICAS = int(os.environ.get("DFSHA_REPLICAS", "1"))


# ---------------------------------------------------------------------------
# Seguridad — RNF6
#
# Todo lo de aquí es opt-in: sin configurar nada, cada valor queda vacío y las
# capas de seguridad se desactivan solas, de modo que el sistema se comporta
# como antes del RNF6. El despliegue y la demo final sí los fijan. Cuando
# DFSHA_REQUIRE_AUTH está activo, arrancar sin los secretos obligatorios es un
# error inmediato (fail-closed), no una degradación silenciosa.
#
# Cada secreto tiene UN solo propósito. No se comparten claves entre cifrado y
# autenticación: comprometer una no debe comprometer la otra.
# ---------------------------------------------------------------------------

def _env_flag(nombre: str, por_defecto: bool = False) -> bool:
    """Lee una variable booleana con las grafías habituales de un compose."""
    valor = os.environ.get(nombre)

    if valor is None:
        return por_defecto

    return valor.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def parse_users(raw: str) -> dict[str, str]:
    """`alice=tokenA,bob=tokenB` a un mapa usuario→token.

    Mismo formato y mismas tolerancias que `DFSHA_BOOTSTRAP`: la variable se
    escribe a mano en un compose y acaba con espacios y comas de más. Se acepta
    tanto `usuario=token` como `usuario:token` porque las dos grafías son
    naturales para credenciales.
    """
    usuarios: dict[str, str] = {}

    for trozo in raw.split(","):
        trozo = trozo.strip()

        if not trozo:
            continue

        if "=" in trozo:
            nombre, _, token = trozo.partition("=")
        else:
            nombre, _, token = trozo.partition(":")

        nombre = nombre.strip()
        token = token.strip()

        if nombre and token:
            usuarios[nombre] = token

    return usuarios


# Si está activo, los secretos obligatorios deben existir o el arranque falla.
REQUIRE_AUTH = _env_flag("DFSHA_REQUIRE_AUTH", False)

# Si está activo, el peer DEBE servir por HTTPS con cert, clave y CA presentes,
# sin ningún fallback a HTTP. Es la forma de exigir cifrado en tránsito en el
# despliegue de seguridad; se deja en false solo para tests y desarrollo legacy.
REQUIRE_TLS = _env_flag("DFSHA_REQUIRE_TLS", False)

# Secreto compartido para autenticar peer↔peer. Viaja en la cabecera
# X-Peer-Token en las llamadas internas entre peers.
PEER_TOKEN = os.environ.get("DFSHA_PEER_TOKEN", "").strip()

# Credenciales de cliente: identidad de usuario asociable a un owner. El valor
# nunca se registra en logs ni se devuelve en respuestas.
USERS = parse_users(os.environ.get("DFSHA_USERS", ""))

# Clave de cifrado en reposo (AES-256-GCM) y SOLO para eso. Se admite en hex
# (64 caracteres) o base64; debe decodificar a 32 bytes. La validación real la
# hace server.security al derivar la clave, para no cargar aquí el import.
MASTER_KEY = os.environ.get("DFSHA_MASTER_KEY", "").strip()

# Secreto para firmar capabilities de bloque (HMAC-SHA256) y SOLO para eso.
CAP_SECRET = os.environ.get("DFSHA_CAP_SECRET", "").strip()

# Validez (en segundos) de una capability de bloque desde que se emite.
CAP_TTL = int(os.environ.get("DFSHA_CAP_TTL", "900"))

# --- TLS -------------------------------------------------------------------
# Rutas a certificado y clave del propio peer (servidor Uvicorn) y al CA que
# cliente y peers usan para verificar certificados. Vacíos => sin TLS.
TLS_CERT = os.environ.get("DFSHA_TLS_CERT", "").strip()
TLS_KEY = os.environ.get("DFSHA_TLS_KEY", "").strip()
TLS_CA = os.environ.get("DFSHA_TLS_CA", "").strip()

TLS_ENABLED = bool(TLS_CERT and TLS_KEY)