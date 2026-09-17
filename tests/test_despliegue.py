"""Los invariantes del despliegue — SPEC-06.

El compose es el unico invariante del despliegue que se puede fijar **sin
Docker**, y por eso se escribe antes que el compose. Protege el error mas caro
y mas silencioso de los tres peers: que dos compartan algo que tiene que ser
suyo. Dos peers con la misma raiz de datos se pisan los bloques y el sistema
parece funcionar hasta que uno borra los del otro; dos con el mismo
`DFSHA_PEER_ID` se creen el mismo nodo del anillo.

Lo que aqui se lee es el YAML ya resuelto —las anclas incluidas—, que es lo que
Docker va a ver, y no el texto. La unica excepcion es el test del bootstrap
escrito tres veces, que es precisamente una afirmacion sobre el texto.
"""

from pathlib import Path

import pytest
import yaml

from server import config


RAIZ = Path(__file__).resolve().parent.parent
COMPOSE = RAIZ / "docker-compose.yml"

PEERS = ("peer1", "peer2", "peer3")

# 1,5 MiB troceados asi son 24 bloques: suficientes para que el reparto se vea
# y para que la probabilidad de que un peer se quede vacio sea despreciable.
TAMANO_DE_LA_DEMO = 1536 * 1024


# ---------------------------------------------------------------------------
# Leer el compose
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def texto() -> str:
    assert COMPOSE.exists(), (
        f"no existe {COMPOSE.name}: el despliegue de la SPEC-06 no esta escrito"
    )

    return COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def servicios(texto) -> dict:
    return yaml.safe_load(texto)["services"]


@pytest.fixture(scope="module")
def peers(servicios) -> dict:
    faltan = [nombre for nombre in PEERS if nombre not in servicios]

    assert not faltan, f"el compose no declara {', '.join(faltan)}"

    return {nombre: servicios[nombre] for nombre in PEERS}


def entorno(servicio: dict) -> dict[str, str]:
    """Las variables del servicio, en forma de mapa y como texto.

    Compose admite las dos formas, `CLAVE: valor` y `- CLAVE=valor`, y los
    numeros pueden venir citados o no. Lo que llega al proceso siempre es
    texto, asi que aqui se comparan textos.
    """
    variables = servicio.get("environment", {})

    if isinstance(variables, list):
        variables = dict(par.partition("=")[::2] for par in variables)

    return {clave: str(valor) for clave, valor in variables.items()}


def volumenes(servicio: dict) -> dict[str, str]:
    """Origen del montaje a destino, en las dos formas que admite compose."""
    montajes = {}

    for montaje in servicio.get("volumes", []):
        if isinstance(montaje, dict):
            montajes[montaje.get("source")] = montaje.get("target")
        else:
            origen, _, destino = str(montaje).partition(":")
            montajes[origen] = destino.split(":")[0]

    return montajes


def puertos_publicados(servicio: dict) -> list[str]:
    publicados = []

    for puerto in servicio.get("ports", []):
        if isinstance(puerto, dict):
            publicados.append(str(puerto.get("published")))
        else:
            publicados.append(str(puerto).split(":")[0])

    return publicados


def orden(servicio: dict) -> str:
    comando = servicio.get("command", "")

    return comando if isinstance(comando, str) else " ".join(comando)


# ---------------------------------------------------------------------------
# AC-06 — lo que los tres comparten y lo que no
# ---------------------------------------------------------------------------

def test_los_tres_peers_comparten_exactamente_el_mismo_bootstrap(peers):
    """La membresia es la misma lista para los tres o no hay un anillo.

    El `ring_version` se deriva de los `peer_id`, asi que un bootstrap
    distinto en un peer no da un error: da dos anillos que colocan las claves
    en sitios distintos, y el archivo se sube a un peer y se busca en otro.
    """
    bootstraps = {
        nombre: entorno(servicio).get("DFSHA_BOOTSTRAP")
        for nombre, servicio in peers.items()
    }

    assert all(bootstraps.values()), "algun peer no tiene DFSHA_BOOTSTRAP"
    assert len(set(bootstraps.values())) == 1, bootstraps


def test_el_bootstrap_nombra_a_los_tres_con_la_direccion_que_cada_uno_anuncia(peers):
    """Se lee con el mismo parser del peer, no con uno de mentira.

    La direccion que anuncia el bootstrap y la que el peer pone en
    `DFSHA_ADDRESS` tienen que ser la misma cadena: el anillo guarda
    direcciones y el cliente las usa para mandar bloques. Si discreparan, los
    otros dos hablarian con una direccion que su dueno no reconoce como suya.
    """
    crudo = entorno(peers["peer1"])["DFSHA_BOOTSTRAP"]
    bootstrap = dict(config.parse_bootstrap(crudo))

    assert sorted(bootstrap) == sorted(PEERS)

    for nombre, servicio in peers.items():
        variables = entorno(servicio)

        assert bootstrap[variables["DFSHA_PEER_ID"]] == variables["DFSHA_ADDRESS"], (
            nombre
        )


@pytest.mark.parametrize("variable", ["DFSHA_PEER_ID", "DFSHA_ADDRESS"])
def test_ningun_par_de_peers_comparte_su_identidad(peers, variable):
    valores = [entorno(servicio).get(variable) for servicio in peers.values()]

    assert len(set(valores)) == len(PEERS), f"{variable} repetido: {valores}"


def test_ningun_par_de_peers_comparte_puerto(peers):
    publicados = [
        puerto for servicio in peers.values()
        for puerto in puertos_publicados(servicio)
    ]

    assert len(publicados) == len(PEERS), publicados
    assert len(set(publicados)) == len(PEERS), f"puerto repetido: {publicados}"


def test_ningun_par_de_peers_comparte_volumen(peers):
    """El fallo silencioso: dos peers escribiendo bloques en el mismo disco.

    No da error en ningun sitio. El `rm` de un archivo borra el directorio
    entero de ese `file_id` en el peer, y si el disco es compartido se lleva
    por delante los bloques del otro.
    """
    origenes = [
        origen for servicio in peers.values() for origen in volumenes(servicio)
    ]

    assert len(origenes) == len(PEERS), origenes
    assert len(set(origenes)) == len(PEERS), f"volumen repetido: {origenes}"


def test_cada_peer_monta_su_volumen_justo_en_su_raiz_de_datos(peers):
    """Montar en otro sitio no falla: escribe en la capa del contenedor y los
    datos se pierden con el `down`, que es lo contrario de lo que la SPEC-04
    prometio sobre la persistencia.
    """
    for nombre, servicio in peers.items():
        raiz = entorno(servicio)["DFSHA_STORAGE_ROOT"]

        assert list(volumenes(servicio).values()) == [raiz], nombre


def test_los_tres_peers_solo_se_diferencian_en_su_identidad(peers):
    """Todo lo demas —bootstrap, raiz, tamano de bloque, vnodes— es comun.

    Son simetricos: esa es la propiedad central del sistema. Un peer con otro
    `DFSHA_VNODES` calcula otro anillo con la misma membresia, y el
    `ring_version` no lo delata porque solo mira los `peer_id`.
    """
    comunes = {
        nombre: {
            clave: valor
            for clave, valor in entorno(servicio).items()
            if clave not in ("DFSHA_PEER_ID", "DFSHA_ADDRESS")
        }
        for nombre, servicio in peers.items()
    }

    for nombre, variables in comunes.items():
        assert variables == comunes["peer1"], f"{nombre} se desvia: {variables}"


# ---------------------------------------------------------------------------
# Que lo que se levanta sea el mismo programa, y sea alcanzable
# ---------------------------------------------------------------------------

def test_los_cuatro_servicios_son_la_misma_imagen(servicios):
    """Tres Dockerfiles dejarian de garantizar la simetria por construccion:
    bastaria que uno se quedara sin actualizar.

    Y no basta con que compartan el `build`: si cada servicio etiqueta su
    propia imagen, `up --build` reconstruye las de los peers y deja la del
    cliente —que no esta en el perfil por defecto— con el codigo de antes. Una
    etiqueta comun es lo que hace que «una sola imagen» sea cierto y no una
    intencion.
    """
    etiquetas = {
        nombre: servicio.get("image") for nombre, servicio in servicios.items()
    }

    assert all(etiquetas.values()), f"algun servicio no nombra su imagen: {etiquetas}"
    assert len(set(etiquetas.values())) == 1, etiquetas


def test_cada_peer_escucha_en_el_puerto_de_su_direccion(peers):
    """El clasico peer3 escuchando en el puerto de peer2.

    No da un error al arrancar: da un peer que responde en una direccion que
    no es la que anuncio en el anillo.
    """
    for nombre, servicio in peers.items():
        puerto = entorno(servicio)["DFSHA_ADDRESS"].rsplit(":", 1)[-1]

        assert f"--port {puerto}" in orden(servicio), nombre
        assert puertos_publicados(servicio) == [puerto], nombre


def test_todos_los_peers_escuchan_en_todas_las_interfaces(peers):
    """Dentro de un contenedor, `127.0.0.1` es el propio contenedor: no
    llegaria nadie, ni el peer de al lado ni el anfitrion.
    """
    for nombre, servicio in peers.items():
        assert "--host 0.0.0.0" in orden(servicio), nombre


def test_cada_peer_se_mira_a_si_mismo_con_python_y_no_con_curl(peers):
    """Instalar un paquete en la imagen solo para preguntar si el peer esta
    vivo, cuando dentro ya hay un interprete con `urllib`, es peso sin motivo.
    Y el healthcheck no es decorativo: es lo que deja que el cliente espere a
    los tres sin dormir a ciegas.
    """
    for nombre, servicio in peers.items():
        prueba = str(servicio.get("healthcheck", {}).get("test", ""))

        assert "python" in prueba, f"{nombre} no se comprueba con python: {prueba}"
        assert "curl" not in prueba, f"{nombre} usa curl: {prueba}"


def test_el_bootstrap_no_esta_escrito_a_mano_tres_veces(texto, peers):
    """Es lo que impide que las tres copias se separen. Se mira el texto a
    proposito: el YAML resuelto no distingue un ancla de tres copias iguales,
    y la diferencia esta justo en lo que pasa cuando alguien edita una.
    """
    valor = entorno(peers["peer1"])["DFSHA_BOOTSTRAP"]

    assert texto.count(valor) == 1, "el bootstrap esta repetido en el YAML"


# ---------------------------------------------------------------------------
# El cliente y la topologia de la demo
# ---------------------------------------------------------------------------

def test_el_cliente_entra_en_la_red_y_espera_a_que_los_tres_esten_sanos(servicios):
    """Todo lo que mueve bytes se ejecuta dentro de la red, porque el anillo
    habla nombres de contenedor: por eso hay un servicio para entrar.
    """
    assert "cliente" in servicios, "el compose no trae con que entrar en la red"

    cliente = servicios["cliente"]

    assert entorno(cliente).get("DFSHA_BOOTSTRAP") == (
        entorno(servicios["peer1"])["DFSHA_BOOTSTRAP"]
    )
    assert not cliente.get("ports"), "el cliente no publica puertos"
    assert not volumenes(cliente), "el cliente no tiene datos que guardar"

    # No arranca con `up`: se invoca con `run`. Sin perfil, `up` deja un REPL
    # sentado en un contenedor al que nadie esta conectado.
    assert cliente.get("profiles"), "el cliente arrancaria con `up`"

    espera = cliente.get("depends_on", {})

    assert sorted(espera) == sorted(PEERS), espera
    assert all(
        condicion.get("condition") == "service_healthy"
        for condicion in espera.values()
    ), espera


def test_el_tamano_de_bloque_del_despliegue_da_24_bloques(peers):
    """La palanca del reparto es el numero de bloques, no el tamano del
    archivo: con 24 la probabilidad de que algun peer se quede vacio no llega
    al 0,018 %, y la demo deja de depender de la suerte.
    """
    tamano = int(entorno(peers["peer1"])["DFSHA_BLOCK_SIZE"])

    assert TAMANO_DE_LA_DEMO / tamano == 24


# ---------------------------------------------------------------------------
# La imagen no instala lo que nadie importa
# ---------------------------------------------------------------------------

def test_requirements_no_instala_lo_que_nadie_importa():
    """`python-multipart` entro con `POST /files/upload`, que la SPEC-04
    retiro. Una imagen que lo sigue instalando es una invitacion a que alguien
    reintroduzca las subidas multiparte porque «ya esta».
    """
    paquetes = {
        linea.strip().lower()
        for linea in (RAIZ / "requirements.txt").read_text().splitlines()
        if linea.strip()
    }

    assert "python-multipart" not in paquetes
    assert "pyyaml" in paquetes, "los tests leen el compose con pyyaml"
