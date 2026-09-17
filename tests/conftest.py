from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import blocks, config, metadata, ring
from server.main import app


@pytest.fixture(autouse=True)
def anillo_del_proceso(monkeypatch):
    """Un anillo con un solo peer, sea cual sea el entorno del proceso.

    `ring.LOCAL` se construye al importar, con lo que diga `DFSHA_BOOTSTRAP`.
    En el anfitrion no hay ninguna variable puesta y sale un anillo de un peer;
    dentro del contenedor hay tres, y entonces los tests que hablan con «un
    peer» a traves del TestClient dejan de ser locales: el peer reenvia lo que
    no es suyo, y esas peticiones salen a peers de verdad que estan al otro
    lado de la red de compose. El ciclo del fallo de la SPEC-05 pasaba en el
    anfitrion y fallaba dentro una vez de cada tres, segun donde cayera un
    `file_id` que se sortea.

    Los tests que necesitan varios peers montan el suyo y este no les estorba.
    Lo que hace es quitar de en medio el entorno del proceso, que no es parte
    de lo que ninguno de ellos afirma.
    """
    solo = ring.Ring()
    solo.add_peer(config.PEER_ID, config.ADDRESS)

    monkeypatch.setattr(ring, "LOCAL", solo)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def arboles(tmp_path, monkeypatch):
    """Los dos arboles del peer, cada uno con su raiz sustituida.

    Los metadatos y los bloques son planos separados, y cada uno es su propia
    jaula: por eso hay que sustituir las dos raices, no una.
    """
    meta = tmp_path / "storage" / "metadata"
    bloques = tmp_path / "storage" / "blocks"

    meta.mkdir(parents=True)
    bloques.mkdir(parents=True)

    monkeypatch.setattr(metadata, "METADATA_ROOT", meta)
    monkeypatch.setattr(blocks, "BLOCKS_ROOT", bloques)

    return SimpleNamespace(metadata=meta, blocks=bloques)


@pytest.fixture
def limite_pequeno(monkeypatch):
    """Baja DFSHA_BLOCK_SIZE para no mover megas en los tests de tamano."""
    monkeypatch.setattr(config, "BLOCK_SIZE", 512)
    return 512
