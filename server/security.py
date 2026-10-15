"""Primitivas de seguridad del RNF6, reunidas en un solo módulo.

Aquí vive todo lo que las demás capas necesitan para autenticar, autorizar y
cifrar, y nada de la lógica de negocio: `blocks`, `metadata` y `main` lo
importan, pero este módulo no importa a ninguno de ellos. Es la misma
separación que ya hay entre `ring` y `routing`, por el mismo motivo —se prueba
sin levantar nada— y además concentra en un punto el manejo de secretos, que
es lo que el RNF6 pide centralizar.

Cuatro responsabilidades, cuatro secretos independientes:

  - AES-256-GCM para el cifrado en reposo        -> DFSHA_MASTER_KEY
  - X-Peer-Token para autenticar peer↔peer        -> DFSHA_PEER_TOKEN
  - X-User / X-User-Token para el cliente          -> DFSHA_USERS
  - capabilities firmadas de bloque (HMAC)         -> DFSHA_CAP_SECRET

Ninguna clave sirve para dos cosas: comprometer el cifrado no debilita la
autenticación, y al revés. Todo es opt-in: si un secreto no está configurado,
su capa queda desactivada y el sistema se comporta como antes del RNF6, salvo
que DFSHA_REQUIRE_AUTH esté activo, en cuyo caso faltar un secreto obligatorio
aborta el arranque (fail-closed).
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time

from fastapi import Header, HTTPException

from server import config


# ---------------------------------------------------------------------------
# Arranque fail-closed
# ---------------------------------------------------------------------------

class ConfiguracionInsegura(RuntimeError):
    """Falta un secreto obligatorio con DFSHA_REQUIRE_AUTH activo."""


def verify_startup_config() -> None:
    """Con la seguridad obligatoria, exige que estén todos los secretos.

    Se llama una vez al arrancar la aplicación. Degradar en silencio cuando el
    operador *pidió* seguridad sería lo peor de los dos mundos: cree que está
    protegido y no lo está. Mejor no arrancar.
    """
    _verify_tls_requirement()

    if not config.REQUIRE_AUTH:
        return

    faltan = []

    if not config.PEER_TOKEN:
        faltan.append("DFSHA_PEER_TOKEN")
    if not config.USERS:
        faltan.append("DFSHA_USERS")
    if not config.MASTER_KEY:
        faltan.append("DFSHA_MASTER_KEY")
    if not config.CAP_SECRET:
        faltan.append("DFSHA_CAP_SECRET")

    if faltan:
        raise ConfiguracionInsegura(
            "DFSHA_REQUIRE_AUTH está activo pero faltan secretos obligatorios: "
            + ", ".join(faltan)
        )

    # Que la clave decodifique a 32 bytes se comprueba aquí para que el fallo
    # salga al arrancar y no en el primer bloque que alguien intente escribir.
    load_master_key()


def _verify_tls_requirement() -> None:
    """Con DFSHA_REQUIRE_TLS, exige cert, clave y CA, sin fallback a HTTP.

    El despliegue de seguridad no puede caer a HTTP en silencio: si se pidió
    TLS obligatorio, faltar cualquiera de las tres piezas aborta el arranque.
    Comprobamos no solo que las rutas estén configuradas sino que los ficheros
    existan, porque una ruta mal escrita es el fallo más probable y el que peor
    se diagnostica cuando el peer ya está sirviendo.
    """
    if not config.REQUIRE_TLS:
        return

    import os

    faltan = []

    for nombre, ruta in (
        ("DFSHA_TLS_CERT", config.TLS_CERT),
        ("DFSHA_TLS_KEY", config.TLS_KEY),
        ("DFSHA_TLS_CA", config.TLS_CA),
    ):
        if not ruta:
            faltan.append(f"{nombre} (no configurada)")
        elif not os.path.isfile(ruta):
            faltan.append(f"{nombre} (no existe: {ruta})")

    if faltan:
        raise ConfiguracionInsegura(
            "DFSHA_REQUIRE_TLS está activo pero el material TLS es inválido: "
            + ", ".join(faltan)
        )


# ---------------------------------------------------------------------------
# Cifrado en reposo — AES-256-GCM
#
# Formato del fichero .blk cuando el cifrado está activo:
#
#     magic(4) | version(1) | nonce(12) | ciphertext || tag(16)
#
# El magic `DFS1` permite distinguir un bloque cifrado de uno en claro escrito
# antes de activar el cifrado, para no tratar bytes planos como criptograma. El
# nonce es único por cifrado (se sortea con os.urandom vía secrets). GCM añade
# y verifica el tag de autenticación: un bloque alterado falla al descifrar, y
# esa es la detección de integridad en reposo que pide el RNF6, además del
# SHA-256 que ya existía.
# ---------------------------------------------------------------------------

MAGIC = b"DFS1"
VERSION = 1
NONCE_LEN = 12
_HEADER_LEN = len(MAGIC) + 1 + NONCE_LEN


class ErrorCifrado(Exception):
    """El bloque no se pudo descifrar: clave mala o datos manipulados."""


def encryption_enabled() -> bool:
    return bool(config.MASTER_KEY)


def load_master_key() -> bytes:
    """La clave de 32 bytes, aceptando hex (64 chars) o base64.

    Dos codificaciones porque las dos son naturales: `openssl rand -hex 32` y
    `openssl rand -base64 32`. Lo que no se acepta es una clave que no mida 32
    bytes: AES-256 necesita exactamente eso, y fallar aquí es mejor que
    truncar o rellenar en silencio una clave mal puesta.
    """
    crudo = config.MASTER_KEY

    if not crudo:
        raise ErrorCifrado("DFSHA_MASTER_KEY no está configurada")

    clave = None

    if len(crudo) == 64:
        try:
            clave = bytes.fromhex(crudo)
        except ValueError:
            clave = None

    if clave is None:
        try:
            clave = base64.b64decode(crudo, validate=True)
        except (binascii.Error, ValueError):
            clave = None

    if clave is None or len(clave) != 32:
        raise ErrorCifrado(
            "DFSHA_MASTER_KEY debe decodificar a 32 bytes "
            "(hex de 64 caracteres o base64 de 32 bytes)"
        )

    return clave


def encrypt_block(plaintext: bytes) -> bytes:
    """Cifra los bytes de un bloque y devuelve el blob que se escribe a disco.

    Se importa `AESGCM` dentro de la función, no arriba: así el módulo se puede
    importar en un entorno sin `cryptography` mientras el cifrado esté apagado,
    y solo quien lo activa paga la dependencia.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = load_master_key()
    nonce = secrets.token_bytes(NONCE_LEN)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)

    return MAGIC + bytes([VERSION]) + nonce + ciphertext


def is_encrypted_blob(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def decrypt_block(blob: bytes) -> bytes:
    """Descifra en memoria y devuelve el texto claro. Nunca toca disco.

    Si el blob no lleva el magic, se asume que es un bloque escrito en claro
    (antes de activar el cifrado, o con el cifrado apagado) y se devuelve tal
    cual: así un almacén que estrena cifrado sigue leyendo lo viejo.

    El tag GCM se valida al descifrar; si el criptograma fue manipulado,
    `cryptography` lanza `InvalidTag` y aquí se traduce a `ErrorCifrado`. El
    texto claro solo existe como `bytes` en memoria —el llamador decide qué
    hacer con él— y jamás se materializa un fichero sin cifrar.
    """
    if not is_encrypted_blob(blob):
        # Bloque en claro sin cabecera DFS1. En modo seguro con cifrado activo,
        # entregarlo sería servir plaintext legacy en silencio: se rechaza y
        # falla cerrado. Fuera del modo seguro se devuelve tal cual, para que un
        # almacén que estrena cifrado siga leyendo lo que escribió antes.
        if config.REQUIRE_AUTH and encryption_enabled():
            raise ErrorCifrado(
                "Bloque sin cifrar (sin cabecera DFS1) en modo seguro: "
                "no se entrega plaintext legacy"
            )
        return blob

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < _HEADER_LEN:
        raise ErrorCifrado("Bloque cifrado truncado")

    nonce = blob[len(MAGIC) + 1:_HEADER_LEN]
    ciphertext = blob[_HEADER_LEN:]
    key = load_master_key()

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise ErrorCifrado(
            "El bloque no supera la verificación de integridad AES-GCM"
        ) from exc


# ---------------------------------------------------------------------------
# TLS — verificación de certificados
#
# Cliente y peers verifican SIEMPRE el certificado del otro extremo contra el
# CA local configurado; nunca se usa verify=False de forma permanente. Si no
# hay CA configurado, se delega en el almacén de CAs del sistema (True), que es
# el comportamiento por defecto y seguro de requests/httpx.
# ---------------------------------------------------------------------------

def httpx_verify() -> str | bool:
    """Lo que pasarle a `verify=` de httpx/requests para hablar con otro peer.

    La ruta al CA local si está configurada; si no, `True` (verificación
    estándar contra las CAs del sistema). Jamás `False`.
    """
    return config.TLS_CA if config.TLS_CA else True


# ---------------------------------------------------------------------------
# Autenticación peer↔peer — X-Peer-Token
# ---------------------------------------------------------------------------

def peer_auth_enabled() -> bool:
    return bool(config.PEER_TOKEN)


def valid_peer_token(token: str | None) -> bool:
    """Comparación en tiempo constante para no filtrar el token por timing."""
    if not peer_auth_enabled():
        return True

    return token is not None and hmac.compare_digest(token, config.PEER_TOKEN)


def peer_headers(extra: dict | None = None) -> dict:
    """Las cabeceras internas de un peer: X-Peer-Token más lo que se pase.

    Centralizar su construcción evita que `routing`, `membership` y `repair`
    cada uno recuerde (o se olvide de) añadir el token. Si no hay token
    configurado, no se añade: una red sin auth sigue hablando como antes.
    """
    cabeceras = dict(extra or {})

    if peer_auth_enabled():
        cabeceras["X-Peer-Token"] = config.PEER_TOKEN

    return cabeceras


def require_peer_token(x_peer_token: str | None = Header(default=None)) -> None:
    """Dependencia FastAPI para endpoints exclusivamente internos.

    Rechaza con 401 si falta el token y con 403 si es incorrecto, que es la
    distinción que pide el RNF6: «no te identificaste» frente a «te
    identificaste mal».
    """
    if not peer_auth_enabled():
        return

    if x_peer_token is None:
        raise HTTPException(status_code=401, detail="Falta X-Peer-Token")

    if not hmac.compare_digest(x_peer_token, config.PEER_TOKEN):
        raise HTTPException(status_code=403, detail="X-Peer-Token inválido")


# ---------------------------------------------------------------------------
# Autenticación de cliente — X-User / X-User-Token
# ---------------------------------------------------------------------------

def user_auth_enabled() -> bool:
    return bool(config.USERS)


def authenticate_user(user: str | None, token: str | None) -> str | None:
    """Devuelve el nombre de usuario si la credencial es válida.

    Con auth desactivada no hay identidad: devuelve None y las comprobaciones
    de permiso lo tratan como «sistema abierto», igual que antes del RNF6.

    El token se compara con `compare_digest` contra el registrado, y se
    comprueba siempre un valor —aunque el usuario no exista— para no revelar
    por el tiempo de respuesta si un nombre está dado de alta.
    """
    if not user_auth_enabled():
        return None

    if not user or token is None:
        return None

    esperado = config.USERS.get(user, "")
    # Se compara aunque `esperado` esté vacío: así un usuario inexistente tarda
    # lo mismo que uno con token equivocado.
    valido = hmac.compare_digest(token, esperado) and user in config.USERS

    return user if valido else None


def require_user(
    x_user: str | None = Header(default=None),
    x_user_token: str | None = Header(default=None),
    x_peer_token: str | None = Header(default=None),
) -> str | None:
    """Dependencia para endpoints que atiende el cliente.

    Un peer que reenvía en nombre de un cliente ya traía la identidad en
    X-User y además acredita el reenvío con X-Peer-Token; esa petición no se
    vuelve a autenticar contra el token de usuario, porque el token no viaja en
    el reenvío (viaja solo en el primer salto cliente→peer). Un X-Peer-Token
    válido es, por tanto, prueba suficiente de que la identidad X-User ya fue
    verificada por el peer de entrada.
    """
    if not user_auth_enabled():
        return x_user  # puede ser None; sin auth la identidad es informativa

    # Petición reenviada por un peer de confianza: la identidad ya fue validada
    # en el primer salto.
    if peer_auth_enabled() and valid_peer_token(x_peer_token):
        if x_user is None:
            raise HTTPException(
                status_code=401,
                detail="Falta la identidad de usuario (X-User)"
            )
        return x_user

    usuario = authenticate_user(x_user, x_user_token)

    if usuario is None:
        raise HTTPException(
            status_code=401,
            detail="Autenticación de usuario requerida"
        )

    return usuario

def require_user_or_peer(
    x_user: str | None = Header(default=None),
    x_user_token: str | None = Header(default=None),
    x_peer_token: str | None = Header(default=None),
) -> str | None:
    """Acepta un usuario autenticado o un peer autenticado.

    Se usa en endpoints como /ring, que necesita consultar tanto el cliente
    como la reconciliación interna entre peers.
    """
    if peer_auth_enabled() and valid_peer_token(x_peer_token):
        return x_user

    return require_user(
        x_user=x_user,
        x_user_token=x_user_token,
        x_peer_token=x_peer_token,
    )


# ---------------------------------------------------------------------------
# Autorización por owner
# ---------------------------------------------------------------------------

def _owner_check(owner: str | None, user: str | None) -> bool:
    """La regla común de read/write/delete sobre una entrada.

    - Sin auth de usuario: todo permitido (comportamiento legacy).
    - Con auth y entrada SIN owner (metadata legacy, previa al RNF6): el
      resultado depende del modo. En modo seguro (DFSHA_REQUIRE_AUTH) se niega
      el acceso —falla cerrada—; fuera del modo seguro se trata como pública,
      para no dejar inaccesible lo ya subido en un entorno de transición.
    - Con auth y owner definido: solo el propietario.
    """
    if not user_auth_enabled():
        return True

    if owner is None:
        return not config.REQUIRE_AUTH

    return user == owner


def can_read(owner: str | None, user: str | None) -> bool:
    return _owner_check(owner, user)


def can_write(owner: str | None, user: str | None) -> bool:
    return _owner_check(owner, user)


def can_delete(owner: str | None, user: str | None) -> bool:
    return _owner_check(owner, user)


# ---------------------------------------------------------------------------
# Capabilities de bloque — HMAC-SHA256
#
# El almacén de bloques no conoce usuarios ni rutas lógicas, y hacérselo
# conocer cruzaría la red en cada lectura. En su lugar, el dueño de la metadata
# —que es quien ya decide los permisos en allocate y lookup— emite un permiso
# firmado y temporal, y el peer de datos solo verifica la firma. Así la
# autorización llega hasta el bloque sin que el bloque sepa de autorización.
#
# Formato (compacto, va en la cabecera X-Block-Cap):
#
#     base64url(file_id|index|perm|exp) . base64url(hmac_sha256(secret, payload))
#
# `index` puede ser `*` para cubrir todos los bloques de un file_id con una
# sola capability: allocate y lookup emiten una por archivo, no una por bloque.
# `perm` es `r` o `w`. `exp` es el epoch de expiración.
# ---------------------------------------------------------------------------

def capabilities_enabled() -> bool:
    return bool(config.CAP_SECRET)


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(texto: str) -> bytes:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


def _sign(payload: bytes) -> str:
    firma = hmac.new(
        config.CAP_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).digest()

    return _b64u(firma)


def issue_capability(
    file_id: str,
    perm: str,
    index: str | int = "*",
    ttl: int | None = None,
) -> str | None:
    """Emite una capability firmada, o None si las capabilities están apagadas.

    La emite el dueño de la metadata DESPUÉS de haber comprobado el permiso del
    usuario; esta función no reevalúa permisos, solo acuña el token una vez que
    la decisión ya se tomó.
    """
    if not capabilities_enabled():
        return None

    expira = int(time.time()) + (config.CAP_TTL if ttl is None else ttl)
    payload = f"{file_id}|{index}|{perm}|{expira}".encode("utf-8")

    return f"{_b64u(payload)}.{_sign(payload)}"


def _parse_capability(cap: str) -> tuple[str, str, str, int] | None:
    """Devuelve (file_id, index, perm, exp) si la firma es válida y no caducó."""
    try:
        cuerpo_b64, firma_b64 = cap.split(".", 1)
        payload = _unb64u(cuerpo_b64)
    except (ValueError, binascii.Error):
        return None

    if not hmac.compare_digest(_sign(payload), firma_b64):
        return None

    try:
        file_id, index, perm, exp = payload.decode("utf-8").split("|")
        expira = int(exp)
    except (ValueError, UnicodeDecodeError):
        return None

    if expira < int(time.time()):
        return None

    return file_id, index, perm, expira


def capability_grants(cap: str | None, file_id: str, index: int, perm: str) -> bool:
    """True si la capability autoriza `perm` sobre ese file_id/index concreto.

    Comprueba firma, caducidad, que el permiso cubra el pedido, que el file_id
    coincida y que el índice sea el mismo o la capability sea comodín (`*`). Un
    permiso de lectura no vale para escribir y viceversa.
    """
    if cap is None:
        return False

    datos = _parse_capability(cap)

    if datos is None:
        return False

    cap_file_id, cap_index, cap_perm, _ = datos

    if cap_file_id != file_id:
        return False

    if cap_perm != perm:
        return False

    return cap_index == "*" or cap_index == str(index)


def require_block_access(
    file_id: str,
    index: int,
    perm: str,
    x_block_cap: str | None,
    x_peer_token: str | None,
) -> None:
    """Guarda del endpoint de bloques: capability de cliente O token de peer.

    Dos caminos legítimos llegan a un bloque: un cliente con una capability que
    le dio el dueño de la metadata (perm `r` o `w`), y un peer interno que
    copia bloques durante la reparación, que se acredita con X-Peer-Token. Si
    ninguna de las dos vías está activada (seguridad apagada), no se exige
    nada: el comportamiento es el de antes del RNF6.
    """
    # Peer interno de confianza (p. ej. reparación): acceso pleno.
    if peer_auth_enabled() and valid_peer_token(x_peer_token):
        return

    # Cliente con capability firmada por el dueño de la metadata.
    if capabilities_enabled():
        if capability_grants(x_block_cap, file_id, index, perm):
            return

        raise HTTPException(
            status_code=403,
            detail="Capability de bloque ausente, inválida o insuficiente"
        )

    # Capabilities apagadas y sin auth de peer: sistema abierto (compatibilidad).
    return
