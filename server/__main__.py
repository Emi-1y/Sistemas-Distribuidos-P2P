"""Arranque del peer con TLS leído de la configuración.

`uvicorn server.main:app --host ... --port ...` sigue funcionando igual que
siempre para servir en HTTP. Este entrypoint —`python -m server`— añade lo que
la línea de comandos no sabe hacer sola: leer DFSHA_TLS_CERT/KEY del entorno y
pasárselos a Uvicorn como certificado y clave, de modo que habilitar TLS es
cuestión de configuración y no de recordar los flags `--ssl-*` en cada peer.

El host y el puerto se toman de DFSHA_ADDRESS (la misma dirección que el peer
anuncia en el anillo), para que escuchar y anunciarse no puedan discrepar.
"""

import os
from urllib.parse import urlparse

import uvicorn

from server import config, security


def main() -> None:
    # Fail-closed de TLS: si se exige TLS obligatorio y falta el material, se
    # aborta aquí, ANTES de que Uvicorn se ate a un socket HTTP. Dejar que el
    # chequeo viviera solo en el lifespan no bastaría: para entonces el peer ya
    # estaría escuchando en claro.
    security.verify_startup_config()

    partes = urlparse(config.ADDRESS)
    host = os.environ.get("DFSHA_HOST", "0.0.0.0")
    puerto = partes.port or (443 if partes.scheme == "https" else 8000)

    opciones = {"host": host, "port": puerto}

    if config.REQUIRE_TLS and not config.TLS_ENABLED:
        # Defensa en profundidad: aunque verify_startup_config ya lo cubre,
        # nunca servir en claro con TLS obligatorio.
        raise security.ConfiguracionInsegura(
            "DFSHA_REQUIRE_TLS está activo pero no hay certificado/clave: "
            "el peer no servirá en HTTP"
        )

    if config.TLS_ENABLED:
        opciones["ssl_certfile"] = config.TLS_CERT
        opciones["ssl_keyfile"] = config.TLS_KEY

    uvicorn.run("server.main:app", **opciones)


if __name__ == "__main__":
    main()
