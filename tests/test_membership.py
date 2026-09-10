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
