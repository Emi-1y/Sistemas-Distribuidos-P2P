"""Membresia y endpoints del anillo — SPEC-02, pasos 5, 6 y 7 del plan TDD.

Ningun test abre un socket: la propagacion del alta se sustituye por un doble
que registra a quien se llamo y con que cabeceras.
"""

import pytest

from server import config, membership, ring
from server.ring import Ring


BOOTSTRAP = [
    ("peer1", "http://peer1:9001"),
    ("peer2", "http://peer2:9002"),
    ("peer3", "http://peer3:9003"),
]


@pytest.fixture
def anillo_local(monkeypatch):
    """Un peer solo, con su identidad y direccion de config."""
    local = Ring(vnodes=128)
    local.add_peer(config.PEER_ID, config.ADDRESS)

    monkeypatch.setattr(ring, "LOCAL", local)
    return local


@pytest.fixture
def anillo_de_tres(monkeypatch):
    """El peer1 arrancado con los tres peers de la demo en el bootstrap."""
    local = Ring(vnodes=128)

    for peer_id, address in BOOTSTRAP:
        local.add_peer(peer_id, address)

    monkeypatch.setattr(ring, "LOCAL", local)
    monkeypatch.setattr(config, "PEER_ID", "peer1")
    return local


@pytest.fixture
def enviados(monkeypatch):
    """Sustituye la propagacion y registra cada reenvio."""
    registro = []

    def falso(target, payload, headers):
        registro.append(
            {"target": target, "payload": payload, "headers": headers}
        )

    monkeypatch.setattr(membership, "_send_join", falso)
    return registro


# ---------------------------------------------------------------------------
# Paso 5 — GET /health y GET /ring (AC-01, AC-02)
# ---------------------------------------------------------------------------

def test_health_responde_la_identidad_y_la_version_del_anillo(client, anillo_local):
    """AC-01: si el peer contesta, esta vivo. Es lo que usara el hito 3."""
    respuesta = client.get("/health")

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "peer_id": config.PEER_ID,
        "status": "ok",
        "ring_version": anillo_local.version(),
    }


def test_ring_devuelve_la_membresia_del_bootstrap(client, anillo_de_tres):
    """AC-02: los tres, el que contesta incluido, sin haber hablado con nadie."""
    respuesta = client.get("/ring")

    assert respuesta.status_code == 200

    cuerpo = respuesta.json()
    assert cuerpo["ring_version"] == anillo_de_tres.version()
    assert cuerpo["peers"] == [
        {"peer_id": peer_id, "address": address}
        for peer_id, address in BOOTSTRAP
    ]


def test_ring_no_expone_los_nodos_virtuales(client, anillo_de_tres):
    """El anillo se reconstruye desde la membresia; las 384 posiciones no viajan."""
    assert len(client.get("/ring").json()["peers"]) == 3


# ---------------------------------------------------------------------------
# Paso 6 — POST /peers/join (AC-08, AC-09, AC-10, AC-11)
# ---------------------------------------------------------------------------

def test_un_peer_nuevo_entra_en_el_anillo(client, anillo_de_tres, enviados):
    """AC-08: responde el anillo ya con el dentro, y la version cambia."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer4", "address": "http://peer4:9004"},
    )

    assert respuesta.status_code == 200

    cuerpo = respuesta.json()
    assert cuerpo["ring_version"] != version_previa
    assert cuerpo["ring_version"] == anillo_de_tres.version()
    assert {"peer_id": "peer4", "address": "http://peer4:9004"} in cuerpo["peers"]


def test_repetir_el_alta_con_la_misma_direccion_es_idempotente(
    client, anillo_de_tres, enviados
):
    """AC-09: mismo peer, misma direccion, misma version."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer2", "address": "http://peer2:9002"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ring_version"] == version_previa
    assert len(respuesta.json()["peers"]) == 3


def test_el_mismo_peer_id_con_otra_direccion_es_un_conflicto(
    client, anillo_de_tres, enviados
):
    """AC-10: 409 y el anillo intacto."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer2", "address": "http://otro-host:9002"},
    )

    assert respuesta.status_code == 409
    assert respuesta.json()["detail"] == "El peer ya está registrado"
    assert anillo_de_tres.version() == version_previa
    assert anillo_de_tres.address_of("peer2") == "http://peer2:9002"


@pytest.mark.parametrize(
    "direccion", ["ftp://peer4:9004", "peer4:9004", "http://", ""]
)
def test_una_direccion_que_no_es_una_url_http_se_rechaza(
    client, anillo_de_tres, enviados, direccion
):
    """AC-11: 400 y el anillo intacto."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer4", "address": direccion},
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["detail"] == "Dirección de peer inválida"
    assert anillo_de_tres.version() == version_previa


# ---------------------------------------------------------------------------
# Paso 7 — propagacion de una ronda (AC-12)
# ---------------------------------------------------------------------------

def test_el_alta_se_reenvia_a_los_peers_conocidos(client, anillo_de_tres, enviados):
    """AC-12: a todos menos a uno mismo y menos al que acaba de entrar."""
    client.post(
        "/peers/join",
        json={"peer_id": "peer4", "address": "http://peer4:9004"},
    )

    destinos = sorted(envio["target"] for envio in enviados)
    assert destinos == ["http://peer2:9002", "http://peer3:9003"]

    for envio in enviados:
        assert envio["payload"] == {
            "peer_id": "peer4",
            "address": "http://peer4:9004",
        }
        assert envio["headers"]["X-Forwarded-By"] == "peer1"


def test_un_alta_ya_reenviada_se_aplica_pero_no_se_vuelve_a_reenviar(
    client, anillo_de_tres, enviados
):
    """AC-12: una sola ronda. Sin esto habria tormenta de reenvios."""
    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer4", "address": "http://peer4:9004"},
        headers={"X-Forwarded-By": "peer9"},
    )

    assert respuesta.status_code == 200
    assert anillo_de_tres.address_of("peer4") == "http://peer4:9004"
    assert enviados == []


def test_un_alta_que_no_cambia_nada_no_se_propaga(client, anillo_de_tres, enviados):
    """Si la membresia no cambio, no hay nada que contarle a nadie."""
    client.post(
        "/peers/join",
        json={"peer_id": "peer3", "address": "http://peer3:9003"},
    )

    assert enviados == []


# ---------------------------------------------------------------------------
# Tests limite — seccion 8 de la SPEC
# ---------------------------------------------------------------------------

def test_la_barra_final_no_convierte_un_alta_repetida_en_un_conflicto(
    client, anillo_de_tres, enviados
):
    """Sin normalizar, un caracter dejaria a peer2 fuera del anillo."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer2", "address": "http://peer2:9002/"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ring_version"] == version_previa
    assert enviados == []


def test_un_peer_que_se_da_de_alta_a_si_mismo_no_cambia_nada(
    client, anillo_de_tres, enviados
):
    """peer1 ya esta en su propio anillo desde el arranque."""
    version_previa = anillo_de_tres.version()

    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peer1", "address": "http://peer1:9001"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ring_version"] == version_previa
    assert enviados == []


# ---------------------------------------------------------------------------
# Paso 8 — el anuncio del peer nuevo (AC-15)
# ---------------------------------------------------------------------------

MEMBRESIA_COMPLETA = {
    "ring_version": "irrelevante",
    "peers": [
        {"peer_id": "peer1", "address": "http://peer1:9001"},
        {"peer_id": "peer2", "address": "http://peer2:9002"},
        {"peer_id": "peer3", "address": "http://peer3:9003"},
        {"peer_id": "peer4", "address": "http://peer4:9004"},
    ],
}


@pytest.fixture
def peer_recien_llegado(monkeypatch):
    """peer4: se conoce solo a si mismo y su bootstrap no lo incluye."""
    local = Ring()
    local.add_peer("peer4", "http://peer4:9004")

    monkeypatch.setattr(ring, "LOCAL", local)
    monkeypatch.setattr(config, "PEER_ID", "peer4")
    monkeypatch.setattr(config, "ADDRESS", "http://peer4:9004")
    monkeypatch.setattr(config, "BOOTSTRAP", BOOTSTRAP)

    return local


def test_el_peer_nuevo_se_anuncia_y_absorbe_la_membresia(
    monkeypatch, peer_recien_llegado
):
    """AC-15: un anillo de un solo nodo se cree dueno de todas las claves.

    Sin absorber, peer4 aceptaria bloques que ningun otro peer sabe que tiene.
    """
    llamadas = []

    def falso(target, payload, headers):
        llamadas.append((target, payload, headers))
        return MEMBRESIA_COMPLETA

    monkeypatch.setattr(membership, "_send_join", falso)

    assert membership.announce() is True

    # Convergio con los demas.
    referencia = Ring()
    for peer in MEMBRESIA_COMPLETA["peers"]:
        referencia.add_peer(peer["peer_id"], peer["address"])

    assert peer_recien_llegado.version() == referencia.version()
    assert [p["peer_id"] for p in peer_recien_llegado.peers()] == [
        "peer1",
        "peer2",
        "peer3",
        "peer4",
    ]


def test_el_anuncio_va_sin_la_cabecera_de_reenvio(monkeypatch, peer_recien_llegado):
    """AC-15: marcarlo impediria que el receptor lo propague a los demas."""
    llamadas = []

    def falso(target, payload, headers):
        llamadas.append((target, payload, headers))
        return MEMBRESIA_COMPLETA

    monkeypatch.setattr(membership, "_send_join", falso)
    membership.announce()

    target, payload, headers = llamadas[0]

    assert target == "http://peer1:9001"
    assert payload == {"peer_id": "peer4", "address": "http://peer4:9004"}
    assert "X-Forwarded-By" not in headers


def test_el_anuncio_se_para_en_el_primero_que_responde(
    monkeypatch, peer_recien_llegado
):
    """Basta uno: todos los peers publican la misma membresia."""
    llamadas = []

    def falso(target, payload, headers):
        llamadas.append(target)
        return MEMBRESIA_COMPLETA

    monkeypatch.setattr(membership, "_send_join", falso)
    membership.announce()

    assert llamadas == ["http://peer1:9001"]


def test_el_anuncio_prueba_con_el_siguiente_si_el_primero_no_contesta(
    monkeypatch, peer_recien_llegado
):
    """Anunciarse a uno solo seria un punto unico de fallo al arrancar."""
    llamadas = []

    def falso(target, payload, headers):
        llamadas.append(target)

        if target == "http://peer1:9001":
            return None

        return MEMBRESIA_COMPLETA

    monkeypatch.setattr(membership, "_send_join", falso)

    assert membership.announce() is True
    assert llamadas == ["http://peer1:9001", "http://peer2:9002"]


def test_si_no_contesta_nadie_el_anillo_queda_como_estaba(
    monkeypatch, peer_recien_llegado
):
    """El peer arranca aislado. Reintentar y reconciliar es hito 3."""
    version_previa = peer_recien_llegado.version()

    monkeypatch.setattr(
        membership, "_send_join", lambda target, payload, headers: None
    )

    assert membership.announce() is False
    assert peer_recien_llegado.version() == version_previa


def test_un_peer_que_esta_en_su_bootstrap_no_habla_con_nadie(
    monkeypatch, anillo_de_tres
):
    """AC-15, la otra mitad: es lo que permite arrancar los tres a la vez.

    Si el arranque dependiera de que otro peer responda, el orden de arranque
    decidiria si el sistema funciona.
    """
    monkeypatch.setattr(config, "BOOTSTRAP", BOOTSTRAP)

    llamadas = []
    monkeypatch.setattr(
        membership,
        "_send_join",
        lambda target, payload, headers: llamadas.append(target),
    )

    assert membership.announce() is False
    assert llamadas == []


def test_sin_bootstrap_no_hay_a_quien_anunciarse(monkeypatch, anillo_local):
    """El peer por defecto del hito 1 arranca solo, como siempre."""
    monkeypatch.setattr(config, "BOOTSTRAP", [])

    llamadas = []
    monkeypatch.setattr(
        membership,
        "_send_join",
        lambda target, payload, headers: llamadas.append(target),
    )

    assert membership.announce() is False
    assert llamadas == []
