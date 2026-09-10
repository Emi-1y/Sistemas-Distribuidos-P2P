"""Configuracion por entorno — SPEC-02, paso 4 del plan TDD.

Tres peers en la misma maquina necesitan raices de datos distintas, y el
docker-compose de la SPEC-06 no puede editar codigo: de ahi este modulo.
"""

import importlib

import pytest

from server import config, filesystem


VARIABLES = (
    "DFSHA_PEER_ID",
    "DFSHA_ADDRESS",
    "DFSHA_BOOTSTRAP",
    "DFSHA_STORAGE_ROOT",
    "DFSHA_BLOCK_SIZE",
    "DFSHA_VNODES",
)


@pytest.fixture
def recargar(monkeypatch):
    """Relee config con el entorno indicado y lo deja limpio al terminar."""
    def _recargar(**variables):
        for nombre in VARIABLES:
            monkeypatch.delenv(nombre, raising=False)

        for nombre, valor in variables.items():
            monkeypatch.setenv(nombre, valor)

        return importlib.reload(config)

    yield _recargar

    for nombre in VARIABLES:
        monkeypatch.delenv(nombre, raising=False)

    importlib.reload(config)


def test_sin_configurar_nada_el_peer_se_comporta_como_el_monolito(recargar):
    """Los valores por defecto reproducen el sistema del hito 1."""
    recargado = recargar()

    assert recargado.PEER_ID == "peer1"
    assert recargado.ADDRESS == "http://127.0.0.1:8000"
    assert recargado.BOOTSTRAP == []
    assert recargado.BLOCK_SIZE == 4 * 1024 * 1024
    assert recargado.VNODES == 128
    assert recargado.STORAGE_ROOT.name == "storage"


def test_la_raiz_de_datos_sale_del_entorno(recargar, tmp_path):
    """Sin esto, tres peers en la misma maquina se pisan los bloques."""
    destino = tmp_path / "peer2"
    recargado = recargar(DFSHA_STORAGE_ROOT=str(destino))

    assert recargado.STORAGE_ROOT == destino.resolve()


def test_filesystem_toma_su_raiz_de_config():
    """resolve_path no cambia: solo cambia de donde viene STORAGE_ROOT.

    Es lo que mantiene verdes los 14 tests del hito 1 sin tocarlos.
    """
    assert filesystem.STORAGE_ROOT == config.STORAGE_ROOT


def test_el_bootstrap_se_lee_como_pares_de_identidad_y_direccion(recargar):
    """Con pares, un peer coloca a los demas en el anillo sin hablar con ellos."""
    recargado = recargar(
        DFSHA_BOOTSTRAP="peer1=http://peer1:9001,peer2=http://peer2:9002"
    )

    assert recargado.BOOTSTRAP == [
        ("peer1", "http://peer1:9001"),
        ("peer2", "http://peer2:9002"),
    ]


def test_el_bootstrap_tolera_espacios_entradas_vacias_y_coma_final(recargar):
    """Es una variable escrita a mano dentro de un docker-compose."""
    recargado = recargar(
        DFSHA_BOOTSTRAP=" peer1=http://peer1:9001 , , peer2=http://peer2:9002/, "
    )

    assert recargado.BOOTSTRAP == [
        ("peer1", "http://peer1:9001"),
        ("peer2", "http://peer2:9002"),
    ]


@pytest.mark.parametrize(
    "escrita, normalizada",
    [
        ("http://peer2:9002", "http://peer2:9002"),
        ("http://peer2:9002/", "http://peer2:9002"),
        ("https://peer2:9002///", "https://peer2:9002"),
    ],
)
def test_normalize_address_quita_la_barra_final(escrita, normalizada):
    """Sin normalizar, un caracter deja un peer fuera del anillo con un 409."""
    assert config.normalize_address(escrita) == normalizada


@pytest.mark.parametrize(
    "direccion",
    ["http://peer2:9002", "https://peer2:9002", "http://127.0.0.1:8000/"],
)
def test_is_valid_address_acepta_urls_http(direccion):
    assert config.is_valid_address(direccion) is True


@pytest.mark.parametrize(
    "direccion",
    ["ftp://peer2:9002", "peer2:9002", "http://", "", "   "],
)
def test_is_valid_address_rechaza_lo_que_no_es_una_url_http(direccion):
    """Una direccion invalida en el anillo falla mucho despues y lejos de aqui."""
    assert config.is_valid_address(direccion) is False
