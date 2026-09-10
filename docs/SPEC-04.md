# SPEC-04 — Metadatos: el espacio de nombres repartido por el anillo

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-04` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/metadata` |
| **dependencias** | `docs/CONTRATOS.md` §Convenciones, §Colocación, §Metadatos, §Errores transversales. `ring.choose_nodes` y `ring.address_of` (SPEC-02). `DELETE /blocks/{file_id}` (SPEC-03). `resolve_path` y `config.BLOCK_SIZE`. |
| **dependencias de paquete** | Ninguna nueva. `json` y `posixpath` son de la biblioteca estándar; `httpx` ya se usa para propagar altas. |

---

## 2. Objetivo

Que el espacio de nombres deje de ser un sistema de archivos y pase a ser un
**conjunto de entradas repartidas por el anillo**, y que cualquier peer sepa
responder cualquier pregunta sobre él, sea suya la clave o no.

Al cerrar esta SPEC:

- Un archivo se reserva con `allocate`, se confirma con `commit` y se encuentra
  con `lookup`. **Hasta el commit no existe para nadie**: no aparece en `ls` y
  `lookup` no lo ve.
- Las cuatro operaciones de directorio funcionan sobre metadatos repartidos, sin
  un solo directorio real en disco.
- Cualquier peer acepta cualquier petición: si la clave no le pertenece, la
  reenvía al dueño y devuelve su respuesta tal cual.
- Los metadatos sobreviven al reinicio del peer.

Lo que sigue sin existir al cerrar: subir y bajar archivos. El cliente no puede
mover un byte hasta la SPEC-05, y esta SPEC **retira** los dos endpoints que hoy
lo hacen. La razón está en la sección 3 y no es una elección.

---

## 3. Contexto y decisiones

Esta es la SPEC que convierte el proyecto en un sistema distribuido de verdad:
hasta ahora había peers que sabían repartir claves y guardar bloques, pero el
espacio de nombres seguía siendo el `mkdir` de una sola máquina. Aquí se rompe
eso, y con ello se rompe temporalmente el cliente.

- **El espacio de nombres deja de ser un árbol de directorios reales, y por eso
  `send` y `receive` se retiran.** No es una decisión de alcance: es una
  consecuencia mecánica. `save_file` comprueba `directory.is_dir()` contra el
  disco; en cuanto `mkdir` deje de crear directorios reales, `send` responde
  `404` a todo lo que no sea la raíz. No hay convivencia posible entre los dos
  modelos — no es que sea fea, es que no funciona. Así que `POST /files/upload`
  y `GET /files/download` salen, y con ellos los catorce tests que los prueban.
  La SPEC-05 los reconstruye como `allocate` → bloques en paralelo → `commit`,
  que es lo que tenían que haber sido desde el principio.

- **El dueño de un directorio guarda su contenido, y la existencia de un
  directorio se registra dos veces.** El dueño de `/universidad` guarda la lista
  de lo que cuelga de `/universidad`. Pero la entrada *de* `/universidad` vive en
  el dueño de `/`, que es otro peer — así que sin más, el dueño de `/universidad`
  no sabría si ese directorio existe, y `allocate /universidad/tarea.pdf` no
  podría dar su `404`. Por eso `mkdir` escribe en dos sitios: la entrada hija en
  el contenido del padre, y el contenido vacío en el dueño del propio directorio.

  El reparto de costes es deliberado: `allocate` es el camino caliente —ocurre
  una vez por archivo subido— y queda **sin ninguna llamada de red**. `mkdir` y
  `rmdir`, que son raros, pagan la segunda escritura.

- **De las dos escrituras de `mkdir`, va primero la entrada en el padre y
  después el contenido en el propio dueño.** Es la decisión menos obvia de la
  SPEC y merece el razonamiento entero, porque la intuición tira hacia el otro
  lado: «que el fallo deje un directorio invisible y no uno que revienta al
  entrar».

  Los dos estados intermedios posibles son estos:

  | Falla la segunda | Qué queda | Qué se puede hacer dentro |
  |---|---|---|
  | Entrada primero | `ls /` lo muestra; `ls /universidad` da `404` | nada: `allocate` dentro también da `404` |
  | Contenido primero | `ls /` no lo muestra; `ls /universidad` da `[]` | **todo**: `allocate` dentro funciona |

  El orden «contenido primero» es el peligroso, por dos motivos que no se ven a
  primera vista:

  1. **El nombre se queda libre.** La entrada en el padre es lo que reserva el
     nombre. Si no se ha escrito, cualquiera puede hacer después un `allocate`
     de un **archivo** llamado `/universidad`, y saldrá bien. Entonces el dueño
     de `/` afirma que `/universidad` es un archivo mientras el dueño de
     `/universidad` guarda contenido de directorio para esa misma ruta. Dos peers
     diciendo cosas incompatibles sobre la misma ruta, sin ninguna regla que
     decida cuál gana.
  2. **Traga datos en silencio.** Con el contenido escrito, `allocate` dentro de
     ese directorio funciona: se suben bloques, se confirma el archivo, y ese
     archivo queda dentro de un directorio que `ls /` niega que exista. Nadie ve
     un error; simplemente hay datos inalcanzables.

  El orden «entrada primero» falla **ruidosamente y sin tragar nada**: el
  directorio aparece, y todo lo que se intente dentro responde `404`. Ninguna
  operación cree que ha ido bien. En un sistema distribuido eso es exactamente lo
  que se prefiere: un fallo que se ve es un fallo que se puede reintentar; uno
  silencioso se descubre cuando ya hay datos perdidos.

  El precio, escrito: un `mkdir` que falle a mitad deja el nombre ocupado, y
  repetirlo responde `409`. Por eso **`rmdir` trata la ausencia de contenido como
  directorio vacío**: borra la entrada del padre e intenta borrar un contenido
  que no está, y el nombre queda libre otra vez. Con esa regla, el único estado
  intermedio posible se deshace con un comando que ya existe, y no hace falta
  ninguna reparación automática hasta el hito 3.

- **El `commit` lleva la ruta en el cuerpo.** El dueño de una entrada se calcula
  desde la ruta, y el cuerpo que había en CONTRATOS solo traía `file_id`: un UUID
  que no dice nada de dónde vive la entrada. Un peer que recibiera un `commit`
  ajeno no tendría con qué decidir a quién reenviarlo. El cliente sí tiene la
  ruta, porque se la devolvió `allocate`.

- **El `commit` reporta en qué peers quedó cada bloque, y esos peers se
  validan.** Esta decisión se entiende mirando la SPEC-03: el almacén de bloques
  es deliberadamente tonto y **acepta cualquier bloque que le manden**, sin
  comprobar si le tocaba, porque comprobarlo bloquearía el tráfico de reparación
  del hito 3. La consecuencia directa cae aquí: **el plan que devuelve `allocate`
  es una intención, no un hecho.** El único que sabe dónde acabaron los bloques
  de verdad es el cliente, porque es quien recibió los `201`. Por eso la entrada
  guarda lo reportado y no lo planeado.

  Y reportar obliga a validar. Si el cliente puede escribir cualquier `peer_id`,
  la metadata sigue mintiendo, solo que con más pasos y con aspecto de verdad. Se
  comprueban dos cosas: que cada `peer_id` reportado **existe en el anillo**, y
  que cada bloque tiene al menos `REPLICAS` peers **distintos**. Lo segundo es lo
  que hace comprobable el `422` del factor de réplica: contar índices solo
  verifica que no falte ningún bloque, que es otra pregunta.

- **El factor de réplica es una constante, hoy en 1.** `REPLICAS = 1` en un solo
  sitio. Todo lo que lo rodea —`choose_nodes(clave, REPLICAS)`, la lista `peers`
  por bloque, el recuento de peers distintos en el commit— ya funciona con 3 sin
  tocar una línea. Es el gancho del hito 3, y esta vez está probado con un test
  que sube la constante, no solo prometido.

- **Los metadatos no se replican.** Factor 1 también aquí: cada directorio vive
  en un peer y en ninguno más. Si ese peer cae, ese directorio no existe para el
  sistema. Es una de las dos preguntas abiertas que el hito 3 tiene que responder
  y está en el handoff; decirlo ahora es más honesto que descubrirlo en la demo.

- **La raíz `/` existe siempre, en todos los peers, y no tiene entrada.** Su
  contenido lo guarda el dueño de la clave `/`, pero nadie la crea y nadie la
  borra. La alternativa —que la raíz fuera una entrada como las demás— obligaría
  a inventar quién la crea al arrancar el sistema y a que cada operación
  distinguiera el caso «todavía no hay raíz». `mkdir /` responde `409` y
  `rmdir /` responde `403`, igual que antes.

- **Las rutas lógicas se normalizan, y una que suba por encima de `/` responde
  `403`.** Esto no es una segunda jaula: la jaula de `resolve_path` protege el
  disco, y las rutas del usuario ya no llegan al disco — se convierten en claves
  del anillo. Lo que se protege aquí es distinto: que `/universidad/../..` no se
  convierta en una clave con significado propio, y que `//universidad//tarea.pdf`
  y `/universidad/tarea.pdf` sean la misma clave. Si dos escrituras de la misma
  ruta pudieran normalizarse distinto, acabarían en peers distintos y el archivo
  desaparecería al leerlo.

- **Persistencia: un JSON por contenido de directorio, escrito de forma
  atómica.** Un solo archivo grande con todo obligaría a reescribirlo entero en
  cada `allocate` y convertiría cualquier escritura a medias en la pérdida de
  todo el espacio de nombres del peer. Un archivo por directorio limita el daño a
  un directorio y hace que el disco se parezca a lo que el modelo dice: un peer
  guarda contenidos de directorio.

  El nombre del archivo es el hash de la ruta, no la ruta: una ruta lógica puede
  contener caracteres que un nombre de archivo no admite, y sintetizar el nombre
  nosotros es lo mismo que ya se hace con los bloques. La ruta va **dentro** del
  JSON, para poder mirar el disco y entender qué hay.

  La escritura es a un `.tmp` seguida de un `os.replace`, que es atómico. Sin
  eso, un peer que muriera a mitad de un `allocate` dejaría un JSON truncado, y
  al arrancar no podría leer ese directorio: el `allocate` de un archivo habría
  destruido los metadatos de todos sus hermanos.

- **El enrutamiento vive en su propio módulo y no tiene lógica de negocio.**
  `metadata.py` no sabe que existe la red; `routing.py` no sabe qué es un
  archivo. Es la misma separación que hay entre `ring.py` y `membership.py`, y
  por el mismo motivo: la capa que decide *qué* significa una operación se prueba
  sin levantar nada, y cuando algo falla en la de red se sabe que el fallo es de
  la red. Esa separación es también la que hace posible partir el plan TDD en dos
  fases.

- **Un solo salto de reenvío, y la respuesta del dueño se devuelve tal cual.** Si
  la clave no es mía, reenvío al dueño con `X-Forwarded-By` y devuelvo lo que
  conteste, código y cuerpo incluidos. **No se reinterpreta**: si el dueño dice
  `409 El archivo ya existe`, el cliente ve exactamente eso, no un `500` genérico
  ni un mensaje traducido. Un peer que reciba una petición ya marcada y tampoco
  sea el dueño responde `508` en vez de reenviar otra vez: con anillos que podrían
  haber divergido, sin ese corte dos peers podrían mandarse la misma petición
  indefinidamente. Y si el dueño no contesta, `503` — el sistema dice que no
  puede, no finge que la ruta no existe.

- **`DELETE /files` borra la entrada primero y los bloques después, sin
  reintentar.** El orden importa: si se borraran primero los bloques y fallara el
  borrado de la entrada, quedaría un archivo que `ls` muestra y que no se puede
  leer — otra vez el fallo silencioso. Al revés, lo que queda son bloques que
  nadie referencia: ocupan disco, pero no mienten. Recogerlos es hito 3 y está en
  el handoff junto a los huérfanos de un commit que nunca llegó.

---

## 4. Alcance

### Incluye

- `POST /files/allocate`, `POST /files/commit`, `GET /files/lookup`.
- El traslado de las cuatro operaciones de directorio (`ls`, `mkdir`, `rmdir`,
  `rm`) desde el sistema de archivos a los metadatos, sin cambiar su contrato de
  cara al usuario.
- El doble registro de directorios y las dos escrituras de `mkdir` y `rmdir`.
- Persistencia en JSON, un archivo por contenido de directorio, con escritura
  atómica.
- El reenvío al peer dueño, con `X-Forwarded-By`, `508` y `503`.
- La retirada de `POST /files/upload`, `GET /files/download`, del árbol
  `namespace/` y de los catorce tests que los prueban.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Partir archivos en bloques, subirlos o bajarlos. Es la SPEC-05, y hasta
  entonces el CLI no mueve datos.
- Replicación de metadatos, reparación de un `mkdir` a medias, recogida de
  bloques huérfanos y qué hacer cuando cae el dueño de un directorio. Hito 3.
- Renombrar y mover archivos o directorios. `mv` no está en el CLI y meterlo aquí
  abriría la puerta a operaciones que tocan tres peers.
- Borrado recursivo. `rmdir` sigue exigiendo el directorio vacío.
- Cuotas, permisos, propietarios y fechas de modificación.
- Cualquier garantía frente a dos clientes que escriban la misma ruta a la vez.
  Las comprobaciones son «mirar y luego escribir», sin bloqueo.

### Consecuencia asumida

Retirar `send` y `receive` deja el CLI **sin mover datos** hasta que cierre la
SPEC-05. Entre las dos SPECs se pueden crear, listar y borrar directorios, y se
pueden reservar y confirmar entradas de archivo, pero no subir ni bajar un
archivo.

Es un **estado intermedio deliberado, no una regresión**. Los dos endpoints que
se van eran monolíticos por construcción —un peer, un archivo entero, su disco—
y no tienen sitio en un sistema donde el espacio de nombres está repartido y los
datos van en bloques. Mantenerlos vivos durante la SPEC-04 habría exigido
sostener dos espacios de nombres a la vez, y ni siquiera es posible: `save_file`
comprueba el directorio destino contra el disco, que ya no existe. La SPEC-05
los devuelve en su forma distribuida y el CLI recupera sus nueve operaciones.

---

## 5. Diseño y contratos

Se toma de `docs/CONTRATOS.md` §Metadatos, §Colocación y §Errores transversales.
Esta SPEC introdujo allí cuatro correcciones antes de escribirse: el doble
registro de directorios en §Colocación, el `path` y los `peers` del `commit` con
su fila `422` nueva, la tabla de enrutamiento con las salidas obligadas, y la
desaparición de `namespace/` del layout.

### Esquema de la entrada

```json
{
  "path": "/universidad/tarea.pdf",
  "file_id": "5f3e...",
  "size": 10485760,
  "block_size": 4194304,
  "state": "committed",
  "created_at": "2026-09-10T21:00:00Z",
  "blocks": [
    {"index": 0, "size": 4194304, "checksum": "9f86d0...", "peers": ["peer2"]},
    {"index": 1, "size": 4194304, "checksum": "3f79bb...", "peers": ["peer3"]},
    {"index": 2, "size": 2097152, "checksum": "b1946a...", "peers": ["peer1"]}
  ]
}
```

### El contenido de un directorio, en disco

```json
{
  "path": "/universidad",
  "entries": {
    "tarea.pdf": { ...la entrada de arriba... },
    "apuntes":   {"name": "apuntes", "type": "directory"}
  }
}
```

En `STORAGE_ROOT/metadata/<stable_hash de la ruta>.json`.

### Endpoints

| Método y ruta | Clave | Éxito |
|---|---|---|
| `POST /files/allocate` | el padre | `200` con la entrada `pending` |
| `POST /files/commit` | el padre de `path` | `200` con la entrada `committed` |
| `GET /files/lookup?path=` | el padre | `200` con la entrada; solo `committed` |
| `GET /files?path=` | la ruta pedida | `200` con `{"path", "items"}` |
| `POST /directories` | el padre | `200` con el mensaje de siempre |
| `DELETE /directories?path=` | el padre | `200` con el mensaje de siempre |
| `DELETE /files?path=` | el padre | `200` con el mensaje de siempre |
| `POST /directories/content` | la ruta pedida | `200`; crea el contenido vacío |
| `DELETE /directories/content?path=` | la ruta pedida | `200`; lo borra si está vacío |

Los dos últimos son la segunda escritura de `mkdir` y el segundo borrado de
`rmdir`. Actúan sobre el **contenido** de un directorio y no sobre su entrada en
el padre, así que su clave es la ruta pedida en sí, como la de `ls`. Los llama
otro peer, no el cliente, pero se exponen igual que el resto: los peers son
simétricos y no hay canal privado entre ellos.

Las tablas de error salen literalmente de CONTRATOS §Metadatos y no se repiten
aquí. Los dos códigos transversales:

| Código | Cuándo | Detalle |
|---|---|---|
| `503` | El peer dueño no responde | `El peer responsable no está disponible` |
| `508` | Llega ya reenviada y tampoco es mía | `Bucle de enrutamiento detectado` |

### `server/metadata.py` — la lógica, sin red

```python
METADATA_ROOT: Path         # config.STORAGE_ROOT / "metadata"
REPLICAS = 1

def normalize(path: str) -> str: ...        # 403 si sube por encima de /
def parent_of(path: str) -> str: ...
def name_of(path: str) -> str: ...

def read_bucket(directory: str) -> dict | None: ...
def write_bucket(bucket: dict) -> None: ...   # .tmp + os.replace
def ensure_bucket(directory: str) -> None: ...  # idempotente
def drop_bucket(directory: str) -> None: ...    # 400 si tiene entradas

def allocate(path: str, size: int) -> dict: ...
def commit(path: str, file_id: str, blocks: list[dict]) -> dict: ...
def lookup(path: str) -> dict: ...
def list_entries(directory: str) -> list[dict]: ...
def add_directory(path: str) -> dict: ...     # el registro hijo del mkdir
def remove_directory(path: str) -> dict: ...  # quita el registro hijo
def remove_file(path: str) -> dict: ...       # quita la entrada y la devuelve
```

Cada función es una operación con nombre propio y **es dueña de sus mensajes de
error**: el `409` de `add_directory` dice «El directorio ya existe» y el de
`allocate` dice «El archivo ya existe», que es la misma colisión contada desde
dos sitios. Una función genérica obligaría a que quien la llama eligiera el
mensaje, y el mensaje es parte del contrato.

Ninguna sabe que existen otros peers: todas operan sobre el contenido del
directorio que les toca, que asumen local.

`read_bucket("/")` **nunca devuelve vacío por ausencia**: si no hay archivo,
devuelve un contenido vacío. Es cómo se implementa que la raíz exista siempre
sin que nadie la cree.

`remove_file` devuelve la entrada borrada, con sus bloques y sus peers, en vez
de avisar ella misma a los peers de los bloques. Ese aviso es red, y la red no
entra en este módulo: lo lanza el endpoint, y se prueba en la fase 2.

### `server/routing.py` — la red, sin lógica

```python
def owner_of(key: str) -> str: ...
def is_mine(key: str) -> bool: ...

def delegate(key, forwarded, method, url, params=None, json=None) -> dict | None:
    """None si la clave es mía. Si no, la respuesta del dueño, tal cual."""

def call_owner(key, method, url, params=None, json=None) -> dict: ...
def _send(address, method, url, params, json, headers) -> httpx.Response: ...
```

`delegate` es lo que va al principio de cada endpoint. `call_owner` es la salida
obligada de `mkdir`, `rmdir` y `rm`, que tienen que tocar un segundo peer aunque
la clave principal sea suya. `_send` está aislada para que los tests la
sustituyan y comprueben a quién se llamó y con qué cabeceras, sin abrir un
socket.

`METADATA_ROOT` y `config.BLOCK_SIZE` se leen en cada llamada, no se copian al
importar, para que los tests puedan sustituirlos.

---

## 6. Criterios de aceptación

### Fase 1 — metadatos locales, un solo peer

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dada** una ruta cuyo directorio padre existe **cuando** se hace `allocate` **entonces** responde `200` con la entrada `pending`, con `file_id` UUID, `block_size`, y un bloque por cada trozo de `size`, con su tamaño y sus `peers` planeados |
| **AC-02** | **Dada** una entrada recién asignada **cuando** se hace `ls` del directorio o `lookup` de la ruta **entonces** no aparece: hasta el commit no existe para nadie |
| **AC-03** | **Dado** un directorio padre que no existe **cuando** se hace `allocate` **entonces** responde `404 El directorio no existe` |
| **AC-04** | **Dada** una ruta ya ocupada, por un archivo confirmado, uno pendiente o un directorio **cuando** se hace `allocate` **entonces** responde `409 El archivo ya existe` |
| **AC-05** | **Dado** un `size` negativo o un `path` vacío **cuando** se hace `allocate` **entonces** responde `400 Parámetros de asignación inválidos` |
| **AC-06** | **Dada** una ruta que sube por encima de `/` **cuando** se usa en cualquier operación **entonces** responde `403 Acceso fuera del sistema DFS no permitido` |
| **AC-07** | **Dada** una entrada `pending` y la confirmación de todos sus bloques **cuando** se hace `commit` **entonces** responde `200` con `state: "committed"`, los checksums rellenados y los `peers` **reportados**, no los planeados |
| **AC-08** | **Dado** un `file_id` que no está en esa ruta **cuando** se hace `commit` **entonces** responde `404 El archivo no existe` |
| **AC-09** | **Dada** una entrada ya confirmada **cuando** se repite el `commit` **entonces** responde `409 El archivo ya fue confirmado` |
| **AC-10** | **Dada** una confirmación a la que le falta un índice, o con un bloque con menos peers **distintos** que `REPLICAS` **cuando** se hace `commit` **entonces** responde `422 Confirmación de bloques incompleta` |
| **AC-11** | **Dado** un `peer_id` reportado que no está en el anillo **cuando** se hace `commit` **entonces** responde `422 Confirmación de bloques inválida` |
| **AC-12** | **Dada** una entrada confirmada **cuando** se hace `lookup` **entonces** responde `200` con la entrada completa; y con una `pending`, `404 El archivo no existe` |
| **AC-13** | **Dado** un directorio con archivos confirmados, archivos pendientes y subdirectorios **cuando** se hace `ls` **entonces** devuelve los confirmados y los subdirectorios, y ningún pendiente |
| **AC-14** | **Dado** un `mkdir` **cuando** termina **entonces** existen las dos cosas: la entrada hija en el contenido del padre y el contenido vacío del nuevo directorio |
| **AC-15** | **Dado** un nombre ya ocupado **cuando** se hace `mkdir` **entonces** responde `409 El directorio ya existe`; y `rmdir` de un directorio con contenido responde `400 El directorio no está vacío` |
| **AC-16** | **Dada** una entrada confirmada **cuando** se hace `rm` **entonces** desaparece de `ls` y la operación **devuelve la entrada borrada**, con sus bloques y los peers donde están |
| **AC-17** | **Dados** unos metadatos escritos **cuando** el módulo se recarga **entonces** siguen ahí, y un JSON escrito a medias no se queda nunca en su sitio definitivo |

### Fase 2 — enrutamiento entre peers

| ID | Given / When / Then |
|---|---|
| **AC-18** | **Dada** una clave que no me pertenece **cuando** llega la petición **entonces** se reenvía al dueño, a su dirección del anillo, con `X-Forwarded-By: <mi peer_id>` |
| **AC-19** | **Dado** que el dueño responde un error **cuando** se reenvía **entonces** el cliente recibe **el mismo código y el mismo detalle**, sin reinterpretar |
| **AC-20** | **Dada** una petición que ya trae `X-Forwarded-By` y cuya clave tampoco es mía **cuando** llega **entonces** responde `508 Bucle de enrutamiento detectado` y no se reenvía |
| **AC-21** | **Dado** un peer dueño que no responde **cuando** se le reenvía **entonces** responde `503 El peer responsable no está disponible` |
| **AC-22** | **Dado** un `mkdir` cuyo directorio pertenece a otro peer **cuando** se ejecuta **entonces** se escribe **primero** la entrada en el padre y **después** el contenido en el dueño del nuevo directorio |
| **AC-23** | **Dado** un `mkdir` cuya segunda escritura falla **entonces** el directorio aparece en `ls` del padre, todo lo que se intente dentro responde `404`, y un `rmdir` posterior lo deja limpio |
| **AC-24** | **Dada** una entrada confirmada con bloques en varios peers **cuando** se hace `rm` **entonces** se lanza `DELETE /blocks/{file_id}` a cada uno de esos peers, después de haber borrado la entrada |

---

## 7. Plan TDD

Orden estricto: escribir el test, verlo fallar, implementar lo mínimo, verde
antes de seguir. **Y en dos fases separadas, que no se mezclan.**

### Por qué dos fases

`metadata.py` decide *qué significa* cada operación. `routing.py` decide *a quién
se le pregunta*. Son dos preguntas distintas y se rompen por motivos distintos:
si los tests las mezclaran, un `409` inesperado no diría si la regla de colisión
está mal o si la petición acabó en el peer equivocado.

Así que la fase 1 se prueba con **un solo peer y sin red**: el anillo tiene un
único miembro, toda clave es suya, y `routing` no llega a intervenir. Cuando la
fase 1 está entera en verde, la semántica está fijada, y a partir de ahí
cualquier fallo de la fase 2 es un fallo de reenvío. La fase 2 no añade ni una
regla de negocio: solo mueve peticiones.

### Fase 1 — metadatos locales

**Paso 1 — Rutas y persistencia** → AC-06, AC-17. Va primero porque todo lo
demás se apoya en ello: `normalize`, `parent_of`, el nombre del archivo en disco
y la escritura atómica. El test de atomicidad comprueba que el `.tmp` nunca
queda como definitivo, interrumpiendo la escritura a mitad.

**Paso 2 — Directorios locales: `mkdir` y `ls`** → AC-13, AC-14, AC-15. Antes que
los archivos porque un archivo necesita un directorio donde vivir, y porque el
doble registro es la estructura que sostiene todo lo demás. Aquí `mkdir` escribe
las dos cosas en el mismo peer, que es el caso de un anillo de uno.

**Paso 3 — `allocate`** → AC-01 a AC-05. El caso feliz obliga al reparto en
bloques y al plan de peers; después los cuatro rechazos. El `409` incluye el
caso menos evidente: una entrada **pendiente** también ocupa el nombre.

**Paso 4 — `commit`** → AC-07 a AC-11. El caso feliz, y después los cuatro
rechazos en orden de menos a más sutil: no existe, ya confirmado, falta un
bloque, y peers que no están en el anillo. Este paso es donde se prueba que los
peers guardados son los reportados y no los planeados.

**Paso 5 — `lookup` y la invisibilidad de lo pendiente** → AC-02, AC-12. Va
después del commit porque la mitad de lo que afirma es sobre entradas que aún no
se han confirmado.

**Paso 6 — `rm` y `rmdir`** → AC-15, AC-16. Los últimos de la fase porque son los
únicos que destruyen. Aquí `rm` solo borra la entrada y la devuelve; avisar a los
peers de los bloques es red y se prueba en el paso 10. Y `rmdir` incluye la regla
que deshace un `mkdir` a medias: un directorio sin contenido se trata como
vacío.

### Fase 2 — enrutamiento

**Paso 7 — `delegate`** → AC-18, AC-19. Con un anillo de tres peers y `_send`
sustituido. Dos tests gemelos: la clave es mía y no pasa nada por la red; la
clave es de otro y se va tal cual, con la cabecera puesta. Y el del error, que
comprueba que el código y el detalle del dueño llegan sin tocar.

**Paso 8 — Bucle y peer caído** → AC-20, AC-21. Los dos códigos transversales.
Van juntos porque los dos son «la petición no se puede resolver» y los dos se
prueban igual: uno con la cabecera puesta, el otro con un `_send` que revienta.

**Paso 9 — Las dos escrituras de `mkdir`** → AC-22, AC-23. Combina las dos capas,
y es el único sitio donde importa el **orden** de dos efectos. El test de AC-22
registra la secuencia de llamadas; el de AC-23 hace fallar la segunda y comprueba
los tres síntomas: se ve desde fuera, no se puede entrar, y `rmdir` lo limpia.

**Paso 10 — El aviso a los peers de los bloques** → AC-24. El último de todos: es
la única salida a la red que no es un reenvío sino un abanico, y necesita que
`rm` ya funcione y que el reenvío ya esté probado.

---

## 8. Tests límite

| Caso | Esperado | Por qué importa |
|---|---|---|
| `//universidad//tarea.pdf` | La misma clave que `/universidad/tarea.pdf` | Si dos escrituras de la misma ruta se normalizaran distinto, acabarían en peers distintos y el archivo desaparecería al leerlo |
| `/universidad/./apuntes/../tarea.pdf` | La misma clave | Mismo motivo, con la forma que de verdad escriben los clientes |
| `/..` y `/universidad/../..` | `403` | Una clave por encima de la raíz no significa nada, y no puede llegar al anillo |
| `mkdir /` | `409` | La raíz existe siempre; que sea un caso especial sin escribir sería peor |
| `rmdir /` | `403` | Se conserva el mensaje del monolito: `No se puede eliminar la raíz del DFS` |
| `allocate` con `size` 0 | `200` con `blocks: []`, y su `commit` con lista vacía lo confirma | Un archivo vacío es válido y no tiene ni un bloque |
| `size` múltiplo exacto de `block_size` | Sin bloque final corto | El error de uno clásico del `ceil` |
| `size` de un byte más que un múltiplo | Un bloque final de un byte | El otro lado del mismo borde |
| `commit` con `peers: ["peer2", "peer2"]` y `REPLICAS = 2` | `422 incompleta` | Se cuentan peers **distintos**: repetir uno no es replicar |
| `ls` de un directorio recién creado | `200` con `[]` | Vacío y no existente son cosas distintas, y es justo lo que distingue el contenido |
| `ls` de un directorio que no existe | `404 El directorio no existe` | El otro lado de la misma distinción |
| `lookup` de una ruta que es un directorio | `404 El archivo no existe` | `lookup` es de archivos; un directorio no tiene bloques que devolver |
| `REPLICAS` subido a 3 en un anillo de 3 | `allocate` planea tres peers distintos por bloque y el `commit` los exige | Es el gancho del hito 3, probado y no prometido |

---

## 9. Tareas

1. Crear `server/metadata.py` con las rutas lógicas, el contenido de directorio y
   su persistencia atómica.
2. Añadir `allocate`, `commit`, `lookup`, `list_entries`, `add_child` y
   `remove_child` al mismo módulo.
3. Crear `server/routing.py` con `owner_of`, `is_mine`, `delegate`, `call_owner`
   y `_send`.
4. Reescribir en `server/main.py` los cuatro endpoints de directorio para que
   usen `metadata` y `routing`, conservando su contrato de cara al usuario.
5. Añadir `POST /files/allocate`, `POST /files/commit` y `GET /files/lookup`, y
   los dos endpoints de contenido de directorio que usan `mkdir` y `rmdir` entre
   peers.
6. Retirar `POST /files/upload`, `GET /files/download`, las funciones
   `save_file`, `get_file`, `list_directory`, `create_directory`,
   `remove_directory` y `remove_file` de `server/filesystem.py`, y el árbol
   `NAMESPACE_ROOT`.
7. Borrar `tests/test_send.py` y `tests/test_receive.py`, y el fixture `storage`
   que solo ellos usaban.
8. Dejar los comandos `send` y `receive` del CLI avisando de que no están
   disponibles hasta la SPEC-05, en vez de lanzar una petición a un endpoint que
   ya no existe.
9. Escribir `tests/test_metadata.py` (fase 1) y `tests/test_routing.py`
   (fase 2), siguiendo el plan del punto 7.

---

## 10. Definición de Done

1. Cada uno de los veinticuatro criterios de aceptación tiene al menos un test
   que lo cubre.
2. Los trece tests límite de la sección 8 están escritos y pasan.
3. La suite pasa completa. De los 138 tests actuales se retiran los catorce de
   `send` y `receive`, porque prueban endpoints que dejan de existir; ninguno de
   los otros 124 se modifica.
4. Ningún test de la fase 1 abre un socket ni sustituye nada de `routing`: la
   semántica se prueba entera sobre un peer.
5. `metadata.py` no importa `routing` ni `httpx`, y `routing.py` no importa
   `metadata`.
6. `REPLICAS` aparece en un solo sitio, y hay un test que lo sube a 3 y sigue
   verde sin tocar código de producción.
7. No queda ninguna referencia a `NAMESPACE_ROOT`, `save_file` ni `get_file` en
   el código, y `server/storage/namespace/` ya no se crea al importar.
8. Ninguna escritura de metadatos deja un archivo a medias en su sitio
   definitivo: todas pasan por `.tmp` y `os.replace`.
9. Los cuatro comandos de directorio del CLI (`ls`, `mkdir`, `rmdir`, `rm`)
   siguen funcionando exactamente igual de cara al usuario, contra un peer
   cualquiera del bootstrap, sea o no el dueño de la clave.
10. Ningún archivo de producción fue escrito antes que su test.
