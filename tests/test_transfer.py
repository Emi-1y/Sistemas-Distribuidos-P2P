"""El troceado y el reensamblado — SPEC-05, pasos 1 a 3 del plan TDD.

Fase 1: aqui no hay red. Ni un socket, ni `requests` sustituido, ni un peer
falso. Solo la aritmetica de los offsets y el lote en paralelo, que es lo que
se rompe por sus propios motivos y no por los de la red.
"""

import hashlib
import threading
import time

import pytest

from client import transfer


def plan(size: int, block_size: int) -> list[dict]:
    """El plan de bloques tal y como lo devuelve `allocate`.

    Se reproduce aqui a proposito: el cliente no lo calcula, lo recibe, asi que
    lo que estos tests ejercen es que sepa leer y rearmar **el plan que le den**.
    Que el servidor lo construya bien ya lo prueba `test_metadata.py`.
    """
    bloques = []
    restante = size
    index = 0

    while restante > 0:
        trozo = min(block_size, restante)

        bloques.append({
            "index": index,
            "size": trozo,
            "checksum": None,
            "peers": ["peer1"],
        })

        restante -= trozo
        index += 1

    return bloques


def archivo(tmp_path, contenido: bytes):
    ruta = tmp_path / "local.bin"
    ruta.write_bytes(contenido)

    return ruta


def trocear(ruta, bloques, block_size):
    """Todos los trozos de un plan, en orden, como hace `send`."""
    return [
        transfer.read_chunk(ruta, b["index"], block_size, b["size"])
        for b in bloques
    ]


class Aforo:
    """Cuenta cuantas tareas hay dentro a la vez y recuerda el maximo.

    Es lo unico que distingue «corren en paralelo» de «corren en paralelo pero
    sin tope»: las dos cosas terminan, y solo el maximo dice cual paso.
    """

    def __init__(self):
        self.dentro = 0
        self.maximo = 0
        self._cerrojo = threading.Lock()

    def entra(self):
        with self._cerrojo:
            self.dentro += 1
            self.maximo = max(self.maximo, self.dentro)

    def sale(self):
        with self._cerrojo:
            self.dentro -= 1


# ---------------------------------------------------------------------------
# Paso 1 — checksum y troceado (AC-01, AC-02)
# ---------------------------------------------------------------------------

def test_el_checksum_es_el_sha256_en_hexadecimal_minuscula():
    """AC-02. Es el formato que CONTRATOS exige en X-Block-Checksum."""
    datos = bytes(range(256))

    calculado = transfer.checksum(datos)

    assert calculado == hashlib.sha256(datos).hexdigest()
    assert calculado == calculado.lower()
    assert len(calculado) == 64


def test_el_checksum_de_lo_vacio_es_el_del_sha256_vacio():
    """AC-02. Un archivo de cero bytes no tiene bloques, pero la funcion si."""
    assert transfer.checksum(b"") == hashlib.sha256(b"").hexdigest()


def test_read_chunk_devuelve_exactamente_el_trozo_del_indice(tmp_path):
    """AC-01. El offset es index * block_size, no el orden de lectura."""
    contenido = bytes(range(20))
    ruta = archivo(tmp_path, contenido)

    assert transfer.read_chunk(ruta, 0, 4, 4) == contenido[0:4]
    assert transfer.read_chunk(ruta, 2, 4, 4) == contenido[8:12]
    assert transfer.read_chunk(ruta, 4, 4, 4) == contenido[16:20]


def test_read_chunk_no_depende_del_orden_en_que_se_pidan_los_trozos(tmp_path):
    """AC-01. Al subir en paralelo los bloques se piden desordenados."""
    contenido = bytes(range(20))
    ruta = archivo(tmp_path, contenido)

    assert transfer.read_chunk(ruta, 3, 4, 4) == contenido[12:16]
    assert transfer.read_chunk(ruta, 1, 4, 4) == contenido[4:8]
    assert transfer.read_chunk(ruta, 3, 4, 4) == contenido[12:16]


def test_los_trozos_de_un_plan_reconstruyen_el_archivo(tmp_path):
    """AC-01. La propiedad que de verdad importa: no se pierde ni un byte."""
    contenido = bytes(range(256)) * 3
    ruta = archivo(tmp_path, contenido)
    bloques = plan(len(contenido), 100)

    assert b"".join(trocear(ruta, bloques, 100)) == contenido


def test_un_tamano_multiplo_exacto_no_deja_bloque_final_corto(tmp_path):
    """Test limite. El error de uno clasico, del lado del cliente."""
    contenido = b"x" * 400
    ruta = archivo(tmp_path, contenido)
    bloques = plan(len(contenido), 100)

    trozos = trocear(ruta, bloques, 100)

    assert len(bloques) == 4
    assert [len(t) for t in trozos] == [100, 100, 100, 100]
    assert b"".join(trozos) == contenido


def test_un_byte_mas_que_un_multiplo_deja_un_bloque_de_un_byte(tmp_path):
    """Test limite. El otro lado del mismo borde."""
    contenido = b"x" * 400 + b"z"
    ruta = archivo(tmp_path, contenido)
    bloques = plan(len(contenido), 100)

    trozos = trocear(ruta, bloques, 100)

    assert [len(t) for t in trozos] == [100, 100, 100, 100, 1]
    assert trozos[-1] == b"z"
    assert b"".join(trozos) == contenido


def test_un_archivo_vacio_no_tiene_ni_un_bloque(tmp_path):
    """Test limite. Cero PUT es lo correcto, no un fallo."""
    ruta = archivo(tmp_path, b"")
    bloques = plan(0, 100)

    assert bloques == []
    assert trocear(ruta, bloques, 100) == []


def test_si_el_archivo_se_encogio_read_chunk_levanta(tmp_path):
    """Test limite. El plan describe un archivo que ya no existe.

    Entre el `allocate` y la lectura pueden pasar cosas. Devolver un trozo
    corto en silencio confirmaria unos metadatos que mienten sobre el tamano.
    """
    ruta = archivo(tmp_path, b"x" * 400)
    bloques = plan(400, 100)

    ruta.write_bytes(b"x" * 250)

    with pytest.raises(transfer.TransferError):
        trocear(ruta, bloques, 100)


# ---------------------------------------------------------------------------
# Paso 2 — reensamblado (AC-03, AC-04)
# ---------------------------------------------------------------------------

def test_reensambla_por_indice_y_no_por_orden_de_llegada():
    """AC-03. El bug que introduce el paralelismo, y el unico que ningun
    checksum por bloque detecta: cada pieza esta intacta y el archivo no."""
    contenido = b"".join(bytes([i]) * 100 for i in range(5))
    bloques = plan(len(contenido), 100)

    piezas = {
        b["index"]: contenido[b["index"] * 100: b["index"] * 100 + b["size"]]
        for b in reversed(bloques)
    }

    assert list(piezas) == [4, 3, 2, 1, 0]
    assert transfer.assemble(piezas, bloques) == contenido


def test_reensambla_un_plan_de_un_solo_bloque():
    """AC-03. El caso en que el orden no puede fallar, y tiene que salir igual."""
    bloques = plan(50, 100)

    assert transfer.assemble({0: b"y" * 50}, bloques) == b"y" * 50


def test_reensamblar_un_plan_vacio_da_un_archivo_vacio():
    """Test limite. Es como se baja un archivo de cero bytes."""
    assert transfer.assemble({}, []) == b""


def test_si_falta_un_indice_del_plan_levanta():
    """AC-04. Un archivo al que le falta un bloque no es un archivo mas corto."""
    contenido = b"x" * 300
    bloques = plan(len(contenido), 100)

    piezas = {0: b"x" * 100, 2: b"x" * 100}

    with pytest.raises(transfer.TransferError):
        transfer.assemble(piezas, bloques)


def test_si_sobra_una_pieza_que_el_plan_no_nombra_levanta():
    """AC-04. Lo que el plan no nombra no se cuela en el archivo."""
    bloques = plan(200, 100)

    piezas = {0: b"x" * 100, 1: b"x" * 100, 2: b"intruso"}

    with pytest.raises(transfer.TransferError):
        transfer.assemble(piezas, bloques)


# ---------------------------------------------------------------------------
# Paso 3 — el lote en paralelo (AC-05, AC-06)
# ---------------------------------------------------------------------------

def test_las_tareas_corren_de_verdad_a_la_vez():
    """AC-05. Con una barrera y no con esperas.

    Si `in_parallel` ejecutara en serie, la barrera nunca reuniria a sus
    participantes y todas las tareas devolverian None: el test falla por lo que
    afirma, no por tiempo.
    """
    barrera = threading.Barrier(transfer.BLOQUES_EN_PARALELO)
    cuantas = 2 * transfer.BLOQUES_EN_PARALELO

    def tarea(i):
        def correr():
            try:
                barrera.wait(timeout=5)
            except threading.BrokenBarrierError:
                return None

            return i

        return correr

    resultados = transfer.in_parallel([tarea(i) for i in range(cuantas)])

    assert resultados == list(range(cuantas))


def test_no_corren_mas_de_bloques_en_paralelo_a_la_vez():
    """AC-05. El tope acota: sin el, un archivo grande abriria una conexion
    por bloque contra tres peers, que no es mas rapido y agota descriptores."""
    aforo = Aforo()
    barrera = threading.Barrier(transfer.BLOQUES_EN_PARALELO)

    def tarea():
        aforo.entra()

        try:
            barrera.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass

        aforo.sale()

        return True

    transfer.in_parallel([tarea] * (2 * transfer.BLOQUES_EN_PARALELO))

    assert aforo.maximo == transfer.BLOQUES_EN_PARALELO


def test_los_resultados_vuelven_en_el_orden_de_entrada():
    """AC-05. Terminan al reves de como se lanzaron y aun asi vuelven en orden:
    es lo que permite emparejar cada resultado con su bloque."""
    cuantas = transfer.BLOQUES_EN_PARALELO

    def tarea(i):
        def correr():
            time.sleep(0.01 * (cuantas - i))
            return i

        return correr

    assert transfer.in_parallel(
        [tarea(i) for i in range(cuantas)]
    ) == list(range(cuantas))


def test_mas_tareas_que_el_tope_se_ejecutan_todas():
    """Test limite. El tope reparte en tandas; no descarta."""
    ejecutadas = []
    cerrojo = threading.Lock()
    cuantas = 3 * transfer.BLOQUES_EN_PARALELO + 1

    def tarea(i):
        def correr():
            with cerrojo:
                ejecutadas.append(i)

            return i

        return correr

    resultados = transfer.in_parallel([tarea(i) for i in range(cuantas)])

    assert resultados == list(range(cuantas))
    assert sorted(ejecutadas) == list(range(cuantas))


def test_el_primer_fallo_se_propaga():
    """AC-06. Quien llama tiene que enterarse: es lo que cancela el commit."""
    def bien():
        return b"ok"

    def mal():
        raise transfer.TransferError("el bloque no subio")

    with pytest.raises(transfer.TransferError):
        transfer.in_parallel([bien, mal, bien])


def test_el_fallo_se_propaga_tal_cual_aunque_no_sea_nuestro():
    """AC-06. Un error de red no se disfraza de TransferError: `send` distingue
    «esto lo rompimos nosotros» de «el peer no contesta»."""
    class ErrorDeRed(Exception):
        pass

    def revienta():
        raise ErrorDeRed("no responde")

    with pytest.raises(ErrorDeRed):
        transfer.in_parallel([revienta])
