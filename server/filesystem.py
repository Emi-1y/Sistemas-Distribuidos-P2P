"""La jaula del disco: la unica comprobacion de contencion del sistema.

Aqui vivia el espacio de nombres del usuario, cuando era un arbol de
directorios reales. La SPEC-04 lo sustituyo por metadatos repartidos, asi que
lo unico que queda es lo que protege los dos arboles que si estan en disco:
`blocks/` y `metadata/`.
"""

from pathlib import Path

from fastapi import HTTPException


def resolve_path(root: Path, remote_path: str) -> Path:
    """Resuelve una ruta dentro de `root`, que es su jaula.

    La raiz entra por parametro y sin valor por defecto. Con dos arboles,
    anclar una sola jaula arriba no serviria: una ruta con `..` desde uno
    caeria dentro de esa jaula comun y aterrizaria en el arbol de al lado. La
    jaula tiene que moverse con el plano que protege, y olvidar el argumento
    tiene que ser un error en el acto y no una caida silenciosa.
    """
    clean_path = remote_path.lstrip("/")
    full_path = (root / clean_path).resolve()

    if full_path != root and root not in full_path.parents:
        raise HTTPException(
            status_code=403,
            detail="Acceso fuera del sistema DFS no permitido"
        )

    return full_path
