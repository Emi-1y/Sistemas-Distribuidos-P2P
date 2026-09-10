"""Metadatos locales — SPEC-04, fase 1 del plan TDD (pasos 1 a 6).

Ningun test de este archivo abre un socket ni sustituye nada de `routing`: la
semantica de cada operacion se prueba entera sobre un solo peer. Si algo falla
aqui, es la logica. El reenvio se prueba en la fase 2, y para entonces esto ya
esta fijado.
"""

import json
import os
import uuid

import pytest
from fastapi import HTTPException

from server import metadata, ring
from server.ring import Ring


def sha(texto: str) -> str:
    """Un checksum con la forma correcta; su valor da igual aqui."""
    return f"{abs(hash(texto)) % (16 ** 64):064x}"


@pytest.fixture
def meta(tmp_path, monkeypatch):
    """METADATA_ROOT temporal y un anillo de un solo peer: toda clave es mia."""
    root = tmp_path / "storage" / "metadata"
    root.mkdir(parents=True)
    monkeypatch.setattr(metadata, "METADATA_ROOT", root)

    local = Ring()
    local.add_peer("peer1", "http://peer1:9001")
    monkeypatch.setattr(ring, "LOCAL", local)

    return root


@pytest.fixture
def anillo_de_tres(meta, monkeypatch):
    """Tres peers, para que el plan de allocate reparta y se pueda contradecir."""
    local = Ring()

    for numero in (1, 2, 3):
        local.add_peer(f"peer{numero}", f"http://peer{numero}:900{numero}")

    monkeypatch.setattr(ring, "LOCAL", local)
    return local


@pytest.fixture
def bloque_pequeno(monkeypatch):
    """512 bytes por bloque, para que los planes quepan en un assert."""
    monkeypatch.setattr(metadata.config, "BLOCK_SIZE", 512)
    return 512


def crear_directorio(ruta: str) -> None:
    """Un mkdir completo: primero la entrada en el padre, luego el contenido."""
    metadata.add_directory(ruta)
    metadata.ensure_bucket(ruta)


def confirmacion(entrada: dict, peers=("peer1",)) -> list[dict]:
    return [
        {
            "index": bloque["index"],
            "checksum": sha(f"{entrada['file_id']}:{bloque['index']}"),
            "peers": list(peers),
        }
        for bloque in entrada["blocks"]
    ]


# ---------------------------------------------------------------------------
# Paso 1 — rutas logicas y persistencia (AC-06, AC-17)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "cruda, esperada",
    [
        ("/universidad/tarea.pdf", "/universidad/tarea.pdf"),
        ("//universidad//tarea.pdf", "/universidad/tarea.pdf"),
        ("/universidad/./apuntes/../tarea.pdf", "/universidad/tarea.pdf"),
        ("/universidad/", "/universidad"),
        ("universidad", "/universidad"),
        ("/", "/"),
        ("//", "/"),
    ],
)
def test_las_rutas_equivalentes_dan_la_misma_clave(cruda, esperada):
    """Si dos escrituras de la misma ruta normalizaran distinto, acabarian en
    peers distintos y el archivo desapareceria al leerlo."""
    assert metadata.normalize(cruda) == esperada


@pytest.mark.parametrize("ruta", ["/..", "/universidad/../..", "..", "", "   "])
def test_una_ruta_que_sube_por_encima_de_la_raiz_se_rechaza(ruta):
    """AC-06: una clave por encima de la raiz no significa nada."""
    with pytest.raises(HTTPException) as error:
        metadata.normalize(ruta)

    assert error.value.status_code == 403
    assert error.value.detail == "Acceso fuera del sistema DFS no permitido"


@pytest.mark.parametrize(
    "ruta, padre, nombre",
    [
        ("/universidad/tarea.pdf", "/universidad", "tarea.pdf"),
        ("/universidad", "/", "universidad"),
        ("/", "/", ""),
    ],
)
def test_el_padre_y_el_nombre_de_una_ruta(ruta, padre, nombre):
    assert metadata.parent_of(ruta) == padre
    assert metadata.name_of(ruta) == nombre


def test_la_raiz_existe_siempre_sin_que_nadie_la_cree(meta):
    """Si hubiera que crearla, cada operacion tendria el caso «aun no hay raiz»."""
    assert metadata.list_entries("/") == []


def test_un_contenido_escrito_se_relee_del_disco(meta):
    """AC-17: los metadatos sobreviven a que el proceso se olvide de todo."""
    crear_directorio("/universidad")

    assert list(meta.glob("*.json")) != []
    assert [item["name"] for item in metadata.list_entries("/")] == ["universidad"]


def test_la_escritura_de_metadatos_es_atomica(meta):
    """AC-17: un JSON a medias no puede quedarse nunca en su sitio definitivo.

    Sin esto, un peer que muriera durante un allocate dejaria ilegible el
    contenido entero del directorio: la subida de un archivo habria destruido
    los metadatos de todos sus hermanos.
    """
    crear_directorio("/universidad")

    def revienta(*args, **kwargs):
        raise OSError("disco lleno")

    # Contexto propio: un monkeypatch.undo() aqui deshacaria tambien los
    # parches del fixture `meta`, que comparte instancia con el test.
    with pytest.MonkeyPatch.context() as parche:
        parche.setattr(os, "replace", revienta)

        with pytest.raises(OSError):
            metadata.add_directory("/universidad/apuntes")

    for archivo in meta.glob("*.json"):
        json.loads(archivo.read_text(encoding="utf-8"))

    assert metadata.list_entries("/universidad") == []


# ---------------------------------------------------------------------------
# Paso 2 — directorios locales: mkdir y ls (AC-13, AC-14, AC-15)
# ---------------------------------------------------------------------------

def test_mkdir_escribe_las_dos_cosas(meta):
    """AC-14: la entrada en el padre y el contenido propio."""
    crear_directorio("/universidad")

    assert metadata.list_entries("/") == [
        {"name": "universidad", "type": "directory", "size": None}
    ]
    assert metadata.list_entries("/universidad") == []


def test_sin_el_contenido_propio_no_se_puede_entrar(meta):
    """El estado que deja un mkdir a medias: se ve desde fuera, no se entra."""
    metadata.add_directory("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.list_entries("/universidad")

    assert error.value.status_code == 404
    assert error.value.detail == "El directorio no existe"


def test_ls_de_un_directorio_recien_creado_es_vacio_y_no_404(meta):
    """Vacio y no existente son cosas distintas, y es lo que distingue el contenido."""
    crear_directorio("/universidad")

    assert metadata.list_entries("/universidad") == []


def test_ls_de_un_directorio_que_no_existe(meta):
    with pytest.raises(HTTPException) as error:
        metadata.list_entries("/universidad")

    assert error.value.status_code == 404
    assert error.value.detail == "El directorio no existe"


def test_mkdir_sobre_un_nombre_ocupado(meta):
    """AC-15."""
    crear_directorio("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.add_directory("/universidad")

    assert error.value.status_code == 409
    assert error.value.detail == "El directorio ya existe"


def test_mkdir_de_la_raiz(meta):
    """La raiz existe siempre; que fuera un caso especial sin escribir seria peor."""
    with pytest.raises(HTTPException) as error:
        metadata.add_directory("/")

    assert error.value.status_code == 409


def test_mkdir_con_el_padre_inexistente(meta):
    with pytest.raises(HTTPException) as error:
        metadata.add_directory("/universidad/apuntes")

    assert error.value.status_code == 404
    assert error.value.detail == "El directorio no existe"


def test_ls_devuelve_directorios_y_archivos_confirmados(meta, bloque_pequeno):
    """AC-13: y ningun pendiente."""
    crear_directorio("/universidad")
    crear_directorio("/universidad/apuntes")

    confirmado = metadata.allocate("/universidad/tarea.pdf", 600)
    metadata.commit(confirmado["path"], confirmado["file_id"], confirmacion(confirmado))

    metadata.allocate("/universidad/borrador.pdf", 600)

    assert metadata.list_entries("/universidad") == [
        {"name": "apuntes", "type": "directory", "size": None},
        {"name": "tarea.pdf", "type": "file", "size": 600},
    ]


# ---------------------------------------------------------------------------
# Paso 3 — allocate (AC-01 a AC-05)
# ---------------------------------------------------------------------------

def test_allocate_reparte_el_archivo_en_bloques(meta, bloque_pequeno):
    """AC-01."""
    crear_directorio("/universidad")

    entrada = metadata.allocate("/universidad/tarea.pdf", 1200)

    assert entrada["path"] == "/universidad/tarea.pdf"
    assert entrada["state"] == "pending"
    assert entrada["size"] == 1200
    assert entrada["block_size"] == 512
    assert entrada["created_at"].endswith("Z")
    uuid.UUID(entrada["file_id"])

    assert [b["index"] for b in entrada["blocks"]] == [0, 1, 2]
    assert [b["size"] for b in entrada["blocks"]] == [512, 512, 176]
    assert all(b["checksum"] is None for b in entrada["blocks"])
    assert all(b["peers"] == ["peer1"] for b in entrada["blocks"])


@pytest.mark.parametrize(
    "size, tamanos",
    [(0, []), (1, [1]), (512, [512]), (1024, [512, 512]), (1025, [512, 512, 1])],
)
def test_el_reparto_en_bloques_en_los_bordes(meta, bloque_pequeno, size, tamanos):
    """El error de uno clasico del techo, por los dos lados."""
    crear_directorio("/universidad")

    entrada = metadata.allocate("/universidad/tarea.pdf", size)

    assert [b["size"] for b in entrada["blocks"]] == tamanos


def test_una_entrada_pendiente_no_existe_para_nadie(meta, bloque_pequeno):
    """AC-02: hasta el commit, ni ls ni lookup la ven."""
    crear_directorio("/universidad")
    metadata.allocate("/universidad/tarea.pdf", 600)

    assert metadata.list_entries("/universidad") == []

    with pytest.raises(HTTPException) as error:
        metadata.lookup("/universidad/tarea.pdf")

    assert error.value.status_code == 404


def test_allocate_con_el_padre_inexistente(meta):
    """AC-03."""
    with pytest.raises(HTTPException) as error:
        metadata.allocate("/universidad/tarea.pdf", 600)

    assert error.value.status_code == 404
    assert error.value.detail == "El directorio no existe"


@pytest.mark.parametrize("ocupante", ["confirmado", "pendiente", "directorio"])
def test_allocate_sobre_un_nombre_ocupado(meta, bloque_pequeno, ocupante):
    """AC-04: una entrada pendiente tambien ocupa el nombre.

    Si no, dos clientes reservarian la misma ruta y el segundo commit ganaria.
    """
    crear_directorio("/universidad")

    if ocupante == "directorio":
        crear_directorio("/universidad/tarea.pdf")
    else:
        entrada = metadata.allocate("/universidad/tarea.pdf", 600)

        if ocupante == "confirmado":
            metadata.commit(
                entrada["path"], entrada["file_id"], confirmacion(entrada)
            )

    with pytest.raises(HTTPException) as error:
        metadata.allocate("/universidad/tarea.pdf", 600)

    assert error.value.status_code == 409
    assert error.value.detail == "El archivo ya existe"


@pytest.mark.parametrize(
    "ruta, size",
    [("/universidad/tarea.pdf", -1), ("", 600), ("   ", 600)],
)
def test_allocate_con_parametros_invalidos(meta, ruta, size):
    """AC-05: y el path vacio da 400, no el 403 de la normalizacion."""
    crear_directorio("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.allocate(ruta, size)

    assert error.value.status_code == 400
    assert error.value.detail == "Parámetros de asignación inválidos"


# ---------------------------------------------------------------------------
# Paso 4 — commit (AC-07 a AC-11)
# ---------------------------------------------------------------------------

def test_commit_confirma_la_entrada(meta, bloque_pequeno):
    """AC-07."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 1200)
    reporte = confirmacion(entrada)

    confirmada = metadata.commit(entrada["path"], entrada["file_id"], reporte)

    assert confirmada["state"] == "committed"
    assert [b["checksum"] for b in confirmada["blocks"]] == [
        r["checksum"] for r in reporte
    ]
    assert [item["name"] for item in metadata.list_entries("/universidad")] == [
        "tarea.pdf"
    ]


def test_los_peers_guardados_son_los_reportados_no_los_planeados(
    meta, anillo_de_tres, bloque_pequeno
):
    """AC-07: el plan de allocate es una intencion, no un hecho.

    El almacen de bloques de la SPEC-03 acepta lo que le manden sin comprobar
    si le tocaba, asi que el cliente puede haber subido a otro peer. El unico
    que conoce el hecho es el que recibio los 201.
    """
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)

    planeado = entrada["blocks"][0]["peers"][0]
    otro = next(p for p in ("peer1", "peer2", "peer3") if p != planeado)

    confirmada = metadata.commit(
        entrada["path"], entrada["file_id"], confirmacion(entrada, peers=(otro,))
    )

    assert [b["peers"] for b in confirmada["blocks"]] == [[otro]] * 2
    assert planeado != otro


def test_commit_de_un_file_id_que_no_esta_en_esa_ruta(meta, bloque_pequeno):
    """AC-08."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)

    with pytest.raises(HTTPException) as error:
        metadata.commit(entrada["path"], str(uuid.uuid4()), confirmacion(entrada))

    assert error.value.status_code == 404
    assert error.value.detail == "El archivo no existe"


def test_commit_repetido(meta, bloque_pequeno):
    """AC-09."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)
    metadata.commit(entrada["path"], entrada["file_id"], confirmacion(entrada))

    with pytest.raises(HTTPException) as error:
        metadata.commit(entrada["path"], entrada["file_id"], confirmacion(entrada))

    assert error.value.status_code == 409
    assert error.value.detail == "El archivo ya fue confirmado"


def test_commit_al_que_le_falta_un_bloque(meta, bloque_pequeno):
    """AC-10."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 1200)

    with pytest.raises(HTTPException) as error:
        metadata.commit(
            entrada["path"], entrada["file_id"], confirmacion(entrada)[:-1]
        )

    assert error.value.status_code == 422
    assert error.value.detail == "Confirmación de bloques incompleta"


def test_commit_con_un_peer_que_no_esta_en_el_anillo(meta, bloque_pequeno):
    """AC-11: sin esto el cliente escribe cualquier cosa y la metadata miente."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)

    with pytest.raises(HTTPException) as error:
        metadata.commit(
            entrada["path"],
            entrada["file_id"],
            confirmacion(entrada, peers=("peer9",)),
        )

    assert error.value.status_code == 422
    assert error.value.detail == "Confirmación de bloques inválida"


def test_repetir_un_peer_no_cuenta_como_replicar(
    meta, anillo_de_tres, bloque_pequeno, monkeypatch
):
    """Se cuentan peers distintos: ["peer2", "peer2"] no son dos replicas."""
    monkeypatch.setattr(metadata, "REPLICAS", 2)
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)

    with pytest.raises(HTTPException) as error:
        metadata.commit(
            entrada["path"],
            entrada["file_id"],
            confirmacion(entrada, peers=("peer2", "peer2")),
        )

    assert error.value.status_code == 422
    assert error.value.detail == "Confirmación de bloques incompleta"


def test_el_factor_de_replica_subido_a_tres(
    meta, anillo_de_tres, bloque_pequeno, monkeypatch
):
    """El gancho del hito 3, probado y no prometido: sin tocar codigo."""
    monkeypatch.setattr(metadata, "REPLICAS", 3)
    crear_directorio("/universidad")

    entrada = metadata.allocate("/universidad/tarea.pdf", 600)

    assert all(len(set(b["peers"])) == 3 for b in entrada["blocks"])

    with pytest.raises(HTTPException) as error:
        metadata.commit(
            entrada["path"],
            entrada["file_id"],
            confirmacion(entrada, peers=("peer1", "peer2")),
        )

    assert error.value.status_code == 422

    confirmada = metadata.commit(
        entrada["path"],
        entrada["file_id"],
        confirmacion(entrada, peers=("peer1", "peer2", "peer3")),
    )

    assert confirmada["state"] == "committed"


# ---------------------------------------------------------------------------
# Paso 5 — lookup (AC-12)
# ---------------------------------------------------------------------------

def test_lookup_de_un_archivo_confirmado(meta, bloque_pequeno):
    """AC-12: la entrada completa, que es lo que la SPEC-05 usara para bajar."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)
    confirmada = metadata.commit(
        entrada["path"], entrada["file_id"], confirmacion(entrada)
    )

    assert metadata.lookup("/universidad/tarea.pdf") == confirmada


def test_lookup_de_una_ruta_que_es_un_directorio(meta):
    """lookup es de archivos; un directorio no tiene bloques que devolver."""
    crear_directorio("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.lookup("/universidad")

    assert error.value.status_code == 404
    assert error.value.detail == "El archivo no existe"


def test_lookup_de_algo_que_no_existe(meta):
    crear_directorio("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.lookup("/universidad/tarea.pdf")

    assert error.value.status_code == 404


# ---------------------------------------------------------------------------
# Paso 6 — rm y rmdir (AC-15, AC-16)
# ---------------------------------------------------------------------------

def test_rm_borra_la_entrada_y_la_devuelve_con_sus_peers(meta, bloque_pequeno):
    """AC-16: devuelve la entrada porque avisar a los peers es red, y la red
    no entra en este modulo."""
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 1200)
    confirmada = metadata.commit(
        entrada["path"], entrada["file_id"], confirmacion(entrada)
    )

    borrada = metadata.remove_file("/universidad/tarea.pdf")

    assert borrada == confirmada
    assert metadata.list_entries("/universidad") == []


def test_rm_de_algo_que_no_existe(meta):
    crear_directorio("/universidad")

    with pytest.raises(HTTPException) as error:
        metadata.remove_file("/universidad/tarea.pdf")

    assert error.value.status_code == 404
    assert error.value.detail == "El archivo no existe"


def test_rm_de_un_directorio(meta):
    crear_directorio("/universidad")
    crear_directorio("/universidad/apuntes")

    with pytest.raises(HTTPException) as error:
        metadata.remove_file("/universidad/apuntes")

    assert error.value.status_code == 400
    assert error.value.detail == "La ruta no corresponde a un archivo"


def test_rmdir_borra_el_registro_y_el_contenido(meta):
    crear_directorio("/universidad")

    metadata.drop_bucket("/universidad")
    metadata.remove_directory("/universidad")

    assert metadata.list_entries("/") == []


def test_rmdir_de_un_directorio_con_contenido(meta, bloque_pequeno):
    """AC-15."""
    crear_directorio("/universidad")
    crear_directorio("/universidad/apuntes")

    with pytest.raises(HTTPException) as error:
        metadata.drop_bucket("/universidad")

    assert error.value.status_code == 400
    assert error.value.detail == "El directorio no está vacío"


def test_un_directorio_sin_contenido_se_trata_como_vacio(meta):
    """La regla que deshace un mkdir a medias, con un comando que ya existe."""
    metadata.add_directory("/universidad")     # sin ensure_bucket: a medias

    metadata.drop_bucket("/universidad")
    metadata.remove_directory("/universidad")

    assert metadata.list_entries("/") == []


def test_rmdir_de_un_archivo(meta, bloque_pequeno):
    crear_directorio("/universidad")
    entrada = metadata.allocate("/universidad/tarea.pdf", 600)
    metadata.commit(entrada["path"], entrada["file_id"], confirmacion(entrada))

    with pytest.raises(HTTPException) as error:
        metadata.remove_directory("/universidad/tarea.pdf")

    assert error.value.status_code == 400
    assert error.value.detail == "La ruta no es un directorio"


def test_rmdir_de_algo_que_no_existe(meta):
    with pytest.raises(HTTPException) as error:
        metadata.remove_directory("/universidad")

    assert error.value.status_code == 404
    assert error.value.detail == "El directorio no existe"


def test_rmdir_de_la_raiz(meta):
    """Se conserva el mensaje del monolito."""
    with pytest.raises(HTTPException) as error:
        metadata.remove_directory("/")

    assert error.value.status_code == 403
    assert error.value.detail == "No se puede eliminar la raíz del DFS"
