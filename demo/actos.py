"""Los actos de la demostracion, ejecutados **dentro** de la red — SPEC-06.

El guion tiene dos mitades y la frontera no es arbitraria: parar un peer es
cosa del anfitrion —`docker compose stop` no existe dentro de la red— y mover
bytes es cosa de dentro, porque el anillo habla nombres de contenedor y el
cliente usa sus direcciones para mandar cada bloque. Un guion que viviera solo
en el contenedor necesitaria el socket de Docker montado dentro, que es un
permiso enorme para un proyecto de clase; uno que viviera solo fuera no podria
subir un bloque.

Asi que aqui vive lo que hay que ejecutar desde dentro, y en `guion.py` lo que
orquesta desde fuera. Este modulo no usa el CLI de otra manera que el usuario:
llama a las mismas funciones de `client.commands` que teclea una persona.
"""

import contextlib
import hashlib
import io
import os
import random
import sys
import tempfile
import textwrap

import requests

from client import commands
from server import ring


MARCA = "X"
VACIO = "·"

# El directorio de la demo y los dos archivos que se suben: uno para el reparto
# y la lectura, otro para el ciclo del fallo. Los dos cuelgan del mismo
# directorio a proposito: el acto 4 apaga todo menos su dueno, y para que el
# `allocate` siga funcionando el directorio tiene que existir ya.
DIRECTORIO = "/demo"
ARCHIVO = "reparto.bin"
ARCHIVO_DEL_FALLO = "fallo.bin"

# 1,5 MiB con DFSHA_BLOCK_SIZE=65536 son 24 bloques. La palanca del reparto es
# el numero de bloques, no el tamano: con 24, la probabilidad de que algun peer
# se quede sin ninguno no llega al 0,018 %.
TAMANO = 1536 * 1024
BLOQUES_ESPERADOS = 24

# El contenido se deriva de una semilla fija, asi que es el mismo en cada acto
# y en cada ejecucion. Es lo que permite que el acto 3 compare con el original
# sin que nadie tenga que llevarse un archivo de un contenedor a otro: cada
# `docker compose run` es un contenedor nuevo y no comparte disco con el
# anterior.
SEMILLA = 20260917

# El reparto no es un tercio exacto y no tiene por que serlo. Sin esta linea la
# demo parece que reparte mal, que es justo la lectura equivocada.
NOTA_DEL_REPARTO = (
    "No es un tercio exacto y no tiene que serlo: la colocacion es un hash, no "
    "un turno, y con dos docenas de claves la varianza se ve."
)


def reparto(file_id: str, bloques, direcciones: dict[str, str]) -> dict[int, list[str]]:
    """Quien tiene cada bloque, **preguntandoselo a los peers**.

    Se le pregunta a los tres por cada bloque, no solo al que dice `lookup`:
    preguntar al que ya sabemos demostraria menos. Un `200` prueba que el
    bloque esta *y* que se puede leer; un `404`, que ese peer no lo tiene.

    La alternativa —listar `storage/<file_id>/` en los tres— obligaria a montar
    los tres volumenes solo para mirarlos, y demostraria menos: que hay un
    archivo en un disco. Aqui contesta la propia API.

    Informa y no juzga: un bloque con dos tenedores o con ninguno sale tal
    cual, y quien dice si eso esta mal es `defectos`.
    """
    tenedores: dict[int, list[str]] = {}

    for indice in bloques:
        tenedores[indice] = [
            peer_id
            for peer_id in sorted(direcciones)
            if _tiene(direcciones[peer_id], file_id, indice)
        ]

    return tenedores


def _tiene(address: str, file_id: str, indice: int) -> bool:
    """Si ese peer sirve ese bloque. Un peer caido no lo tiene, a efectos del
    dibujo: no contestar y contestar 404 se ven igual desde fuera, y el acto 4
    apaga peers a proposito."""
    try:
        respuesta = requests.get(f"{address}/blocks/{file_id}/{indice}")
    except requests.RequestException:
        return False

    return respuesta.status_code == 200


def defectos(reparto: dict[int, list[str]], segun_lookup, peers) -> list[str]:
    """Todo lo que el acto 2 afirma, comprobado en un solo sitio.

    Devuelve los problemas en texto, listos para imprimir, y una lista vacia
    cuando no hay ninguno. Que sea una lista y no un booleano es lo que permite
    que el guion **diga que fallo** en vez de solo fallar.
    """
    problemas = []

    for indice in sorted(reparto):
        tiene = reparto[indice]
        esperado = list(segun_lookup.get(indice, []))

        if not tiene:
            problemas.append(f"el bloque {indice} no lo sirve ningun peer")
        elif len(tiene) > 1:
            # Con factor de replica 1, dos tenedores no son redundancia: son
            # una escritura que fue a parar donde no le tocaba.
            problemas.append(
                f"el bloque {indice} lo sirven {len(tiene)} peers: {', '.join(tiene)}"
            )
        elif tiene != esperado:
            # El enlace entre los dos planos: los bytes estan donde los
            # metadatos dicen, o el `receive` de otro dia no los encontrara.
            problemas.append(
                f"el bloque {indice} lo sirve {tiene[0]} y `lookup` dice "
                f"{', '.join(esperado) or 'nadie'}"
            )

    for peer_id in peers:
        if not any(peer_id in tiene for tiene in reparto.values()):
            # No es un fallo del sistema: es una demo que no demuestra lo que
            # dice. Con 24 bloques pasa una vez de cada cinco mil.
            problemas.append(f"{peer_id} no guarda ni un bloque: el reparto no se ve")

    return problemas


def matriz(reparto: dict[int, list[str]], peers) -> str:
    """El dibujo bloque x peer, y la cuenta por peer. Solo dibuja.

    Separarlo de quien pregunta es lo que deja probar el dibujo sin red y la
    pregunta sin mirar el dibujo.
    """
    columnas = list(peers)
    lineas = ["bloque  " + " ".join(f"{peer_id:>5}" for peer_id in columnas)]

    for indice in sorted(reparto):
        celdas = [
            MARCA if peer_id in reparto[indice] else VACIO
            for peer_id in columnas
        ]
        lineas.append(f"{indice:>6}  " + " ".join(f"{celda:>5}" for celda in celdas))

    cuenta = {
        peer_id: sum(1 for tiene in reparto.values() if peer_id in tiene)
        for peer_id in columnas
    }
    lineas.append(
        "reparto: " + "  ".join(f"{peer_id}={n}" for peer_id, n in cuenta.items())
    )

    return "\n".join(lineas)


def dueno_de(directorio: str) -> str:
    """El peer al que le toca el contenido de ese directorio. Sin red.

    Importa `server.ring` a proposito: es un guion, no el cliente. El anillo es
    una funcion pura de la membresia, asi que esto se puede calcular **antes**
    de apagar nada —que es justo lo que el acto 4 necesita— y no hay que
    preguntarselo a un peer que quiza ya este parado.

    Calcularlo con una copia propia seria peor que preguntarlo: el dia que las
    dos discreparan, la demo pararia al peer equivocado.
    """
    return ring.choose_nodes(directorio)[0]


# ---------------------------------------------------------------------------
# El archivo de la demo, y con quien se habla
# ---------------------------------------------------------------------------

def contenido() -> bytes:
    """Los mismos 1,5 MiB en cada acto y en cada ejecucion.

    De una semilla fija y no de `urandom`: el acto 3 compara lo que baja con el
    original, y los dos actos corren en contenedores distintos que no comparten
    disco. Con una semilla, el original se vuelve a generar donde haga falta.
    """
    return random.Random(SEMILLA).randbytes(TAMANO)


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def conectar() -> dict[str, str]:
    """Conecta el CLI y devuelve el anillo tal y como lo ve el peer conectado.

    Vale cualquier peer vivo: son simetricos. En el acto 4 esto es literal —los
    dos primeros del bootstrap estan parados y `connect` cae en el tercero sin
    que nadie se lo diga.
    """
    peer = commands.connect()

    if peer is None:
        raise SystemExit("no respondio ningun peer del bootstrap")

    print(f"cliente conectado a {peer}")

    return commands.peer_addresses()


def entrada_de(archivo: str):
    """La entrada de metadatos, o el codigo de estado si no hay ninguna."""
    respuesta = requests.get(
        f"{commands.PEER_URL}/files/lookup",
        params={"path": f"{DIRECTORIO}/{archivo}"},
    )

    return respuesta.json() if respuesta.status_code == 200 else respuesta.status_code


# ---------------------------------------------------------------------------
# Acto 1 — la convergencia se ensena, no se provoca
# ---------------------------------------------------------------------------

def convergencia() -> bool:
    """Los tres `ring_version`, y que son el mismo.

    Los tres peers estan en su propio `DFSHA_BOOTSTRAP`, asi que colocan a los
    otros dos al arrancar y **ninguno emite una sola peticion**. Que las tres
    versiones coincidan es la prueba de que el anillo es una funcion pura de la
    membresia y no un acuerdo negociado: un contador local no lo garantizaria.
    """
    versiones = {}

    for peer in ring.LOCAL.peers():
        salud = requests.get(f"{peer['address']}/health").json()
        anillo = requests.get(f"{peer['address']}/ring").json()
        miembros = [otro["peer_id"] for otro in anillo["peers"]]

        versiones[peer["peer_id"]] = (salud["ring_version"], tuple(miembros))

        print(
            f"  {peer['peer_id']:<6} ring_version={salud['ring_version']}  "
            f"ve a {', '.join(miembros)}"
        )

    distintas = set(versiones.values())

    if len(distintas) != 1:
        print(f"FALLO: los tres no ven el mismo anillo: {versiones}")
        return False

    print(
        "Los tres coinciden, y ninguno ha tenido que preguntarselo a nadie: "
        "el anillo se deriva del bootstrap."
    )

    return True


# ---------------------------------------------------------------------------
# Acto 2 — el reparto, preguntandoselo a los peers
# ---------------------------------------------------------------------------

def enviar() -> bool:
    """Sube el archivo de la demo y dibuja la matriz bloque x peer."""
    direcciones = conectar()

    # Antes de subir nada: si el archivo ya estuviera, el `allocate` daria 409,
    # el `send` fallaria y todo lo que este acto comprueba despues seguiria
    # cuadrando —sobre el archivo de la vez anterior—. El acto diria que fue
    # bien. Por eso el guion empieza con `down -v`, y por eso esto se mira.
    if isinstance(entrada_de(ARCHIVO), dict):
        print(
            f"FALLO: {DIRECTORIO}/{ARCHIVO} ya existe de una ejecucion anterior. "
            "El guion empieza con `docker compose down -v` justo para que no pase."
        )
        return False

    commands.mkdir("/", DIRECTORIO.lstrip("/"))

    origen = os.path.join(tempfile.mkdtemp(), ARCHIVO)

    with open(origen, "wb") as archivo:
        archivo.write(contenido())

    print(f"subiendo {ARCHIVO} ({TAMANO} bytes) a {DIRECTORIO}")
    commands.send(DIRECTORIO, origen)

    entrada = entrada_de(ARCHIVO)

    if not isinstance(entrada, dict):
        print(f"FALLO: `lookup` responde {entrada}: el archivo no se subio")
        return False

    bloques = [bloque["index"] for bloque in entrada["blocks"]]

    if len(bloques) != BLOQUES_ESPERADOS:
        print(
            f"FALLO: el archivo tiene {len(bloques)} bloques y la demo cuenta "
            f"con {BLOQUES_ESPERADOS}: mira DFSHA_BLOCK_SIZE"
        )
        return False

    segun_lookup = {bloque["index"]: bloque["peers"] for bloque in entrada["blocks"]}
    tenedores = reparto(entrada["file_id"], bloques, direcciones)

    print()
    print(matriz(tenedores, sorted(direcciones)))
    print(NOTA_DEL_REPARTO)
    print()

    problemas = defectos(tenedores, segun_lookup, sorted(direcciones))

    for problema in problemas:
        print(f"FALLO: {problema}")

    return not problemas


# ---------------------------------------------------------------------------
# Acto 3 — la lectura, byte a byte
# ---------------------------------------------------------------------------

def recibir() -> bool:
    """Baja el archivo y compara su SHA-256 con el del original."""
    conectar()

    os.chdir(tempfile.mkdtemp())

    commands.receive(DIRECTORIO, ARCHIVO)

    try:
        bajado = open(ARCHIVO, "rb").read()
    except FileNotFoundError:
        print("FALLO: no se descargo ningun archivo")
        return False

    original = sha256(contenido())
    copia = sha256(bajado)

    print(f"  original  {original}  ({TAMANO} bytes)")
    print(f"  bajado    {copia}  ({len(bajado)} bytes)")

    if original != copia:
        print("FALLO: lo que baja no es lo que subio")
        return False

    print("Identico byte a byte.")

    return True


# ---------------------------------------------------------------------------
# Acto 4 — el ciclo del fallo
# ---------------------------------------------------------------------------

class Espia:
    """Deja pasar todo lo del CLI y se queda con el `file_id` del `allocate`.

    No cambia el cliente ni lo que hace: envuelve el `requests` que usa, mira
    las respuestas de paso y las devuelve tal cual.

    Existe porque una entrada **pendiente** no es visible desde fuera —`lookup`
    solo devuelve las confirmadas— y sin su `file_id` no se le puede preguntar
    a un peer por sus bloques. Y eso es justo lo que el acto tiene que
    comprobar: que el `rm` se llevo los bloques que si llegaron a subir. La
    alternativa seria buscar el `file_id` en los logs de los peers, que es
    frágil y hace que el guion dependa del formato de un log.
    """

    def __init__(self, real):
        self._real = real
        self.file_id = None

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    def post(self, url, **kwargs):
        respuesta = self._real.post(url, **kwargs)

        if url.endswith("/files/allocate") and respuesta.status_code == 200:
            self.file_id = respuesta.json()["file_id"]

        return respuesta


def _lo_que_dijo_el_cli(funcion, *args) -> str:
    """Ejecuta una operacion del CLI y devuelve lo que imprimio, imprimiendolo.

    El CLI no levanta excepciones ni devuelve nada: le cuenta al usuario como
    fue. Para afirmar que un `send` fallo con un 409 hay que leer lo mismo que
    lee el usuario, y para que la demo siga siendo una demo hay que enseñarlo.
    """
    papel = io.StringIO()

    with contextlib.redirect_stdout(papel):
        funcion(*args)

    dicho = papel.getvalue()
    print(textwrap.indent(dicho.rstrip(), "  | "))

    return dicho


def _archivo_del_fallo() -> str:
    origen = os.path.join(tempfile.mkdtemp(), ARCHIVO_DEL_FALLO)

    with open(origen, "wb") as archivo:
        archivo.write(contenido())

    return origen


def dueno() -> bool:
    """El dueno del directorio de la demo, para que el guion pare a los otros.

    Es lo unico que el guion necesita saber antes de apagar nada, y se calcula
    sin red: parar un peer cualquiera casi siempre bastaria, pero «casi» no es
    una demo repetible. Parando los dos que no son duenos, la probabilidad de
    que el peer que queda no tenga ningun bloque es `(1/3)^24`: una entre
    doscientos ochenta y dos mil millones.
    """
    elegido = dueno_de(DIRECTORIO)
    otros = [peer["peer_id"] for peer in ring.LOCAL.peers() if peer["peer_id"] != elegido]

    print(f"dueno={elegido}")
    print(f"parar={','.join(otros)}")

    return True


def fallo_envio() -> bool:
    """Con los otros dos parados: el `send` falla y no deja un archivo a medias.

    Lo que sigue en pie es todo lo que depende del dueno del directorio —el
    `allocate`, el `ls`, el `lookup` y el `rm`—, y lo unico que se cae es subir
    los bloques. Que es exactamente lo que hay que ensenar.
    """
    conectar()

    espia = Espia(commands.requests)
    commands.requests = espia

    try:
        print(f"send {ARCHIVO_DEL_FALLO} con dos peers parados:")
        dicho = _lo_que_dijo_el_cli(commands.send, DIRECTORIO, _archivo_del_fallo())

        if "Archivo subido correctamente" in dicho:
            print("FALLO: el send funciono, y con dos peers parados no deberia")
            return False

        if espia.file_id is None:
            print("FALLO: el `allocate` ni siquiera llego a reservar el nombre")
            return False

        items = requests.get(
            f"{commands.PEER_URL}/files", params={"path": DIRECTORIO}
        ).json()["items"]
        nombres = [item["name"] for item in items]

        print(f"  ls {DIRECTORIO} -> {', '.join(nombres) or 'vacio'}")

        if ARCHIVO_DEL_FALLO in nombres:
            print("FALLO: un archivo sin commit no puede aparecer en el ls")
            return False

        estado = entrada_de(ARCHIVO_DEL_FALLO)
        print(f"  lookup {DIRECTORIO}/{ARCHIVO_DEL_FALLO} -> {estado}")

        if estado != 404:
            print("FALLO: sin commit, `lookup` tiene que dar 404")
            return False

        # El nombre si esta ocupado por la entrada pendiente, y por eso repetir
        # el send da 409: es la unica huella que deja un envio a medias.
        print("repetir el mismo send:")
        repetido = _lo_que_dijo_el_cli(
            commands.send, DIRECTORIO, _archivo_del_fallo()
        )

        if "409" not in repetido:
            print("FALLO: el nombre tendria que estar ocupado y dar 409")
            return False

    finally:
        commands.requests = espia._real

    print(f"file_id={espia.file_id}")

    return True


def fallo_rm(file_id: str | None = None) -> bool:
    """El `rm` suelta el nombre y se lleva los bloques que si subieron.

    Los otros dos peers siguen parados, asi que todo lo que llego a subirse
    esta en el superviviente: se le pregunta a el, bloque a bloque, antes y
    despues.
    """
    if file_id is None:
        print("FALLO: hace falta el file_id que imprimio `fallo-envio`")
        return False

    conectar()

    vivo = dueno_de(DIRECTORIO)
    direcciones = {vivo: ring.LOCAL.address_of(vivo)}
    indices = list(range(BLOQUES_ESPERADOS))

    antes = reparto(file_id, indices, direcciones)
    subidos = [indice for indice, tiene in antes.items() if tiene]

    print(f"  {vivo} sirve {len(subidos)} de los {BLOQUES_ESPERADOS} bloques: {subidos}")

    if not subidos:
        # Sin un solo bloque arriba, comprobar que el `rm` los borra no
        # demuestra nada: pasaria igual sin borrar nada.
        print("FALLO: no subio ningun bloque, asi que el borrado no prueba nada")
        return False

    print(f"rm {DIRECTORIO}/{ARCHIVO_DEL_FALLO}:")
    dicho = _lo_que_dijo_el_cli(commands.rm, DIRECTORIO, ARCHIVO_DEL_FALLO)

    if "Archivo eliminado correctamente" not in dicho:
        print("FALLO: el rm no respondio 200")
        return False

    despues = reparto(file_id, indices, direcciones)
    quedan = [indice for indice, tiene in despues.items() if tiene]

    print(f"  {vivo} sirve ahora {len(quedan)} bloques de ese archivo")

    if quedan:
        print(f"FALLO: el rm dejo bloques sin borrar: {quedan}")
        return False

    # Y el nombre vuelve a estar libre, que es la otra mitad de lo que el `rm`
    # promete cuando el CLI lo sugiere.
    items = requests.get(
        f"{commands.PEER_URL}/files", params={"path": DIRECTORIO}
    ).json()["items"]

    if ARCHIVO_DEL_FALLO in [item["name"] for item in items]:
        print("FALLO: el nombre sigue ocupado despues del rm")
        return False

    print("El nombre esta libre y no queda un solo bloque.")

    return True


def reintento() -> bool:
    """Con los tres peers de vuelta, el mismo `send` funciona."""
    direcciones = conectar()

    print("el mismo send, con los tres peers vivos:")
    dicho = _lo_que_dijo_el_cli(commands.send, DIRECTORIO, _archivo_del_fallo())

    if "Archivo subido correctamente" not in dicho:
        print("FALLO: el send sigue sin funcionar con los tres peers vivos")
        return False

    entrada = entrada_de(ARCHIVO_DEL_FALLO)

    if not isinstance(entrada, dict):
        print(f"FALLO: `lookup` responde {entrada} despues de un send que dijo que si")
        return False

    bloques = [bloque["index"] for bloque in entrada["blocks"]]
    segun_lookup = {bloque["index"]: bloque["peers"] for bloque in entrada["blocks"]}
    tenedores = reparto(entrada["file_id"], bloques, direcciones)

    problemas = defectos(tenedores, segun_lookup, sorted(direcciones))

    for problema in problemas:
        print(f"FALLO: {problema}")

    if problemas:
        return False

    print(f"Los {len(bloques)} bloques estan repartidos y confirmados.")

    return True


# ---------------------------------------------------------------------------
# Cada acto es un subcomando
# ---------------------------------------------------------------------------

ACTOS = {
    "convergencia": convergencia,
    "enviar": enviar,
    "recibir": recibir,
    "dueno": dueno,
    "fallo-envio": fallo_envio,
    "fallo-rm": fallo_rm,
    "reintento": reintento,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ACTOS:
        print(f"uso: python -m demo.actos [{' | '.join(ACTOS)}]")
        return 2

    return 0 if ACTOS[argv[1]](*argv[2:]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
