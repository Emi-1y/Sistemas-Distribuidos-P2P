# SPEC-03 — API de bloques: el peer como almacén inmutable

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-03` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/block-api` |
| **dependencias** | `docs/CONTRATOS.md` §Convenciones, §Bloques, §Layout en disco. `resolve_path` de `server/filesystem.py`, cuya firma cambia aquí. `config.STORAGE_ROOT` y `config.BLOCK_SIZE` de la SPEC-02. |
| **dependencias de paquete** | Ninguna nueva. `hashlib`, `uuid` y `shutil` son de la biblioteca estándar. |

---

## 2. Objetivo

Convertir cada peer en un **almacén de bloques inmutables y verificables**,
direccionable por `(file_id, index)`, que no sabe nada de rutas lógicas, ni de
archivos, ni de quién es dueño de qué.

Al cerrar esta SPEC:

- Un bloque se escribe una vez, se lee tantas veces como haga falta y no puede
  sobrescribirse.
- Todo bloque que entra trae su SHA-256 y se rechaza si no cuadra. Ninguno queda
  a medio escribir en disco, ni siquiera cuando la subida se corta.
- Los bloques viven en un árbol propio que **ninguna ruta del usuario alcanza**,
  ni escribiéndola directa ni saliéndose hacia arriba.
- Todos los bloques de un archivo se borran de un peer con una sola llamada, la
  tenga o no.

Lo que este peer sigue sin saber: a qué archivo lógico pertenece un bloque, en
qué orden va, y si él era el destino correcto. Eso es de la SPEC-04 y de quien
construyó el plan.

---

## 3. Contexto y decisiones

Hasta aquí el sistema sabía repartir claves pero no guardaba ni un byte
repartido. Esta SPEC construye el plano de datos, y lo construye deliberadamente
**tonto**: es la pieza que más veces se va a tocar en el hito 3, y cuanto menos
sepa, menos habrá que reescribir.

- **El almacén de bloques no conoce rutas, no comprueba dueños y no reenvía
  nada.** Hay dos razones, y la segunda es la que sorprende.

  La primera es el rendimiento: los bytes no pueden atravesar un tercer peer.
  Es la decisión que sostiene todo el protocolo de escritura en tres tiempos —
  si un peer reenviara un bloque de 4 MiB al dueño real, duplicaría el tráfico y
  reintroduciría exactamente el cuello de botella que se evita subiendo directo.

  La segunda es que **comprobar «¿soy yo el dueño?» rompería el hito 3**. Cuando
  un peer entra o sale, la replicación tiene que empujar bloques hacia sus nuevos
  dueños durante una ventana en la que el anillo y la realidad no coinciden. Un
  almacén estricto rechazaría justo el tráfico de reparación. Un almacén tonto
  acepta lo que le den y guarda dónde le dijeron.

  El precio, escrito para no fingir que no existe: nada impide que un cliente con
  un anillo viejo escriba un bloque en el peer equivocado. Se detecta tarde, al
  leer, con un `404` o con un checksum que no cuadra. Va al handoff del hito 3.

- **Dos árboles bajo `STORAGE_ROOT`, y `resolve_path` recibe la raíz como
  parámetro.** Los bloques van a `blocks/` y el espacio de nombres del usuario
  baja a `namespace/`. No es orden: `ls`, `cd`, `rm` y `rmdir` operan sobre
  rutas que escribe el usuario, así que con los dos planos mezclados
  `cd /blocks` o `rmdir /blocks/<file_id>` serían operaciones legales, y ese
  `rmdir` borraría bloques de verdad.

  Y **mover el espacio de nombres sin mover la jaula no cierra nada**: si
  `resolve_path` sigue anclado en `STORAGE_ROOT`, la ruta `/../blocks/<file_id>`
  se resuelve dentro de `STORAGE_ROOT` y pasa la comprobación. Solo cambia el
  ataque directo por uno con un `..` delante. La jaula tiene que moverse con el
  plano que protege, y como hay dos planos hacen falta dos raíces.

  La firma pasa a `resolve_path(root, remote_path)` **sin valor por defecto**:
  con dos árboles, olvidar el argumento tiene que ser un `TypeError` en el acto,
  no una caída silenciosa al árbol equivocado. Sigue siendo la única jaula del
  sistema — no se añade ninguna segunda comprobación de contención en ningún
  sitio.

- **`file_id` se valida como UUID y se normaliza a minúsculas antes de tocar el
  disco.** La validación hace que la ruta del bloque esté **enteramente
  sintetizada por nosotros**: un UUID no puede contener `/` ni `..`, así que en
  este árbol `resolve_path` deja de ser la primera línea y pasa a ser el cinturón
  sobre los tirantes. La normalización es menos obvia y más peligrosa de omitir:
  el hexadecimal de un UUID es indiferente a mayúsculas, así que `5F3E...` y
  `5f3e...` crearían dos directorios para el mismo archivo, y al leer faltaría
  la mitad de los bloques sin que nada diera error.

- **`index` se declara como texto en la ruta y se convierte a mano.** Si se
  declarara como entero, FastAPI respondería **su propio `422`**, con `detail` en
  forma de lista, ante `/blocks/<uuid>/abc`. Eso choca dos veces: con el `422`
  de CONTRATOS, que significa «el checksum no coincide» y es un problema
  completamente distinto, y con la convención de que todo error tiene la forma
  `{"detail": "<texto>"}`. Convirtiéndolo nosotros, el `422` queda reservado a la
  integridad y todos los errores tienen la misma forma.

- **El tamaño se rechaza dos veces: por `Content-Length` antes de leer, y por
  acumulación mientras se escribe.** Es la deuda técnica del monolito, y aquí sí
  se puede pagar. En `send` no había forma de cortar: Starlette recibe el
  multiparte entero antes de invocar el handler, así que el corte por fragmentos
  evitaba escribir en disco pero no cancelaba la recepción. Aquí el cuerpo es
  `application/octet-stream` y se consume con `request.stream()`, así que el
  corte es real.

  Hacen falta las dos defensas. El `Content-Length` es la barata: rechaza al
  cliente honesto **sin leer un solo byte**. La acumulación cubre al que miente
  en la cabecera y al que usa `Transfer-Encoding: chunked` y no manda longitud
  ninguna. Con una sola de las dos queda un agujero.

- **El checksum es obligatorio, y si no cuadra el bloque se borra antes de
  responder.** Que falte la cabecera no puede significar «guárdalo sin
  verificar»: el bloque es la unidad que el hito 3 va a replicar y comparar, y un
  almacén que acepta sin verificar propaga corrupción en vez de detectarla. Por
  eso falta o malformada es `400`, no aceptación silenciosa.

  Y el borrado del `422` no es limpieza cosmética: **si un bloque corrupto se
  quedara en disco, el `409` de WORM impediría después reescribirlo**. Un bloque
  malo sería permanente. Es peor que ninguno.

- **El checksum que devuelve el `GET` se recalcula al leer; no se guarda en
  ningún sitio.** Guardarlo en un fichero al lado obligaría a mantener dos cosas
  en sincronía y añadiría entradas al layout que CONTRATOS no contempla.
  Recalcular cuesta una pasada de SHA-256 sobre 4 MiB y **regala detección de
  corrupción en reposo**: si el disco pudre un byte, el checksum que devuelve el
  `GET` deja de cuadrar con el que quedó guardado en el commit, y el cliente lo
  ve al reensamblar.

  El peer no juzga, informa de lo que leyó. Quien verifica de punta a punta es el
  cliente, porque es el único que tiene el checksum de referencia. Que el peer
  guardase su propia copia solo añadiría una versión más que podría estar mal.

- **`GET` responde con `FileResponse`, no con el contenido en memoria.** Cargar
  4 MiB por cada petición concurrente escala mal. La segunda pasada por el
  archivo, la del checksum, es lectura secuencial y el sistema operativo la sirve
  de caché.

- **`DELETE` es idempotente y borra el directorio entero del archivo.** Es lo que
  la SPEC-04 va a lanzar a varios peers a la vez al borrar un archivo, y varios
  de ellos no tendrán ni un bloque suyo. Si «no tengo nada» fuera un `404`, quien
  borra tendría que distinguir un error real de la respuesta normal en cada
  llamada. `deleted: 0` es una respuesta, no un fallo.

- **Un bloque de cero bytes se acepta.** Rechazarlo exigiría un código de error
  que CONTRATOS no tiene, y no hace daño: es inmutable y viaja con su checksum,
  que es el de la cadena vacía. Un archivo de tamaño cero simplemente no genera
  bloques; que aparezca uno vacío es un cliente raro, no un peligro.

- **El `409` de WORM es incondicional, también cuando el contenido es idéntico.**
  Se escribe aquí la consecuencia para que no se descubra tarde: **un reintento
  de subida tras un timeout de red recibirá `409`, y la SPEC-05 tendrá que leerlo
  como «ya está, sigue», no como error.** La alternativa —comparar el checksum y
  devolver `201` si coincide— haría el reintento más cómodo, pero metería una
  excepción dentro de la regla que hace WORM fácil de explicar y de defender. Se
  prefiere la regla simple, y que el cliente sepa leerla.

---

## 4. Alcance

### Incluye

- `PUT`, `GET` y `DELETE` de bloques, con su lógica en un módulo propio.
- El árbol `blocks/`, el traslado del espacio de nombres a `namespace/` y
  `resolve_path` recibiendo la raíz. **Los dos traslados van juntos**: hasta que
  los dos planos se muevan, la frontera no existe.
- Validación y normalización de `file_id` e `index`.
- Checksum obligatorio, con borrado del bloque cuando no cuadra.
- Doble límite de tamaño: `Content-Length` y acumulación.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Metadatos: `allocate`, `commit`, `lookup` y las operaciones de directorio. Van
  en la SPEC-04, y con ellas el reemplazo del árbol `namespace/`.
- Enrutamiento al peer dueño, `X-Forwarded-By`, `508` y `503`. Un bloque se pide
  al peer que lo tiene, y quien lo sabe es el plan.
- Comprobar que este peer es el dueño del bloque que le mandan.
- Que `send` y `receive` partan en bloques. Es la SPEC-05; hasta entonces
  `MAX_FILE_SIZE` sigue en 10 MB y las dos operaciones siguen usando el árbol
  `namespace/` tal cual.
- Replicación, reparación, rebalanceo, recogida de bloques huérfanos y repaso
  periódico de integridad (*scrubbing*). Todo hito 3.
- Compresión, cifrado y deduplicación de bloques idénticos entre archivos.

---

## 5. Diseño y contratos

Se toma de `docs/CONTRATOS.md` §Bloques y §Layout en disco. Esta SPEC ya
introdujo allí cuatro correcciones antes de escribirse: la fila `400` del
checksum ausente, el `400` heredado por `GET` y `DELETE`, la idempotencia
explícita del `DELETE`, y los dos árboles con `resolve_path` parametrizado.

### `PUT /blocks/{file_id}/{index}`

**Request** — cuerpo binario `application/octet-stream`, cabecera
`X-Block-Checksum: <sha256 hexadecimal>`.

**Respuesta `201 Created`:**

```json
{"file_id": "5f3e...", "index": 0, "size": 4194304, "checksum": "9f86d0..."}
```

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `index` no es un entero mayor o igual que 0, o `file_id` no es un UUID | `Identificador de bloque inválido` |
| `400` | Falta `X-Block-Checksum`, o no es un SHA-256 hexadecimal | `Checksum de bloque inválido` |
| `409` | Ese bloque ya existe | `El bloque ya existe` |
| `413` | El bloque supera `DFSHA_BLOCK_SIZE` | `El bloque supera el tamaño máximo permitido` |
| `422` | El checksum recibido no coincide con lo escrito | `El checksum del bloque no coincide` |

### `GET /blocks/{file_id}/{index}`

`200` → cuerpo binario, con `X-Block-Checksum` recalculado sobre lo leído.

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `index` no es un entero mayor o igual que 0, o `file_id` no es un UUID | `Identificador de bloque inválido` |
| `404` | No existe ese bloque en este peer | `El bloque no existe` |

### `DELETE /blocks/{file_id}`

`200` → `{"file_id": "5f3e...", "deleted": 3}`. Idempotente: sin bloques,
`deleted: 0`.

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `file_id` no es un UUID | `Identificador de bloque inválido` |

### Layout en disco

```
STORAGE_ROOT/
  blocks/
    5f3e0000-0000-4000-8000-000000000001/
      000000.blk
      000001.blk
  namespace/
    universidad/
      tarea.pdf
```

### `server/filesystem.py`

```python
NAMESPACE_ROOT: Path    # config.STORAGE_ROOT / "namespace"

def resolve_path(root: Path, remote_path: str) -> Path: ...
```

Los siete puntos de llamada existentes pasan `NAMESPACE_ROOT`. La comprobación
de contención no cambia de forma: cambia contra qué raíz se hace.
`remove_directory` deja de comparar contra `STORAGE_ROOT` y compara contra
`NAMESPACE_ROOT` para su `403 No se puede eliminar la raíz del DFS`.

### `server/blocks.py`

```python
BLOCKS_ROOT: Path       # config.STORAGE_ROOT / "blocks"

def parse_identifiers(file_id: str, index: str) -> tuple[str, int]: ...  # 400
def parse_checksum(header: str | None) -> str: ...                       # 400
def block_path(file_id: str, index: int) -> Path: ...

async def save_block(file_id, index, chunks, checksum, declared_size) -> dict: ...
def read_block(file_id: str, index: int) -> tuple[Path, str]: ...
def delete_blocks(file_id: str) -> dict: ...
```

`save_block` recibe `chunks`, un iterable asíncrono de `bytes`, y no un objeto
`Request`: así el límite de tamaño y el borrado tras un checksum malo se prueban
sin levantar HTTP ni fabricar una petición. `declared_size` es el
`Content-Length`, o `None` si no vino.

`read_block` devuelve la ruta —para que el endpoint la sirva con
`FileResponse`— y el checksum recalculado.

`BLOCKS_ROOT` y `NAMESPACE_ROOT` se leen como variables de módulo en cada
llamada, igual que hoy se lee `STORAGE_ROOT`, para que los tests puedan
sustituirlas. `config.BLOCK_SIZE` se consulta en el momento, no se copia al
importar, para que un test pueda bajar el límite sin recargar el módulo.

---

## 6. Criterios de aceptación

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dado** un bloque válido con su checksum correcto **cuando** se hace `PUT` **entonces** responde `201` con `file_id`, `index`, `size` y `checksum`, y el archivo queda en `blocks/<file_id>/<index:06d>.blk` |
| **AC-02** | **Dado** un bloque ya escrito **cuando** se hace `PUT` sobre el mismo `(file_id, index)` **entonces** responde `409` y el contenido original queda intacto, aunque el cuerpo nuevo sea distinto |
| **AC-03** | **Dado** un cuerpo cuyo SHA-256 no coincide con `X-Block-Checksum` **cuando** se hace `PUT` **entonces** responde `422` y **no queda ningún archivo** en disco para ese bloque |
| **AC-04** | **Dada** una petición sin `X-Block-Checksum`, o con un valor que no es un SHA-256 hexadecimal **cuando** se hace `PUT` **entonces** responde `400 Checksum de bloque inválido` y no se escribe nada |
| **AC-05** | **Dado** un `file_id` que no es un UUID **cuando** se hace `PUT`, `GET` o `DELETE` **entonces** las tres responden `400 Identificador de bloque inválido` |
| **AC-06** | **Dado** un `index` negativo o no numérico **cuando** se hace `PUT` o `GET` **entonces** responde `400` con `detail` en forma de texto, y no el `422` con `detail` en forma de lista que produciría FastAPI |
| **AC-07** | **Dado** un `Content-Length` mayor que `DFSHA_BLOCK_SIZE` **cuando** se hace `PUT` **entonces** responde `413` y **el contador de lecturas del cuerpo se queda en cero**: no se consume ni un trozo |
| **AC-08** | **Dado** un cuerpo que supera el límite y un `Content-Length` que no lo delata **cuando** se hace `PUT` **entonces** responde `413`, el contador de lecturas es mayor que cero pero menor que el total de trozos —se corta a mitad—, y no queda bloque parcial |
| **AC-09** | **Dado** un bloque almacenado **cuando** se hace `GET` **entonces** responde `200` con los bytes idénticos y `X-Block-Checksum` igual al SHA-256 de esos bytes |
| **AC-10** | **Dado** un bloque que no existe en este peer **cuando** se hace `GET` **entonces** responde `404 El bloque no existe` |
| **AC-11** | **Dado** un archivo con tres bloques en este peer **cuando** se hace `DELETE` **entonces** responde `200` con `deleted: 3` y el directorio del archivo desaparece |
| **AC-12** | **Dado** un `file_id` del que este peer no tiene ningún bloque **cuando** se hace `DELETE` **entonces** responde `200` con `deleted: 0` |
| **AC-13** | **Dados** bloques almacenados **cuando** el usuario hace `ls /`, `cd /blocks`, `rmdir /blocks/<file_id>` o `rm /../blocks/<file_id>/000000.blk` **entonces** ninguna de esas operaciones ve ni toca un solo bloque |
| **AC-14** | **Dada** una ruta que se sale de su árbol **cuando** se resuelve **entonces** responde `403`, y esto vale por separado para `namespace/` y para `blocks/` |
| **AC-15** | **Dado** el mismo `file_id` escrito en mayúsculas y en minúsculas **cuando** se usa en `PUT` y luego en `GET` **entonces** ambos se refieren al mismo bloque y existe un solo directorio en disco |

---

## 7. Plan TDD

Orden estricto: escribir el test, verlo fallar, implementar lo mínimo, verde
antes de seguir.

El orden es **primero lo que puede romper lo que ya funciona, después lo nuevo
de menos a más hostil**. El cambio de `resolve_path` toca los ochenta y dos
tests que ya existen; si algo se rompe, tiene que verse antes de haber añadido
un solo endpoint, no mezclado con fallos de código recién escrito.

**Paso 1 — Las dos jaulas** → AC-13, AC-14. Mueve el espacio de nombres a
`namespace/`, crea `blocks/` y parametriza `resolve_path`. El test que manda es
el que recorre las cuatro operaciones del usuario (`ls`, `cd`, `rmdir`, `rm`)
contra rutas que apuntan a los bloques, tanto directas como con `..`. Al
terminar el paso, la suite entera del hito 1 y 2 tiene que seguir verde.

**Paso 2 — Identificadores** → AC-05, AC-06, AC-15. Validación pura, sin disco:
`parse_identifiers` y `parse_checksum`. Va antes que cualquier endpoint porque
las tres operaciones la comparten, y porque AC-06 es una afirmación sobre la
forma del error que hay que fijar antes de que FastAPI imponga la suya.

**Paso 3 — Escritura y WORM** → AC-01, AC-02. El caso feliz obliga al layout,
al directorio del archivo y al nombre con seis dígitos. El `409` justo después,
porque la inmutabilidad es la propiedad que define este almacén y no una
comprobación más.

**Paso 4 — Integridad** → AC-03, AC-04. Primero la cabecera ausente o
malformada, que se rechaza sin escribir nada; después el checksum que no cuadra,
que es el primer caso que obliga a **deshacer** algo ya escrito.

**Paso 5 — Límite de tamaño** → AC-07, AC-08. El último del camino de escritura
porque es el único que exige consumir el cuerpo por trozos y abortar a mitad.

Aquí el código de respuesta **no prueba nada**: con un cuerpo grande, los dos
caminos devuelven `413`. Si alguien quitara la comprobación previa de
`Content-Length` y dejara solo la acumulación, un test que mirase el status
seguiría pasando sin probar lo que dice. Lo que discrimina es **cuánto se leyó**,
así que `save_block` recibe el cuerpo como un iterable asíncrono y los tests le
pasan un doble que cuenta trozos y bytes:

- AC-07 exige `lecturas == 0`. Es el test que se cae si desaparece la
  comprobación de `Content-Length`.
- AC-08 exige `0 < lecturas < total`. Es su complemento: demuestra que el otro
  camino sí consume, y que se corta antes de agotar el cuerpo.

Los dos juntos fijan la diferencia entre las dos defensas; ninguno de los dos
por separado lo hace.

**Paso 6 — Lectura** → AC-09, AC-10. Después de la escritura porque reutiliza
todo lo anterior y añade una sola cosa: el checksum recalculado.

**Paso 7 — Borrado** → AC-11, AC-12. El último porque es el más simple y el que
menos sujeta: dos casos, con bloques y sin ellos.

---

## 8. Tests límite

| Caso | Esperado | Por qué importa |
|---|---|---|
| Bloque de 0 bytes | `201`, checksum de la cadena vacía | Rechazarlo exigiría un código que CONTRATOS no tiene; es inmutable y verificado como cualquier otro |
| Bloque de exactamente `DFSHA_BLOCK_SIZE` | `201` | El contrato dice «supera», no «alcanza»: el límite es inclusivo y es un error de uno clásico |
| Bloque de `DFSHA_BLOCK_SIZE + 1` | `413` | El otro lado del mismo borde |
| `index` con ceros delante en la URL (`/007`) | El mismo bloque que `/7`, escrito en `000007.blk` | Si no, el mismo bloque tendría dos nombres y el archivo se leería incompleto |
| `index` con un `+` o espacios (`/+7`, `/ 7`) | `400` | `int()` de Python acepta cosas que una ruta no debería aceptar |
| `file_id` con forma de travesía (`../../etc`) | `400`, no `403` | Nunca llega a ser una ruta: lo para la validación de UUID, antes de `resolve_path` |
| `X-Block-Checksum` en mayúsculas | `201`, y el cuerpo de la respuesta lo devuelve en minúsculas | Comparar sensible a mayúsculas rechazaría clientes correctos; CONTRATOS fija minúscula solo para lo que se emite |
| `X-Block-Checksum` con longitud distinta de 64, o con caracteres no hexadecimales | `400 Checksum de bloque inválido` | Un checksum truncado que se comparase «como venga» nunca coincidiría, y daría `422` en vez de decir la verdad |
| `DELETE` dos veces seguidas | `200` con `deleted: 3`, y después `200` con `deleted: 0` | Es la definición operativa de idempotente, y lo que la SPEC-04 va a necesitar |
| `GET` de un `index` que no existe en un archivo que sí | `404` | El directorio del archivo existe; el error tiene que venir del bloque, no del archivo |
| `PUT` de dos índices distintos del mismo `file_id` | Dos `.blk` en un solo directorio | Es el caso normal del particionado y conviene tenerlo fijado explícitamente |

---

## 9. Tareas

1. Añadir `NAMESPACE_ROOT` a `server/filesystem.py` y cambiar la firma de
   `resolve_path` a `(root, remote_path)`, sin valor por defecto.
2. Actualizar los siete puntos de llamada y la comparación de raíz de
   `remove_directory`.
3. Rehacer el fixture `storage` de `tests/conftest.py` para que cree los dos
   árboles y sustituya las dos raíces, **devolviendo la del espacio de nombres**
   para que los tests del hito 1 no cambien ni una línea.
4. Crear `server/blocks.py` con `BLOCKS_ROOT`, `parse_identifiers`,
   `parse_checksum`, `block_path`, `save_block`, `read_block` y `delete_blocks`.
5. Añadir los tres endpoints a `server/main.py`, sin lógica: `PUT` como
   `async def` para poder consumir `request.stream()`.
6. Escribir `tests/test_blocks.py` y `tests/test_jaulas.py` siguiendo el plan
   del punto 7.

---

## 10. Definición de Done

1. Cada uno de los quince criterios de aceptación tiene al menos un test que lo
   cubre.
2. Los once tests límite de la sección 8 están escritos y pasan.
3. La suite pasa completa. De los ochenta y dos tests que ya existían, ochenta y
   uno no cambian ni una línea: el traslado de raíces se absorbe entero en el
   fixture `storage`, que ahora devuelve la raíz del espacio de nombres. El
   único que se toca es `test_filesystem_toma_su_raiz_de_config`, porque
   afirmaba literalmente lo que esta SPEC cambia —que `filesystem` es dueño de
   `STORAGE_ROOT` entero— y se sustituye por la afirmación equivalente sobre los
   dos árboles. Mantenerlo verde habría exigido dejar en `filesystem` un alias
   muerto de `STORAGE_ROOT` que nadie usa.
4. `resolve_path` no tiene valor por defecto para `root`, y es la única
   comprobación de contención del sistema: no existe ninguna otra en ningún
   módulo.
5. Ninguna ruta de bloque se construye concatenando texto que venga del cliente
   sin haber pasado por `parse_identifiers`.
6. Ningún error de la API de bloques responde con `detail` en forma de lista.
7. Tras correr la suite, ni `server/storage/` ni ningún árbol de bloques queda
   con archivos residuales.
8. Un `PUT` que falla —por tamaño, por checksum o por identificador— no deja
   ningún archivo en `blocks/`, ni completo ni parcial.
9. No existe ningún endpoint ni función de metadatos, enrutamiento o
   replicación, y `send` y `receive` siguen exactamente como estaban.
10. Ningún archivo de producción fue escrito antes que su test.
