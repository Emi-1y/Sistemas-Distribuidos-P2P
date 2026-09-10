"""Alta de peers en el anillo y su propagación de una ronda.

`ring.py` no habla por red, y eso es lo que permite probarlo sin levantar nada.
La validación que devuelve 400 y 409, y el reenvío, viven aquí: la misma
separación que hay entre `filesystem.py` y `main.py`.
"""

import httpx
from fastapi import HTTPException

from server import config, ring


TIMEOUT = 2.0


def join(peer_id: str, address: str, forwarded: bool) -> dict:
    if not config.is_valid_address(address):
        raise HTTPException(
            status_code=400,
            detail="Dirección de peer inválida"
        )

    address = config.normalize_address(address)

    try:
        registrada = ring.LOCAL.address_of(peer_id)
    except KeyError:
        registrada = None

    if registrada is not None and registrada != address:
        raise HTTPException(
            status_code=409,
            detail="El peer ya está registrado"
        )

    cambio = ring.LOCAL.add_peer(peer_id, address)

    # Si la membresía no cambió no hay nada que contarle a nadie, y un alta que
    # ya viene reenviada no se vuelve a reenviar: una sola ronda, sin tormenta.
    if cambio and not forwarded:
        _propagate(peer_id, address)

    return {
        "ring_version": ring.LOCAL.version(),
        "peers": ring.LOCAL.peers()
    }


def _propagate(peer_id: str, address: str) -> None:
    payload = {"peer_id": peer_id, "address": address}
    headers = {"X-Forwarded-By": config.PEER_ID}

    for peer in ring.LOCAL.peers():
        # Uno mismo ya lo sabe, y el que entra es el origen del alta.
        if peer["peer_id"] in (config.PEER_ID, peer_id):
            continue

        _send_join(peer["address"], payload, headers)


def announce() -> bool:
    """Se presenta a los peers del bootstrap y absorbe su membresía.

    Solo si este peer NO figura en su propio bootstrap. Un peer que sí figura no
    habla con nadie al arrancar, y eso es lo que permite levantar los tres de la
    demo a la vez sin que el orden de arranque decida si el sistema funciona.

    Sin esto el alta funcionaría a medias: los demás aprenderían del recién
    llegado, pero él seguiría con un anillo de un solo nodo — creyéndose dueño
    de todas las claves y aceptando bloques que ningún otro peer sabe que tiene.

    Va sin `X-Forwarded-By`: el anuncio tiene que propagarse a los demás, y
    marcarlo lo impediría.
    """
    conocidos = {peer_id for peer_id, _ in config.BOOTSTRAP}

    if not config.BOOTSTRAP or config.PEER_ID in conocidos:
        return False

    payload = {"peer_id": config.PEER_ID, "address": config.ADDRESS}

    for _, address in config.BOOTSTRAP:
        if absorb(_send_join(address, payload, {})):
            return True

    return False


def absorb(cuerpo: dict | None) -> bool:
    """Mete en el anillo local la membresía que publicó otro peer."""
    if not cuerpo:
        return False

    cambio = False

    for peer in cuerpo.get("peers", []):
        if ring.LOCAL.add_peer(peer["peer_id"], peer["address"]):
            cambio = True

    return cambio


def _send_join(target: str, payload: dict, headers: dict) -> dict | None:
    """El cuerpo de la respuesta, o None si el peer no contestó.

    Best-effort: si un peer no contesta, se queda con una membresía vieja.
    Reintentar y reconciliar anillos divergentes es hito 3.
    """
    try:
        respuesta = httpx.post(
            f"{target}/peers/join",
            json=payload,
            headers=headers,
            timeout=TIMEOUT
        )

        if respuesta.status_code == 200:
            return respuesta.json()

    except httpx.HTTPError:
        pass

    return None
