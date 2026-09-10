"""Anillo de hashing consistente — SPEC-02, pasos 1, 2, 3 y 4 del plan TDD.

Ningun test de este archivo abre un socket ni toca disco: el anillo es una
funcion pura de la membresia, y esa es justo la propiedad que se comprueba.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server import config
from server.ring import Ring, stable_hash


REPO_ROOT = Path(__file__).resolve().parent.parent


def anillo(*peer_ids, vnodes=config.VNODES):
    """Un anillo con esos peers, cada uno en una direccion previsible."""
    resultado = Ring(vnodes=vnodes)

    for peer_id in peer_ids:
        resultado.add_peer(peer_id, f"http://{peer_id}:9000")

    return resultado


def claves(cantidad):
    """Claves con la forma real de un bloque: file_id:index."""
    return [f"5f3e0000-0000-4000-8000-{i:012d}:{i % 5}" for i in range(cantidad)]


def reparto(anillo_dado, lista_de_claves):
    return {clave: anillo_dado.choose_nodes(clave)[0] for clave in lista_de_claves}


# ---------------------------------------------------------------------------
# Paso 1 — determinismo entre procesos (AC-03) y ring_version (AC-14)
# ---------------------------------------------------------------------------

# El determinismo no depende de cuantos nodos virtuales haya, asi que la sonda
# usa pocos y va rapido. Entra por argumento, no cableado: ninguna asercion de
# este archivo puede depender de que sean ocho.
VNODES_SONDA = 8

SONDA = """
import json
import sys
from server.ring import Ring

r = Ring(vnodes=int(sys.argv[1]))

for peer_id in ("peer1", "peer2", "peer3"):
    r.add_peer(peer_id, "http://" + peer_id + ":9000")

print(json.dumps({
    "posiciones": r.positions(),
    "version": r.version(),
    "duenio": r.choose_nodes("/universidad"),
    "hash_de_python": hash("/universidad"),
}))
"""


def sonda(semilla, vnodes=VNODES_SONDA):
    """Construye un anillo en otro proceso, con PYTHONHASHSEED fijado."""
    entorno = dict(os.environ)
    entorno["PYTHONHASHSEED"] = semilla
    entorno["PYTHONPATH"] = str(REPO_ROOT)

    salida = subprocess.run(
        [sys.executable, "-c", SONDA, str(vnodes)],
        cwd=REPO_ROOT,
        env=entorno,
        capture_output=True,
        text=True,
        check=True,
    )

    return json.loads(salida.stdout)


def test_dos_procesos_con_semillas_distintas_construyen_el_mismo_anillo():
    """AC-03: el anillo entero coincide, no solo el dueno de una clave.

    Comparar solo choose_nodes seria enganarse: con tres peers, dos anillos
    completamente distintos aciertan el mismo dueno una vez de cada tres.
    """
    uno = sonda("1")
    otro = sonda("2")

    assert uno["posiciones"] == otro["posiciones"]
    assert len(uno["posiciones"]) == VNODES_SONDA * 3
    assert uno["version"] == otro["version"]
    assert uno["duenio"] == otro["duenio"]


def test_el_determinismo_no_depende_de_cuantos_nodos_virtuales_haya():
    """La sonda usa pocos vnodes por velocidad, no porque el numero importe.

    Con otro valor el anillo es distinto, pero sigue siendo el mismo en los dos
    procesos: lo que se prueba es el hash, no el reparto.
    """
    assert sonda("1", vnodes=5)["posiciones"] == sonda("2", vnodes=5)["posiciones"]
    assert sonda("1", vnodes=5)["posiciones"] != sonda("1", vnodes=7)["posiciones"]


def test_las_semillas_elegidas_de_verdad_cambian_el_hash_de_python():
    """El test de arriba no vale nada si las dos semillas dan lo mismo.

    Esto comprueba que el experimento es real: si Python dejara de aleatorizar
    hash(), el test de determinismo pasaria sin haber probado nada.
    """
    assert sonda("1")["hash_de_python"] != sonda("2")["hash_de_python"]


def test_la_version_del_anillo_es_hexadecimal_corto():
    """AC-14: ocho caracteres hexadecimales en minuscula."""
    version = anillo("peer1", "peer2", "peer3").version()

    assert len(version) == 8
    assert all(caracter in "0123456789abcdef" for caracter in version)


def test_la_version_del_anillo_no_depende_del_orden_de_alta():
    """AC-14: misma membresia, misma version, aunque entren en otro orden."""
    uno = anillo("peer1", "peer2", "peer3")
    otro = anillo("peer3", "peer1", "peer2")

    assert uno.version() == otro.version()


def test_la_version_del_anillo_no_depende_de_las_direcciones():
    """AC-14: cambiar de direccion no mueve ninguna clave, no es otro anillo."""
    uno = Ring()
    uno.add_peer("peer1", "http://10.0.0.1:9001")

    otro = Ring()
    otro.add_peer("peer1", "http://192.168.1.7:9001")

    assert uno.version() == otro.version()


def test_membresias_distintas_dan_versiones_distintas():
    """AC-14: si el conjunto de peer_id cambia, la version cambia."""
    tres = anillo("peer1", "peer2", "peer3")
    cuatro = anillo("peer1", "peer2", "peer3", "peer4")

    assert tres.version() != cuatro.version()


# ---------------------------------------------------------------------------
# Paso 2 — choose_nodes: reparto y no repeticion (AC-04, AC-06, AC-07)
# ---------------------------------------------------------------------------

def test_tres_peers_se_reparten_las_claves_sin_que_ninguno_acapare():
    """AC-04: entre el 20 % y el 50 % cada uno; el ideal es 33 %."""
    lista = claves(3000)
    duenios = reparto(anillo("peer1", "peer2", "peer3"), lista)

    for peer_id in ("peer1", "peer2", "peer3"):
        cuota = sum(1 for d in duenios.values() if d == peer_id) / len(lista)
        assert 0.20 <= cuota <= 0.50, f"{peer_id} se lleva {cuota:.2%}"


def test_tres_replicas_sobre_tres_peers_son_tres_peers_distintos():
    """AC-06: el gancho del hito 3. Sin esto, factor 3 replica en un solo nodo."""
    ring = anillo("peer1", "peer2", "peer3")

    for clave in claves(500):
        elegidos = ring.choose_nodes(clave, replicas=3)

        assert len(elegidos) == 3
        assert len(set(elegidos)) == 3


def test_pedir_mas_replicas_que_peers_devuelve_los_que_hay():
    """AC-07: el anillo dice quien existe; que sean pocos lo decide el commit."""
    elegidos = anillo("peer1", "peer2").choose_nodes("/universidad", replicas=5)

    assert sorted(elegidos) == ["peer1", "peer2"]


# ---------------------------------------------------------------------------
# Paso 3 — estabilidad ante la entrada de un peer (AC-05)
# ---------------------------------------------------------------------------

def test_la_entrada_de_un_peer_remapea_menos_del_40_por_ciento():
    """AC-05: el test que justifica el hashing consistente frente a modulo N."""
    lista = claves(2000)
    ring = anillo("peer1", "peer2", "peer3")

    antes = reparto(ring, lista)
    ring.add_peer("peer4", "http://peer4:9000")
    despues = reparto(ring, lista)

    movidas = [clave for clave in lista if antes[clave] != despues[clave]]

    assert 0 < len(movidas) / len(lista) < 0.40


def test_las_claves_que_se_mueven_van_todas_al_peer_que_entro():
    """AC-05: ninguna clave se mueve entre dos peers que ya estaban.

    Es la propiedad fuerte del anillo: el que entra roba, nadie mas se toca.
    Con modulo N esto seria falso para casi todas las claves movidas.
    """
    lista = claves(2000)
    ring = anillo("peer1", "peer2", "peer3")

    antes = reparto(ring, lista)
    ring.add_peer("peer4", "http://peer4:9000")
    despues = reparto(ring, lista)

    for clave in lista:
        if antes[clave] != despues[clave]:
            assert despues[clave] == "peer4"


def test_el_umbral_de_ac05_descarta_una_colocacion_con_modulo_n():
    """Comprueba que el umbral de AC-05 discrimina de verdad.

    Un umbral suelto no demuestra nada. Aqui se implementa la colocacion
    ingenua stable_hash(clave) % N y se la somete al mismo experimento: si
    AC-05 admitiera esta implementacion, este test fallaria.
    """
    lista = claves(2000)

    def modulo(clave, cuantos):
        return f"peer{stable_hash(clave) % cuantos + 1}"

    movidas = sum(
        1 for clave in lista if modulo(clave, 3) != modulo(clave, 4)
    )

    assert movidas / len(lista) > 0.60


# ---------------------------------------------------------------------------
# Paso 4 — address_of (AC-13)
# ---------------------------------------------------------------------------

def test_address_of_devuelve_la_direccion_normalizada():
    """AC-13: choose_nodes da identidades; para hablar hace falta la URL."""
    ring = Ring(vnodes=16)
    ring.add_peer("peer2", "http://peer2:9002/")

    assert ring.address_of("peer2") == "http://peer2:9002"


def test_address_of_levanta_keyerror_si_el_peer_no_esta():
    """AC-13: no es un error HTTP, es un invariante roto nuestro."""
    with pytest.raises(KeyError):
        anillo("peer1").address_of("peer9")


# ---------------------------------------------------------------------------
# Tests limite — seccion 8 de la SPEC
# ---------------------------------------------------------------------------

def test_un_anillo_sin_peers_no_coloca_nada():
    """bisect sobre lista vacia: el borde clasico de esta estructura."""
    assert Ring().choose_nodes("/universidad") == []


@pytest.mark.parametrize("replicas", [0, -1])
def test_pedir_cero_replicas_o_menos_no_devuelve_nada(replicas):
    """Ninguna replica es una respuesta valida, no un error."""
    assert anillo("peer1", "peer2").choose_nodes("/x", replicas=replicas) == []


def test_la_clave_vacia_es_una_clave_valida():
    """Nada debe tratar la cadena vacia como caso especial."""
    ring = anillo("peer1", "peer2", "peer3")

    assert ring.choose_nodes("") == anillo("peer1", "peer2", "peer3").choose_nodes("")
    assert len(ring.choose_nodes("")) == 1


def test_las_claves_no_ascii_se_hashean_siempre_en_utf8():
    """Si dependiera de la codificacion del sistema, dos peers divergirian."""
    clave = "/universidad/ñandú🙂.pdf"
    esperado = int.from_bytes(
        hashlib.sha1(clave.encode("utf-8")).digest()[:8], "big"
    )

    assert stable_hash(clave) == esperado


def test_una_posicion_que_cae_sobre_un_nodo_virtual_es_de_ese_nodo():
    """El borde entre bisect_left y bisect_right.

    No se puede alcanzar desde una clave —habria que invertir SHA-1— y por eso
    nodes_at esta separada de choose_nodes.
    """
    ring = anillo("peer1", "peer2", "peer3", vnodes=16)

    for posicion, peer_id in ring.positions():
        assert ring.nodes_at(posicion)[0] == peer_id


def test_bajar_los_nodos_virtuales_estropea_el_reparto():
    """Denuncia a quien baje DFSHA_VNODES creyendo que el numero da igual.

    Los tres peers de la demo pasan AC-04 incluso con ocho nodos virtuales
    (28 % / 32 % / 40 %), asi que probar solo con ellos no delataria nada. Se
    barren treinta membresias de tres peers: con el valor decidido ninguna se
    sale de los limites de AC-04, y con ocho se salen varias.
    """
    lista = claves(1000)
    membresias = [
        (f"peer{3 * k + 1}", f"peer{3 * k + 2}", f"peer{3 * k + 3}")
        for k in range(30)
    ]

    def cuantas_violan_ac04(vnodes):
        violan = 0

        for miembros in membresias:
            duenios = reparto(anillo(*miembros, vnodes=vnodes), lista)
            cuotas = [
                sum(1 for d in duenios.values() if d == peer_id) / len(lista)
                for peer_id in miembros
            ]

            if min(cuotas) < 0.20 or max(cuotas) > 0.50:
                violan += 1

        return violan

    assert cuantas_violan_ac04(config.VNODES) == 0
    assert cuantas_violan_ac04(8) > 0


def test_un_solo_nodo_virtual_por_peer_sigue_funcionando():
    """DFSHA_VNODES es un parametro de calidad, no de correctitud."""
    ring = anillo("peer1", "peer2", "peer3", vnodes=1)

    assert len(ring.positions()) == 3
    assert ring.choose_nodes("/universidad", replicas=2) != []
