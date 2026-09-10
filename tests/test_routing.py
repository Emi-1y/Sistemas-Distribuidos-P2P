"""Enrutamiento entre peers — SPEC-04, fase 2 del plan TDD (pasos 7 a 10).

Ningun test abre un socket: `_send` se sustituye por un doble que registra a
quien se llamo, con que cabeceras y en que orden. La semantica quedo fijada en
la fase 1, asi que todo lo que falle aqui es reenvio.
"""

import httpx
import pytest
from fastapi import HTTPException

from server import config, metadata, ring, routing
from server.ring import Ring


class Respuesta:
    """Lo minimo que `_send` devuelve y `routing` mira."""

    def __init__(self, status_code, cuerpo=None):
        self.status_code = status_code
        self._cuerpo = {} if cuerpo is None else cuerpo

    def json(self):
        return self._cuerpo


class Doble:
    """Sustituye `routing._send`: registra la llamada y devuelve lo pactado.

    `respuesta` puede ser un objeto o un invocable que recibe la url, para que
    un test haga fallar una operacion y no otra, o mire el estado del disco
    justo en el instante en que se sale a la red.
    """

    def __init__(self):
        self.llamadas = []
        self.respuesta = Respuesta(200)
        self.error = None

    def __call__(self, address, method, url, params, json, headers):
        self.llamadas.append({
            "address": address,
            "method": method,
            "url": url,
            "params": params,
            "json": json,
            "headers": headers,
        })

        if self.error is not None:
            raise self.error

        if callable(self.respuesta):
            return self.respuesta(url)

        return self.respuesta


@pytest.fixture
def tres_peers(tmp_path, monkeypatch):
    """Tres peers, y yo soy el dueno de la raiz.

    Serlo hace que un `mkdir` de primer nivel se resuelva localmente y que solo
    la segunda escritura salga a la red, que es justo lo que hay que observar.
    """
    root = tmp_path / "storage" / "metadata"
    root.mkdir(parents=True)
    monkeypatch.setattr(metadata, "METADATA_ROOT", root)

    local = Ring()

    for numero in (1, 2, 3):
        local.add_peer(f"peer{numero}", f"http://peer{numero}:900{numero}")

    monkeypatch.setattr(ring, "LOCAL", local)
    monkeypatch.setattr(config, "PEER_ID", ring.choose_nodes("/")[0])

    return root


@pytest.fixture
def enviados(monkeypatch):
    doble = Doble()
    monkeypatch.setattr(routing, "_send", doble)
    return doble


@pytest.fixture
def bloque_pequeno(monkeypatch):
    monkeypatch.setattr(metadata.config, "BLOCK_SIZE", 512)
    return 512


def ruta_mia() -> str:
    for numero in range(2000):
        ruta = f"/dir{numero}"

        if ring.choose_nodes(ruta)[0] == config.PEER_ID:
            return ruta

    raise AssertionError("ninguna ruta de primer nivel me pertenece")


def ruta_ajena() -> str:
    """Una ruta de primer nivel cuyo contenido pertenece a otro peer."""
    for numero in range(2000):
        ruta = f"/dir{numero}"

        if ring.choose_nodes(ruta)[0] != config.PEER_ID:
            return ruta

    raise AssertionError("todas las rutas de primer nivel me pertenecen")


def direccion_de(ruta: str) -> str:
    return ring.address_of(ring.choose_nodes(ruta)[0])


# ---------------------------------------------------------------------------
# Paso 7 — delegate (AC-18, AC-19)
# ---------------------------------------------------------------------------

def test_una_clave_mia_no_sale_a_la_red(tres_peers, enviados):
    mia = ruta_mia()

    resultado = routing.delegate(
        mia, forwarded=False, method="GET", url="/files", params={"path": mia}
    )

    assert resultado is None
    assert enviados.llamadas == []


def test_una_clave_ajena_se_reenvia_al_dueno(tres_peers, enviados):
    """AC-18: a su direccion del anillo y con la cabecera puesta."""
    ajena = ruta_ajena()
    enviados.respuesta = Respuesta(200, {"path": ajena, "items": []})

    resultado = routing.delegate(
        ajena, forwarded=False, method="GET", url="/files",
        params={"path": ajena}
    )

    assert resultado == {"path": ajena, "items": []}
    assert len(enviados.llamadas) == 1

    envio = enviados.llamadas[0]
    assert envio["address"] == direccion_de(ajena)
    assert envio["method"] == "GET"
    assert envio["url"] == "/files"
    assert envio["params"] == {"path": ajena}
    assert envio["headers"]["X-Forwarded-By"] == config.PEER_ID


def test_el_error_del_dueno_llega_tal_cual(tres_peers, enviados):
    """AC-19: ni un 500 generico ni un mensaje traducido."""
    ajena = ruta_ajena()
    enviados.respuesta = Respuesta(409, {"detail": "El archivo ya existe"})

    with pytest.raises(HTTPException) as error:
        routing.delegate(
            ajena, forwarded=False, method="POST", url="/files/allocate",
            json={"path": f"{ajena}/tarea.pdf", "size": 600}
        )

    assert error.value.status_code == 409
    assert error.value.detail == "El archivo ya existe"


# ---------------------------------------------------------------------------
# Paso 8 — bucle y peer caido (AC-20, AC-21)
# ---------------------------------------------------------------------------

def test_una_peticion_ya_reenviada_que_tampoco_es_mia_es_un_bucle(
    tres_peers, enviados
):
    """AC-20: con anillos que pudieron divergir, sin este corte dos peers se
    mandarian la misma peticion indefinidamente."""
    ajena = ruta_ajena()

    with pytest.raises(HTTPException) as error:
        routing.delegate(
            ajena, forwarded=True, method="GET", url="/files",
            params={"path": ajena}
        )

    assert error.value.status_code == 508
    assert error.value.detail == "Bucle de enrutamiento detectado"
    assert enviados.llamadas == []


def test_una_peticion_reenviada_que_si_es_mia_se_atiende(tres_peers, enviados):
    """La cabecera no es un rechazo: significa que el reenvio hizo su trabajo."""
    mia = ruta_mia()

    resultado = routing.delegate(
        mia, forwarded=True, method="GET", url="/files", params={"path": mia}
    )

    assert resultado is None
    assert enviados.llamadas == []


def test_un_dueno_que_no_responde(tres_peers, enviados):
    """AC-21: el sistema dice que no puede, no finge que la ruta no existe."""
    ajena = ruta_ajena()
    enviados.error = httpx.ConnectError("no hay nadie al otro lado")

    with pytest.raises(HTTPException) as error:
        routing.delegate(
            ajena, forwarded=False, method="GET", url="/files",
            params={"path": ajena}
        )

    assert error.value.status_code == 503
    assert error.value.detail == "El peer responsable no está disponible"


# ---------------------------------------------------------------------------
# Paso 7 (endpoints) — toda operacion ajena sale al dueno
# ---------------------------------------------------------------------------

def peticiones_ajenas(base: str) -> dict:
    """Una peticion por endpoint, todas con la clave en un directorio ajeno."""
    ruta = f"{base}/tarea.pdf"

    return {
        "ls": ("GET", "/files", {"path": base}, None),
        "mkdir": ("POST", "/directories", None, {"path": ruta}),
        "rmdir": ("DELETE", "/directories", {"path": ruta}, None),
        "rm": ("DELETE", "/files", {"path": ruta}, None),
        "allocate": ("POST", "/files/allocate", None, {"path": ruta, "size": 600}),
        "commit": (
            "POST", "/files/commit",
            None, {"path": ruta, "file_id": "5f3e", "blocks": []},
        ),
        "lookup": ("GET", "/files/lookup", {"path": ruta}, None),
        "contenido_crear": ("POST", "/directories/content", None, {"path": base}),
        "contenido_borrar": (
            "DELETE", "/directories/content", {"path": base}, None
        ),
    }


@pytest.mark.parametrize("operacion", list(peticiones_ajenas("/x")))
def test_toda_operacion_con_clave_ajena_sale_al_dueno(
    client, tres_peers, enviados, operacion
):
    """AC-18 a nivel de endpoint: `delegate` esta cableado en los nueve."""
    base = ruta_ajena()
    metodo, url, params, cuerpo = peticiones_ajenas(base)[operacion]
    enviados.respuesta = Respuesta(200, {"ok": True})

    respuesta = client.request(metodo, url, params=params, json=cuerpo)

    assert respuesta.status_code == 200
    assert respuesta.json() == {"ok": True}
    assert len(enviados.llamadas) == 1
    assert enviados.llamadas[0]["headers"]["X-Forwarded-By"] == config.PEER_ID


# ---------------------------------------------------------------------------
# Paso 9 — las dos escrituras de mkdir (AC-22, AC-23)
# ---------------------------------------------------------------------------

def test_mkdir_escribe_primero_la_entrada_y_despues_el_contenido(
    client, tres_peers, enviados
):
    """AC-22: el orden es la decision, asi que el orden es lo que se prueba.

    El doble mira el disco en el instante exacto en que se sale a la red: si la
    entrada en el padre ya esta, es que se escribio primero.
    """
    ruta = ruta_ajena()
    nombre = ruta.lstrip("/")
    visto_al_salir = []

    def responder(url):
        visto_al_salir.extend(
            item["name"] for item in metadata.list_entries("/")
        )
        return Respuesta(200, {"path": ruta})

    enviados.respuesta = responder

    respuesta = client.post("/directories", json={"path": ruta})

    assert respuesta.status_code == 200
    assert visto_al_salir == [nombre]

    envio = enviados.llamadas[0]
    assert envio["address"] == direccion_de(ruta)
    assert envio["method"] == "POST"
    assert envio["url"] == "/directories/content"
    assert envio["json"] == {"path": ruta}


def test_si_la_segunda_escritura_falla_el_directorio_queda_visible_y_cerrado(
    client, tres_peers, enviados
):
    """AC-23: falla ruidosamente, no traga nada, y `rmdir` lo deshace."""
    ruta = ruta_ajena()
    nombre = ruta.lstrip("/")
    enviados.error = httpx.ConnectError("el dueno no responde")

    respuesta = client.post("/directories", json={"path": ruta})

    # Ruidoso: nadie cree que ha ido bien.
    assert respuesta.status_code == 503

    # Se ve desde fuera: el nombre quedo reservado, que es lo que impide que
    # despues alguien cree un archivo con esa misma ruta.
    assert [item["name"] for item in metadata.list_entries("/")] == [nombre]

    # Y se deshace con un comando que ya existe: el dueno del contenido no
    # tiene nada, y la ausencia se trata como vacio.
    enviados.error = None
    enviados.respuesta = Respuesta(200, {"path": ruta})

    borrado = client.delete("/directories", params={"path": ruta})

    assert borrado.status_code == 200
    assert metadata.list_entries("/") == []


def test_el_nombre_reservado_impide_crear_un_archivo_con_esa_ruta(
    client, tres_peers, enviados, bloque_pequeno
):
    """El motivo de que la entrada vaya primero, escrito como test."""
    ruta = ruta_ajena()
    enviados.error = httpx.ConnectError("el dueno no responde")

    client.post("/directories", json={"path": ruta})

    with pytest.raises(HTTPException) as error:
        metadata.allocate(ruta, 600)

    assert error.value.status_code == 409


# ---------------------------------------------------------------------------
# Paso 10 — el aviso a los peers de los bloques (AC-24)
# ---------------------------------------------------------------------------

def archivo_confirmado(peers=("peer2", "peer3")) -> dict:
    """Un archivo en la raiz, que es mia, con bloques en peers ajenos."""
    entrada = metadata.allocate("/tarea.pdf", 600)

    return metadata.commit(
        entrada["path"],
        entrada["file_id"],
        [
            {
                "index": bloque["index"],
                "checksum": f"{bloque['index']:064d}",
                "peers": [peers[bloque["index"] % len(peers)]],
            }
            for bloque in entrada["blocks"]
        ],
    )


def test_rm_avisa_a_los_peers_de_los_bloques(
    client, tres_peers, enviados, bloque_pequeno
):
    """AC-24: a cada uno de los peers donde de verdad estan los bloques."""
    entrada = archivo_confirmado()

    respuesta = client.delete("/files", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 200

    destinos = sorted(envio["address"] for envio in enviados.llamadas)
    assert destinos == ["http://peer2:9002", "http://peer3:9003"]

    for envio in enviados.llamadas:
        assert envio["method"] == "DELETE"
        assert envio["url"] == f"/blocks/{entrada['file_id']}"


def test_el_aviso_va_despues_de_haber_borrado_la_entrada(
    client, tres_peers, enviados, bloque_pequeno
):
    """AC-24: al reves quedaria un archivo que `ls` muestra y no se puede leer.

    Lo que queda asi son bloques que nadie referencia: ocupan disco, pero no
    mienten.
    """
    archivo_confirmado()
    visto_al_salir = []

    def responder(url):
        visto_al_salir.append(metadata.list_entries("/"))
        return Respuesta(200)

    enviados.respuesta = responder

    client.delete("/files", params={"path": "/tarea.pdf"})

    assert visto_al_salir == [[], []]


def test_un_peer_que_no_recoge_sus_bloques_no_rompe_el_borrado(
    client, tres_peers, enviados, bloque_pequeno
):
    """El aviso es best-effort: los bloques que no se borran quedan huerfanos.

    Es deuda conocida y va al handoff del hito 3, no un fallo del borrado.
    """
    archivo_confirmado()
    enviados.error = httpx.ConnectError("ese peer no esta")

    respuesta = client.delete("/files", params={"path": "/tarea.pdf"})

    assert respuesta.status_code == 200
    assert metadata.list_entries("/") == []
