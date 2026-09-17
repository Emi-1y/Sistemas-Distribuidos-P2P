# SPEC-05 — Cliente distribuido: `send` y `receive` en tres tiempos

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-05` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/distributed-client` |
| **dependencias** | `docs/CONTRATOS.md` §Convenciones, §Bloques, §Metadatos. `POST /files/allocate`, `POST /files/commit`, `GET /files/lookup` y `GET /ring`, todos ya en pie (SPEC-02 y SPEC-04). `PUT` y `GET /blocks/{file_id}/{index}` (SPEC-03). `commands.connect` y `commands.build_path` (SPEC-02). |
| **dependencias de paquete** | Ninguna nueva. `concurrent.futures` y `hashlib` son de la biblioteca estándar; `requests` ya lo usa el cliente. |

---

## 2. Objetivo

Devolver al CLI las dos operaciones que mueven datos, ahora repartidas: `send`
trocea el archivo y sube los bloques **en paralelo y directos a los peers de
datos**; `receive` hace `lookup`, baja en paralelo, reensambla por índice y
verifica el checksum de cada bloque antes de escribir nada.

Al cerrar esta SPEC:

- `send archivo.bin` recorre los tres tiempos —`allocate`, bloques, `commit`—
  sin que el usuario sepa que existen.
- `receive archivo.bin` devuelve un archivo **idéntico byte a byte** al que se
  subió, o no devuelve ninguno.
- Ningún byte de un bloque atraviesa un peer que no sea destino de ese bloque.
- El CLI recupera sus nueve operaciones y **ninguna cambia de cara al usuario**.

Esto no es una mejora: la SPEC-04 retiró `send` y `receive`, así que hoy el
sistema no mueve datos. Esta SPEC es lo que lo devuelve a funcionar, y por eso
todo lo que se pueda dejar fuera se queda fuera.

---

## 3. Contexto y decisiones

Las tres piezas ya existen y están probadas por separado: el anillo sabe quién
guarda qué, el almacén de bloques acepta y sirve bloques verificados, y los
metadatos saben reservar, confirmar y encontrar una entrada. Lo que no existe es
**quien las junta**. El guion de verificación en vivo de la SPEC-04 ya recorre
los tres tiempos a mano contra tres peers reales y funciona; esta SPEC es meter
ese guion dentro del cliente.

- **El que orquesta es el cliente, y no hay ningún endpoint que suba un
  archivo.** La alternativa obvia —resucitar `POST /files/upload` para que un
  peer reciba el archivo entero, lo trocee y reparta los bloques— es justo lo que
  la SPEC-03 decidió evitar cuando dejó el almacén de bloques tonto: **cada byte
  cruzaría un peer que no es su destino**, duplicando el tráfico y convirtiendo
  al peer conectado en el cuello de botella de todo el sistema. Con el cliente
  orquestando, ese peer ve tres peticiones pequeñas de metadatos y ni un solo
  byte de datos.

  La consecuencia es la que da nombre a la SPEC: **el cliente habla con un peer
  para metadatos y con N para datos**. Es la primera vez que el cliente conoce la
  topología, y es inevitable: nadie más sabe dónde acabaron los bloques.

- **El cliente consulta el anillo, no lo calcula.** El plan de `allocate` trae
  `peer_id`, y para emitir un `PUT` hace falta una dirección. Esa traducción sale
  de `GET /ring`, no de importar `server.ring` en el cliente. Recalcular el
  anillo aquí exigiría que el cliente leyera también `DFSHA_VNODES` y la
  membresía, y el día que las dos copias discreparan los bloques se escribirían
  en un peer y se buscarían en otro. Es la misma postura que ya tomó
  `bootstrap_addresses`: de la lista de bootstrap se queda con la URL y descarta
  el `peer_id`, porque el anillo se pregunta.

  El anillo se pide **una vez por operación** y no se cachea entre comandos: la
  membresía cambia, y un mapa viejo manda bytes a una dirección muerta.

- **Se sube a todos los peers que el plan nombra para cada bloque, no al
  primero.** Hoy `REPLICAS = 1` y esa lista tiene un elemento, así que el bucle
  da una vuelta. Escribir `bloque["peers"][0]` costaría lo mismo y rompería en el
  cliente la propiedad que la SPEC-04 se molestó en probar: subir la constante a
  3 no obliga a tocar código. Con factor 3 el `commit` exige tres peers
  **distintos** por bloque, y un cliente que subiera solo al primero provocaría un
  `422` que nadie sabría leer.

  Por simetría, al bajar se recorre la misma lista y vale el primer peer que
  conteste. Con factor 1 el bucle da una vuelta y no hay a quién recurrir; con
  factor 3, la lectura tolera un peer caído sin una línea más.

- **El `commit` va después de todos los `201`, y un solo bloque que falle lo
  cancela.** No hay commits parciales: el contrato exige todos los índices, y esa
  es exactamente la regla que hace que un archivo a medias no exista. Si un `PUT`
  falla, el cliente aborta, avisa y **no confirma**.

  Lo que queda entonces es una entrada `pending`: invisible en `ls` y en
  `lookup`, con el nombre ocupado, y con los bloques que sí subieron ocupando
  disco. No se implementa ninguna limpieza automática, y no hace falta: `rm` de
  esa ruta borra la entrada y —porque los `peers` planeados son justo donde el
  cliente estaba subiendo— lanza el `DELETE /blocks` a los peers correctos. La
  salida ya existe y es un comando que el usuario tiene. La recogida automática
  de huérfanos sigue donde estaba: en el handoff del hito 3.

  Y **se prueba entera, no se promete**: decir «ya hay una salida» sin
  demostrarlo dejaría al usuario con un nombre ocupado que no sabe soltar, que
  es peor que no tener salida, porque nadie la buscaría. El AC-21 recorre el
  ciclo completo —el `PUT` que revienta, la entrada pendiente e invisible, y el
  `rm` que libera el nombre y se lleva los bloques que sí subieron— y es el
  único test que habla con un peer de verdad, porque la mitad de lo que afirma
  es del servidor y no del cliente.

- **El checksum que manda es el de la entrada, y el cliente lo recalcula.** Al
  bajar, el peer devuelve `X-Block-Checksum`, pero esa cabecera es lo que **ese
  peer** anotó: si el bloque se corrompió en su disco, la cabecera se corrompió
  con él. La autoridad es la entrada de metadatos, que es lo que se confirmó en
  el `commit`. Así que el cliente calcula el SHA-256 de los bytes que le llegan y
  lo compara con el de la entrada. Eso detecta a la vez la corrupción en disco y
  la del transporte, y no depende de que el peer sea honesto.

  No se añade un checksum del archivo completo. Obligaría a cambiar el esquema de
  la entrada que la SPEC-04 congeló, y no diría nada que la suma de los bloques
  no diga ya.

- **El reensamblado es por índice, y el archivo local se escribe una sola vez, al
  final.** Bajar en paralelo significa que los bloques llegan desordenados;
  concatenarlos por orden de llegada daría un archivo corrupto que ningún
  checksum por bloque detectaría, porque cada bloque estaría intacto. Se guardan
  en un diccionario por índice y se unen en orden.

  Que la escritura sea única y posterior a la verificación de todos los bloques
  sale gratis y regala una propiedad: **un fallo a mitad de `receive` no deja un
  archivo local a medias**. O está entero y verificado, o no está.

  El precio, escrito para no fingir que no existe: el archivo bajado se sostiene
  entero en memoria. Con los tamaños de este proyecto no es un problema; con un
  archivo mayor que la RAM, sí. El reensamblado en streaming —cada bloque a su
  offset en un archivo temporal— va al handoff del hito 3.

  Al subir no hace falta: el archivo local ya está en disco y cada bloque se lee
  con un `seek` a su offset. La asimetría es deliberada.

- **El troceado y el reensamblado viven en su propio módulo, sin red.** Es la
  misma separación que hay entre `metadata` y `routing`, y por el mismo motivo:
  `transfer.py` decide **qué trozo es cada bloque y cómo se vuelven a juntar**;
  `commands.py` decide **a quién se le pide**. Así la aritmética de los offsets
  —el error de uno del último bloque, el archivo vacío, el múltiplo exacto— se
  prueba entera sin un socket, y cuando algo falla en la otra mitad se sabe que
  el fallo es de la red.

- **El paralelismo son hilos con un tope, no `asyncio`.** El cliente es síncrono
  y usa `requests`; meter un bucle de eventos obligaría a cambiar el REPL entero
  y a añadir una dependencia. Un `ThreadPoolExecutor` acotado hace lo mismo en
  cuatro líneas, y las peticiones HTTP son espera de red, que es exactamente lo
  que los hilos resuelven bien.

  El tope existe porque no tenerlo no es «más rápido»: un archivo de 100 MiB son
  25 bloques, y abrir 25 conexiones simultáneas contra tres peers no gana nada y
  puede agotar descriptores. `BLOQUES_EN_PARALELO = 8`, en un solo sitio.

- **`send` y `receive` no cambian de cara al usuario.** Mismos nombres, mismos
  argumentos, mismas validaciones previas del cliente, y los errores del servidor
  impresos tal cual con `print_error`, como el resto de comandos. El usuario no
  ve `allocate`, ni bloques, ni peers. Que la implementación de debajo no se
  parezca en nada a la del hito 1 es asunto nuestro.

- **El límite de 10 MB de la SPEC-01 no vuelve.** Existía porque un peer escribía
  el archivo entero en su disco; ahora el archivo va repartido y el único límite
  que queda es el del bloque, que aplica el almacén con su `413` y que el cliente
  no puede violar, porque trocea con el `block_size` que le dio `allocate`.

- **Esta SPEC no añade ni cambia un solo endpoint.** Es la comprobación de que
  los contratos de las SPECs 02–04 estaban bien puestos: si hubiera que abrir un
  endpoint para que el cliente funcione, sería que algo se diseñó mal. El guion
  de verificación de la SPEC-04 ya demostró que no hace falta.

- **Pero sí toca `server/` una vez, y no era opcional.** Lo descubrió el primer
  `send` de verdad. `resolve_path` compara la ruta resuelta con su raíz, y en
  Windows `Path.resolve()` devuelve **a veces** la forma larga `\\?\C:\...`, que
  es la misma ruta con otra ancla: `\\?\C:\` en vez de `C:\`. Comparada con una
  raíz normal, la jaula rechazaba rutas que estaban dentro de ella y respondía
  `403` a un bloque perfectamente legítimo.

  Qué rama toma la resolución depende de si el directorio existe **en ese
  instante**, así que el fallo solo aparece cuando dos escrituras del mismo
  archivo corren a la vez. Nadie escribía bloques en paralelo hasta ahora: el
  bug estaba puesto desde la SPEC-03 y esta SPEC es la primera que lo pisa. En
  una tanda de 120 escrituras concurrentes fallaban 12.

  El arreglo normaliza **los dos lados** de la comparación, no uno: el objetivo
  es comparar peras con peras, no ablandar la jaula. Sigue siendo la única
  comprobación de contención del sistema y sigue rechazando lo que se sale, con
  dos tests nuevos en `test_jaulas.py` que lo fijan: uno concurrente, que es la
  única forma de recorrer esa rama, y uno con la raíz en forma larga.

  Que la Definición de Done dijera «no se ha tocado `server/`» y ahora diga otra
  cosa es la parte honesta: la afirmación era una predicción, no un requisito, y
  el requisito de verdad —ningún endpoint nuevo ni modificado— se mantiene.

---

## 4. Alcance

### Incluye

- `client/transfer.py`: troceado, checksum, reensamblado por índice y el lote en
  paralelo con su tope.
- `send`: `allocate` → bloques en paralelo y directos → `commit`, con los peers y
  los checksums **reportados**.
- `receive`: `lookup` → bloques en paralelo → verificación → reensamblado →
  escritura única.
- La traducción `peer_id` → dirección vía `GET /ring`.
- La retirada de los dos avisos de «no disponible todavía» que dejó la SPEC-04.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Endpoints nuevos o cambios en los que hay. Lo único que se toca de `server/`
  es el arreglo de la jaula que la sección 3 explica, y llega con sus tests.
- Reintentos, backoff o reparación de un bloque que falla. Un fallo aborta.
- Recogida automática de los bloques huérfanos de un `send` abortado. Hoy la hace
  `rm`, que ya existe.
- Reanudación de una subida o una bajada interrumpida, y subidas parciales.
- Barras de progreso y cualquier otra salida que no sea el mensaje final.
- Reensamblado en streaming: el archivo bajado se sostiene en memoria.
- Un checksum del archivo completo, que cambiaría el esquema de la entrada.
- Caché del anillo entre operaciones.
- Sobrescritura, ni en el DFS ni en el disco local. Las dos responden que ya
  existe.
- Cualquier garantía frente a dos clientes que suban la misma ruta a la vez.

---

## 5. Diseño y contratos

No hay contratos nuevos. Los tres tiempos son las llamadas que ya están en
`docs/CONTRATOS.md`, en este orden:

| Tiempo | Llamada | A quién | Qué se saca |
|---|---|---|---|
| 0 | `GET /ring` | el peer conectado | `peer_id` → dirección |
| 1 | `POST /files/allocate` | el peer conectado | `file_id`, `block_size` y el plan de bloques |
| 2 | `PUT /blocks/{file_id}/{index}` | **cada peer de datos**, en paralelo | un `201` por bloque |
| 3 | `POST /files/commit` | el peer conectado | la entrada `committed` |

Y la lectura, tres:

| Tiempo | Llamada | A quién |
|---|---|---|
| 0 | `GET /ring` | el peer conectado |
| 1 | `GET /files/lookup?path=` | el peer conectado |
| 2 | `GET /blocks/{file_id}/{index}` | **cada peer de datos**, en paralelo |

Los tiempos 1 y 3 pueden acabar en cualquier peer: si la clave no es del peer
conectado, él la reenvía al dueño y devuelve su respuesta tal cual. Al cliente
eso no le consta ni le importa. El tiempo 2 **nunca se reenvía**: la dirección
sale del anillo y el bloque aterriza en su destino.

### `client/transfer.py` — el troceado, sin red

```python
BLOQUES_EN_PARALELO = 8

class TransferError(Exception): ...

def checksum(data: bytes) -> str: ...
def read_chunk(path: Path, index: int, block_size: int, size: int) -> bytes: ...
def assemble(piezas: dict[int, bytes], bloques: list[dict]) -> bytes: ...
def in_parallel(tareas: list) -> list: ...
```

`read_chunk` hace `seek` al offset `index * block_size` y lee `size` bytes. Si
lee menos de los que el plan dice, levanta `TransferError`: el archivo cambió de
tamaño después del `allocate` y el plan ya no lo describe.

`assemble` exige que estén **todos** los índices del plan y concatena en orden de
índice, no de llegada. Devuelve los bytes; no escribe.

`TransferError` es la única excepción propia, y existe para que `commands` sepa
distinguir «esto lo rompimos nosotros» de un `requests.RequestException`, que es
la red. Sin ella habría que elegir entre levantar `ValueError` —que también
levantan la mitad de las bibliotecas estándar— o devolver `None` y comprobarlo
en cada llamada.

`in_parallel` recibe callables, los reparte en un `ThreadPoolExecutor` de
`BLOQUES_EN_PARALELO` y devuelve los resultados **en el orden de entrada**. El
primer fallo se propaga; las tareas ya lanzadas terminan, pero no habrá `commit`
ni archivo, que es lo único que importa.

Este módulo **no importa `requests`**. Es lo que permite probar toda la
aritmética sin levantar nada.

### `client/commands.py` — la red, sin aritmética

```python
def peer_addresses() -> dict[str, str]: ...   # GET /ring
def send(current_path: str, filename: str): ...
def receive(current_path: str, filename: str): ...
```

`send` construye la ruta remota con `build_path(current_path, basename)`, igual
que en la SPEC-01: el archivo aterriza en el directorio remoto actual con su
nombre. `receive` escribe en el directorio de trabajo local, con el nombre de la
ruta remota, y **no sobrescribe**: si ya hay un archivo con ese nombre, avisa y
no baja nada.

Los errores del servidor se imprimen con `print_error`, sin reinterpretar: si
`allocate` responde `409 El archivo ya existe`, eso es lo que el usuario lee.

---

## 6. Criterios de aceptación

### Fase 1 — troceado y reensamblado, sin red

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dado** un archivo local y un plan de bloques **cuando** se lee el bloque `i` **entonces** devuelve exactamente los bytes de `[i*block_size, i*block_size+size)` |
| **AC-02** | **Dado** un trozo de bytes **cuando** se le calcula el checksum **entonces** es su SHA-256 en hexadecimal minúscula |
| **AC-03** | **Dadas** las piezas de un archivo en desorden **cuando** se reensamblan **entonces** el resultado es idéntico al original: ordenado por índice y no por llegada |
| **AC-04** | **Dadas** unas piezas a las que les falta un índice del plan **cuando** se reensamblan **entonces** levanta y no devuelve contenido a medias |
| **AC-05** | **Dadas** más tareas que `BLOQUES_EN_PARALELO` **cuando** se ejecutan **entonces** corren de verdad a la vez, como mucho `BLOQUES_EN_PARALELO` simultáneas, y los resultados vuelven en el orden de entrada |
| **AC-06** | **Dada** una tarea que falla **cuando** se ejecuta el lote **entonces** el fallo se propaga a quien llamó |

### Fase 2 — `send`

| ID | Given / When / Then |
|---|---|
| **AC-07** | **Dado** un archivo local **cuando** se hace `send` **entonces** ocurren los tres tiempos y **en orden**: `allocate`, los `PUT`, y `commit` el último |
| **AC-08** | **Dado** un plan cuyos bloques caen en peers distintos del conectado **cuando** se suben **entonces** cada `PUT` va a la dirección que `GET /ring` da para **ese** peer, y el peer conectado no recibe ni un byte de datos |
| **AC-09** | **Dados** los bloques subidos **cuando** se confirma **entonces** el `commit` lleva los checksums calculados por el cliente y los peers que respondieron `201`, no el plan repetido sin más |
| **AC-10** | **Dado** un archivo de varios bloques **cuando** se hace `send` **entonces** los `PUT` se lanzan en paralelo, no uno detrás de otro |
| **AC-11** | **Dado** un `PUT` que falla **cuando** se hace `send` **entonces** no se envía `commit` y se avisa al usuario — que el archivo tampoco se vea es del servidor, y lo prueba el AC-21 |
| **AC-12** | **Dado** un `allocate` que responde error **cuando** se hace `send` **entonces** se imprime su código y su detalle tal cual y no se sube ni un bloque |
| **AC-13** | **Dado** un archivo local que no existe, o un nombre vacío **cuando** se hace `send` **entonces** se avisa sin emitir ninguna petición |
| **AC-14** | **Dado** un archivo de cero bytes **cuando** se hace `send` **entonces** hay `allocate`, ningún `PUT`, y un `commit` con la lista vacía que lo deja confirmado |
| **AC-15** | **Dado** un plan con varios peers por bloque **cuando** se hace `send` **entonces** el bloque se sube a **todos** ellos y todos se reportan en el `commit` |

### Fase 2 — `receive`

| ID | Given / When / Then |
|---|---|
| **AC-16** | **Dada** una entrada confirmada **cuando** se hace `receive` **entonces** se baja cada bloque de su peer, en paralelo, y el archivo local queda idéntico byte a byte al original |
| **AC-17** | **Dado** un bloque cuyos bytes no cuadran con el checksum **de la entrada** **cuando** se hace `receive` **entonces** se avisa y **no se escribe ningún archivo local** |
| **AC-18** | **Dado** un `lookup` que responde `404` **cuando** se hace `receive` **entonces** se imprime tal cual, no se crea nada y no se pide ni un bloque |
| **AC-19** | **Dado** un archivo local que ya existe con ese nombre **cuando** se hace `receive` **entonces** se avisa, no se sobrescribe y no se emite ninguna petición de bloque |
| **AC-20** | **Dado** un bloque con varios peers, el primero de los cuales no responde **cuando** se hace `receive` **entonces** se pide al siguiente; si no responde ninguno, se aborta sin escribir |

### Fase 2 — el ciclo del fallo, contra un peer real

| ID | Given / When / Then |
|---|---|
| **AC-21** | **Dado** un `send` cuyo `PUT` revienta a mitad **cuando** termina **entonces** (a) no hubo `commit`, (b) el archivo no aparece en `ls` y `lookup` responde `404`, (c) el nombre **está ocupado**: repetir el `send` responde `409`, y (d) `rm` de esa ruta responde `200`, libera el nombre —un `send` posterior vuelve a funcionar— y borra los bloques que sí habían subido |

Es el único criterio que necesita un peer de verdad: los puntos (c) y (d)
afirman cosas del servidor, no del cliente, y contra un peer falso serían la
maqueta confirmándose a sí misma.

---

## 7. Plan TDD

Orden estricto: escribir el test, verlo fallar, implementar lo mínimo, verde
antes de seguir. Y en dos fases, por el mismo motivo que en la SPEC-04: la
aritmética y la red se rompen por causas distintas, y mezclarlas haría que un
archivo corrupto no dijera si el error está en los offsets o en a quién se le
pidió cada bloque.

### Fase 1 — `client/transfer.py`, sin un socket

**Paso 1 — Checksum y troceado** → AC-01, AC-02. Lo primero porque todo lo demás
se apoya en ello. Aquí entran los tres bordes de la aritmética: el múltiplo
exacto, el byte de más y el archivo vacío.

**Paso 2 — Reensamblado** → AC-03, AC-04. Con las piezas deliberadamente
desordenadas: un test que las pase en orden no probaría nada, porque el bug que
se busca es exactamente concatenar por llegada.

**Paso 3 — El lote en paralelo** → AC-05, AC-06. La concurrencia se prueba con
una barrera, no con esperas: si las tareas se ejecutaran en serie la barrera no
se completaría y el test fallaría por tiempo, en vez de pasar por casualidad.

### Fase 2 — `send` y `receive`, contra un peer falso

Se sustituye `commands.requests`, como ya hace `test_client_bootstrap.py`: un
doble que registra método, URL, cabeceras y cuerpo, y responde lo que el test le
diga. Ningún test abre un socket.

**Paso 4 — `send`, el camino feliz** → AC-07, AC-08, AC-09, AC-10. Obliga a
todo: el anillo, el troceado, los `PUT` directos, el paralelismo y el `commit`
con lo reportado. El test comprueba la **secuencia** de llamadas, porque el orden
es parte del contrato.

**Paso 5 — `send`, los rechazos** → AC-11, AC-12, AC-13, AC-14. De menos a más
sutil: no hay archivo, el `allocate` dice que no, un bloque falla y no hay
commit, y el archivo vacío, que es el único caso en que cero `PUT` es lo
correcto.

**Paso 6 — `receive`, el camino feliz** → AC-16. El de ida y vuelta: se sube un
contenido conocido y se comprueba que lo que se escribe en disco es exactamente
ese, con las respuestas de los bloques llegando en orden inverso.

**Paso 7 — `receive`, los rechazos** → AC-17, AC-18, AC-19. Los tres comparten
una afirmación, y es la importante: **no queda archivo local**.

**Paso 8 — El factor de réplica** → AC-15, AC-20. Un plan con dos peers por
bloque: se sube a los dos, se reportan los dos, y al bajar el segundo cubre al
primero. Es el gancho del hito 3, probado en el cliente como ya lo está en el
servidor.

**Paso 9 — El ciclo del fallo, de punta a punta** → AC-21. El último, porque
necesita que `send` funcione y que sepa fallar. Se monta un peer real con el
`TestClient` de FastAPI y se hace que `commands.requests` hable con él, de modo
que el cliente ejerza el sistema entero sin abrir un socket. El `PUT` de un
bloque se hace reventar, y se comprueban los cuatro síntomas en orden: no hay
commit, no se ve, el nombre está ocupado, y `rm` lo suelta. La afirmación que
cierra el test es que el `send` **repetido después del `rm` funciona**: sin ella
solo se habría probado que `rm` devuelve `200`.

---

## 8. Tests límite

| Caso | Esperado | Por qué importa |
|---|---|---|
| Archivo de 0 bytes | Sube sin ningún `PUT` y baja como archivo local de 0 bytes | Un archivo vacío es válido y no tiene ni un bloque; el bucle tiene que aguantar la lista vacía |
| Tamaño múltiplo exacto de `block_size` | Sin bloque final corto | El error de uno clásico, ahora del lado del cliente |
| Un byte más que un múltiplo | Un bloque final de un byte | El otro lado del mismo borde |
| El archivo local se encoge entre el `allocate` y la lectura | Se aborta sin `commit` | El plan describe un archivo que ya no existe; confirmarlo guardaría metadatos que mienten |
| Las respuestas de los bloques llegan en orden inverso | Archivo idéntico | Es el bug que introduce el paralelismo y que ningún checksum por bloque detecta |
| Más bloques que `BLOQUES_EN_PARALELO` | Todos suben, en tandas | Que el tope acote y no descarte |
| El plan nombra un `peer_id` que `GET /ring` no conoce | Se aborta antes de subir un byte | El anillo cambió entre el `allocate` y el `PUT`; escribir a ciegas dejaría bloques que nadie encontrará |
| Cuerpo correcto y `X-Block-Checksum` que miente | Se acepta | La autoridad es la entrada, no la cabecera del peer |
| Cuerpo corrupto y `X-Block-Checksum` que coincide con la entrada | Se rechaza | El otro lado de lo mismo: creer la cabecera sería no verificar nada |
| `receive` de una ruta que es un directorio | El `404` del `lookup`, tal cual | `lookup` es de archivos, y el mensaje es del servidor |
| `send` de un nombre que ya existe en el DFS | El `409` del servidor, sin subir bloques | La colisión la decide el dueño de la entrada, no el cliente |

---

## 9. Tareas

1. Crear `client/transfer.py` con el checksum, el troceado, el reensamblado por
   índice y el lote en paralelo con su tope.
2. Escribir `tests/test_transfer.py` (fase 1), siguiendo los pasos 1 a 3.
3. Añadir `peer_addresses` a `client/commands.py`.
4. Reescribir `send`: `allocate`, los `PUT` en paralelo a todos los peers de cada
   bloque, y el `commit` con los checksums y los peers reportados.
5. Reescribir `receive`: `lookup`, los `GET` en paralelo, la verificación contra
   la entrada, el reensamblado y la escritura única.
6. Escribir `tests/test_client_transfer.py` (fase 2), siguiendo los pasos 4 a 9,
   incluido el ciclo del fallo contra un peer real.
7. Comprobar el conjunto contra tres peers reales con el guion de verificación en
   vivo de la SPEC-04, sustituyendo sus llamadas a mano por `send` y `receive`.
8. Anotar en `docs/SPEC-06.md` lo que salga de esa comprobación.

---

## 10. Definición de Done

1. Cada uno de los veintiún criterios de aceptación tiene al menos un test que lo
   cubre.
2. Los once tests límite de la sección 8 están escritos y pasan.
3. La suite pasa completa —251 tests— y los 205 que ya había **no cambian ni
   una línea**: esta SPEC solo añade.
4. No hay endpoints nuevos ni modificados. De `server/` se toca una sola cosa,
   la comparación de `resolve_path`, y por una razón que la sección 3 explica:
   un `403` intermitente que el primer `send` concurrente destapó. Llega con sus
   dos tests y no relaja la jaula.
5. `client/transfer.py` no importa `requests`, y `client/commands.py` no
   reimplementa el troceado ni el reensamblado.
6. El cliente no importa nada de `server/`: la topología se consulta, no se
   calcula.
7. Ningún test de la fase 1 sustituye nada de `requests` ni abre un socket.
8. Los bytes de un bloque solo viajan entre el cliente y un peer que es destino
   de ese bloque: ningún `PUT` ni `GET` de bloque va al peer conectado por ser el
   conectado.
9. Ningún fallo parcial deja un archivo local a medias, ni una entrada confirmada
   con bloques que no están.
10. La salida de un `send` abortado está **probada, no prometida**: hay un test
    que la recorre entera contra un peer real y que termina volviendo a subir el
    mismo archivo con éxito.
11. `send` y `receive` ya no imprimen el aviso de la SPEC-04, el CLI vuelve a sus
    nueve operaciones, y ninguna cambia de cara al usuario.
12. `BLOQUES_EN_PARALELO` aparece en un solo sitio, y subir el factor de réplica
    del servidor no obliga a tocar el cliente.
13. Ningún test abre un socket, ni siquiera el del ciclo del fallo.
14. Ningún archivo de producción fue escrito antes que su test.
