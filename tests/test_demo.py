"""Los ayudantes del guion — SPEC-06, paso 5 del plan.

El guion orquesta y no calcula nada: lo que decide si el reparto esta bien, y
lo que lo dibuja, vive aqui y se prueba con respuestas de mentira. Lo que no se
puede probar asi —que los peers contesten— es lo que se verifica levantando el
despliegue, y por eso conviene que todo lo demas ya este cerrado antes.

La regla que estos tests fijan es la de la SPEC: **el reparto se demuestra
preguntandoselo a los peers**, no listando directorios. Un `200` prueba que el
bloque esta *y* que se puede leer; un `404` prueba que ese peer no lo tiene.
"""

import pytest

from demo import actos
from server import ring


FILE_ID = "5f3e0000-0000-4000-8000-000000000001"

DIRECCIONES = {
    "peer1": "http://peer1:9001",
    "peer2": "http://peer2:9002",
    "peer3": "http://peer3:9003",
}


class Respuesta:
    """Lo unico que `reparto` mira de una respuesta."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture
def peers_que_tienen(monkeypatch):
    """Tres peers de mentira que contestan segun quien tenga cada bloque."""
    de_direccion = {address: peer_id for peer_id, address in DIRECCIONES.items()}

    def _monta(tenedores: dict[int, list[str]]):
        def get(url, **kwargs):
            base, _, cola = url.partition("/blocks/")
            _, _, indice = cola.rpartition("/")
            peer_id = de_direccion[base]

            tiene = peer_id in tenedores.get(int(indice), [])

            return Respuesta(200 if tiene else 404)

        monkeypatch.setattr(actos.requests, "get", get)

    return _monta


@pytest.fixture
def sin_red(monkeypatch):
    """Cualquier peticion es un fallo del test, no un fallo de la red."""
    def prohibido(*args, **kwargs):
        raise AssertionError(f"esto no deberia hablar con nadie: {args}")

    for verbo in ("get", "post", "put", "delete"):
        monkeypatch.setattr(actos.requests, verbo, prohibido)


# ---------------------------------------------------------------------------
# `reparto` — quien contesto 200, bloque a bloque
# ---------------------------------------------------------------------------

def test_el_reparto_es_lo_que_contestan_los_peers(peers_que_tienen):
    peers_que_tienen({0: ["peer2"], 1: ["peer1"], 2: ["peer2"]})

    assert actos.reparto(FILE_ID, [0, 1, 2], DIRECCIONES) == {
        0: ["peer2"],
        1: ["peer1"],
        2: ["peer2"],
    }


def test_un_bloque_que_nadie_sirve_sale_sin_tenedores(peers_que_tienen):
    """Tiene que poder representarse, porque es justo lo que pasa cuando el
    acto 4 sube con dos peers apagados."""
    peers_que_tienen({0: ["peer2"], 1: []})

    assert actos.reparto(FILE_ID, [0, 1], DIRECCIONES)[1] == []


def test_un_bloque_en_dos_peers_sale_con_los_dos(peers_que_tienen):
    """`reparto` informa, no juzga: con factor de replica 1 dos tenedores son
    un defecto, pero quien lo dice es `defectos`, no el que pregunta."""
    peers_que_tienen({0: ["peer1", "peer3"]})

    assert actos.reparto(FILE_ID, [0], DIRECCIONES)[0] == ["peer1", "peer3"]


def test_el_reparto_pregunta_a_todos_los_peers_por_todos_los_bloques(monkeypatch):
    """Nueve preguntas para tres bloques y tres peers. Preguntar solo al peer
    que dice `lookup` demostraria menos: que el bloque esta donde ya sabiamos.
    """
    preguntas = []

    def get(url, **kwargs):
        preguntas.append(url)
        return Respuesta(404)

    monkeypatch.setattr(actos.requests, "get", get)

    actos.reparto(FILE_ID, [0, 1, 2], DIRECCIONES)

    assert len(preguntas) == 9
    assert all(f"/blocks/{FILE_ID}/" in url for url in preguntas)


# ---------------------------------------------------------------------------
# `defectos` — lo que el guion afirma del acto 2, en un solo sitio
# ---------------------------------------------------------------------------

def test_un_reparto_sano_no_tiene_defectos():
    reparto = {0: ["peer2"], 1: ["peer1"], 2: ["peer3"]}
    segun_lookup = {0: ["peer2"], 1: ["peer1"], 2: ["peer3"]}

    assert actos.defectos(reparto, segun_lookup, sorted(DIRECCIONES)) == []


def test_un_bloque_sin_tenedor_es_un_defecto():
    reparto = {0: ["peer2"], 1: []}

    problemas = actos.defectos(reparto, {0: ["peer2"], 1: ["peer1"]}, ["peer1", "peer2"])

    assert any("1" in problema for problema in problemas)
    assert problemas


def test_un_bloque_con_dos_tenedores_es_un_defecto():
    """Con factor de replica 1 el mismo bloque en dos peers no es redundancia:
    es que alguien escribio donde no le tocaba."""
    reparto = {0: ["peer1", "peer2"]}

    assert actos.defectos(reparto, {0: ["peer1"]}, ["peer1", "peer2"])


def test_un_tenedor_que_no_es_el_que_dice_lookup_es_un_defecto():
    """Es la comprobacion que enlaza los dos planos: los bytes estan donde los
    metadatos dicen que estan, o el `receive` de otro dia no los encontrara."""
    reparto = {0: ["peer3"]}

    assert actos.defectos(reparto, {0: ["peer1"]}, ["peer1", "peer3"])


def test_un_peer_sin_un_solo_bloque_es_un_defecto():
    """No es un fallo del sistema, es una demo que no demuestra lo que dice:
    si un peer se queda vacio, el reparto no se ve."""
    reparto = {0: ["peer1"], 1: ["peer2"]}

    problemas = actos.defectos(reparto, reparto, ["peer1", "peer2", "peer3"])

    assert any("peer3" in problema for problema in problemas)


# ---------------------------------------------------------------------------
# `matriz` — solo dibuja
# ---------------------------------------------------------------------------

def test_la_matriz_pone_una_marca_donde_esta_el_bloque():
    dibujo = actos.matriz({0: ["peer2"], 1: ["peer1"]}, sorted(DIRECCIONES))
    lineas = dibujo.splitlines()

    assert lineas[0].split() == ["bloque", "peer1", "peer2", "peer3"]
    assert lineas[1].split() == ["0", actos.VACIO, actos.MARCA, actos.VACIO]
    assert lineas[2].split() == ["1", actos.MARCA, actos.VACIO, actos.VACIO]


def test_la_matriz_dibuja_tambien_lo_que_esta_mal():
    """Un bloque sin tenedor y otro con dos tienen que verse en el dibujo: es
    el defecto lo que hay que poder mirar, no solo leer."""
    dibujo = actos.matriz({0: [], 1: ["peer1", "peer3"]}, sorted(DIRECCIONES))
    lineas = dibujo.splitlines()

    assert lineas[1].split() == ["0", actos.VACIO, actos.VACIO, actos.VACIO]
    assert lineas[2].split() == ["1", actos.MARCA, actos.VACIO, actos.MARCA]


def test_la_matriz_termina_contando_cuantos_tiene_cada_uno():
    """La cuenta es lo que se mira de verdad cuando hay 24 filas."""
    reparto = {0: ["peer1"], 1: ["peer1"], 2: ["peer2"]}

    final = actos.matriz(reparto, sorted(DIRECCIONES)).splitlines()[-1]

    assert "peer1=2" in final
    assert "peer2=1" in final
    assert "peer3=0" in final


# ---------------------------------------------------------------------------
# `dueno_de` — el anillo es una funcion pura de la membresia
# ---------------------------------------------------------------------------

def test_el_dueno_de_un_directorio_se_calcula_sin_hablar_con_nadie(sin_red):
    """Por eso el acto 4 puede saber a quien dejar vivo **antes** de apagar
    nada: no hace falta preguntarselo a un peer que quiza ya este parado."""
    assert actos.dueno_de("/demo") == ring.choose_nodes("/demo")[0]


def test_el_dueno_es_el_mismo_que_calcularia_el_peer(sin_red):
    """Mismo modulo, mismo anillo: si el guion calculara el suyo, el dia que
    discreparan la demo pararia al peer equivocado y nadie sabria por que."""
    for directorio in ("/", "/demo", "/universidad"):
        assert actos.dueno_de(directorio) in {
            peer["peer_id"] for peer in ring.LOCAL.peers()
        }
