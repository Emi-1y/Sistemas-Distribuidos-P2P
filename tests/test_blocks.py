"""API de bloques — SPEC-03, pasos 2 a 7 del plan TDD."""

import asyncio
import hashlib

import pytest
from fastapi import HTTPException

from server import blocks


UUID_A = "5f3e0000-0000-4000-8000-000000000001"
UUID_B = "5f3e0000-0000-4000-8000-000000000002"

VACIO = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def pon(client, file_id, index, contenido, checksum=None):
    """PUT de un bloque, con su checksum correcto salvo que se diga otro."""
    return client.put(
        f"/blocks/{file_id}/{index}",
        content=contenido,
        headers={"X-Block-Checksum": checksum or sha256(contenido)},
    )


class Lector:
    """Iterable asincrono que cuenta cuanto le han leido.

    Es lo que distingue las dos defensas de tamano: con un cuerpo grande las
    dos devuelven 413, y solo el contador dice cual actuo.
    """

    def __init__(self, trozos):
        self._trozos = list(trozos)
        self.total = len(self._trozos)
        self.lecturas = 0
        self.bytes_leidos = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._trozos:
            raise StopAsyncIteration

        trozo = self._trozos.pop(0)
        self.lecturas += 1
        self.bytes_leidos += len(trozo)

        return trozo


# ---------------------------------------------------------------------------
# Paso 2 — identificadores (AC-05, AC-06, AC-15)
# ---------------------------------------------------------------------------

def test_el_uuid_se_normaliza_a_minusculas():
    """AC-15: sin normalizar habria dos directorios para el mismo archivo."""
    file_id, index = blocks.parse_identifiers(UUID_A.upper(), "7")

    assert file_id == UUID_A
    assert index == 7


@pytest.mark.parametrize(
    "file_id",
    ["no-es-un-uuid", "../../etc", "", "5f3e0000", "5f3e0000-0000-4000-8000"],
)
def test_un_file_id_que_no_es_uuid_se_rechaza(file_id):
    """AC-05: lo para la validacion, antes de que llegue a ser una ruta."""
    with pytest.raises(HTTPException) as error:
        blocks.parse_identifiers(file_id, "0")

    assert error.value.status_code == 400
    assert error.value.detail == "Identificador de bloque inválido"


@pytest.mark.parametrize("index", ["-1", "abc", "", "+7", " 7", "1.0", "٧"])
def test_un_index_que_no_es_un_entero_no_negativo_se_rechaza(index):
    """AC-06: int() de Python acepta cosas que una ruta no deberia."""
    with pytest.raises(HTTPException) as error:
        blocks.parse_identifiers(UUID_A, index)

    assert error.value.status_code == 400
    assert error.value.detail == "Identificador de bloque inválido"


def test_los_ceros_a_la_izquierda_son_el_mismo_index():
    """Si no, el mismo bloque tendria dos nombres y el archivo se leeria roto."""
    assert blocks.parse_identifiers(UUID_A, "007")[1] == 7


def test_un_index_no_numerico_no_devuelve_el_422_de_fastapi(client, arboles):
    """AC-06: el 422 esta reservado al checksum, y detail es texto, no lista."""
    respuesta = client.get(f"/blocks/{UUID_A}/abc")

    assert respuesta.status_code == 400
    assert respuesta.json()["detail"] == "Identificador de bloque inválido"


@pytest.mark.parametrize(
    "metodo, ruta",
    [
        ("PUT", "/blocks/no-es-un-uuid/0"),
        ("GET", "/blocks/no-es-un-uuid/0"),
        ("DELETE", "/blocks/no-es-un-uuid"),
    ],
)
def test_un_file_id_invalido_se_rechaza_en_las_tres_operaciones(
    client, arboles, metodo, ruta
):
    """AC-05: las tres comparten la misma validacion."""
    respuesta = client.request(
        metodo, ruta, headers={"X-Block-Checksum": sha256(b"x")}
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["detail"] == "Identificador de bloque inválido"


def test_el_mismo_file_id_en_mayusculas_es_el_mismo_bloque(client, arboles):
    """AC-15, de punta a punta: se escribe con uno y se lee con el otro."""
    contenido = b"un bloque cualquiera"

    assert pon(client, UUID_A.upper(), 0, contenido).status_code == 201

    respuesta = client.get(f"/blocks/{UUID_A}/0")

    assert respuesta.status_code == 200
    assert respuesta.content == contenido
    assert [d.name for d in arboles.blocks.iterdir()] == [UUID_A]


# ---------------------------------------------------------------------------
# Paso 3 — escritura y WORM (AC-01, AC-02)
# ---------------------------------------------------------------------------

def test_escribe_un_bloque(client, arboles):
    """AC-01: 201, el cuerpo del contrato, y el .blk con seis digitos."""
    contenido = b"contenido del bloque cero"

    respuesta = pon(client, UUID_A, 0, contenido)

    assert respuesta.status_code == 201
    assert respuesta.json() == {
        "file_id": UUID_A,
        "index": 0,
        "size": len(contenido),
        "checksum": sha256(contenido),
    }
    assert (arboles.blocks / UUID_A / "000000.blk").read_bytes() == contenido


def test_dos_indices_del_mismo_archivo_van_al_mismo_directorio(client, arboles):
    """Es el caso normal del particionado."""
    pon(client, UUID_A, 0, b"primero")
    pon(client, UUID_A, 125, b"segundo")

    nombres = sorted(p.name for p in (arboles.blocks / UUID_A).iterdir())

    assert nombres == ["000000.blk", "000125.blk"]


def test_un_bloque_escrito_no_se_puede_sobrescribir(client, arboles):
    """AC-02: la inmutabilidad es lo que define este almacen."""
    original = b"el contenido original"
    pon(client, UUID_A, 0, original)

    respuesta = pon(client, UUID_A, 0, b"otro distinto")

    assert respuesta.status_code == 409
    assert respuesta.json()["detail"] == "El bloque ya existe"
    assert (arboles.blocks / UUID_A / "000000.blk").read_bytes() == original


def test_un_bloque_vacio_se_acepta(client, arboles):
    """Rechazarlo exigiria un codigo que CONTRATOS no tiene."""
    respuesta = pon(client, UUID_A, 0, b"")

    assert respuesta.status_code == 201
    assert respuesta.json()["size"] == 0
    assert respuesta.json()["checksum"] == VACIO


# ---------------------------------------------------------------------------
# Paso 4 — integridad (AC-03, AC-04)
# ---------------------------------------------------------------------------

def test_un_checksum_que_no_cuadra_no_deja_bloque(client, arboles):
    """AC-03: se borra lo escrito antes de responder."""
    respuesta = pon(client, UUID_A, 0, b"unos bytes", checksum=sha256(b"otros"))

    assert respuesta.status_code == 422
    assert respuesta.json()["detail"] == "El checksum del bloque no coincide"
    assert not (arboles.blocks / UUID_A / "000000.blk").exists()


def test_un_bloque_corrupto_no_se_queda_bloqueando_el_hueco(client, arboles):
    """El 422 borra porque, si no, el 409 de WORM lo haria permanente.

    Es el encadenamiento de las dos reglas: sin el borrado, un bloque malo
    ocuparia el sitio del bueno para siempre.
    """
    contenido = b"los bytes buenos"
    pon(client, UUID_A, 0, contenido, checksum=sha256(b"mal"))

    respuesta = pon(client, UUID_A, 0, contenido)

    assert respuesta.status_code == 201
    assert (arboles.blocks / UUID_A / "000000.blk").read_bytes() == contenido


@pytest.mark.parametrize(
    "cabecera",
    [None, "", "no-hexadecimal", "abc123", sha256(b"x")[:63], sha256(b"x") + "0"],
)
def test_un_checksum_ausente_o_malformado_se_rechaza(client, arboles, cabecera):
    """AC-04: que falte no puede significar «guardalo sin verificar»."""
    cabeceras = {} if cabecera is None else {"X-Block-Checksum": cabecera}

    respuesta = client.put(
        f"/blocks/{UUID_A}/0", content=b"unos bytes", headers=cabeceras
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["detail"] == "Checksum de bloque inválido"
    assert not (arboles.blocks / UUID_A).exists()


def test_un_checksum_en_mayusculas_se_acepta(client, arboles):
    """CONTRATOS fija minuscula para lo que se emite, no para lo que se recibe."""
    contenido = b"un bloque"

    respuesta = pon(client, UUID_A, 0, contenido, checksum=sha256(contenido).upper())

    assert respuesta.status_code == 201
    assert respuesta.json()["checksum"] == sha256(contenido)


# ---------------------------------------------------------------------------
# Paso 5 — limite de tamano (AC-07, AC-08)
# ---------------------------------------------------------------------------

def test_un_content_length_por_encima_del_limite_no_lee_el_cuerpo(
    arboles, limite_pequeno
):
    """AC-07: el codigo de respuesta no prueba nada aqui; el contador si.

    Con un cuerpo grande, la acumulacion tambien devuelve 413. Si alguien
    quitara la comprobacion previa de Content-Length, un test que solo mirase
    el status seguiria pasando sin probar lo que dice. Lo que discrimina es
    que no se haya consumido ni un trozo.
    """
    lector = Lector([b"x" * 100] * 10)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            blocks.save_block(UUID_A, 0, lector, VACIO, declared_size=10_000)
        )

    assert error.value.status_code == 413
    assert error.value.detail == "El bloque supera el tamaño máximo permitido"
    assert lector.lecturas == 0
    assert lector.bytes_leidos == 0
    assert not (arboles.blocks / UUID_A).exists()


def test_sin_content_length_fiable_se_corta_leyendo(arboles, limite_pequeno):
    """AC-08: el complemento del anterior, y lo que hace que aquel discrimine.

    Aqui si se consume el cuerpo, y se corta antes de agotarlo. Si la
    comprobacion previa desapareciera, AC-07 se comportaria como este test y
    su `lecturas == 0` fallaria.
    """
    lector = Lector([b"x" * 100] * 10)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            blocks.save_block(UUID_A, 0, lector, VACIO, declared_size=None)
        )

    assert error.value.status_code == 413
    assert 0 < lector.lecturas < lector.total
    assert lector.bytes_leidos > limite_pequeno
    assert not (arboles.blocks / UUID_A).exists()


def test_un_content_length_que_miente_tambien_se_corta(arboles, limite_pequeno):
    """La cabecera barata no basta: hay que cubrir al cliente que miente."""
    lector = Lector([b"x" * 100] * 10)

    with pytest.raises(HTTPException) as error:
        asyncio.run(blocks.save_block(UUID_A, 0, lector, VACIO, declared_size=10))

    assert error.value.status_code == 413
    assert lector.lecturas > 0


def test_un_bloque_de_exactamente_el_limite_se_acepta(client, arboles, limite_pequeno):
    """El contrato dice «supera», no «alcanza»: el limite es inclusivo."""
    respuesta = pon(client, UUID_A, 0, b"x" * limite_pequeno)

    assert respuesta.status_code == 201
    assert respuesta.json()["size"] == limite_pequeno


def test_un_bloque_de_un_byte_mas_que_el_limite_se_rechaza(
    client, arboles, limite_pequeno
):
    """El otro lado del mismo borde."""
    respuesta = pon(client, UUID_A, 0, b"x" * (limite_pequeno + 1))

    assert respuesta.status_code == 413
    assert respuesta.json()["detail"] == "El bloque supera el tamaño máximo permitido"
    assert not (arboles.blocks / UUID_A).exists()


# ---------------------------------------------------------------------------
# Paso 6 — lectura (AC-09, AC-10)
# ---------------------------------------------------------------------------

def test_lee_un_bloque_con_su_checksum(client, arboles):
    """AC-09: los bytes identicos y el SHA-256 de esos bytes."""
    contenido = b"contenido que va y vuelve"
    pon(client, UUID_A, 0, contenido)

    respuesta = client.get(f"/blocks/{UUID_A}/0")

    assert respuesta.status_code == 200
    assert respuesta.content == contenido
    assert respuesta.headers["x-block-checksum"] == sha256(contenido)


def test_el_checksum_del_get_se_recalcula_sobre_lo_leido(client, arboles):
    """El checksum no se guarda: se recalcula, y eso delata la corrupcion en reposo.

    Si el disco pudre un byte, el GET devuelve un checksum distinto del que
    quedo en el commit y el cliente lo ve al reensamblar.
    """
    contenido = b"contenido sano"
    pon(client, UUID_A, 0, contenido)

    podrido = b"contenido roto"
    (arboles.blocks / UUID_A / "000000.blk").write_bytes(podrido)

    respuesta = client.get(f"/blocks/{UUID_A}/0")

    assert respuesta.headers["x-block-checksum"] == sha256(podrido)
    assert respuesta.headers["x-block-checksum"] != sha256(contenido)


def test_un_bloque_que_no_existe(client, arboles):
    """AC-10."""
    respuesta = client.get(f"/blocks/{UUID_A}/0")

    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "El bloque no existe"


def test_un_index_que_no_existe_en_un_archivo_que_si(client, arboles):
    """El error tiene que venir del bloque, no del archivo."""
    pon(client, UUID_A, 0, b"solo el cero")

    respuesta = client.get(f"/blocks/{UUID_A}/1")

    assert respuesta.status_code == 404
    assert respuesta.json()["detail"] == "El bloque no existe"


# ---------------------------------------------------------------------------
# Paso 7 — borrado (AC-11, AC-12)
# ---------------------------------------------------------------------------

def test_borra_todos_los_bloques_del_archivo(client, arboles):
    """AC-11."""
    for index in range(3):
        pon(client, UUID_A, index, f"bloque {index}".encode())

    respuesta = client.delete(f"/blocks/{UUID_A}")

    assert respuesta.status_code == 200
    assert respuesta.json() == {"file_id": UUID_A, "deleted": 3}
    assert not (arboles.blocks / UUID_A).exists()


def test_borrar_un_archivo_del_que_no_hay_nada_no_es_un_error(client, arboles):
    """AC-12: el borrado se lanza a varios peers y la mayoria no tendra nada."""
    respuesta = client.delete(f"/blocks/{UUID_A}")

    assert respuesta.status_code == 200
    assert respuesta.json() == {"file_id": UUID_A, "deleted": 0}


def test_borrar_dos_veces_seguidas_es_idempotente(client, arboles):
    """La definicion operativa de idempotente."""
    pon(client, UUID_A, 0, b"algo")

    primera = client.delete(f"/blocks/{UUID_A}")
    segunda = client.delete(f"/blocks/{UUID_A}")

    assert primera.json()["deleted"] == 1
    assert segunda.json()["deleted"] == 0
    assert segunda.status_code == 200


def test_borrar_un_archivo_no_toca_los_bloques_de_otro(client, arboles):
    """El borrado es por file_id, no por peer."""
    pon(client, UUID_A, 0, b"del archivo A")
    pon(client, UUID_B, 0, b"del archivo B")

    client.delete(f"/blocks/{UUID_A}")

    assert not (arboles.blocks / UUID_A).exists()
    assert (arboles.blocks / UUID_B / "000000.blk").read_bytes() == b"del archivo B"
