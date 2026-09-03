import pytest
from fastapi.testclient import TestClient

from server import filesystem
from server.main import app


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Redirige el DFS a un directorio temporal.

    resolve_path lee STORAGE_ROOT en cada llamada, asi que basta con
    sustituir la global para que ningun test escriba en server/storage.
    """
    root = tmp_path / "storage"
    root.mkdir()

    monkeypatch.setattr(filesystem, "STORAGE_ROOT", root)
    return root


@pytest.fixture
def client():
    return TestClient(app)
