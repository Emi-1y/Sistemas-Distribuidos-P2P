"""Reconciliación de anillos — Hito 3, consistencia.

Estos tests verifican que un peer puede aprender peers que otros conocen y
él no, sin sobrescribir direcciones de peers ya conocidos.
"""

import pytest

from server import config, membership, ring


PEER1 = "peer1"
PEER2 = "peer2"
PEER3 = "peer3"

ANILLO_COMPLETO = {
    PEER1: "http://peer1:9001",
    PEER2: "http://peer2:9002",
    PEER3: "http://peer3:9003",
}


def construir_anillo(monkeypatch, peers):
    """Crea un anillo local con los peers indicados."""
    nuevo = ring.Ring()

    for peer_id, address in peers.items():
        nuevo.add_peer(peer_id, address)

    monkeypatch.setattr(ring, "LOCAL", nuevo)
    monkeypatch.setattr(config, "PEER_ID", PEER1)

    return nuevo


class PeerRemoto:
    """Simula las respuestas de /ring de otros peers."""

    def __init__(self, anillos):
        self.anillos = anillos  # {peer_id: {peer_id: address}}

    def get(self, url, **kwargs):
        class Respuesta:
            def __init__(self, status_code, cuerpo):
                self.status_code = status_code
                self._cuerpo = cuerpo

            def json(self):
                return self._cuerpo

        for peer_id, address in ANILLO_COMPLETO.items():
            if url.startswith(f"{address}/ring"):
                return Respuesta(200, {
                    "ring_version": "abc",
                    "peers": [
                        {"peer_id": pid, "address": addr}
                        for pid, addr in self.anillos[peer_id].items()
                    ]
                })

        return Respuesta(404, {})


@pytest.fixture
def red(monkeypatch):
    """Anillo donde peer1 conoce a peer1 y peer2; peer2 conoce a los tres."""
    construir_anillo(monkeypatch, {
        PEER1: ANILLO_COMPLETO[PEER1],
        PEER2: ANILLO_COMPLETO[PEER2],
    })

    anillos = {
        PEER2: {
            PEER1: ANILLO_COMPLETO[PEER1],
            PEER2: ANILLO_COMPLETO[PEER2],
            PEER3: ANILLO_COMPLETO[PEER3],
        }
    }

    simulada = PeerRemoto(anillos)

    import httpx
    monkeypatch.setattr(httpx, "get", simulada.get)

    return simulada


def test_reconcile_with_aprende_peer_desconocido(red, monkeypatch):
    """peer1 aprende a peer3 preguntándole a peer2."""
    assert PEER3 not in {p["peer_id"] for p in ring.LOCAL.peers()}

    cambio = membership.reconcile_with(PEER2)

    assert cambio is True
    assert PEER3 in {p["peer_id"] for p in ring.LOCAL.peers()}


def test_reconcile_all_recorre_todos_los_peers(red):
    """reconcile_all devuelve los peers desde los que se aprendió."""
    actualizados = membership.reconcile_all()

    assert PEER2 in actualizados
    assert PEER3 in {p["peer_id"] for p in ring.LOCAL.peers()}


def test_reconcile_no_sobrescribe_direcciones_conocidas(monkeypatch):
    """Si ya conozco a un peer, no cambio su dirección por la que diga otro."""
    construir_anillo(monkeypatch, {
        PEER1: ANILLO_COMPLETO[PEER1],
        PEER2: ANILLO_COMPLETO[PEER2],
    })

    direccion_local = "http://peer2:9002"
    direccion_remota = "http://peer2-falso:9999"

    class PeerMentiroso:
        def get(self, url, **kwargs):
            class Respuesta:
                status_code = 200

                def json(self):
                    return {
                        "ring_version": "abc",
                        "peers": [
                            {"peer_id": PEER1, "address": ANILLO_COMPLETO[PEER1]},
                            {"peer_id": PEER2, "address": direccion_remota},
                        ]
                    }

            return Respuesta()

    import httpx
    monkeypatch.setattr(httpx, "get", PeerMentiroso().get)

    membership.reconcile_with(PEER2)

    assert ring.address_of(PEER2) == direccion_local


def test_reconcile_endpoint(client, monkeypatch):
    """El endpoint POST /peers/reconcile expone la reconciliación."""
    construir_anillo(monkeypatch, {
        PEER1: ANILLO_COMPLETO[PEER1],
        PEER2: ANILLO_COMPLETO[PEER2],
    })

    class PeerRemotoSimple:
        def get(self, url, **kwargs):
            class Respuesta:
                status_code = 200

                def json(self):
                    return {
                        "ring_version": "abc",
                        "peers": [
                            {"peer_id": PEER1, "address": ANILLO_COMPLETO[PEER1]},
                            {"peer_id": PEER2, "address": ANILLO_COMPLETO[PEER2]},
                            {"peer_id": PEER3, "address": ANILLO_COMPLETO[PEER3]},
                        ]
                    }

            return Respuesta()

    import httpx
    monkeypatch.setattr(httpx, "get", PeerRemotoSimple().get)

    respuesta = client.post("/peers/reconcile")

    assert respuesta.status_code == 200
    assert PEER3 in {p["peer_id"] for p in respuesta.json()["peers"]}
    assert PEER2 in respuesta.json()["updated_from"]
