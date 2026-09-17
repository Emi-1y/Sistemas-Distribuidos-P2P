"""`send` y `receive` distribuidos — SPEC-05, pasos 4 a 9 del plan TDD.

Fase 2: aqui si hay red, pero no hay sockets. Casi todo se prueba contra un
peer falso que registra a quien se llamo, con que y en que orden; el ultimo
test, el del ciclo del fallo, habla con un peer de verdad a traves del
TestClient, porque la mitad de lo que afirma es del servidor.
"""

import hashlib
import threading
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

from client import commands, transfer
from server.main import app


PEER_URL = "http://peer1:9001"

ANILLO = {
    "peer1": "http://peer1:9001",
    "peer2": "http://peer2:9002",
    "peer3": "http://peer3:9003",
}

BLOQUE = 100
FILE_ID = "5f3e0000-0000-4000-8000-000000000001"

# El reparto por defecto evita al peer conectado: es lo que hace comprobable
# que los bytes no pasan por el.
FUERA_DEL_CONECTADO = ["peer2", "peer3"]


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def trozo(contenido: bytes, bloque: dict, block_size: int = BLOQUE) -> bytes:
    inicio = bloque["index"] * block_size

    return contenido[inicio:inicio + bloque["size"]]


def plan_pendiente(path, size, reparto=None, block_size=BLOQUE):
    """La entrada `pending` que devolveria `allocate`."""
    if reparto is None:
        def reparto(index):
            return [FUERA_DEL_CONECTADO[index % len(FUERA_DEL_CONECTADO)]]

    bloques = []
    restante = size
    index = 0

    while restante > 0:
        medida = min(block_size, restante)

        bloques.append({
            "index": index,
            "size": medida,
            "checksum": None,
            "peers": reparto(index),
        })

        restante -= medida
        index += 1

    return {
        "path": path,
        "file_id": FILE_ID,
        "size": size,
        "block_size": block_size,
        "state": "pending",
        "created_at": "2026-09-13T00:00:00Z",
        "blocks": bloques,
    }


def entrada_confirmada(path, contenido, reparto=None, block_size=BLOQUE):
    """La entrada que devolveria `lookup`, con sus checksums rellenados."""
    entrada = plan_pendiente(path, len(contenido), reparto, block_size)
    entrada["state"] = "committed"

    for bloque in entrada["blocks"]:
        bloque["checksum"] = sha256(trozo(contenido, bloque, block_size))

    return entrada


def sembrar(peer, entrada, contenido, block_size=BLOQUE):
    """Deja cada bloque en sus peers, como si `send` los hubiera subido."""
    for bloque in entrada["blocks"]:
        datos = trozo(contenido, bloque, block_size)

        for peer_id in bloque["peers"]:
            peer.almacen[(peer_id, bloque["index"])] = (datos, bloque["checksum"])


def etiquetas(peer):
    """Las llamadas en orden, con la URL reducida a lo que importa."""
    marcas = []

    for metodo, url in peer.llamadas:
        ruta = urlsplit(url).path
        marcas.append(
            f"{metodo} bloque" if ruta.startswith("/blocks/") else f"{metodo} {ruta}"
        )

    return marcas


def puestas(peer):
    return [url for metodo, url in peer.llamadas if metodo == "PUT"]


def bajadas(peer):
    return [url for metodo, url in peer.llamadas if "/blocks/" in url and metodo == "GET"]


class Respuesta:
    def __init__(self, status_code, cuerpo=None, contenido=b"", headers=None):
        self.status_code = status_code
        self.content = contenido
        self.headers = headers or {}
        self._cuerpo = cuerpo

    def json(self):
        if self._cuerpo is None:
            raise ValueError("sin cuerpo")

        return self._cuerpo


class PeerFalso:
    """El anillo entero, sin un socket.

    Registra cada llamada como `(metodo, url)` en el orden en que le llegan: la
    secuencia es parte del contrato de `send`, asi que hay que poder mirarla.
    """

    # `commands` hace `except requests.RequestException`, y el doble sustituye
    # al modulo entero: la excepcion tiene que seguir siendo la de verdad.
    RequestException = requests.RequestException

    def __init__(self):
        self.anillo = dict(ANILLO)
        self.llamadas = []
        self.plan = None
        self.entrada = None
        self.commit = None
        self.asignacion = None
        self.subidos = {}
        self.almacen = {}
        self.error_allocate = None
        self.error_lookup = None
        self.error_commit = None
        self.put_falla = set()
        self.caidos = set()
        self.al_asignar = None
        self.barrera = None
        self.en_serie = False

    # -- utilidades internas -------------------------------------------------

    def _peer_de(self, url):
        for peer_id, direccion in self.anillo.items():
            if url.startswith(f"{direccion}/blocks/"):
                return peer_id

        raise AssertionError(f"ningun peer del anillo tiene la direccion {url}")

    def _indice(self, url):
        return int(urlsplit(url).path.rsplit("/", 1)[1])

    def _compasar(self):
        """Si el test puso una barrera, aqui se ve si de verdad van a la vez."""
        if self.barrera is None:
            return

        try:
            self.barrera.wait(timeout=5)
        except threading.BrokenBarrierError:
            self.en_serie = True

    # -- la cara de `requests` -----------------------------------------------

    def get(self, url, params=None, **kwargs):
        self.llamadas.append(("GET", url))

        if url.endswith("/ring"):
            return Respuesta(200, {
                "ring_version": "a3f19c04",
                "peers": [
                    {"peer_id": peer_id, "address": direccion}
                    for peer_id, direccion in sorted(self.anillo.items())
                ],
            })

        if url.endswith("/files/lookup"):
            if self.error_lookup:
                codigo, detalle = self.error_lookup
                return Respuesta(codigo, {"detail": detalle})

            return Respuesta(200, self.entrada)

        if "/blocks/" in url:
            return self._servir_bloque(url)

        raise AssertionError(f"nadie deberia pedir {url}")

    def put(self, url, data=None, headers=None, **kwargs):
        self.llamadas.append(("PUT", url))

        peer_id = self._peer_de(url)
        index = self._indice(url)

        if peer_id in self.caidos:
            raise requests.RequestException("el peer no responde")

        self._compasar()

        if index in self.put_falla:
            return Respuesta(500, {"detail": "el peer revento a mitad"})

        self.subidos[(peer_id, index)] = (data, (headers or {}).get("X-Block-Checksum"))

        return Respuesta(201, {
            "file_id": FILE_ID,
            "index": index,
            "size": len(data),
            "checksum": (headers or {}).get("X-Block-Checksum"),
        })

    def post(self, url, json=None, **kwargs):
        self.llamadas.append(("POST", url))

        if url.endswith("/files/allocate"):
            self.asignacion = json

            if self.al_asignar is not None:
                self.al_asignar()

            if self.error_allocate:
                codigo, detalle = self.error_allocate
                return Respuesta(codigo, {"detail": detalle})

            return Respuesta(200, self.plan)

        if url.endswith("/files/commit"):
            self.commit = json

            if self.error_commit:
                codigo, detalle = self.error_commit
                return Respuesta(codigo, {"detail": detalle})

            return Respuesta(200, dict(self.plan, state="committed"))

        raise AssertionError(f"nadie deberia llamar a {url}")

    def _servir_bloque(self, url):
        peer_id = self._peer_de(url)
        index = self._indice(url)

        if peer_id in self.caidos:
            raise requests.RequestException("el peer no responde")

        self._compasar()

        if (peer_id, index) not in self.almacen:
            return Respuesta(404, {"detail": "El bloque no existe"})

        datos, cabecera = self.almacen[(peer_id, index)]

        return Respuesta(
            200, contenido=datos, headers={"X-Block-Checksum": cabecera}
        )


class PeerReal:
    """Traduce las llamadas de `requests` al TestClient de FastAPI.

    Es el unico doble que habla con un peer de verdad, y solo lo usa el ciclo
    del fallo: lo que ese test afirma sobre `ls`, el `409` y el `rm` es del
    servidor, y contra un peer falso seria la maqueta confirmandose a si misma.
    """

    RequestException = requests.RequestException

    def __init__(self, cliente):
        self._cliente = cliente
        self.llamadas = []
        self.put_falla = set()

    def _ruta(self, url):
        return urlsplit(url).path

    def get(self, url, params=None, **kwargs):
        self.llamadas.append(("GET", self._ruta(url)))

        return self._cliente.get(self._ruta(url), params=params)

    def post(self, url, json=None, **kwargs):
        self.llamadas.append(("POST", self._ruta(url)))

        return self._cliente.post(self._ruta(url), json=json)

    def put(self, url, data=None, headers=None, **kwargs):
        ruta = self._ruta(url)
        self.llamadas.append(("PUT", ruta))

        if int(ruta.rsplit("/", 1)[1]) in self.put_falla:
            return Respuesta(500, {"detail": "el peer revento a mitad"})

        return self._cliente.put(ruta, content=data, headers=headers)

    def delete(self, url, params=None, **kwargs):
        self.llamadas.append(("DELETE", self._ruta(url)))

        return self._cliente.delete(self._ruta(url), params=params)


@pytest.fixture
def peer(monkeypatch):
    doble = PeerFalso()

    monkeypatch.setattr(commands, "requests", doble)
    monkeypatch.setattr(commands, "PEER_URL", PEER_URL)

    return doble


@pytest.fixture
def local(tmp_path, monkeypatch):
    """El directorio de trabajo del usuario: de ahi sale `send` y ahi cae
    `receive`."""
    monkeypatch.chdir(tmp_path)

    return tmp_path


# ---------------------------------------------------------------------------
# Paso 4 — `send`, el camino feliz (AC-07, AC-08, AC-09, AC-10)
# ---------------------------------------------------------------------------

def test_send_recorre_los_tres_tiempos_en_orden(peer, local):
    """AC-07. El orden es parte del contrato: confirmar antes de que esten
    todos los bloques daria por bueno un archivo que no esta entero."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))

    commands.send("/", "tarea.bin")

    marcas = etiquetas(peer)

    assert marcas[0] == "GET /ring"
    assert marcas[1] == "POST /files/allocate"
    assert marcas[-1] == "POST /files/commit"
    assert marcas[2:-1] == ["PUT bloque"] * 3


def test_los_bloques_van_directos_al_peer_de_datos(peer, local):
    """AC-08. La decision que evita el cuello de botella: el peer conectado ve
    tres peticiones de metadatos y ni un byte de datos."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))

    commands.send("/", "tarea.bin")

    esperadas = {
        f"{ANILLO[bloque['peers'][0]]}/blocks/{FILE_ID}/{bloque['index']}"
        for bloque in peer.plan["blocks"]
    }

    assert set(puestas(peer)) == esperadas
    assert [url for _, url in peer.llamadas if url.startswith(f"{PEER_URL}/blocks")] == []


def test_cada_bloque_llega_con_sus_bytes_y_su_checksum(peer, local):
    """AC-08. Lo que se sube es el trozo que toca, no el archivo entero."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))

    commands.send("/", "tarea.bin")

    for bloque in peer.plan["blocks"]:
        datos, cabecera = peer.subidos[(bloque["peers"][0], bloque["index"])]

        assert datos == trozo(contenido, bloque)
        assert cabecera == sha256(datos)


def test_el_commit_reporta_los_checksums_calculados_y_los_peers_que_respondieron(
    peer, local
):
    """AC-09. `peers` es donde quedo el bloque de verdad, no el plan repetido:
    el plan era una intencion y el unico que conoce el hecho es el cliente."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))

    commands.send("/", "tarea.bin")

    assert peer.commit["path"] == "/tarea.bin"
    assert peer.commit["file_id"] == FILE_ID
    assert peer.commit["blocks"] == [
        {
            "index": bloque["index"],
            "checksum": sha256(trozo(contenido, bloque)),
            "peers": bloque["peers"],
        }
        for bloque in peer.plan["blocks"]
    ]


def test_los_put_se_lanzan_en_paralelo(peer, local):
    """AC-10. Con una barrera: si `send` subiera en serie, nunca coincidirian
    tres bloques dentro y el test lo dice."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))
    peer.barrera = threading.Barrier(3)

    commands.send("/", "tarea.bin")

    assert peer.en_serie is False
    assert peer.commit is not None


# ---------------------------------------------------------------------------
# Paso 5 — `send`, los rechazos (AC-11, AC-12, AC-13, AC-14)
# ---------------------------------------------------------------------------

def test_un_put_que_falla_cancela_el_commit(peer, local, capsys):
    """AC-11. Sin commit el archivo no existe para nadie, que es exactamente lo
    que se quiere de una subida a medias."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/tarea.bin", len(contenido))
    peer.put_falla = {1}

    commands.send("/", "tarea.bin")

    assert peer.commit is None
    assert "Error" in capsys.readouterr().out


def test_un_allocate_que_falla_no_sube_ni_un_bloque(peer, local, capsys):
    """AC-12 y test limite. El `409` de la colision lo decide el dueno de la
    entrada, y el cliente lo repite tal cual."""
    (local / "tarea.bin").write_bytes(b"x" * 250)
    peer.plan = plan_pendiente("/tarea.bin", 250)
    peer.error_allocate = (409, "El archivo ya existe")

    commands.send("/", "tarea.bin")

    salida = capsys.readouterr().out

    assert "409" in salida
    assert "El archivo ya existe" in salida
    assert puestas(peer) == []
    assert peer.commit is None


def test_sin_archivo_local_no_se_emite_ninguna_peticion(peer, local, capsys):
    """AC-13. La comprobacion del cliente es comodidad: evita una peticion
    inutil y un `allocate` que dejaria una entrada pendiente para nada."""
    commands.send("/", "no-esta.bin")

    assert peer.llamadas == []
    assert "Error" in capsys.readouterr().out


def test_un_nombre_en_blanco_no_emite_ninguna_peticion(peer, local, capsys):
    """AC-13. El otro lado del mismo paso previo."""
    commands.send("/", "   ")

    assert peer.llamadas == []
    assert "Error" in capsys.readouterr().out


def test_un_archivo_vacio_se_confirma_con_la_lista_vacia(peer, local):
    """AC-14 y test limite. Un archivo de cero bytes es valido y no tiene ni un
    bloque: cero PUT es lo correcto, no un fallo."""
    (local / "vacio.bin").write_bytes(b"")
    peer.plan = plan_pendiente("/vacio.bin", 0)

    commands.send("/", "vacio.bin")

    assert peer.plan["blocks"] == []
    assert puestas(peer) == []
    assert peer.commit["blocks"] == []


def test_si_el_anillo_no_conoce_un_peer_del_plan_no_se_sube_nada(peer, local, capsys):
    """Test limite. El anillo cambio entre el `allocate` y el `PUT`: escribir a
    ciegas dejaria bloques que nadie encontrara."""
    (local / "tarea.bin").write_bytes(bytes(range(250)))
    peer.plan = plan_pendiente("/tarea.bin", 250, reparto=lambda i: ["peer9"])

    commands.send("/", "tarea.bin")

    assert puestas(peer) == []
    assert peer.commit is None
    assert "Error" in capsys.readouterr().out


def test_si_el_archivo_se_encoge_entre_el_allocate_y_la_lectura_no_hay_commit(
    peer, local, capsys
):
    """Test limite. Confirmarlo guardaria unos metadatos que mienten."""
    ruta = local / "tarea.bin"
    ruta.write_bytes(bytes(range(250)))
    peer.plan = plan_pendiente("/tarea.bin", 250)
    peer.al_asignar = lambda: ruta.write_bytes(b"corto")

    commands.send("/", "tarea.bin")

    assert peer.commit is None
    assert "Error" in capsys.readouterr().out


def test_mas_bloques_que_el_tope_suben_todos(peer, local):
    """Test limite. El tope reparte en tandas; no descarta."""
    cuantos = transfer.BLOQUES_EN_PARALELO + 4
    contenido = b"".join(bytes([i]) * BLOQUE for i in range(cuantos))
    (local / "grande.bin").write_bytes(contenido)
    peer.plan = plan_pendiente("/grande.bin", len(contenido))

    commands.send("/", "grande.bin")

    assert len(puestas(peer)) == cuantos
    assert len(peer.commit["blocks"]) == cuantos


# ---------------------------------------------------------------------------
# Paso 6 y 7 — `receive` (AC-16 a AC-19)
# ---------------------------------------------------------------------------

def test_receive_baja_reensambla_y_escribe_el_archivo(peer, local):
    """AC-16. El de ida y vuelta: lo que se escribe es identico byte a byte."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada("/tarea.bin", contenido)
    sembrar(peer, peer.entrada, contenido)
    peer.barrera = threading.Barrier(3)

    commands.receive("/", "tarea.bin")

    assert (local / "tarea.bin").read_bytes() == contenido
    assert peer.en_serie is False
    assert len(bajadas(peer)) == 3


def test_receive_pide_cada_bloque_al_peer_que_lo_tiene(peer, local):
    """AC-16. Tambien al bajar los bytes van directos, sin pasar por el peer
    conectado."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada("/tarea.bin", contenido)
    sembrar(peer, peer.entrada, contenido)

    commands.receive("/", "tarea.bin")

    esperadas = {
        f"{ANILLO[bloque['peers'][0]]}/blocks/{FILE_ID}/{bloque['index']}"
        for bloque in peer.entrada["blocks"]
    }

    assert set(bajadas(peer)) == esperadas


def test_un_archivo_vacio_baja_como_archivo_local_de_cero_bytes(peer, local):
    """Test limite. Sin bloques que pedir, pero con archivo que crear."""
    peer.entrada = entrada_confirmada("/vacio.bin", b"")

    commands.receive("/", "vacio.bin")

    assert (local / "vacio.bin").read_bytes() == b""
    assert bajadas(peer) == []


def test_un_bloque_corrupto_no_deja_archivo_local(peer, local, capsys):
    """AC-17 y test limite. La cabecera del peer dice lo que la entrada, y aun
    asi se rechaza: creer la cabecera seria no verificar nada."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada("/tarea.bin", contenido)
    sembrar(peer, peer.entrada, contenido)

    bloque = peer.entrada["blocks"][1]
    peer.almacen[(bloque["peers"][0], 1)] = (b"z" * bloque["size"], bloque["checksum"])

    commands.receive("/", "tarea.bin")

    assert not (local / "tarea.bin").exists()
    assert "Error" in capsys.readouterr().out


def test_una_cabecera_que_miente_con_el_cuerpo_correcto_se_acepta(peer, local):
    """Test limite. La autoridad es la entrada, no lo que el peer anoto."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada("/tarea.bin", contenido)
    sembrar(peer, peer.entrada, contenido)

    bloque = peer.entrada["blocks"][2]
    peer.almacen[(bloque["peers"][0], 2)] = (
        trozo(contenido, bloque), "0" * 64
    )

    commands.receive("/", "tarea.bin")

    assert (local / "tarea.bin").read_bytes() == contenido


def test_un_lookup_que_falla_no_pide_bloques_ni_crea_nada(peer, local, capsys):
    """AC-18 y test limite. Cubre los dos casos a la vez, porque el servidor
    responde lo mismo a una ruta que no existe y a una que es un directorio:
    `lookup` es de archivos, y que el directorio de tambien 404 lo prueba
    `test_metadata.py`. Aqui lo que se afirma es que el cliente no lo
    reinterpreta y no crea nada."""
    detalle = "El archivo no existe"
    peer.error_lookup = (404, detalle)

    commands.receive("/", "tarea.bin")

    salida = capsys.readouterr().out

    assert "404" in salida
    assert detalle in salida
    assert bajadas(peer) == []
    assert not (local / "tarea.bin").exists()


def test_no_sobrescribe_un_archivo_local_existente(peer, local, capsys):
    """AC-19. Misma postura que el `409` del DFS: no se destruye nada que
    nadie pidio destruir."""
    (local / "tarea.bin").write_bytes(b"lo mio")

    commands.receive("/", "tarea.bin")

    assert (local / "tarea.bin").read_bytes() == b"lo mio"
    assert peer.llamadas == []
    assert "Error" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Paso 8 — el factor de replica (AC-15, AC-20)
# ---------------------------------------------------------------------------

def test_se_sube_a_todos_los_peers_del_bloque(peer, local):
    """AC-15. Con factor 3 el commit exige tres peers distintos; un cliente que
    subiera solo al primero provocaria un 422 que nadie sabria leer."""
    contenido = bytes(range(250))
    (local / "tarea.bin").write_bytes(contenido)
    peer.plan = plan_pendiente(
        "/tarea.bin", len(contenido), reparto=lambda i: ["peer2", "peer3"]
    )

    commands.send("/", "tarea.bin")

    assert len(puestas(peer)) == 6

    for bloque in peer.plan["blocks"]:
        assert peer.subidos[("peer2", bloque["index"])][0] == trozo(contenido, bloque)
        assert peer.subidos[("peer3", bloque["index"])][0] == trozo(contenido, bloque)

    assert all(
        confirmado["peers"] == ["peer2", "peer3"]
        for confirmado in peer.commit["blocks"]
    )


def test_si_el_primer_peer_no_responde_se_pide_al_siguiente(peer, local):
    """AC-20. Con factor 3 la lectura tolera un peer caido sin una linea mas."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada(
        "/tarea.bin", contenido, reparto=lambda i: ["peer2", "peer3"]
    )
    sembrar(peer, peer.entrada, contenido)
    peer.caidos = {"peer2"}

    commands.receive("/", "tarea.bin")

    assert (local / "tarea.bin").read_bytes() == contenido

    # Se intenta con peer2 —de eso trata el criterio— y lo sirve peer3: los dos
    # tienen que aparecer, uno por bloque cada uno.
    assert len([url for url in bajadas(peer) if "peer2" in url]) == 3
    assert len([url for url in bajadas(peer) if "peer3" in url]) == 3


def test_si_no_responde_ningun_peer_del_bloque_no_se_escribe_nada(peer, local, capsys):
    """AC-20. El otro lado: sin copia viva no hay archivo, y se dice."""
    contenido = bytes(range(250))
    peer.entrada = entrada_confirmada(
        "/tarea.bin", contenido, reparto=lambda i: ["peer2", "peer3"]
    )
    sembrar(peer, peer.entrada, contenido)
    peer.caidos = {"peer2", "peer3"}

    commands.receive("/", "tarea.bin")

    assert not (local / "tarea.bin").exists()
    assert "Error" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Paso 9 — el ciclo del fallo, contra un peer real (AC-21)
# ---------------------------------------------------------------------------

def test_el_ciclo_del_fallo_deja_el_nombre_liberable(
    monkeypatch, tmp_path, arboles, limite_pequeno, capsys
):
    """AC-21. Un `send` a medias deja el nombre ocupado, y la salida tiene que
    estar probada y no prometida: si el usuario no puede soltar ese nombre, se
    queda sin poder subir su archivo y nadie le ha dicho por que."""
    cliente = TestClient(app)
    doble = PeerReal(cliente)

    monkeypatch.setattr(commands, "requests", doble)
    monkeypatch.setattr(commands, "PEER_URL", "http://127.0.0.1:8000")
    monkeypatch.chdir(tmp_path)

    contenido = bytes(range(256)) * 6          # 1536 bytes -> 3 bloques de 512
    (tmp_path / "grande.bin").write_bytes(contenido)

    doble.put_falla = {2}
    commands.send("/", "grande.bin")

    # (a) no hubo commit
    assert ("POST", "/files/commit") not in doble.llamadas

    # (b) el archivo no existe para nadie
    assert cliente.get("/files", params={"path": "/"}).json()["items"] == []
    assert cliente.get(
        "/files/lookup", params={"path": "/grande.bin"}
    ).status_code == 404

    # (c) pero el nombre esta ocupado por la entrada pendiente
    doble.put_falla = set()
    capsys.readouterr()
    commands.send("/", "grande.bin")

    assert "409" in capsys.readouterr().out

    # (d) `rm` lo libera, y se lleva los bloques que si habian subido
    assert list(arboles.blocks.rglob("*.blk")) != []

    commands.rm("/", "grande.bin")

    assert list(arboles.blocks.rglob("*.blk")) == []

    # y el mismo `send` vuelve a funcionar, que es lo que cierra el ciclo
    commands.send("/", "grande.bin")
    ficha = cliente.get("/files/lookup", params={"path": "/grande.bin"})

    assert ficha.status_code == 200
    assert ficha.json()["state"] == "committed"

    # Y el archivo vuelve entero. Hay que quitar el local primero: `receive` no
    # sobrescribe, que es justo lo que prueba el AC-19.
    (tmp_path / "grande.bin").unlink()
    commands.receive("/", "grande.bin")

    assert (tmp_path / "grande.bin").read_bytes() == contenido
