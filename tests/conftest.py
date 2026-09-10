from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import blocks, config, filesystem
from server.main import app


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Redirige el espacio de nombres del DFS a un directorio temporal.

    Devuelve la raiz del espacio de nombres, no la del peer: asi los tests del
    hito 1 siguen mirando `storage / "universidad"` sin cambiar una linea, y
    `storage.parent` sigue siendo el sitio de fuera de la jaula.
    """
    root = tmp_path / "storage" / "namespace"
    root.mkdir(parents=True)

    monkeypatch.setattr(filesystem, "NAMESPACE_ROOT", root)
    return root


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def arboles(tmp_path, monkeypatch):
    """Los dos arboles del peer, cada uno con su raiz sustituida.

    El espacio de nombres y los bloques son planos separados, y cada uno es su
    propia jaula: por eso hay que sustituir las dos raices, no una.
    """
    namespace = tmp_path / "storage" / "namespace"
    bloques = tmp_path / "storage" / "blocks"

    namespace.mkdir(parents=True)
    bloques.mkdir(parents=True)

    monkeypatch.setattr(filesystem, "NAMESPACE_ROOT", namespace)
    monkeypatch.setattr(blocks, "BLOCKS_ROOT", bloques)

    return SimpleNamespace(namespace=namespace, blocks=bloques)


@pytest.fixture
def limite_pequeno(monkeypatch):
    """Baja DFSHA_BLOCK_SIZE para no mover megas en los tests de tamano."""
    monkeypatch.setattr(config, "BLOCK_SIZE", 512)
    return 512
