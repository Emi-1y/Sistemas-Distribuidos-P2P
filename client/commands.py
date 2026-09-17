import os
import posixpath
from pathlib import Path

import requests

from client import transfer


DEFAULT_PEER = "http://127.0.0.1:8000"
HEALTH_TIMEOUT = 2

# El peer con el que se esta hablando. Lo fija connect() al arrancar el REPL.
PEER_URL = None


def bootstrap_addresses():
    """Las direcciones de DFSHA_BOOTSTRAP, en orden.

    Se lee el mismo formato que en el servidor, `peer_id=url`, y se descarta el
    peer_id: el cliente no calcula el anillo, lo consulta. Una sola variable
    evita que un docker-compose mantenga dos listas que dicen lo mismo.
    """
    direcciones = []

    for trozo in os.environ.get("DFSHA_BOOTSTRAP", "").split(","):
        _, _, address = trozo.rpartition("=")
        address = address.strip().rstrip("/")

        if address:
            direcciones.append(address)

    return direcciones or [DEFAULT_PEER]


def connect():
    """El primer peer del bootstrap que responda /health, o None.

    Todos los peers son simetricos: vale cualquiera que este vivo. Conocer uno
    solo seria un punto unico de fallo.
    """
    global PEER_URL

    for address in bootstrap_addresses():
        try:
            respuesta = requests.get(
                f"{address}/health",
                timeout=HEALTH_TIMEOUT
            )

            if respuesta.status_code == 200:
                PEER_URL = address
                return address

        except requests.RequestException:
            continue

    return None


def build_path(current_path: str, target: str) -> str:
    if target.startswith("/"):
        path = target
    else:
        path = posixpath.join(current_path, target)

    path = posixpath.normpath(path)

    if not path.startswith("/"):
        path = "/" + path

    return path


def print_error(response):
    try:
        detail = response.json().get("detail", "Error desconocido")
    except ValueError:
        detail = "Error desconocido"

    print(f"Error {response.status_code}: {detail}")


def ls(current_path: str):
    try:
        response = requests.get(
            f"{PEER_URL}/files",
            params={"path": current_path}
        )

        if response.status_code == 200:
            data = response.json()
            items = data["items"]

            if not items:
                print("Directorio vacío")
                return

            for item in items:
                if item["type"] == "directory":
                    print(f"[DIR]  {item['name']}")
                else:
                    print(f"[FILE] {item['name']}")

        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def mkdir(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.post(
            f"{PEER_URL}/directories",
            json={"path": path}
        )

        if response.status_code == 200:
            print("Directorio creado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def rmdir(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.delete(
            f"{PEER_URL}/directories",
            params={"path": path}
        )

        if response.status_code == 200:
            print("Directorio eliminado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def rm(current_path: str, name: str):
    path = build_path(current_path, name)

    try:
        response = requests.delete(
            f"{PEER_URL}/files",
            params={"path": path}
        )

        if response.status_code == 200:
            print("Archivo eliminado correctamente")
        else:
            print_error(response)

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def peer_addresses() -> dict[str, str]:
    """El anillo como lo ve el peer conectado: `peer_id` a direccion.

    El cliente no calcula el anillo, lo consulta. Recalcularlo aqui exigiria
    leer tambien los nodos virtuales y la membresia, y el dia que las dos
    copias discreparan los bloques se escribirian en un peer y se buscarian en
    otro.

    Se pide una vez por operacion y no se cachea: la membresia cambia, y un
    mapa viejo manda bytes a una direccion muerta.
    """
    respuesta = requests.get(f"{PEER_URL}/ring")

    if respuesta.status_code != 200:
        raise transfer.TransferError("el peer no supo decir como esta el anillo")

    return {
        peer["peer_id"]: peer["address"]
        for peer in respuesta.json()["peers"]
    }


def _subida(origen, entrada, bloque, direcciones):
    """La tarea de subir un bloque a todos sus peers.

    Se sube a todos los que el plan nombra, no al primero: con factor de
    replica 3 el commit exige tres peers distintos, y subir solo al primero
    provocaria un 422 que nadie sabria leer. Hoy la lista tiene un elemento y
    el bucle da una vuelta.
    """
    def subir():
        datos = transfer.read_chunk(
            origen, bloque["index"], entrada["block_size"], bloque["size"]
        )
        suma = transfer.checksum(datos)
        puestos = []

        for peer_id in bloque["peers"]:
            respuesta = requests.put(
                f"{direcciones[peer_id]}/blocks"
                f"/{entrada['file_id']}/{bloque['index']}",
                data=datos,
                headers={"X-Block-Checksum": suma},
            )

            if respuesta.status_code != 201:
                raise transfer.TransferError(
                    f"el peer {peer_id} rechazo el bloque {bloque['index']} "
                    f"con un {respuesta.status_code}"
                )

            puestos.append(peer_id)

        # Lo que se reporta es donde quedo el bloque de verdad, no donde lo
        # planeo el allocate: el plan era una intencion, y el unico que conoce
        # el hecho es quien recibio los 201.
        return {
            "index": bloque["index"],
            "checksum": suma,
            "peers": puestos,
        }

    return subir


def upload_blocks(origen, entrada, direcciones) -> list[dict]:
    """Todos los bloques, en paralelo y directos a sus peers."""
    desconocidos = sorted({
        peer_id
        for bloque in entrada["blocks"]
        for peer_id in bloque["peers"]
        if peer_id not in direcciones
    })

    # Antes de subir un byte: si el anillo cambio entre el allocate y ahora,
    # escribir a ciegas dejaria bloques que nadie va a encontrar.
    if desconocidos:
        raise transfer.TransferError(
            f"el anillo ya no conoce a {', '.join(desconocidos)}"
        )

    return transfer.in_parallel([
        _subida(origen, entrada, bloque, direcciones)
        for bloque in entrada["blocks"]
    ])


def _bajada(entrada, bloque, direcciones):
    """La tarea de bajar un bloque, del primer peer suyo que conteste."""
    def bajar():
        for peer_id in bloque["peers"]:
            if peer_id not in direcciones:
                continue

            try:
                respuesta = requests.get(
                    f"{direcciones[peer_id]}/blocks"
                    f"/{entrada['file_id']}/{bloque['index']}"
                )
            except requests.RequestException:
                continue

            if respuesta.status_code != 200:
                continue

            datos = respuesta.content

            # La autoridad es la entrada, no la cabecera que devuelve el peer:
            # si el bloque se corrompio en su disco, la cabecera se corrompio
            # con el. Un peer que no contesta se sustituye por el siguiente;
            # uno que contesta con bytes que no cuadran es un problema que hay
            # que contar, no uno que haya que rodear.
            if transfer.checksum(datos) != bloque["checksum"]:
                raise transfer.TransferError(
                    f"el bloque {bloque['index']} que sirvio {peer_id} no "
                    "coincide con el checksum confirmado"
                )

            return datos

        raise transfer.TransferError(
            f"ningun peer sirvio el bloque {bloque['index']}"
        )

    return bajar


def download_blocks(entrada, direcciones) -> dict[int, bytes]:
    """Todos los bloques, en paralelo, indexados para poder reensamblar."""
    bloques = entrada["blocks"]

    piezas = transfer.in_parallel([
        _bajada(entrada, bloque, direcciones) for bloque in bloques
    ])

    return {
        bloque["index"]: datos
        for bloque, datos in zip(bloques, piezas)
    }


def send(current_path: str, filename: str):
    """Sube un archivo local al directorio remoto actual, en tres tiempos.

    `allocate` reserva la ruta y devuelve el plan; los bloques van **directos**
    a sus peers, sin pasar por el peer conectado; `commit` confirma con los
    checksums y los peers de verdad. El usuario no ve ninguno de los tres.
    """
    origen = Path(filename.strip()) if filename.strip() else None

    if origen is None or not origen.is_file():
        print(f"Error: no existe el archivo local '{filename}'")
        return

    destino = build_path(current_path, origen.name)
    reservado = False

    try:
        direcciones = peer_addresses()

        respuesta = requests.post(
            f"{PEER_URL}/files/allocate",
            json={"path": destino, "size": origen.stat().st_size},
        )

        if respuesta.status_code != 200:
            print_error(respuesta)
            return

        entrada = respuesta.json()
        reservado = True

        confirmados = upload_blocks(origen, entrada, direcciones)

        # El commit va despues de todos los 201: no hay commits parciales, y
        # esa es justo la regla que hace que un archivo a medias no exista.
        respuesta = requests.post(
            f"{PEER_URL}/files/commit",
            json={
                "path": entrada["path"],
                "file_id": entrada["file_id"],
                "blocks": confirmados,
            },
        )

        if respuesta.status_code != 200:
            print_error(respuesta)
            return

        print("Archivo subido correctamente")

    except transfer.TransferError as error:
        print(f"Error: {error}")

        # Sin commit el archivo no existe para nadie, pero la entrada pendiente
        # ocupa el nombre. Decir como se suelta evita que el usuario se quede
        # con una ruta que no puede reutilizar y no sabe por que.
        if reservado:
            print(
                f"No se subio nada en firme. 'rm {destino}' libera el nombre y "
                "borra los bloques que si llegaron."
            )

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def receive(current_path: str, filename: str):
    """Baja un archivo del DFS al directorio de trabajo local.

    `lookup` dice donde vive cada bloque, se bajan en paralelo, se verifica
    cada uno contra el checksum de la entrada y se reensambla **por indice**:
    en paralelo llegan desordenados, y juntarlos por orden de llegada daria un
    archivo corrupto que ningun checksum por bloque detectaria.
    """
    remoto = build_path(current_path, filename)
    destino = Path(Path(filename).name)

    if destino.exists():
        print(f"Error: ya existe un archivo local llamado '{destino.name}'")
        return

    try:
        direcciones = peer_addresses()

        respuesta = requests.get(
            f"{PEER_URL}/files/lookup",
            params={"path": remoto}
        )

        if respuesta.status_code != 200:
            print_error(respuesta)
            return

        entrada = respuesta.json()
        piezas = download_blocks(entrada, direcciones)
        contenido = transfer.assemble(piezas, entrada["blocks"])

        # Una sola escritura, despues de verificarlo todo: un fallo a mitad no
        # deja un archivo local a medias. O esta entero, o no esta.
        destino.write_bytes(contenido)

        print("Archivo descargado correctamente")

    except transfer.TransferError as error:
        print(f"Error: {error}")

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")


def change_directory(current_path: str, target: str) -> str:
    new_path = build_path(current_path, target)

    try:
        response = requests.get(
            f"{PEER_URL}/files",
            params={"path": new_path}
        )

        if response.status_code == 200:
            return new_path

        print_error(response)
        return current_path

    except requests.RequestException:
        print("Error: no se pudo conectar con el servidor")
        return current_path