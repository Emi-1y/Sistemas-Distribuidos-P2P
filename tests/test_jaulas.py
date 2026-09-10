"""Las dos jaulas — SPEC-03, paso 1 del plan TDD.

El espacio de nombres y los bloques son planos separados. La separacion no es
orden: `ls`, `cd`, `rm` y `rmdir` operan sobre rutas que escribe el usuario, y
con los dos planos mezclados `rmdir /blocks/<file_id>` borraria bloques.
"""

import pytest
from fastapi import HTTPException

from server import filesystem


UUID_DEMO = "5f3e0000-0000-4000-8000-000000000001"


@pytest.fixture
def bloque_en_disco(arboles):
    """Un bloque real en el arbol de datos, para intentar alcanzarlo desde fuera."""
    directorio = arboles.blocks / UUID_DEMO
    directorio.mkdir(parents=True)

    bloque = directorio / "000000.blk"
    bloque.write_bytes(b"los bytes de un bloque")

    return bloque


# ---------------------------------------------------------------------------
# AC-13 — ninguna operacion del usuario ve ni toca un bloque
# ---------------------------------------------------------------------------

def test_ls_de_la_raiz_no_lista_los_bloques(client, arboles, bloque_en_disco):
    """AC-13: el plano de datos no aparece en el espacio de nombres."""
    (arboles.namespace / "universidad").mkdir()

    items = client.get("/files", params={"path": "/"}).json()["items"]

    assert [item["name"] for item in items] == ["universidad"]


def test_no_se_puede_entrar_en_el_arbol_de_bloques(client, bloque_en_disco):
    """AC-13: `cd /blocks` no existe dentro del espacio de nombres."""
    respuesta = client.get("/files", params={"path": "/blocks"})

    assert respuesta.status_code == 404
    assert bloque_en_disco.exists()


def test_no_se_puede_borrar_un_directorio_de_bloques(client, bloque_en_disco):
    """AC-13: con los planos mezclados, este rmdir borraria bloques de verdad."""
    respuesta = client.delete(
        "/directories", params={"path": f"/blocks/{UUID_DEMO}"}
    )

    assert respuesta.status_code == 404
    assert bloque_en_disco.exists()


def test_no_se_puede_borrar_un_bloque_saliendo_por_arriba(client, bloque_en_disco):
    """AC-13: mover el namespace sin mover la jaula solo anade un `..` al ataque."""
    respuesta = client.delete(
        "/files", params={"path": f"/../blocks/{UUID_DEMO}/000000.blk"}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["detail"] == "Acceso fuera del sistema DFS no permitido"
    assert bloque_en_disco.exists()


def test_no_se_puede_subir_un_archivo_al_arbol_de_bloques(client, bloque_en_disco):
    """AC-13: tampoco por el camino de escritura."""
    respuesta = client.post(
        "/files/upload",
        data={"path": f"/../blocks/{UUID_DEMO}"},
        files={"file": ("000001.blk", b"colado")},
    )

    assert respuesta.status_code == 403
    assert list((bloque_en_disco.parent).iterdir()) == [bloque_en_disco]


# ---------------------------------------------------------------------------
# AC-14 — cada arbol es su propia jaula
# ---------------------------------------------------------------------------

def test_resolve_path_exige_la_raiz(arboles):
    """AC-14: sin valor por defecto.

    Con dos arboles, olvidar el argumento tiene que ser un error en el acto y
    no una caida silenciosa al arbol equivocado.
    """
    with pytest.raises(TypeError):
        filesystem.resolve_path("/universidad")


@pytest.mark.parametrize("arbol", ["namespace", "blocks"])
def test_cada_arbol_tiene_su_propia_jaula(arboles, arbol):
    """AC-14: la misma ruta que se sale responde 403 en los dos."""
    raiz = getattr(arboles, arbol)

    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(raiz, "/../fuera")

    assert error.value.status_code == 403
    assert error.value.detail == "Acceso fuera del sistema DFS no permitido"


def test_una_ruta_del_namespace_no_alcanza_el_arbol_de_bloques(arboles):
    """AC-14: es exactamente lo que pasaria con una sola jaula en STORAGE_ROOT.

    `namespace/../blocks/<file_id>` cae dentro de STORAGE_ROOT, asi que una
    jaula anclada arriba lo dejaria pasar.
    """
    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(arboles.namespace, f"/../blocks/{UUID_DEMO}")

    assert error.value.status_code == 403


def test_una_ruta_de_bloques_no_alcanza_el_espacio_de_nombres(arboles):
    """AC-14: la frontera vale en las dos direcciones."""
    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(arboles.blocks, "/../namespace/universidad")

    assert error.value.status_code == 403


def test_la_raiz_del_espacio_de_nombres_sigue_sin_poder_borrarse(client, arboles):
    """El 403 de remove_directory ahora compara contra NAMESPACE_ROOT."""
    respuesta = client.delete("/directories", params={"path": "/"})

    assert respuesta.status_code == 403
    assert respuesta.json()["detail"] == "No se puede eliminar la raíz del DFS"
    assert arboles.namespace.is_dir()
