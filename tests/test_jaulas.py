"""Las dos jaulas — SPEC-03, actualizado por la SPEC-04.

Los dos arboles que hay en disco son `blocks/` y `metadata/`, y cada uno es su
propia jaula. El espacio de nombres del usuario ya no es un tercero: dejo de
ser un arbol de directorios reales y paso a ser entradas repartidas por el
anillo, asi que las rutas que escribe el usuario **ya no llegan al disco**.

Eso no hace la frontera innecesaria, la hace mas facil de defender: ningun
nombre de archivo de metadatos puede aterrizar entre los bloques, ni al reves.
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
# Ninguna operacion del usuario ve ni toca un bloque
# ---------------------------------------------------------------------------

def test_ls_de_la_raiz_no_lista_los_bloques(client, arboles, bloque_en_disco):
    """El plano de datos no aparece en el espacio de nombres."""
    client.post("/directories", json={"path": "/universidad"})

    items = client.get("/files", params={"path": "/"}).json()["items"]

    assert [item["name"] for item in items] == ["universidad"]


def test_no_se_puede_entrar_en_el_arbol_de_bloques(client, arboles, bloque_en_disco):
    """`cd /blocks` no es una ruta del disco: es una clave que nadie ha creado."""
    respuesta = client.get("/files", params={"path": "/blocks"})

    assert respuesta.status_code == 404
    assert bloque_en_disco.exists()


def test_no_se_puede_borrar_un_directorio_de_bloques(
    client, arboles, bloque_en_disco
):
    """Con los planos mezclados, este rmdir borraria bloques de verdad."""
    respuesta = client.delete(
        "/directories", params={"path": f"/blocks/{UUID_DEMO}"}
    )

    assert respuesta.status_code == 404
    assert bloque_en_disco.exists()


def test_no_se_puede_borrar_un_bloque_saliendo_por_arriba(
    client, arboles, bloque_en_disco
):
    """Una ruta que sube por encima de la raiz no llega a ser una clave."""
    respuesta = client.delete(
        "/files", params={"path": f"/../blocks/{UUID_DEMO}/000000.blk"}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["detail"] == "Acceso fuera del sistema DFS no permitido"
    assert bloque_en_disco.exists()


def test_un_directorio_del_usuario_llamado_blocks_no_toca_nada(
    client, arboles, bloque_en_disco
):
    """La separacion por construccion, en su forma mas clara.

    El usuario puede llamar `/blocks` a un directorio suyo y no pasa nada: su
    nombre vive en un JSON de metadatos, y el arbol de datos ni se entera.
    """
    respuesta = client.post("/directories", json={"path": "/blocks"})

    assert respuesta.status_code == 200
    assert client.get("/files", params={"path": "/blocks"}).json()["items"] == []
    assert bloque_en_disco.exists()
    assert [d.name for d in arboles.blocks.iterdir()] == [UUID_DEMO]


def test_la_raiz_no_se_puede_borrar(client, arboles):
    """Se conserva el mensaje del monolito."""
    respuesta = client.delete("/directories", params={"path": "/"})

    assert respuesta.status_code == 403
    assert respuesta.json()["detail"] == "No se puede eliminar la raíz del DFS"


# ---------------------------------------------------------------------------
# Cada arbol del disco es su propia jaula
# ---------------------------------------------------------------------------

def test_resolve_path_exige_la_raiz(arboles):
    """Sin valor por defecto.

    Con dos arboles, olvidar el argumento tiene que ser un error en el acto y
    no una caida silenciosa al arbol equivocado.
    """
    with pytest.raises(TypeError):
        filesystem.resolve_path("/universidad")


@pytest.mark.parametrize("arbol", ["metadata", "blocks"])
def test_cada_arbol_tiene_su_propia_jaula(arboles, arbol):
    """La misma ruta que se sale responde 403 en los dos."""
    raiz = getattr(arboles, arbol)

    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(raiz, "/../fuera")

    assert error.value.status_code == 403
    assert error.value.detail == "Acceso fuera del sistema DFS no permitido"


def test_una_ruta_de_metadatos_no_alcanza_el_arbol_de_bloques(arboles):
    """Es exactamente lo que pasaria con una sola jaula en STORAGE_ROOT.

    `metadata/../blocks/<file_id>` cae dentro de STORAGE_ROOT, asi que una
    jaula anclada arriba lo dejaria pasar.
    """
    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(arboles.metadata, f"/../blocks/{UUID_DEMO}")

    assert error.value.status_code == 403


def test_una_ruta_de_bloques_no_alcanza_los_metadatos(arboles):
    """La frontera vale en las dos direcciones."""
    with pytest.raises(HTTPException) as error:
        filesystem.resolve_path(arboles.blocks, "/../metadata/0000000000000000.json")

    assert error.value.status_code == 403
