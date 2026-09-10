"""Anillo de hashing consistente con nodos virtuales.

Este módulo no habla por red y no toca disco: el anillo es una función pura de
la membresía. Es lo que permite comprobar, sin levantar nada, que tres peers
calculan exactamente el mismo anillo.
"""

import hashlib
from bisect import bisect_left

from server import config


def stable_hash(text: str) -> int:
    """SHA-1 truncado a 64 bits, siempre sobre UTF-8.

    Nunca `hash()` de Python: está aleatorizado por proceso vía PYTHONHASHSEED
    y tres peers calcularían tres anillos distintos, con un fallo intermitente
    e invisible. Aquí no se busca criptografía, sino reparto uniforme y el
    mismo entero en cualquier proceso, máquina y versión.
    """
    return int.from_bytes(hashlib.sha1(text.encode("utf-8")).digest()[:8], "big")


class Ring:
    """La membresía y las posiciones que se derivan de ella."""

    def __init__(self, vnodes: int | None = None) -> None:
        self._vnodes = config.VNODES if vnodes is None else vnodes
        self._addresses: dict[str, str] = {}
        self._positions: list[tuple[int, str]] = []
        self._hashes: list[int] = []

    def add_peer(self, peer_id: str, address: str) -> bool:
        """Registra al peer. Devuelve si la membresía cambió."""
        address = config.normalize_address(address)

        if self._addresses.get(peer_id) == address:
            return False

        self._addresses[peer_id] = address
        self._rebuild()

        return True

    def _rebuild(self) -> None:
        self._positions = sorted(
            (stable_hash(f"{peer_id}#{i}"), peer_id)
            for peer_id in self._addresses
            for i in range(self._vnodes)
        )
        self._hashes = [posicion for posicion, _ in self._positions]

    def peers(self) -> list[dict]:
        return [
            {"peer_id": peer_id, "address": self._addresses[peer_id]}
            for peer_id in sorted(self._addresses)
        ]

    def positions(self) -> list[tuple[int, str]]:
        """El anillo entero: vnodes x peers pares ordenados por hash."""
        return list(self._positions)

    def version(self) -> str:
        """Hash de la lista de peer_id ordenada, en hexadecimal corto.

        Sin direcciones: cambiar de dirección no mueve ninguna clave, así que
        no es otro anillo. Al derivarse de la membresía, dos peers con la misma
        versión tienen necesariamente el mismo anillo — cosa que un contador
        local no garantizaría.
        """
        firma = "\n".join(sorted(self._addresses))

        return hashlib.sha1(firma.encode("utf-8")).hexdigest()[:8]

    def nodes_at(self, position: int, replicas: int = 1) -> list[str]:
        """Recorre el anillo desde esa posición y acumula peers distintos.

        Saltarse los nodos virtuales de un peer ya elegido es lo que impide que
        el hito 3 coloque las tres réplicas en el mismo peer físico. Si se piden
        más réplicas que peers hay, devuelve los que hay: que sean pocos lo
        decide el commit, no el anillo.
        """
        if replicas <= 0 or not self._positions:
            return []

        elegidos: list[str] = []
        indice = bisect_left(self._hashes, position) % len(self._positions)

        for _ in range(len(self._positions)):
            peer_id = self._positions[indice][1]

            if peer_id not in elegidos:
                elegidos.append(peer_id)

                if len(elegidos) >= min(replicas, len(self._addresses)):
                    break

            indice = (indice + 1) % len(self._positions)

        return elegidos

    def choose_nodes(self, key: str, replicas: int = 1) -> list[str]:
        return self.nodes_at(stable_hash(key), replicas)

    def address_of(self, peer_id: str) -> str:
        """KeyError si no está: no lo provoca el usuario, es un invariante roto."""
        return self._addresses[peer_id]


LOCAL = Ring()
LOCAL.add_peer(config.PEER_ID, config.ADDRESS)

for _peer_id, _address in config.BOOTSTRAP:
    LOCAL.add_peer(_peer_id, _address)


def choose_nodes(key: str, replicas: int = 1) -> list[str]:
    return LOCAL.choose_nodes(key, replicas)


def address_of(peer_id: str) -> str:
    return LOCAL.address_of(peer_id)
