"""Reparación de réplicas — Hito 3.

Estos tests verifican que el sistema detecta bloques con menos réplicas de las
esperadas y los copia a nuevos peers. No abren sockets de verdad: las llamadas
entre peers se simulan con monkeypatch sobre `httpx`.
"""

import hashlib

import pytest
import requests
from fastapi.testclient import TestClient

from server import blocks, config, membership, metadata, repair, ring
from server.main import app


PEER1 = "peer1"
PEER2 = "peer2"
PEER3 = "peer3"

ANILLO = {
    PEER1: "http://127.0.0.1:8001",
    PEER2: "http://127.0.0.1:8002",
    PEER3: "http://127.0.0.1:8003",
}

BLOQUE_SIZE = 512


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def crear_anillo(monkeypatch):
    """Anillo de tres peers para los tests de reparación."""
    nuevo = ring.Ring()

    for peer_id, address in ANILLO.items():
        nuevo.add_peer(peer_id, address)

    monkeypatch.setattr(ring, "LOCAL", nuevo)
    return nuevo


def crear_archivo(arboles, path, contenido, replicas=2):
    """Crea un archivo confirmado con el contenido y el factor de réplica.

    También escribe los bloques en el peer local (peer1) para que la
    reparación tenga al menos una fuente real. Para los tests forzamos que
    peer1 siempre figure entre los peers planificados.
    """
    directorio = str(metadata.parent_of(path))

    if directorio != "/":
        metadata.add_directory(directorio)
        metadata.ensure_bucket(directorio)

    entrada = metadata.allocate(path, len(contenido))
    confirmados = []

    for bloque in entrada["blocks"]:
        trozo = contenido[
            bloque["index"] * bloque["size"]:
            (bloque["index"] + 1) * bloque["size"]
        ]

        # Aseguramos que peer1 local esté entre los peers planificados.
        peers_plan = list(dict.fromkeys([PEER1] + bloque["peers"]))[:replicas]

        confirmados.append({
            "index": bloque["index"],
            "checksum": sha256(trozo),
            "peers": peers_plan,
        })

        # Escribimos el bloque en el peer local.
        destino = blocks.block_path(entrada["file_id"], bloque["index"])
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(trozo)

    return metadata.commit(path, entrada["file_id"], confirmados)


class RedSimulada:
    """Simula el almacén de bloques de los peers remotos.

    El peer local (peer1) usa el TestClient/app real; los otros dos peers
    viven en el diccionario `almacen`. Las funciones de `httpx` se
    monkeypatchean para hablar con este diccionario.
    """

    def __init__(self, cliente):
        self.cliente = cliente
        self.almacen = {}          # {(peer_id, file_id, index): bytes}
        self.bloques_locales = {}  # {(file_id, index): bytes}

    def guardar_local(self, file_id, index, datos):
        self.bloques_locales[(file_id, index)] = datos

    def guardar_remoto(self, peer_id, file_id, index, datos):
        self.almacen[(peer_id, file_id, index)] = datos

    def _peer_de(self, url):
        for peer_id, address in ANILLO.items():
            if url.startswith(address):
                return peer_id
        raise ValueError(f"URL desconocida: {url}")

    def _parsear_bloque(self, url):
        partes = url.split("/blocks/")[1].split("/")
        return partes[0], int(partes[1])

    def head(self, url, **kwargs):
        peer_id = self._peer_de(url)
        file_id, index = self._parsear_bloque(url)

        if peer_id == PEER1:
            status = 200 if blocks.block_exists(file_id, index) else 404
        else:
            status = 200 if (peer_id, file_id, index) in self.almacen else 404

        class Respuesta:
            status_code = status

        return Respuesta()

    def get(self, url, **kwargs):
        peer_id = self._peer_de(url)
        file_id, index = self._parsear_bloque(url)

        class Respuesta:
            def __init__(self, contenido, status):
                self.content = contenido
                self.status_code = status

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.RequestException("error")

        if peer_id == PEER1:
            datos, _ = blocks.read_block(file_id, index)
            return Respuesta(datos, 200)

        datos = self.almacen.get((peer_id, file_id, index), b"")
        return Respuesta(datos, 200 if datos else 404)

    def put(self, url, data=None, headers=None, **kwargs):
        peer_id = self._peer_de(url)
        file_id, index = self._parsear_bloque(url)

        class Respuesta:
            def __init__(self, status):
                self.status_code = status

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.RequestException("error")

        if peer_id == PEER1:
            # El peer local rechaza si ya existe (WORM).
            if blocks.block_exists(file_id, index):
                return Respuesta(409)

            checksum = (headers or {}).get("X-Block-Checksum", "")
            import server.blocks as blocks_mod

            blocks_mod.block_path(file_id, index).parent.mkdir(
                parents=True, exist_ok=True
            )
            blocks_mod.block_path(file_id, index).write_bytes(data)

            if sha256(data) != checksum:
                blocks_mod.block_path(file_id, index).unlink(missing_ok=True)
                return Respuesta(422)

            return Respuesta(201)

        self.almacen[(peer_id, file_id, index)] = data
        return Respuesta(201)


@pytest.fixture
def red(client, monkeypatch, arboles):
    """Red de tres peers: peer1 real, peer2/peer3 simulados."""
    crear_anillo(monkeypatch)
    monkeypatch.setattr(config, "REPLICAS", 2)
    monkeypatch.setattr(config, "BLOCK_SIZE", BLOQUE_SIZE)

    simulada = RedSimulada(client)

    import httpx

    monkeypatch.setattr(httpx, "head", simulada.head)
    monkeypatch.setattr(httpx, "get", simulada.get)
    monkeypatch.setattr(httpx, "put", simulada.put)

    return simulada


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _trozo(contenido, bloque):
    return contenido[
        bloque["index"] * bloque["size"]:
        (bloque["index"] + 1) * bloque["size"]
    ]


def test_repair_no_hace_nada_si_todas_las_replicas_estan(client, red, arboles):
    """Un archivo sano no se toca."""
    contenido = bytes(range(256)) * 4        # 1024 bytes -> 2 bloques
    entrada = crear_archivo(
        arboles, "/tarea.pdf", contenido, replicas=2
    )
    file_id = entrada["file_id"]

    # Ambas r�plicas existen: peer1 local y el otro peer planificado remoto.
    for bloque in entrada["blocks"]:
        for peer_id in bloque["peers"]:
            if peer_id != PEER1:
                red.guardar_remoto(
                    peer_id, file_id, bloque["index"], _trozo(contenido, bloque)
                )

    respuesta = client.post("/files/repair", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 200
    assert respuesta.json()["repaired"] is False
    assert respuesta.json()["changes"] == []


def test_repair_recupera_una_replica_perdida(client, red, arboles):
    """Si falta una r�plica, la reparaci�n la copia a un peer nuevo."""
    contenido = bytes(range(256)) * 4
    entrada = crear_archivo(
        arboles, "/tarea.pdf", contenido, replicas=2
    )
    file_id = entrada["file_id"]

    # Identificamos el peer planificado que no es peer1 y no lo poblamos.
    peer_perdido = None
    for bloque in entrada["blocks"]:
        for peer_id in bloque["peers"]:
            if peer_id != PEER1:
                peer_perdido = peer_id
                break
        if peer_perdido:
            break

    assert peer_perdido is not None

    respuesta = client.post("/files/repair", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 200
    data = respuesta.json()
    assert data["repaired"] is True
    assert len(data["changes"]) == len(entrada["blocks"])

    # Verificamos que los metadatos ahora tengan 2 peers distintos.
    ficha = client.get("/files/lookup", params={"path": "/tarea.pdf"}).json()
    for bloque in ficha["blocks"]:
        assert len(bloque["peers"]) == 2
        assert PEER1 in bloque["peers"]

    # Y que el nuevo peer realmente tenga los bloques.
    for bloque in ficha["blocks"]:
        nuevo_peer = [p for p in bloque["peers"] if p != PEER1][0]
        assert (nuevo_peer, file_id, bloque["index"]) in red.almacen


def test_repair_falla_si_no_hay_ninguna_replica_viva(client, red, arboles):
    """Si ning�n peer del plan tiene el bloque, no se puede reparar."""
    contenido = bytes(range(256)) * 4
    entrada = crear_archivo(
        arboles, "/tarea.pdf", contenido, replicas=2
    )

    # Borramos los bloques locales para que no haya ninguna r�plica viva.
    import shutil
    shutil.rmtree(arboles.blocks)
    arboles.blocks.mkdir(parents=True)

    respuesta = client.post("/files/repair", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 422
    detalle = respuesta.json()["detail"]
    assert "ninguna" in detalle and "viva" in detalle


def test_repair_verifica_checksum_al_copiar(client, red, arboles):
    """Si la fuente devuelve datos corruptos, la reparaci�n falla."""
    contenido = bytes(range(256)) * 4
    entrada = crear_archivo(
        arboles, "/tarea.pdf", contenido, replicas=2
    )
    file_id = entrada["file_id"]

    # Identificamos el peer remoto planificado y lo llenamos con datos corruptos.
    peer_corrupto = None
    for bloque in entrada["blocks"]:
        for peer_id in bloque["peers"]:
            if peer_id != PEER1:
                peer_corrupto = peer_id
                red.guardar_remoto(peer_corrupto, file_id, bloque["index"], b"corrupto")

    assert peer_corrupto is not None

    # Borramos los bloques locales para que el �nico peer vivo sea el corrupto.
    import shutil
    shutil.rmtree(arboles.blocks)
    arboles.blocks.mkdir(parents=True)

    respuesta = client.post("/files/repair", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 422
    assert "corrupto" in respuesta.json()["detail"]


def test_repair_all_repara_todo_lo_local(client, red, arboles):
    """repair_all escanea todas las entradas locales y repara las afectadas."""
    contenido = bytes(range(256)) * 4

    sano = crear_archivo(arboles, "/sano.pdf", contenido, replicas=2)
    afectado = crear_archivo(arboles, "/afectado.pdf", contenido, replicas=2)

    # Poblamos la r�plica remota del archivo sano para dejarlo completo.
    for bloque in sano["blocks"]:
        for peer_id in bloque["peers"]:
            if peer_id != PEER1:
                red.guardar_remoto(
                    peer_id, sano["file_id"], bloque["index"],
                    _trozo(contenido, bloque)
                )

    # El archivo afectado solo tiene la r�plica local.
    respuesta = client.post("/files/repair-all")

    assert respuesta.status_code == 200
    data = respuesta.json()
    assert data["scanned"] == 2
    assert data["repaired_files"] == 1
    assert data["total_changes"] == len(afectado["blocks"])


def test_repair_for_peer_solo_toca_archivos_afectados(client, red, arboles):
    """repair_for_peer('peer2') solo repara archivos que ten�an bloques en peer2."""
    contenido = bytes(range(256)) * 4

    afectado = crear_archivo(arboles, "/afectado.pdf", contenido, replicas=2)
    no_afectado = crear_archivo(arboles, "/no-afectado.pdf", contenido, replicas=2)

    # Forzamos peers planificados conocidos.
    for bloque in afectado["blocks"]:
        bloque["peers"] = [PEER1, PEER2]
    for bloque in no_afectado["blocks"]:
        bloque["peers"] = [PEER1, PEER3]

    # Reflejamos los cambios en metadatos.
    for bloque in afectado["blocks"]:
        metadata.update_block_peers(
            afectado["path"], afectado["file_id"], bloque["index"], bloque["peers"]
        )
    for bloque in no_afectado["blocks"]:
        metadata.update_block_peers(
            no_afectado["path"], no_afectado["file_id"], bloque["index"],
            bloque["peers"]
        )

    # Poblamos las r�plicas remotas para que no-afectado ya est� completo.
    for bloque in no_afectado["blocks"]:
        red.guardar_remoto(
            PEER3, no_afectado["file_id"], bloque["index"],
            _trozo(contenido, bloque)
        )

    resultado = repair.repair_for_peer(PEER2)

    assert resultado["fallen_peer"] == PEER2
    assert resultado["repaired_files"] == 1
    assert resultado["total_changes"] == len(afectado["blocks"])


def test_health_check_detecta_peer_caido(monkeypatch):
    """health_check_all marca como ca�do al peer que no responde."""
    crear_anillo(monkeypatch)

    respuestas = {"peer2": False, "peer3": True}

    def fake_health(peer_id):
        return respuestas.get(peer_id, True)

    monkeypatch.setattr(membership, "health_check_peer", fake_health)

    caidos = membership.health_check_all()

    assert caidos == [PEER2]
