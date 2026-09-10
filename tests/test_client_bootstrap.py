"""Bootstrap del cliente — SPEC-02, tarea 7.

Con SERVER_URL fijo el cliente tenia un punto unico de fallo y la topologia
dejaba de ser simetrica justo donde el usuario la ve.
"""

import pytest
import requests

from client import commands


class RespuestaFalsa:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.fixture(autouse=True)
def peer_url_limpio():
    """El peer elegido es estado de modulo: se restaura entre tests."""
    previo = commands.PEER_URL
    yield
    commands.PEER_URL = previo


def test_server_url_ya_no_existe():
    """Definicion de Done, punto 6."""
    assert not hasattr(commands, "SERVER_URL")


def test_el_bootstrap_ignora_el_peer_id_y_se_queda_con_la_url(monkeypatch):
    """Una sola variable para servidor y cliente: al cliente le basta la URL."""
    monkeypatch.setenv(
        "DFSHA_BOOTSTRAP",
        "peer1=http://peer1:9001,peer2=http://peer2:9002",
    )

    assert commands.bootstrap_addresses() == [
        "http://peer1:9001",
        "http://peer2:9002",
    ]


def test_el_bootstrap_tolera_espacios_vacios_y_barra_final(monkeypatch):
    monkeypatch.setenv(
        "DFSHA_BOOTSTRAP",
        " peer1=http://peer1:9001/ , , peer2=http://peer2:9002, ",
    )

    assert commands.bootstrap_addresses() == [
        "http://peer1:9001",
        "http://peer2:9002",
    ]


def test_sin_bootstrap_el_cliente_usa_el_peer_por_defecto(monkeypatch):
    """Sin configurar nada, el cliente se comporta como en el hito 1."""
    monkeypatch.delenv("DFSHA_BOOTSTRAP", raising=False)

    assert commands.bootstrap_addresses() == ["http://127.0.0.1:8000"]


def test_connect_se_queda_con_el_primer_peer_que_responde(monkeypatch):
    """No hay servidor: hay peers, y vale cualquiera que este vivo."""
    monkeypatch.setenv(
        "DFSHA_BOOTSTRAP",
        "peer1=http://caido:9001,peer2=http://vivo:9002",
    )

    consultados = []

    def get(url, **kwargs):
        consultados.append(url)

        if url.startswith("http://caido:9001"):
            raise requests.RequestException("no responde")

        return RespuestaFalsa(200)

    monkeypatch.setattr(commands.requests, "get", get)

    assert commands.connect() == "http://vivo:9002"
    assert commands.PEER_URL == "http://vivo:9002"
    assert consultados == [
        "http://caido:9001/health",
        "http://vivo:9002/health",
    ]


def test_connect_devuelve_none_si_no_responde_ninguno(monkeypatch):
    """El REPL no debe arrancar creyendo que hay con quien hablar."""
    monkeypatch.setenv("DFSHA_BOOTSTRAP", "peer1=http://caido:9001")

    def get(url, **kwargs):
        raise requests.RequestException("no responde")

    monkeypatch.setattr(commands.requests, "get", get)

    assert commands.connect() is None


def test_los_comandos_hablan_con_el_peer_conectado(monkeypatch, capsys):
    """El peer elegido en connect es el que usan las operaciones."""
    commands.PEER_URL = "http://vivo:9002"

    pedidas = []

    def get(url, **kwargs):
        pedidas.append(url)

        respuesta = RespuestaFalsa(200)
        respuesta.json = lambda: {"path": "/", "items": []}
        return respuesta

    monkeypatch.setattr(commands.requests, "get", get)

    commands.ls("/")

    assert pedidas == ["http://vivo:9002/files"]
