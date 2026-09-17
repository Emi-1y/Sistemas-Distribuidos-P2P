"""El troceado y el reensamblado. Sin una sola peticion.

Este modulo decide **que trozo es cada bloque y como se vuelven a juntar**;
`commands` decide a quien se le pide. Es la misma separacion que hay en el
servidor entre `metadata` y `routing`, y por el mismo motivo: la aritmetica de
los offsets se prueba entera sin levantar nada, y cuando algo falla en la otra
mitad se sabe que el fallo es de la red.

Por eso aqui no se importa `requests`.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# Cuantos bloques viajan a la vez. El tope existe porque no tenerlo no es mas
# rapido: un archivo de 100 MiB son 25 bloques, y abrir 25 conexiones contra
# tres peers no gana nada y puede agotar descriptores.
BLOQUES_EN_PARALELO = 8


class TransferError(Exception):
    """Lo que rompimos nosotros, no la red.

    Existe para que `commands` distinga esto de un `requests.RequestException`
    y pueda decir cual de las dos cosas paso. Sin ella habria que elegir entre
    levantar `ValueError` —que tambien levantan media docena de bibliotecas
    estandar— o devolver `None` y comprobarlo en cada llamada.
    """


def checksum(data: bytes) -> str:
    """El SHA-256 del bloque crudo, en hexadecimal minuscula.

    Es el formato que CONTRATOS exige en `X-Block-Checksum`, y el mismo que el
    peer recalcula antes de aceptar el bloque.
    """
    return hashlib.sha256(data).hexdigest()


def read_chunk(path: Path, index: int, block_size: int, size: int) -> bytes:
    """Los bytes del bloque `index`, leidos por `seek` y no por recorrido.

    El offset se calcula, no se acumula: los bloques se suben en paralelo y se
    piden desordenados, asi que leer el `n` no puede depender de haber leido
    antes el `n-1`.

    Si vienen menos bytes de los que el plan dice, el archivo cambio de tamano
    despues del `allocate` y el plan ya no lo describe. Devolver un trozo corto
    en silencio acabaria confirmando unos metadatos que mienten.
    """
    with open(path, "rb") as archivo:
        archivo.seek(index * block_size)
        datos = archivo.read(size)

    if len(datos) != size:
        raise TransferError(
            f"el archivo local ya no tiene el tamano que se reservo: "
            f"el bloque {index} deberia medir {size} bytes y mide {len(datos)}"
        )

    return datos


def assemble(piezas: dict[int, bytes], bloques: list[dict]) -> bytes:
    """Junta las piezas **por indice**, no por orden de llegada.

    Es el bug que introduce el paralelismo y el unico que ningun checksum por
    bloque detecta: cada pieza esta intacta y el archivo no.

    Exige exactamente los indices del plan. Si falta uno, el resultado no seria
    un archivo mas corto sino uno corrupto; si sobra uno, seria contenido que
    nadie confirmo.
    """
    esperados = {bloque["index"] for bloque in bloques}

    if set(piezas) != esperados:
        raise TransferError(
            "los bloques recibidos no son los del archivo: "
            f"faltan {sorted(esperados - set(piezas))} y "
            f"sobran {sorted(set(piezas) - esperados)}"
        )

    return b"".join(piezas[index] for index in sorted(esperados))


def in_parallel(tareas: list) -> list:
    """Ejecuta las tareas a la vez, como mucho `BLOQUES_EN_PARALELO`.

    Hilos y no `asyncio`: el cliente es sincrono, y lo que se solapa es espera
    de red, que es justo lo que los hilos resuelven bien. Meter un bucle de
    eventos obligaria a reescribir el REPL entero.

    Los resultados vuelven **en el orden de entrada**, que es lo que permite
    emparejar cada uno con su bloque. El primer fallo se propaga tal cual: las
    tareas ya lanzadas terminan, pero no habra commit ni archivo, que es lo
    unico que importa.
    """
    tareas = list(tareas)

    if not tareas:
        return []

    with ThreadPoolExecutor(max_workers=BLOQUES_EN_PARALELO) as pool:
        return list(pool.map(lambda tarea: tarea(), tareas))
