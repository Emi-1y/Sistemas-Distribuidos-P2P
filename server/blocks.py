"""Almacén de bloques del peer: escribir una vez, leer muchas, no tocar.

Este módulo no conoce rutas lógicas, ni archivos, ni de quién es cada clave.
Es deliberadamente tonto: los bytes no pueden atravesar un tercer peer, y un
almacén que comprobara «¿soy yo el dueño?» rechazaría justo el tráfico de
reparación que la replicación del hito 3 necesita.
"""

import hashlib
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException

from server import config
from server.filesystem import resolve_path


# El plano de datos: un árbol propio, hermano del espacio de nombres.
BLOCKS_ROOT = config.STORAGE_ROOT / "blocks"

CHUNK_SIZE = 1024 * 1024           # 1 MB
CHECKSUM_LENGTH = 64               # SHA-256 en hexadecimal
HEXADECIMAL = set("0123456789abcdef")

BLOCKS_ROOT.mkdir(parents=True, exist_ok=True)


def parse_file_id(file_id: str) -> str:
    """Valida que sea un UUID y lo normaliza a minúsculas.

    Es lo que hace que la ruta del bloque esté enteramente sintetizada por
    nosotros: un UUID no puede contener `/` ni `..`. Y la normalización evita
    que `5F3E...` y `5f3e...` creen dos directorios para el mismo archivo, con
    la mitad de los bloques desaparecidos al leer y sin que nada dé error.
    """
    try:
        return str(UUID(file_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="Identificador de bloque inválido"
        )


def parse_index(index: str) -> int:
    """Entero mayor o igual que cero, y nada más.

    No vale `int()`: acepta `+7`, ` 7` y dígitos no ASCII, que darían dos URLs
    distintas para el mismo bloque. Y declararlo como entero en la ruta haría
    que FastAPI respondiera su propio `422`, que aquí significa otra cosa.
    """
    if not (index.isascii() and index.isdigit()):
        raise HTTPException(
            status_code=400,
            detail="Identificador de bloque inválido"
        )

    return int(index)


def parse_identifiers(file_id: str, index: str) -> tuple[str, int]:
    return parse_file_id(file_id), parse_index(index)


def parse_checksum(header: str | None) -> str:
    """El checksum es obligatorio.

    Que falte no puede significar «guárdalo sin verificar»: el bloque es la
    unidad que el hito 3 va a replicar y comparar, y un almacén que acepta sin
    verificar propaga corrupción en vez de detectarla.
    """
    valor = (header or "").strip().lower()

    if len(valor) != CHECKSUM_LENGTH or not set(valor) <= HEXADECIMAL:
        raise HTTPException(
            status_code=400,
            detail="Checksum de bloque inválido"
        )

    return valor


def block_dir(file_id: str) -> Path:
    return resolve_path(BLOCKS_ROOT, file_id)


def block_path(file_id: str, index: int) -> Path:
    return resolve_path(BLOCKS_ROOT, f"{file_id}/{index:06d}.blk")


async def save_block(
    file_id: str,
    index: int,
    chunks,
    checksum: str,
    declared_size: int | None = None
) -> dict:
    """Escribe un bloque, una sola vez, verificando lo que entra.

    `chunks` es un iterable asíncrono de bytes y no una petición: así el límite
    de tamaño y la limpieza tras un checksum malo se prueban sin levantar HTTP.
    """
    limite = config.BLOCK_SIZE

    # Primera defensa de tamaño: rechaza al cliente honesto sin leer un byte.
    if declared_size is not None and declared_size > limite:
        raise HTTPException(
            status_code=413,
            detail="El bloque supera el tamaño máximo permitido"
        )

    destination = block_path(file_id, index)

    if destination.exists():
        raise HTTPException(
            status_code=409,
            detail="El bloque ya existe"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    written = 0

    try:
        with destination.open("wb") as buffer:
            async for chunk in chunks:
                written += len(chunk)

                # Segunda defensa: cubre al que miente en la cabecera y al que
                # no manda ninguna longitud.
                if written > limite:
                    raise HTTPException(
                        status_code=413,
                        detail="El bloque supera el tamaño máximo permitido"
                    )

                digest.update(chunk)
                buffer.write(chunk)

        calculated = digest.hexdigest()

        if calculated != checksum:
            # Se borra antes de responder: si un bloque corrupto se quedara en
            # disco, el 409 de WORM impediría después reescribirlo y el bloque
            # malo sería permanente.
            raise HTTPException(
                status_code=422,
                detail="El checksum del bloque no coincide"
            )

    except HTTPException:
        destination.unlink(missing_ok=True)
        _discard_empty(destination.parent)
        raise

    return {
        "file_id": file_id,
        "index": index,
        "size": written,
        "checksum": calculated
    }


def read_block(file_id: str, index: int) -> tuple[Path, str]:
    """La ruta, para servirla, y el checksum recalculado sobre lo que hay.

    No se guarda en ningún sitio: recalcularlo cuesta una pasada de SHA-256 y
    regala detección de corrupción en reposo. El peer no juzga, informa de lo
    que leyó; quien verifica de punta a punta es el cliente, porque es el único
    que tiene el checksum de referencia.
    """
    path = block_path(file_id, index)

    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="El bloque no existe"
        )

    digest = hashlib.sha256()

    with path.open("rb") as buffer:
        for chunk in iter(lambda: buffer.read(CHUNK_SIZE), b""):
            digest.update(chunk)

    return path, digest.hexdigest()


def delete_blocks(file_id: str) -> dict:
    """Borra todos los bloques del archivo en este peer. Idempotente.

    El borrado de un archivo se lanza a varios peers a la vez y la mayoría no
    tendrá ninguno: «no tengo nada» es una respuesta, no un fallo.
    """
    directory = block_dir(file_id)

    if not directory.is_dir():
        return {"file_id": file_id, "deleted": 0}

    deleted = len(list(directory.glob("*.blk")))
    shutil.rmtree(directory)

    return {"file_id": file_id, "deleted": deleted}


def _discard_empty(directory: Path) -> None:
    """Retira el directorio del archivo si el bloque fallido lo dejó vacío."""
    try:
        directory.rmdir()
    except OSError:
        pass   # tiene otros bloques: se queda
