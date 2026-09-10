"""El reenvío al peer dueño. Sin una sola regla de negocio.

`metadata` decide **qué significa** cada operación; este módulo decide **a
quién se le pregunta**. Por eso no se importan el uno al otro: la semántica se
prueba sin levantar nada, y cuando algo falla aquí se sabe que es la red.
"""

import httpx
from fastapi import HTTPException

from server import config, ring


TIMEOUT = 5.0


def owner_of(key: str) -> str:
    return ring.choose_nodes(key)[0]


def is_mine(key: str) -> bool:
    return owner_of(key) == config.PEER_ID


def delegate(
    key: str,
    forwarded: bool,
    method: str,
    url: str,
    params: dict | None = None,
    json: dict | None = None
) -> dict | None:
    """`None` si la clave es mía; si no, la respuesta del dueño, tal cual.

    El orden importa: primero se mira si es mía. Una petición marcada cuya
    clave sí me pertenece se atiende — la cabecera no es un rechazo, significa
    que el reenvío hizo su trabajo. El `508` es solo para la que llega marcada
    y **tampoco** es mía: con anillos que pudieron divergir, sin ese corte dos
    peers se mandarían la misma petición indefinidamente.
    """
    if is_mine(key):
        return None

    if forwarded:
        raise HTTPException(
            status_code=508,
            detail="Bucle de enrutamiento detectado"
        )

    return _relay(owner_of(key), method, url, params, json)


def call_owner(
    key: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
    local
):
    """La salida obligada: se ejecuta aquí si la clave es mía, o en el dueño.

    `local` es lo que hay que hacer cuando resulta que el dueño soy yo. Entra
    como parámetro para que este módulo no tenga que importar `metadata`.
    """
    if is_mine(key):
        return local()

    return _relay(owner_of(key), method, url, params, json)


def notify(
    peer_id: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json: dict | None = None
) -> None:
    """Aviso a un peer concreto, best-effort: si no contesta, se sigue.

    Lo usa el borrado de un archivo para avisar a los peers de sus bloques. Un
    peer que no lo recoja deja bloques huérfanos: ocupan disco, pero no mienten.
    """
    try:
        _send(ring.address_of(peer_id), method, url, params, json, _headers())
    except (httpx.HTTPError, KeyError):
        pass


def _headers() -> dict:
    return {"X-Forwarded-By": config.PEER_ID}


def _relay(peer_id: str, method: str, url: str, params, json) -> dict:
    """Habla con el dueño y devuelve su respuesta sin reinterpretarla.

    Si el dueño dice `409 El archivo ya existe`, el cliente ve exactamente eso.
    Traducirlo a un error propio perdería la única información útil.
    """
    try:
        respuesta = _send(
            ring.address_of(peer_id), method, url, params, json, _headers()
        )
    except (httpx.HTTPError, KeyError):
        raise HTTPException(
            status_code=503,
            detail="El peer responsable no está disponible"
        )

    try:
        cuerpo = respuesta.json()
    except ValueError:
        cuerpo = {}

    if respuesta.status_code >= 400:
        raise HTTPException(
            status_code=respuesta.status_code,
            detail=cuerpo.get("detail", "El peer responsable no está disponible")
        )

    return cuerpo


def _send(address: str, method: str, url: str, params, json, headers):
    """Aislada para que los tests la sustituyan y no abran un socket."""
    return httpx.request(
        method,
        f"{address}{url}",
        params=params,
        json=json,
        headers=headers,
        timeout=TIMEOUT
    )
