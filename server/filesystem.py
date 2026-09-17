"""La jaula del disco: la unica comprobacion de contencion del sistema.

Aqui vivia el espacio de nombres del usuario, cuando era un arbol de
directorios reales. La SPEC-04 lo sustituyo por metadatos repartidos, asi que
lo unico que queda es lo que protege los dos arboles que si estan en disco:
`blocks/` y `metadata/`.
"""

from pathlib import Path

from fastapi import HTTPException


PREFIJO_LARGO = "\\\\?\\"


def sin_prefijo_largo(ruta: Path) -> Path:
    """La misma ruta, sin la forma larga de Windows.

    `Path.resolve()` devuelve a veces `\\\\?\\C:\\...`, que es la misma ruta
    escrita de otra manera pero con otro ancla: `\\\\?\\C:\\` en vez de `C:\\`.
    Comparada con una raiz normal, la jaula rechazaria rutas que estan dentro
    de ella.

    Que rama tome la resolucion depende de si el directorio existe **en ese
    instante**, asi que el fallo solo aparece con dos escrituras del mismo
    archivo a la vez. Nadie escribia bloques en paralelo hasta que el cliente
    de la SPEC-05 empezo a hacerlo.

    Se normalizan los dos lados de la comparacion, no uno: el objetivo es
    comparar peras con peras, no ablandar la jaula.
    """
    texto = str(ruta)

    if not texto.startswith(PREFIJO_LARGO):
        return ruta

    resto = texto[len(PREFIJO_LARGO):]

    # `\\?\UNC\servidor\recurso` es la forma larga de `\\servidor\recurso`:
    # quitarle solo el prefijo dejaria una ruta relativa llamada `UNC`.
    if resto.upper().startswith("UNC\\"):
        return Path("\\\\" + resto[4:])

    return Path(resto)


def resolve_path(root: Path, remote_path: str) -> Path:
    """Resuelve una ruta dentro de `root`, que es su jaula.

    La raiz entra por parametro y sin valor por defecto. Con dos arboles,
    anclar una sola jaula arriba no serviria: una ruta con `..` desde uno
    caeria dentro de esa jaula comun y aterrizaria en el arbol de al lado. La
    jaula tiene que moverse con el plano que protege, y olvidar el argumento
    tiene que ser un error en el acto y no una caida silenciosa.
    """
    clean_path = remote_path.lstrip("/")
    root = sin_prefijo_largo(Path(root).resolve())
    full_path = sin_prefijo_largo((root / clean_path).resolve())

    if full_path != root and root not in full_path.parents:
        raise HTTPException(
            status_code=403,
            detail="Acceso fuera del sistema DFS no permitido"
        )

    return full_path
