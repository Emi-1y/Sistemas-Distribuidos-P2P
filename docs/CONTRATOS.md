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

El dueño de un directorio guarda **su contenido**: las entradas de todo lo que
cuelga directamente de él. Por eso `ls` es una sola llamada a un solo dueño y no
un barrido de los N peers. Los bytes siguen repartidos bloque a bloque: el
metadato es lo único que se agrupa por directorio, y es lo que menos pesa.

**La existencia de un directorio se registra dos veces**, en dos peers
distintos y por dos motivos distintos:

| Dónde | Qué es | Para qué |
|---|---|---|
| En el contenido de su **padre** | una entrada hija `{"name": "universidad", "type": "directory"}` | que aparezca en el `ls` del padre, y que el nombre quede ocupado |
| En su **propio** dueño | su contenido, aunque esté vacío | que sus hijos sepan que existe sin preguntar a nadie |

Sin la segunda, `allocate /universidad/tarea.pdf` no podría comprobar que
`/universidad` existe: esa entrada vive en el dueño de `/`, que es otro peer.
Con ella, el dueño de `/universidad` resuelve las dos comprobaciones —que el
padre existe y que el nombre está libre— sin salir del peer, y `allocate`, que
es el camino caliente, no gasta ni una llamada de red.

El precio lo paga `mkdir`, que escribe en dos peers, y `rmdir`, que borra en
dos. Son operaciones raras; `allocate` ocurre una vez por archivo subido.

## Bloques — SPEC-03

### `PUT /blocks/{file_id}/{index}`
Request → cuerpo binario. Cabecera `X-Block-Checksum: <sha256>`.
`201` → `{"file_id": "...", "index": 0, "size": 4194304, "checksum": "..."}`

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `index` no es un entero mayor o igual que 0, o `file_id` no es un UUID | `Identificador de bloque inválido` |
| `400` | Falta `X-Block-Checksum`, o no es un SHA-256 hexadecimal | `Checksum de bloque inválido` |
| `409` | Ese bloque ya existe | `El bloque ya existe` |
| `413` | El bloque supera `DFSHA_BLOCK_SIZE` | `El bloque supera el tamaño máximo permitido` |
| `422` | El checksum recibido no coincide con lo escrito | `El checksum del bloque no coincide` |

El `409` es la garantía WORM: un bloque escrito no se toca. El `422` borra lo
escrito antes de responder — no quedan bloques corruptos.

### `GET /blocks/{file_id}/{index}`
`200` → cuerpo binario, con `X-Block-Checksum` en la respuesta.

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `index` no es un entero mayor o igual que 0, o `file_id` no es un UUID | `Identificador de bloque inválido` |
| `404` | No existe ese bloque en este peer | `El bloque no existe` |

### `DELETE /blocks/{file_id}`
Borra el directorio completo del archivo en este peer. Idempotente: si no
tiene ningún bloque de ese archivo responde `200` con `"deleted": 0`, no `404`.
El borrado de un archivo se lanza a varios peers a la vez y muchos de ellos no
tendrán nada; «no tengo nada» es una respuesta, no un fallo.

`200` → `{"file_id": "...", "deleted": 3}`

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `file_id` no es un UUID | `Identificador de bloque inválido` |

### Layout en disco
Cada peer guarda **dos árboles separados** bajo `STORAGE_ROOT`:

| Árbol | Qué guarda | Quién lo escribe |
|---|---|---|
| `STORAGE_ROOT/blocks/<file_id>/<index:06d>.blk` | Los bloques | SPEC-03 |
| `STORAGE_ROOT/metadata/<hash de la ruta>.json` | El contenido de los directorios cuya clave le toca | SPEC-04 |

El árbol `STORAGE_ROOT/namespace/`, que hasta la SPEC-03 guardaba directorios y
archivos reales, **desaparece con la SPEC-04**: el espacio de nombres deja de
ser un sistema de archivos y pasa a ser un conjunto de entradas repartidas por
el anillo.

La separación no es orden, es una frontera. Mientras el espacio de nombres fue
un árbol real, mezclarlo con los bloques habría hecho de `cd /blocks` y
`rmdir /blocks/<file_id>` operaciones perfectamente legales. Hoy las rutas del
usuario ya no llegan al disco —son claves del anillo— y la frontera sigue
sirviendo para lo mismo: ningún nombre de archivo de metadatos puede aterrizar
entre los bloques, ni al revés.

Toda ruta, de cualquiera de los dos árboles, se resuelve con `resolve_path`,
que **recibe la raíz como parámetro**:

```python
def resolve_path(root: Path, remote_path: str) -> Path: ...
```

Anclar una única jaula en `STORAGE_ROOT` no basta: una ruta con `..` desde un
árbol cae **dentro** de `STORAGE_ROOT` y pasaría la comprobación, aterrizando en
el árbol de al lado. La jaula tiene que moverse con el plano que protege, y como
hay dos planos hacen falta dos raíces. Sigue siendo la única jaula **del disco**:
no existe ninguna segunda comprobación de contención en ningún otro sitio.

La normalización de las **rutas lógicas** —las que escribe el usuario y hoy ya
no tocan el disco— es un asunto distinto y vive en la SPEC-04: una ruta que
suba por encima de `/` responde `403` antes de convertirse en clave del anillo.

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
Request → `{"path": "...", "file_id": "...", "blocks": [{"index": 0, "checksum": "...", "peers": ["peer2"]}]}`
`200` → la entrada, ya con `state: "committed"`

| Código | Cuándo | Detalle |
|---|---|---|
| `404` | No hay entrada con ese `file_id` en esa ruta | `El archivo no existe` |
| `409` | La entrada ya está confirmada | `El archivo ya fue confirmado` |
| `422` | Falta algún bloque, o algún bloque tiene menos peers distintos que el factor de réplica | `Confirmación de bloques incompleta` |
| `422` | Algún `peer_id` reportado no está en el anillo | `Confirmación de bloques inválida` |

`path` va en el cuerpo porque el dueño de una entrada se calcula desde la ruta:
sin ella no hay forma de enrutar un `commit`, y el `file_id` no sirve — es un
UUID que no dice nada de dónde vive la entrada.

`peers` es **dónde quedó cada bloque de verdad**, no dónde lo planeó
`allocate`. El almacén de bloques acepta lo que le manden sin comprobar si le
tocaba (SPEC-03), así que el plan es una intención y el único que conoce el
hecho es el cliente, que recibió los `201`. La entrada guarda lo reportado.

Reportarlo obliga a validarlo: cada `peer_id` tiene que existir en el anillo, y
los peers de un bloque se cuentan **distintos**. Sin eso el cliente podría
escribir cualquier cosa y la metadata seguiría mintiendo, solo que con más
pasos.

Hasta el commit el archivo **no aparece en `ls`**. El `422` con factor 1 es
trivial; con factor 3 funciona igual sin tocar el código.

### `GET /files/lookup?path=...`
`200` → la entrada completa. Solo entradas `committed`.
`404` → `El archivo no existe`

### Operaciones de directorio
Se mudan del monolito sin cambiar su contrato: `GET /files?path=` (ls),
`POST /directories`, `DELETE /directories`, `DELETE /files?path=`.

### Enrutamiento y salidas obligadas
La clave de enrutamiento es siempre **el directorio cuyo contenido hay que
mirar o tocar**. Para casi todo eso es el directorio padre de la ruta pedida;
para `ls` es la ruta pedida en sí, porque el contenido que se lista es el suyo.

| Operación | Clave | Se resuelve en el dueño | Además sale a |
|---|---|---|---|
| `GET /files` (ls) | la ruta pedida | listar su contenido | — |
| `POST /files/allocate` | el padre | que existe su contenido y que el nombre está libre | — |
| `POST /files/commit` | el padre de `path` | todo | — |
| `GET /files/lookup` | el padre | todo | — |
| `DELETE /files` (rm) | el padre | borrar la entrada | `DELETE /blocks/{file_id}` a los peers de la entrada |
| `POST /directories` (mkdir) | el padre | que el nombre está libre, y escribir la entrada hija | crear el contenido vacío en el dueño del nuevo directorio |
| `DELETE /directories` (rmdir) | el padre | que la entrada existe y es un directorio, y borrarla | comprobar que el contenido está vacío y borrarlo, en su dueño |

`mkdir` y `rmdir` son las dos únicas que tocan dos peers, y es la consecuencia
directa del doble registro. `rmdir` trata **la ausencia de contenido como
directorio vacío**: es lo que permite deshacer un `mkdir` que se quedó a
medias.

### El contenido de un directorio, entre peers
La segunda escritura de `mkdir` y el segundo borrado de `rmdir` necesitan una
operación que actúe sobre el **contenido** de un directorio, no sobre su entrada
en el padre. Su clave es la ruta pedida en sí, como la de `ls`.

#### `POST /directories/content`
Request → `{"path": "/universidad"}`. Crea el contenido vacío. Idempotente: si
ya existe, `200` sin tocarlo.
`200` → `{"path": "/universidad"}`

#### `DELETE /directories/content?path=...`
Borra el contenido si está vacío. Si no hay contenido, también `200`: es lo que
deshace un `mkdir` a medias.
`200` → `{"path": "/universidad"}`

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | El contenido tiene entradas | `El directorio no está vacío` |

Las dos son simétricas al resto y no tienen canal privado: los peers exponen la
misma API, y quien las llama es otro peer, no el cliente.

## Errores transversales

| Código | Cuándo | Detalle |
|---|---|---|
| `503` | El peer dueño no responde | `El peer responsable no está disponible` |
| `508` | Bucle de reenvío detectado | `Bucle de enrutamiento detectado` |