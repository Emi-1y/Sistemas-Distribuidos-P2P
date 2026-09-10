# Contratos de comunicación — DFSha P2P

Superficie HTTP completa de los peers. Congelada antes de escribir las SPECs
02–06. Cada SPEC desarrolla la parte que le toca sin cambiar lo de aquí.

## Convenciones comunes

- Todos los peers exponen la misma API: son simétricos.
- `file_id` es un UUID4 en texto. Nunca la ruta lógica.
- `index` es un entero base 0 en JSON. En disco se escribe con seis dígitos
  y relleno de ceros: `000000.blk`.
- Los cuerpos binarios van como `application/octet-stream`. El resto, JSON.
- El checksum es SHA-256 en hexadecimal minúscula, del bloque crudo.
- Errores: siempre `{"detail": "<texto>"}`, con el texto literal de las tablas.
- **Enrutamiento**: el cliente puede pedirle cualquier cosa a cualquier peer.
  Si la clave no le pertenece, reenvía al dueño y devuelve su respuesta tal
  cual. La petición reenviada lleva `X-Forwarded-By: <peer_id>`; un peer que
  reciba una petición con esa cabecera y tampoco sea el dueño responde `508`
  en vez de reenviar otra vez.

## Anillo y membresía — SPEC-02

### `GET /health`
`200` → `{"peer_id": "...", "status": "ok", "ring_version": "a3f19c04"}`

### `GET /ring`
`200` → `{"ring_version": "a3f19c04", "peers": [{"peer_id": "...", "address": "http://peer2:9002"}]}`

### `ring_version`
Cadena hexadecimal corta, **derivada de la membresía**: el hash de la lista de
`peer_id` ordenada. Las direcciones no entran. Dos peers con el mismo
`ring_version` tienen el mismo anillo; dos peers con `ring_version` distinto
tienen anillos distintos. No es un contador y no es un reloj lógico.

### `POST /peers/join`
Request → `{"peer_id": "...", "address": "http://peer2:9002"}`
`200` → mismo cuerpo que `GET /ring`, ya con el nuevo peer dentro

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `address` no es una URL http válida | `Dirección de peer inválida` |
| `409` | Ese `peer_id` ya está en el anillo con otra dirección | `El peer ya está registrado` |

### Colocación
Dos funciones, en un solo módulo:

```python
def choose_nodes(key: str, replicas: int = 1) -> list[str]: ...
def address_of(peer_id: str) -> str: ...
```

`choose_nodes` devuelve una **lista** de `peer_id`, siempre, aunque hoy tenga
un elemento. `address_of` traduce ese `peer_id` a la URL base con la que se le
habla; sin ella ni el reenvío al dueño ni el cliente pueden emitir la petición.

Claves:

| Qué se coloca | Clave |
|---|---|
| Un bloque | `f"{file_id}:{index}"` |
| La entrada de metadatos de `/dir/archivo` | `"/dir"` — el **directorio padre** |
| La entrada del propio directorio `/a/b` | `"/a"` — también su padre |

El dueño de un directorio guarda las entradas de todo lo que cuelga
directamente de él. Por eso `allocate` comprueba el padre y la colisión de
nombre sin salir del peer, y `ls` es una sola llamada a un solo dueño, no un
barrido de los N peers. Los bytes siguen repartidos bloque a bloque: el
metadato es lo único que se agrupa por directorio, y es lo que menos pesa.

## Bloques — SPEC-03

### `PUT /blocks/{file_id}/{index}`
Request → cuerpo binario. Cabecera `X-Block-Checksum: <sha256>`.
`201` → `{"file_id": "...", "index": 0, "size": 4194304, "checksum": "..."}`

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `index` negativo o `file_id` que no es UUID | `Identificador de bloque inválido` |
| `409` | Ese bloque ya existe | `El bloque ya existe` |
| `413` | El bloque supera `DFSHA_BLOCK_SIZE` | `El bloque supera el tamaño máximo permitido` |
| `422` | El checksum recibido no coincide con lo escrito | `El checksum del bloque no coincide` |

El `409` es la garantía WORM: un bloque escrito no se toca. El `422` borra lo
escrito antes de responder — no quedan bloques corruptos.

### `GET /blocks/{file_id}/{index}`
`200` → cuerpo binario, con `X-Block-Checksum` en la respuesta.

| Código | Cuándo | Detalle |
|---|---|---|
| `404` | No existe ese bloque en este peer | `El bloque no existe` |

### `DELETE /blocks/{file_id}`
Borra el directorio completo del archivo en este peer. Idempotente.
`200` → `{"file_id": "...", "deleted": 3}`

### Layout en disco
`storage/<file_id>/<index:06d>.blk`, dentro de `STORAGE_ROOT`, resuelto con
`resolve_path`. Sin excepción: es la única jaula del sistema.

## Metadatos — SPEC-04

### Esquema de la entrada

```json
{
  "path": "/universidad/tarea.pdf",
  "file_id": "5f3e...",
  "size": 10485760,
  "block_size": 4194304,
  "state": "committed",
  "created_at": "2026-09-09T21:00:00Z",
  "blocks": [
    {"index": 0, "size": 4194304, "checksum": null, "peers": ["peer2"]},
    {"index": 1, "size": 4194304, "checksum": null, "peers": ["peer3"]},
    {"index": 2, "size": 2097152, "checksum": null, "peers": ["peer1"]}
  ]
}
```

`state` es `pending` o `committed`. `peers` es lista siempre. `checksum` se
rellena en el commit.

### `POST /files/allocate`
Request → `{"path": "/universidad/tarea.pdf", "size": 10485760}`
`200` → la entrada completa con `state: "pending"`

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `size` negativo o `path` vacío | `Parámetros de asignación inválidos` |
| `403` | La ruta queda fuera del espacio de nombres | `Acceso fuera del sistema DFS no permitido` |
| `404` | El directorio padre no existe | `El directorio no existe` |
| `409` | Ya hay un archivo en esa ruta | `El archivo ya existe` |

### `POST /files/commit`
Request → `{"file_id": "...", "blocks": [{"index": 0, "checksum": "..."}]}`
`200` → la entrada, ya con `state: "committed"`

| Código | Cuándo | Detalle |
|---|---|---|
| `404` | No hay entrada con ese `file_id` | `El archivo no existe` |
| `409` | La entrada ya está confirmada | `El archivo ya fue confirmado` |
| `422` | Falta algún bloque, o hay menos confirmaciones que el factor de réplica | `Confirmación de bloques incompleta` |

Hasta el commit el archivo **no aparece en `ls`**. El `422` con factor 1 es
trivial; con factor 3 funciona igual sin tocar el código.

### `GET /files/lookup?path=...`
`200` → la entrada completa. Solo entradas `committed`.
`404` → `El archivo no existe`

### Operaciones de directorio
Se mudan del monolito sin cambiar su contrato: `GET /files?path=` (ls),
`POST /directories`, `DELETE /directories`, `DELETE /files?path=`.
`DELETE /files` borra la entrada y lanza `DELETE /blocks/{file_id}` a los
peers que aparezcan en ella.

Las cuatro se enrutan igual que el resto: la clave es el directorio padre de
la ruta pedida, salvo `ls`, cuya clave es la ruta pedida en sí, porque quien
responde es el dueño del directorio que se lista.

## Errores transversales

| Código | Cuándo | Detalle |
|---|---|---|
| `503` | El peer dueño no responde | `El peer responsable no está disponible` |
| `508` | Bucle de reenvío detectado | `Bucle de enrutamiento detectado` |