"""El guion de la demostracion, desde el anfitrion — SPEC-06.

Levanta tres peers desde cero y recorre los cuatro actos sin que nadie toque
nada. Se ejecuta con `python demo/guion.py`.

**Orquesta y no calcula nada.** Todo lo que decide algo —quien es el dueno del
directorio, si el reparto esta bien, si lo que baja es lo que subio— vive en
`demo/actos.py` y se ejecuta **dentro** de la red de compose. Aqui solo se
levanta, se para, se arranca y se lee lo que los actos contestaron.

La frontera es esa y no otra por un motivo concreto: parar un peer es cosa del
anfitrion —`docker compose stop` no existe dentro de la red— y mover bytes es
cosa de dentro, porque el anillo habla nombres de contenedor y `send` los usa
para mandar cada bloque directo a su peer. Un guion que viviera solo en el
contenedor necesitaria el socket de Docker montado dentro; uno que viviera solo
fuera no podria subir un bloque.
"""

import subprocess
import sys


COMPOSE = ["docker", "compose"]

ANCHO = 72


def titular(texto: str) -> None:
    print()
    print("=" * ANCHO)
    print(f" {texto}")
    print("=" * ANCHO)


def paso(texto: str) -> None:
    print()
    print(f"--- {texto} " + "-" * max(0, ANCHO - len(texto) - 5))


def compose(*argumentos: str, capturar: bool = True) -> subprocess.CompletedProcess:
    """Un `docker compose ...` contra el proyecto de este repositorio."""
    return subprocess.run(
        COMPOSE + list(argumentos),
        capture_output=capturar,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def hay_docker() -> bool:
    """El error mas probable de quien clone el repo, dicho claro.

    Se pregunta con `compose ls` y no con `compose version`: la version la
    contesta el propio binario sin hablar con nadie, asi que responde igual de
    bien con Docker Desktop parado, y entonces el primer error que ve quien
    ejecuta esto es un `dial tcp ... connectex` de trescientos caracteres en
    mitad de la demo. `ls` pregunta por los proyectos, que ya es el demonio.
    """
    try:
        return compose("ls").returncode == 0
    except FileNotFoundError:
        return False


def acto(nombre: str, *argumentos: str, sin_dependencias: bool = False):
    """Ejecuta un acto **dentro** de la red y devuelve si fue bien y que dijo.

    `--no-deps` hace falta cuando hay peers parados: el servicio del cliente
    espera a que los tres esten sanos, que es lo que evita dormir a ciegas en
    los actos normales y lo que le impediria arrancar en el acto 4.
    """
    orden = ["run", "--rm", "-T"]

    if sin_dependencias:
        orden.append("--no-deps")

    resultado = compose(*orden, "cliente", "python", "-m", "demo.actos", nombre,
                        *argumentos)
    salida = resultado.stdout or ""

    print(salida.rstrip())

    if resultado.returncode != 0 and not salida.strip():
        print(resultado.stderr.rstrip())

    return resultado.returncode == 0, salida


def valor_de(salida: str, clave: str) -> str | None:
    """Lo que un acto dejo dicho para el siguiente, en una linea `clave=valor`.

    Llevar un valor de un acto a otro es orquestar, no calcular: cada
    `docker compose run` es un contenedor nuevo y no hay forma de que dos actos
    compartan una variable.
    """
    for linea in salida.splitlines():
        if linea.startswith(f"{clave}="):
            return linea.split("=", 1)[1].strip()

    return None


def ciclo_del_fallo() -> bool:
    """El acto 4, que es el unico que apaga cosas.

    Se para **todo menos el dueno del directorio de la demo**, y no un peer
    cualquiera: la colocacion depende de un `file_id` que se sortea en cada
    `allocate`, y con un solo peer parado la probabilidad de que no tuviera
    ningun bloque es `(2/3)^24` —una entre diecisiete mil—, que para una demo
    que se ejecuta delante de gente no es «nunca». Parando dos baja a
    `(1/3)^24`. Con el dueno vivo, el `allocate`, el `ls`, el `lookup` y el
    `rm` siguen funcionando: lo unico que se cae es subir los bloques, que es
    justo lo que hay que ensenar.
    """
    bien, salida = acto("dueno")

    if not bien:
        return False

    parar = (valor_de(salida, "parar") or "").split(",")
    dueno = valor_de(salida, "dueno")

    if not dueno or not all(parar):
        print("FALLO: el acto no dijo a quien hay que parar")
        return False

    print(f"  dueno de la demo: {dueno} — se paran {', '.join(parar)}")

    compose("stop", *parar)

    try:
        bien, salida = acto("fallo-envio", sin_dependencias=True)

        if not bien:
            return False

        file_id = valor_de(salida, "file_id")

        if file_id is None:
            print("FALLO: el acto no dijo de que archivo hablaba")
            return False

        paso("Acto 4b: el `rm` suelta el nombre y se lleva los bloques")
        bien, _ = acto("fallo-rm", file_id, sin_dependencias=True)

        if not bien:
            return False

    finally:
        # Pase lo que pase, los peers vuelven: un guion que deja el despliegue
        # a medias hace que el siguiente fallo sea de otra cosa.
        print()
        print(f"  levantando otra vez {', '.join(parar)}")
        compose("start", *parar)

    paso("Acto 4c: con los tres de vuelta, el mismo `send` funciona")

    # Sin `--no-deps`: el servicio del cliente espera a que los tres vuelvan a
    # estar sanos, que es la forma de esperar a que converjan sin dormir.
    bien, _ = acto("reintento")

    return bien


def main() -> int:
    if not hay_docker():
        print(
            "No responde `docker compose`. Arranca Docker Desktop y vuelve a "
            "intentarlo: el guion levanta tres peers en contenedores."
        )
        return 2

    titular("DFSha — demostracion del hito 2")

    # Se empieza tirando los volumenes siempre. Una demo que depende de lo que
    # quedo de la vez anterior no es repetible, y la segunda vez fallaria por
    # el 409 de un archivo que ya existe.
    print("\nTirando lo que quedara de antes (`down -v`) y levantando de cero...")
    compose("down", "-v")

    levantado = compose("up", "-d", "--build", "--wait")

    if levantado.returncode != 0:
        print("No se pudo levantar el despliegue:")
        print(levantado.stderr.rstrip())
        return 1

    print("Tres peers en 9001, 9002 y 9003, los tres sanos.")

    resultados = []

    paso("Acto 1: el anillo converge sin que nadie lo negocie")
    resultados.append(("Acto 1  convergencia", acto("convergencia")[0]))

    paso("Acto 2: un archivo repartido, preguntandoselo a los peers")
    resultados.append(("Acto 2  reparto", acto("enviar")[0]))

    paso("Acto 3: la lectura, byte a byte")
    resultados.append(("Acto 3  lectura", acto("recibir")[0]))

    paso("Acto 4a: con dos peers parados, el `send` falla y no deja nada")
    resultados.append(("Acto 4  ciclo del fallo", ciclo_del_fallo()))

    titular("RESUMEN")

    for nombre, bien in resultados:
        relleno = "." * max(3, 40 - len(nombre))
        print(f"  {nombre} {relleno} {'BIEN' if bien else 'MAL'}")

    todo_bien = all(bien for _, bien in resultados)

    print()
    print(f"TODO BIEN: {todo_bien}")
    print()
    print("Los peers siguen en pie. `docker compose down -v` no deja nada.")

    return 0 if todo_bien else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    sys.exit(main())
