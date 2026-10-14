"""Preparación para despliegue multi-VM — simetría y parametrización.

Estos tests comprueban que un peer se configura ENTERAMENTE por entorno y que
nada lo ata a nombres de Docker Compose, a `localhost` para hablar con otros,
ni a la identidad `peer1`. No despliegan AWS ni abren sockets: ejercen la
configuración y el compose de peer único como ficheros y como estado.

La propiedad que protegen es la simetría: tomar la misma imagen y el mismo
compose, cambiar solo el .env, y obtener peer1 / peer2 / peer3.
"""

import importlib
import os
from pathlib import Path

import pytest
import yaml


RAIZ = Path(__file__).resolve().parent.parent
COMPOSE_PEER = RAIZ / "docker-compose.peer.yml"
ENV_PEER_EXAMPLE = RAIZ / ".env.peer.example"


def recargar_config(monkeypatch, **entorno):
    """Recarga server.config con un entorno dado y devuelve el módulo.

    config.py lee el entorno al importar, así que para probar que una variable
    arbitraria se respeta hay que fijar el entorno y reimportar. Se restaura
    siempre al terminar el test.
    """
    for clave in list(os.environ):
        if clave.startswith("DFSHA_"):
            monkeypatch.delenv(clave, raising=False)

    for clave, valor in entorno.items():
        monkeypatch.setenv(clave, valor)

    import server.config as config
    return importlib.reload(config)


@pytest.fixture(autouse=True)
def _restaurar_config():
    """Deja server.config como estaba, para no contaminar otros tests."""
    yield
    import server.config as config
    importlib.reload(config)


# ---------------------------------------------------------------------------
# 1. Un peer no depende del nombre peer1
# ---------------------------------------------------------------------------

def test_el_peer_id_es_arbitrario(monkeypatch):
    """La identidad sale del entorno; `peer1` es solo el valor por defecto."""
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="superpeer-tokio",
        DFSHA_ADDRESS="https://10.20.30.40:9001",
    )

    assert config.PEER_ID == "superpeer-tokio"


def test_el_codigo_no_hardcodea_peer1(monkeypatch):
    """El anillo local se arma con el PEER_ID configurado, sea cual sea."""
    recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="nodo-x",
        DFSHA_ADDRESS="https://10.0.0.9:9001",
    )
    import server.ring as ring
    ring = importlib.reload(ring)

    ids = {p["peer_id"] for p in ring.LOCAL.peers()}
    assert "nodo-x" in ids
    assert "peer1" not in ids


# ---------------------------------------------------------------------------
# 2. DFSHA_ADDRESS arbitraria se anuncia correctamente
# ---------------------------------------------------------------------------

def test_la_direccion_anunciada_es_la_configurada(monkeypatch):
    """ADDRESS es la fuente de verdad de lo que el peer anuncia en /ring."""
    direccion = "https://10.10.0.11:9001"
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer1",
        DFSHA_ADDRESS=direccion,
    )
    import server.ring as ring
    ring = importlib.reload(ring)

    anunciada = {p["peer_id"]: p["address"] for p in ring.LOCAL.peers()}
    assert anunciada["peer1"] == direccion


def test_una_direccion_de_vpn_no_se_convierte_en_localhost(monkeypatch):
    """El código NO reescribe la dirección a localhost/127.0.0.1."""
    direccion = "https://10.10.0.13:9001"
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer3",
        DFSHA_ADDRESS=direccion,
    )
    import server.ring as ring
    ring = importlib.reload(ring)

    anunciada = {p["peer_id"]: p["address"] for p in ring.LOCAL.peers()}
    assert anunciada["peer3"] == direccion
    assert "localhost" not in anunciada["peer3"]
    assert "127.0.0.1" not in anunciada["peer3"]


# ---------------------------------------------------------------------------
# 3. Bootstrap acepta tres direcciones externas configurables
# ---------------------------------------------------------------------------

def test_bootstrap_acepta_tres_direcciones_de_vpn(monkeypatch):
    """El bootstrap es una lista configurable de peers externos."""
    bootstrap = (
        "peer1=https://10.10.0.11:9001,"
        "peer2=https://10.10.0.12:9001,"
        "peer3=https://10.10.0.13:9001"
    )
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer2",
        DFSHA_ADDRESS="https://10.10.0.12:9001",
        DFSHA_BOOTSTRAP=bootstrap,
    )

    pares = dict(config.BOOTSTRAP)
    assert pares == {
        "peer1": "https://10.10.0.11:9001",
        "peer2": "https://10.10.0.12:9001",
        "peer3": "https://10.10.0.13:9001",
    }


def test_el_anillo_descubre_los_tres_desde_el_bootstrap(monkeypatch):
    """Con el bootstrap puesto, el anillo local ya conoce a los tres, sin red."""
    bootstrap = (
        "peer1=https://10.10.0.11:9001,"
        "peer2=https://10.10.0.12:9001,"
        "peer3=https://10.10.0.13:9001"
    )
    recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer2",
        DFSHA_ADDRESS="https://10.10.0.12:9001",
        DFSHA_BOOTSTRAP=bootstrap,
    )
    import server.ring as ring
    ring = importlib.reload(ring)

    ids = {p["peer_id"] for p in ring.LOCAL.peers()}
    assert ids == {"peer1", "peer2", "peer3"}


# ---------------------------------------------------------------------------
# 5. TLS sigue siendo fail-closed
# ---------------------------------------------------------------------------

def test_tls_fail_closed_sigue_vigente(monkeypatch):
    """DFSHA_REQUIRE_TLS sin material TLS aborta el arranque."""
    recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer1",
        DFSHA_ADDRESS="https://10.10.0.11:9001",
        DFSHA_REQUIRE_TLS="true",
        DFSHA_REQUIRE_AUTH="true",
        DFSHA_PEER_TOKEN="x",
        DFSHA_USERS="alice=t",
        DFSHA_MASTER_KEY="a" * 64,
        DFSHA_CAP_SECRET="c",
    )
    import server.security as security
    security = importlib.reload(security)

    with pytest.raises(security.ConfiguracionInsegura):
        security.verify_startup_config()


def test_el_codigo_no_usa_verify_false(monkeypatch):
    """httpx_verify nunca devuelve False: la verificación TLS no se desactiva."""
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer1",
        DFSHA_ADDRESS="https://10.10.0.11:9001",
        DFSHA_TLS_CA="/certs/ca.crt",
    )
    import server.security as security
    security = importlib.reload(security)

    assert security.httpx_verify() == "/certs/ca.crt"

    # Y sin CA, el valor es True (verificación estándar), nunca False.
    config2 = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer1",
        DFSHA_ADDRESS="https://10.10.0.11:9001",
    )
    security2 = importlib.reload(security)
    assert security2.httpx_verify() is True


# ---------------------------------------------------------------------------
# 7. La misma config base inicializa peers con IDs distintos (simetría)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "peer_id, address",
    [
        ("peer1", "https://10.10.0.11:9001"),
        ("peer2", "https://10.10.0.12:9001"),
        ("peer3", "https://10.10.0.13:9001"),
    ],
)
def test_la_misma_base_inicializa_cualquier_peer(monkeypatch, peer_id, address):
    """Cambiar solo PEER_ID/ADDRESS basta para ser cualquiera de los tres."""
    bootstrap = (
        "peer1=https://10.10.0.11:9001,"
        "peer2=https://10.10.0.12:9001,"
        "peer3=https://10.10.0.13:9001"
    )
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID=peer_id,
        DFSHA_ADDRESS=address,
        DFSHA_BOOTSTRAP=bootstrap,
    )
    import server.ring as ring
    ring = importlib.reload(ring)

    assert config.PEER_ID == peer_id
    assert config.ADDRESS == address
    # Los tres ven el mismo anillo: misma versión = mismo conjunto de miembros.
    assert {p["peer_id"] for p in ring.LOCAL.peers()} == {"peer1", "peer2", "peer3"}


# ---------------------------------------------------------------------------
# 8. El almacenamiento usa el path configurado
# ---------------------------------------------------------------------------

def test_el_storage_root_es_el_configurado(monkeypatch, tmp_path):
    """STORAGE_ROOT sale del entorno; no hay path fijo compartido."""
    destino = tmp_path / "datos-de-esta-vm"
    config = recargar_config(
        monkeypatch,
        DFSHA_PEER_ID="peer1",
        DFSHA_ADDRESS="https://10.10.0.11:9001",
        DFSHA_STORAGE_ROOT=str(destino),
    )

    assert config.STORAGE_ROOT == destino.resolve()


# ---------------------------------------------------------------------------
# El compose de peer único es genérico (sin peer1/peer2/peer3)
# ---------------------------------------------------------------------------

def test_el_compose_de_peer_tiene_un_solo_servicio_generico():
    """docker-compose.peer.yml declara un único servicio `peer`, sin numerar."""
    assert COMPOSE_PEER.exists(), "falta docker-compose.peer.yml"

    doc = yaml.safe_load(COMPOSE_PEER.read_text(encoding="utf-8"))
    servicios = doc["services"]

    assert list(servicios) == ["peer"], servicios
    assert "peer1" not in servicios
    assert "peer2" not in servicios
    assert "peer3" not in servicios


def test_el_compose_de_peer_no_fija_identidad_ni_red_compartida():
    """No hardcodea PEER_ID/ADDRESS ni ata el peer a una red Docker compartida."""
    texto = COMPOSE_PEER.read_text(encoding="utf-8")
    doc = yaml.safe_load(texto)
    peer = doc["services"]["peer"]

    # La identidad viene del .env (env_file), no fijada en el YAML.
    assert "env_file" in peer
    entorno = peer.get("environment", {}) or {}
    assert "DFSHA_PEER_ID" not in entorno
    assert "DFSHA_ADDRESS" not in entorno

    # No declara redes nombradas compartidas entre peers.
    assert "networks" not in doc
    assert "networks" not in peer


def test_el_compose_de_peer_persiste_en_un_path_configurable():
    """El volumen es un bind mount a un path del host configurable por variable."""
    texto = COMPOSE_PEER.read_text(encoding="utf-8")

    assert "DFSHA_DATA_DIR" in texto
    assert "DFSHA_STORAGE_ROOT" in texto
    # No usa un volumen con nombre compartido entre peers.
    doc = yaml.safe_load(texto)
    assert "volumes" not in doc  # sin bloque top-level de volúmenes con nombre


def test_el_compose_de_peer_usa_la_misma_imagen_parametrizable():
    """Una sola imagen, parametrizable, con comando TLS-aware."""
    doc = yaml.safe_load(COMPOSE_PEER.read_text(encoding="utf-8"))
    peer = doc["services"]["peer"]

    assert "dfsha-peer" in str(peer["image"])
    assert "python -m server" in " ".join(
        peer["command"] if isinstance(peer["command"], list) else [peer["command"]]
    )


# ---------------------------------------------------------------------------
# La plantilla de .env documenta la configuración sin secretos reales
# ---------------------------------------------------------------------------

def test_la_plantilla_env_peer_existe_y_cubre_las_variables_clave():
    assert ENV_PEER_EXAMPLE.exists(), "falta .env.peer.example"

    texto = ENV_PEER_EXAMPLE.read_text(encoding="utf-8")

    for variable in (
        "DFSHA_PEER_ID", "DFSHA_ADDRESS", "DFSHA_BOOTSTRAP", "DFSHA_STORAGE_ROOT",
        "DFSHA_REQUIRE_AUTH", "DFSHA_REQUIRE_TLS",
        "DFSHA_PEER_TOKEN", "DFSHA_USERS", "DFSHA_MASTER_KEY", "DFSHA_CAP_SECRET",
        "DFSHA_TLS_CERT", "DFSHA_TLS_KEY", "DFSHA_TLS_CA",
        "DFSHA_DATA_DIR", "DFSHA_CERTS_DIR",
    ):
        assert variable in texto, f"la plantilla no documenta {variable}"


def test_la_plantilla_env_peer_no_trae_secretos_reales():
    """Placeholders, no claves de verdad: nada que parezca un hex de 64 reales
    salvo el placeholder explícito."""
    texto = ENV_PEER_EXAMPLE.read_text(encoding="utf-8")

    # Los valores de ejemplo son claramente placeholders.
    assert "pon-aqui" in texto

    # La clave de la CA jamás debe asignarse a una variable del .env (sí puede
    # mencionarse en un comentario advirtiendo de que no se copia a las VMs).
    for linea in texto.splitlines():
        limpia = linea.strip()
        if limpia.startswith("#") or "=" not in limpia:
            continue
        _, _, valor = limpia.partition("=")
        assert "ca.key" not in valor, f"ca.key no debe ser un valor del .env: {linea}"
