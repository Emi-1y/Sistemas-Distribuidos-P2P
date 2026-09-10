from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import blocks, config, metadata
from server.main import app


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
