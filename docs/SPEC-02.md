# SPEC-02 — Anillo de hashing consistente y membresía de peers

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-02` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/ring-membership` |
| **dependencias** | `docs/CONTRATOS.md` §Convenciones, §Anillo y membresía, §Colocación. `STORAGE_ROOT` en `server/filesystem.py`, cuyo origen se traslada aquí. |
| **dependencias de paquete** | Ninguna nueva. El anillo usa `hashlib` y `bisect` de la biblioteca estándar; la propagación del alta usa `httpx`, que ya está en `requirements.txt` porque `TestClient` lo arrastra. |

---

## 2. Objetivo

Dotar al sistema de la única pieza que permite que N peers simétricos, sin
autoridad central y sin preguntarle nada a nadie, coincidan en **quién es el
dueño de cada clave**: un anillo de hashing consistente con nodos virtuales, y
la membresía que lo alimenta.

Al cerrar esta SPEC:

- Cualquier peer sabe, para una clave dada, qué lista de `peer_id` le
  corresponde, y a qué dirección HTTP se le habla a cada uno.
- Tres peers arrancados con la misma lista de bootstrap calculan **el mismo
  anillo**, en procesos distintos, sin haberse hablado nunca.
- Un peer que no estaba en esa lista puede entrar con `POST /peers/join` y los
  demás se enteran.
- La identidad, la dirección, el directorio de datos y el tamaño de bloque de
  cada peer vienen del entorno, no del código; y el cliente conoce una lista de
  peers, no un servidor.

No se toca ni un archivo, ni un bloque, ni un metadato. Esta SPEC solo produce
la respuesta a la pregunta "¿de quién es esta clave?".

---

## 3. Contexto y decisiones

Hasta ahora había un servidor. A partir de aquí hay N peers iguales, y todo el
hito 2 descansa en una sola propiedad: **si dos peers calculan dueños distintos
para la misma clave, el sistema se rompe en silencio** — un bloque se escribe
donde nadie lo va a buscar, y el error no aparece hasta que alguien intenta
leer. Casi todas las decisiones de abajo existen para blindar esa propiedad.

- **Hashing consistente sobre un anillo, no round robin ni módulo N.** Ya estaba
  decidido, y esta SPEC es donde se paga: con `hash(key) % N`, entrar o salir un
  nodo cambia el dueño de *casi todas* las claves, y habría que mover casi todos
  los bloques. En el anillo, entrar un nodo solo remapea ~1/N de las claves y
  ninguna de las demás se mueve. Sin esa propiedad, la replicación del hito 3 no
  tendría dónde apoyarse.

- **El hash es SHA-1 truncado a 64 bits, nunca `hash()` de Python.** Esta es la
  decisión más fácil de pasar por alto y la que más caro sale. `hash()` de
  cadenas está aleatorizado por proceso vía `PYTHONHASHSEED`: tres peers
  arrancados con la misma membresía calcularían **tres anillos distintos**, y el
  fallo sería intermitente e invisible. SHA-1 da el mismo entero en cualquier
  proceso, máquina y versión. No se usa por criptografía —aquí solo hace falta
  reparto uniforme y determinismo— sino porque está en la estándar y es rápido.
  Se trunca a 64 bits porque un entero de ese tamaño ya hace las colisiones
  irrelevantes y es cómodo de comparar y de imprimir al depurar.

- **128 nodos virtuales por peer.** Con tres peers físicos, tres puntos al azar
  en el anillo dejan sectores muy desiguales: un archivo de tres bloques puede
  caer 3/0/0 y el reparto que este hito tiene que demostrar no se ve. Repartir
  cada peer en 128 posiciones (`sha1(f"{peer_id}#{i}")`) hace que la desviación
  del reparto caiga aproximadamente con la raíz de ese número, y además reparte
  el trasvase: cuando entra un peer se lleva 1/N de las claves **en 128 trozos
  pequeños tomados de todos los demás**, no un sector entero robado a un solo
  vecino. 128 × 3 peers son 384 puntos: una lista ordenada y una búsqueda
  binaria, coste despreciable.

- **El `peer_id` viene del entorno y es estable entre reinicios; no se genera.**
  El `peer_id` *es* la posición del peer en el anillo. Si se generase un UUID en
  cada arranque, reiniciar un peer equivaldría a que uno se fuera y otro
  distinto entrara: todas sus claves se remapearían y los `.blk` que ya tiene en
  disco quedarían inalcanzables. Con `DFSHA_PEER_ID=peer1` fijo, reiniciar es
  transparente. Es lo contrario del `file_id`, que sí es un UUID aleatorio
  porque no tiene que sobrevivir a nada.

- **`choose_nodes` devuelve siempre peers distintos entre sí.** Al recorrer el
  anillo desde la clave hay que saltarse los nodos virtuales que pertenecen a un
  peer ya elegido. Con `replicas=1` no se nota, y por eso es justo la trampa: si
  no se hace ahora, el hito 3 pondría factor 3 y colocaría las tres réplicas en
  el mismo peer físico, que es exactamente lo que la replicación pretende
  evitar. Se resuelve aquí, donde cuesta cuatro líneas.

- **Si se piden más réplicas que peers hay, se devuelven todos los que hay, sin
  error.** El anillo responde qué peers existen; decidir que son pocos es una
  regla de negocio y vive en el `commit` (`422 Confirmación de bloques
  incompleta`, ya en CONTRATOS). Meter esa decisión en el anillo obligaría a
  cambiarlo el día que la política cambie.

- **Al arrancar, el peer construye el anillo con su propia identidad más la
  lista de bootstrap, sin hablar con nadie.** El `docker-compose` de la SPEC-06
  levanta los tres a la vez: si el arranque dependiera de que otro peer
  responda, el orden de arranque decidiría si el sistema funciona. Como el
  bootstrap trae pares `peer_id=dirección`, cada peer puede colocar a los otros
  en el anillo aunque todavía no estén levantados, y los tres llegan a la misma
  membresía sin intercambiar un solo mensaje. La red se usa para lo que la
  configuración no puede saber, no para lo que ya sabe.

- **`POST /peers/join` es para el peer que *no* estaba en la lista, y se propaga
  una sola ronda, best-effort.** Quien recibe el alta la aplica y se la reenvía a
  todos los peers que conoce, marcando la petición con `X-Forwarded-By`; quien
  recibe un alta ya marcada la aplica pero **no la vuelve a propagar**, para que
  no haya tormenta ni bucle. No se reintenta: si un peer no contesta, se queda
  con una membresía vieja. Eso es una divergencia real y conocida, y repararla
  es hito 3 — aquí lo que importa es que el mecanismo esté y que su límite esté
  escrito. Se reutiliza la cabecera que CONTRATOS ya define en §Convenciones en
  vez de inventar otra; no se responde `508` porque un alta es idempotente y no
  está dirigida a un dueño, así que reenviarla de más no rompe nada, solo
  desperdicia.

- **El peer que no aparece en su propio bootstrap se anuncia al arrancar y
  absorbe la membresía que recibe.** Es la otra mitad de `POST /peers/join`, y
  sin ella el alta funciona a medias: los demás aprenden del recién llegado,
  pero nadie le enseña a él quiénes son. Un peer con un anillo de un solo nodo
  no se queda simplemente incompleto — **se cree dueño de todas las claves**,
  acepta bloques que ningún otro peer sabe que tiene y los entierra donde nadie
  los va a buscar. La respuesta de `/peers/join` ya trae la membresía entera,
  así que absorberla no cuesta ni una petición extra. El anuncio es
  condicional: un peer que sí figura en su propio bootstrap **no habla con
  nadie**, que es lo que permite que los tres de la demo arranquen a la vez sin
  depender del orden. La regla en una frase: un peer usa la red solo cuando su
  configuración no le basta para conocer el anillo.

- **`ring_version` se deriva de la membresía: es el hash de la lista de
  `peer_id` ordenada, en hexadecimal corto.** No es un contador ni un reloj
  lógico, y la diferencia es justo la que hace falta: dos peers con el mismo
  `ring_version` tienen necesariamente el mismo anillo, y dos con `ring_version`
  distinto tienen necesariamente anillos distintos. Un contador local no daría
  eso — dos peers que aplicaran altas diferentes llegarían al mismo número con
  membresías distintas y la divergencia quedaría invisible, que es exactamente
  el fallo silencioso contra el que va toda esta SPEC. **Las direcciones no
  entran en el hash**: el anillo se construye solo con los `peer_id`, así que
  cambiar la dirección de un peer no mueve ninguna clave y no debe presentarse
  como un anillo distinto. Existe desde ya, aunque nadie lo consulte, para que
  el hito 3 no tenga que añadir el campo a todas las respuestas después.

- **`address_of(peer_id)` vive en el mismo módulo que `choose_nodes`.**
  `choose_nodes` devuelve identidades, pero para emitir una petición hace falta
  una URL. Si la traducción viviera en otro sitio, habría dos fuentes de verdad
  sobre la membresía y se desincronizarían. Es la única corrección que esta SPEC
  introdujo en CONTRATOS.md, y se hizo allí antes de escribir esto.

- **Las direcciones se normalizan quitando la barra final antes de comparar.**
  `http://peer2:9002` y `http://peer2:9002/` son el mismo peer. Sin normalizar,
  un alta repetida con la barra puesta dispararía un `409 El peer ya está
  registrado` falso y el peer se quedaría fuera del anillo por un carácter.

- **La configuración es un módulo único leído del entorno al importar, con
  valores por defecto que reproducen el monolito de hoy.** Tres peers en la
  misma máquina necesitan `STORAGE_ROOT` distinto o se pisan los bloques, y el
  `docker-compose` de la SPEC-06 no puede editar código. `filesystem.py`
  conserva su global `STORAGE_ROOT`, ahora inicializada desde ese módulo: así
  `resolve_path` no cambia y los 14 tests existentes, que sustituyen esa global,
  siguen verdes sin tocarlos.

- **El cliente recibe una lista de peers y usa el primero que responda
  `/health`.** Es la deuda técnica que bloqueaba todo lo demás: con `SERVER_URL`
  fijo el cliente tiene un punto único de fallo y la topología deja de ser
  simétrica en el único sitio donde el usuario la ve. El cliente **no calcula el
  anillo**: cuando necesite direcciones de peers de datos, las consultará con
  `GET /ring`. Duplicar el cálculo del anillo en el cliente sería una segunda
  fuente de verdad, y el día que se cambie el número de nodos virtuales el
  cliente colocaría bloques donde ningún peer los busca.

---

## 4. Alcance

### Incluye

- `server/config.py`: identidad, dirección, bootstrap, raíz de datos, tamaño de
  bloque y número de nodos virtuales, todo desde el entorno.
- `server/ring.py`: el hash estable, el anillo con nodos virtuales,
  `choose_nodes`, `address_of`, alta de peers y `ring_version`.
- Los endpoints `GET /health`, `GET /ring` y `POST /peers/join`.
- La propagación de una ronda del alta a los peers conocidos.
- El anuncio al arrancar del peer que no figura en su propio bootstrap, y la
  absorción de la membresía que recibe.
- El cliente pasa de `SERVER_URL` fijo a una lista de bootstrap.
- `.env.example` con los tres peers de la demo.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Bloques, metadatos, particionado y cualquier cambio a `send` o `receive`.
- Enrutamiento de peticiones al peer dueño y la respuesta `508`. No hay todavía
  ninguna petición que enrutar; entra con los metadatos, en la SPEC-04.
- Baja de peers, detección de caídas, latidos periódicos, reintentos del alta y
  reconciliación de anillos divergentes. Todo eso es hito 3.
- Replicación. `replicas` es un parámetro y se queda en 1.
- Movimiento de bloques cuando cambia la membresía (rebalanceo).
- `docker-compose`, que es la SPEC-06.

---

## 5. Diseño y contratos

Se toma de `docs/CONTRATOS.md` §Anillo y membresía y §Colocación, sin
reinventar nada. Esta SPEC ya introdujo allí dos correcciones —`address_of` y la
clave de metadatos por directorio padre— antes de escribirse.

### `GET /health`

`200` → `{"peer_id": "peer1", "status": "ok", "ring_version": "a3f19c04"}`

Sin errores: si el peer contesta, está vivo. Es la definición más simple que
sirve, y es la que el hito 3 va a usar para decidir que un peer se cayó.

### `GET /ring`

`200` → `{"ring_version": "a3f19c04", "peers": [{"peer_id": "peer2", "address": "http://peer2:9002"}]}`

La lista incluye al peer que responde. Es la membresía, no las 384 posiciones
virtuales: los nodos virtuales son un detalle de cómo se calcula el anillo, y
cualquier peer los reconstruye a partir de la membresía.

### `POST /peers/join`

Request → `{"peer_id": "peer4", "address": "http://peer4:9004"}`
`200` → el mismo cuerpo que `GET /ring`, ya con el nuevo peer dentro

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | `address` no es una URL http válida | `Dirección de peer inválida` |
| `409` | Ese `peer_id` ya está en el anillo con otra dirección | `El peer ya está registrado` |

Un alta repetida con la misma dirección responde `200` y **no** cambia
`ring_version`: es idempotente. Con `X-Forwarded-By` presente, se aplica y no se
propaga.

### `server/config.py`

| Variable | Por defecto | Para qué |
|---|---|---|
| `DFSHA_PEER_ID` | `peer1` | Identidad y posición en el anillo |
| `DFSHA_ADDRESS` | `http://127.0.0.1:8000` | Cómo le hablan los demás |
| `DFSHA_BOOTSTRAP` | vacío | `peer1=http://...,peer2=http://...` |
| `DFSHA_STORAGE_ROOT` | `server/storage` | Raíz de datos de **este** peer |
| `DFSHA_BLOCK_SIZE` | `4194304` | 4 MiB, para la SPEC-03 |
| `DFSHA_VNODES` | `128` | Posiciones virtuales por peer |

Los valores por defecto reproducen exactamente el monolito actual: sin
configurar nada, el sistema se comporta como hoy.

Aquí viven además los dos ayudantes que saben cómo se escribe la dirección de un
peer, porque el bootstrap también tiene que normalizarlas y `ring.py` no puede
importar de vuelta a `config`:

```python
def normalize_address(address: str) -> str: ...   # sin barra final
def is_valid_address(address: str) -> bool: ...   # esquema http o https, con host
```

### `server/ring.py`

```python
def stable_hash(text: str) -> int: ...

class Ring:
    def __init__(self, vnodes: int = config.VNODES) -> None: ...
    def add_peer(self, peer_id: str, address: str) -> bool: ...  # True si cambió
    def peers(self) -> list[dict]: ...
    def positions(self) -> list[tuple[int, str]]: ...   # el anillo entero
    def version(self) -> str: ...
    def nodes_at(self, position: int, replicas: int = 1) -> list[str]: ...
    def choose_nodes(self, key: str, replicas: int = 1) -> list[str]: ...
    def address_of(self, peer_id: str) -> str: ...      # KeyError si no está

LOCAL = Ring()   # sembrado desde config al importar

def choose_nodes(key: str, replicas: int = 1) -> list[str]: ...  # delega en LOCAL
def address_of(peer_id: str) -> str: ...                         # delega en LOCAL
```

`stable_hash` es `int.from_bytes(sha1(text.encode("utf-8")).digest()[:8], "big")`.

**El anillo es una clase, con una instancia de proceso.** CONTRATOS fija las
funciones de módulo y se conservan tal cual, delegando en `LOCAL`; pero el
anillo tiene que poder construirse aparte, porque dos criterios de aceptación
comparan dos anillos independientes (AC-03) o el mismo anillo antes y después de
una entrada (AC-05). Con estado global haría falta una función de reinicio que
solo existiría para los tests, y la frase "el anillo es una función pura de la
membresía" dejaría de ser cierta en el código.

`positions` devuelve el anillo entero: los `vnodes x peers` pares
`(hash, peer_id)` ordenados. Es lo que compara el test de determinismo, y no el
dueño de una clave: con tres peers, dos anillos completamente distintos aciertan
el mismo dueño una vez de cada tres por pura casualidad.

`nodes_at` es donde vive el `bisect`; `choose_nodes(key)` es
`nodes_at(stable_hash(key))`. Están separadas porque el borde interesante del
`bisect` —una posición que cae exactamente sobre un nodo virtual— no se puede
alcanzar desde una clave, ya que habría que invertir SHA-1. Con `nodes_at` ese
caso se prueba directamente.

`version` es el hexadecimal de los primeros cuatro bytes de
`sha1("\n".join(sorted(peer_ids)))`: ocho caracteres. Sin direcciones, por lo
dicho en la sección 3.

`address_of` levanta `KeyError` ante un `peer_id` desconocido. No es un error
HTTP porque no lo provoca el usuario: significa que el anillo y la tabla de
direcciones se desincronizaron, es decir, un fallo de invariante nuestro.

### `server/membership.py`

```python
def join(peer_id: str, address: str, forwarded: bool) -> dict: ...
def announce() -> bool: ...                  # al arrancar; True si aprendió algo
def absorb(cuerpo: dict | None) -> bool: ...  # mete una membresía ajena en LOCAL
def _send_join(target: str, payload: dict, headers: dict) -> dict | None: ...
```

`announce` no hace nada si el bootstrap está vacío o si este peer figura en él.
En caso contrario recorre el bootstrap, se da de alta en **el primero que
responda** y absorbe su respuesta. Va **sin** `X-Forwarded-By`: el anuncio tiene
que propagarse a los demás, y marcarlo lo impediría.

`_send_join` devuelve el cuerpo de la respuesta, o `None` si el peer no
contestó. Es lo que permite absorber; la propagación lo ignora, porque quien
propaga ya conoce más que quien recibe.

`ring.py` no habla por red: es lo que permite probarlo sin levantar nada. La
validación que devuelve `400` y `409` y la propagación de una ronda viven en un
módulo aparte, igual que `filesystem.py` es la lógica detrás de `main.py`. La
propagación se aísla en `_send_join` para que los tests puedan sustituirla y
comprobar a quién se llamó y con qué cabeceras, sin abrir un socket. El destino
del reenvío son los peers conocidos **menos uno mismo y menos el que acaba de
entrar**: uno ya lo sabe y el otro es el origen del alta.

### Cliente

`DFSHA_BOOTSTRAP` se lee con el mismo formato que en el servidor y el cliente
ignora la parte `peer_id=`: le basta la URL. Una sola variable para los dos
lados evita que un `docker-compose` tenga que mantener dos listas que digan lo
mismo. Al arrancar, prueba `GET /health` en orden y se queda con el primero que
conteste; si ninguno contesta, lo dice y no entra al REPL. Los nueve comandos no
cambian de cara para el usuario.

---

## 6. Criterios de aceptación

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dado** un peer arrancado **cuando** se pide `GET /health` **entonces** responde `200` con su `peer_id`, `status: "ok"` y su `ring_version` |
| **AC-02** | **Dada** una lista de bootstrap con tres peers **cuando** se pide `GET /ring` **entonces** responde los tres, incluido el que contesta, sin haber hablado con ninguno |
| **AC-03** | **Dados** dos procesos distintos con `PYTHONHASHSEED` distinto y la misma membresía **cuando** cada uno construye su anillo **entonces** las dos listas completas de posiciones `(hash, peer_id)` son idénticas, y también lo son su `ring_version` y el dueño de una clave |
| **AC-04** | **Dada** una membresía de tres peers y muchas claves distintas **cuando** se colocan todas **entonces** ningún peer se queda con menos del 20 % ni más del 50 % |
| **AC-05** | **Dado** un anillo de tres peers con muchas claves colocadas **cuando** entra un cuarto peer **entonces** se remapea menos del 40 % de las claves y el resto conserva su dueño |
| **AC-06** | **Dada** una membresía de tres peers **cuando** se piden tres réplicas **entonces** devuelve tres `peer_id` distintos entre sí |
| **AC-07** | **Dada** una membresía de dos peers **cuando** se piden cinco réplicas **entonces** devuelve los dos que hay, sin error |
| **AC-08** | **Dado** un peer que no está en el anillo **cuando** se hace `POST /peers/join` **entonces** responde `200`, aparece en `GET /ring` y `ring_version` cambia |
| **AC-09** | **Dado** un peer ya registrado con esa misma dirección **cuando** se repite el alta **entonces** responde `200` y `ring_version` **no** cambia |
| **AC-10** | **Dado** un `peer_id` ya registrado con otra dirección **cuando** se hace el alta **entonces** responde `409` y el anillo queda intacto |
| **AC-11** | **Dada** una `address` que no es una URL http **cuando** se hace el alta **entonces** responde `400` y el anillo queda intacto |
| **AC-12** | **Dado** un peer que recibe un alta nueva **cuando** la aplica **entonces** la reenvía con `X-Forwarded-By` a los peers que conoce, y un alta que ya trae esa cabecera se aplica pero no se reenvía |
| **AC-13** | **Dado** un `peer_id` presente en el anillo **cuando** se llama a `address_of` **entonces** devuelve su dirección normalizada, y ante un `peer_id` ausente levanta `KeyError` |
| **AC-14** | **Dadas** dos membresías **cuando** se comparan sus `ring_version` **entonces** coinciden si y solo si coincide el conjunto de `peer_id`, sin que influyan las direcciones ni el orden de alta |
| **AC-15** | **Dado** un peer arrancado con un bootstrap en el que él **no** aparece **cuando** se anuncia **entonces** se da de alta en el primero que responda, absorbe la membresía recibida y su `ring_version` coincide con la de los demás; y **dado** un peer que sí aparece en su propio bootstrap, no habla con nadie al arrancar |

---

## 7. Plan TDD

Orden estricto: escribir el test, verlo fallar, implementar lo mínimo, verde
antes de seguir.

El orden va **de dentro hacia fuera**. El anillo es una función pura: se prueba
sin HTTP, sin disco y sin red, y todo lo demás depende de él. Si se rompe, que
se rompa ahí, donde el fallo es legible. Los endpoints van al final porque son
una capa fina sobre el anillo, igual que `main.py` ya es una capa fina sobre
`filesystem.py`.

**Paso 1 — Determinismo del hash y `ring_version`** → AC-03, AC-14. Va primero
porque si el hash no es estable entre procesos, cualquier otro test que pase es
una casualidad. El test lanza dos subprocesos con `PYTHONHASHSEED` distinto y
compara **las posiciones enteras del anillo**, no solo el dueño de una clave:
con tres peers, dos anillos completamente distintos acertarían el mismo dueño
una vez de cada tres, y el test pasaría sin haber probado nada. Se comparan
además el `ring_version` y el dueño de una clave.

El paso lleva un segundo test que **comprueba que el primero no es vacío**:
verifica que las dos semillas elegidas sí hacen que `hash()` de Python devuelva
valores distintos. Si algún día Python dejara de aleatorizar, el test de
determinismo pasaría solo, sin probar nada, y esta comprobación lo denunciaría.

**Paso 2 — `choose_nodes`: reparto y no repetición** → AC-04, AC-06, AC-07.
Obliga a construir el anillo con nodos virtuales, a ordenarlo, al `bisect` y al
salto de nodos virtuales del mismo peer. Es el núcleo de la SPEC.

**Paso 3 — Estabilidad ante la entrada de un peer** → AC-05. Después del paso 2
porque necesita `add_peer` y un anillo que ya reparta bien. Es el test que
justifica el hashing consistente frente a `% N`.

Y como un umbral suelto no demuestra nada, el paso lleva **un segundo test que
verifica que el umbral discrimina**: implementa dentro del propio test la
colocación ingenua `stable_hash(key) % N`, la somete al mismo experimento y
comprueba que remapea muy por encima del 40 %. Así queda escrito, y no solo
dicho, que AC-05 no lo pasaría una implementación con módulo.

**Paso 4 — Configuración y `address_of`** → AC-13, y los valores por defecto de
los que dependen AC-01 y AC-02. Aquí `filesystem.STORAGE_ROOT` pasa a leerse de
`config`; los 14 tests que ya existen tienen que seguir verdes sin tocarlos, y
esa es la comprobación de que el traslado no rompió nada.

**Paso 5 — `GET /health` y `GET /ring`** → AC-01, AC-02. Primeros endpoints, y
los únicos sin efectos secundarios: se prueban con el `TestClient` que ya usa la
suite.

**Paso 6 — `POST /peers/join`** → AC-08, AC-09, AC-10, AC-11. El primero que
muta estado. El caso feliz antes que los errores, como en la SPEC-01.

**Paso 7 — Propagación del alta** → AC-12. Último porque es el único que habla
por red y necesita sustituir el cliente HTTP por un doble que registre a quién
se llamó y con qué cabeceras.

**Paso 8 — El anuncio del peer nuevo** → AC-15. Va el último porque necesita
todo lo anterior: el anillo, el alta y la propagación. Se prueba llamando a
`announce()` con `_send_join` sustituido, sin levantar nada. Cubre las dos
mitades: el peer que no está en su bootstrap se anuncia y converge, y el que sí
está no emite ni una petición.

---

## 8. Tests límite

| Caso | Esperado | Por qué importa |
|---|---|---|
| Anillo con cero peers | `choose_nodes` devuelve `[]` | No debe reventar; en la práctica un peer siempre se tiene a sí mismo, pero el `bisect` sobre lista vacía es un borde clásico |
| `replicas=0` o negativo | `[]` | Ninguna réplica es una respuesta válida, no un error |
| Clave vacía `""` | Un peer, determinista | El hash de la cadena vacía es válido; nada debe tratarlo como caso especial |
| Clave con acentos o emoji | Determinista | Se codifica siempre a UTF-8 antes de hashear; si dependiera de la codificación del sistema, dos peers divergirían |
| Clave que hashea exactamente al valor de un nodo virtual | Ese nodo, no el siguiente | Es el borde entre `bisect_left` y `bisect_right`, el error silencioso típico de esta estructura |
| Alta con `http://peer2:9002/` sobre un `http://peer2:9002` ya registrado | `200` idempotente, no `409` | Es la normalización de la barra final: sin ella, un carácter deja un peer fuera del anillo |
| Alta de un peer consigo mismo | `200`, `ring_version` no cambia | El peer ya está en su propio anillo desde el arranque |
| `address` con esquema `ftp://` o texto suelto | `400 Dirección de peer inválida` | Una dirección inválida en el anillo produce fallos de red mucho más tarde y lejos de su causa |
| `DFSHA_BOOTSTRAP` con espacios, entradas vacías o coma final | Se ignoran las entradas vacías | Es una variable escrita a mano en un `docker-compose` |
| El primer peer del bootstrap no contesta al anuncio | Se prueba con el siguiente | Anunciarse a uno solo lo convertiría en un punto único de fallo justo al arrancar |
| Ningún peer del bootstrap contesta al anuncio | El anillo queda como estaba | El peer arranca aislado; reintentar es hito 3 |
| `DFSHA_VNODES=1` | Funciona, reparte peor | Demuestra que los nodos virtuales son un parámetro, no una condición de correctitud |

---

## 9. Tareas

1. Crear `server/config.py` leyendo las seis variables con sus valores por
   defecto.
2. Trasladar el origen de `STORAGE_ROOT` en `server/filesystem.py` a `config`,
   conservando el nombre de la global para no romper `conftest.py`.
3. Crear `server/ring.py` con `stable_hash`, la construcción del anillo con
   nodos virtuales, `add_peer`, `peers`, `ring_version`, `choose_nodes` y
   `address_of`.
4. Sembrar el anillo al arrancar con la identidad propia más el bootstrap.
5. Añadir `GET /health`, `GET /ring` y `POST /peers/join` a `server/main.py`,
   sin lógica: solo llamadas al anillo, como el resto del archivo.
6. Crear `server/membership.py` con la validación del alta y la propagación de
   una ronda con `X-Forwarded-By`.
7. Sustituir `SERVER_URL` en `client/commands.py` por la lista de bootstrap y la
   selección del primer peer que responda `/health`.
8. Añadir `client/__init__.py` y borrar los imports muertos de
   `server/main.py`, que son deuda técnica que estorba en los archivos que esta
   SPEC toca.
9. Añadir `announce()` y `absorb()` a `server/membership.py` y llamarlos desde
   el arranque de la aplicación.
10. Escribir `.env.example` con los tres peers de los puertos 9001-9003.
11. Escribir `tests/test_config.py`, `tests/test_ring.py`,
    `tests/test_membership.py` y `tests/test_client_bootstrap.py` siguiendo el
    plan del punto 7.

---

## 10. Definición de Done

1. Cada uno de los quince criterios de aceptación tiene al menos un test que
   lo cubre.
2. Los doce tests límite de la sección 8 están escritos y pasan.
3. La suite pasa completa, incluidos los 14 tests que ya existían, **sin haber
   modificado ninguno de ellos**.
4. En ningún módulo se llama a `hash()` sobre una cadena que influya en la
   colocación.
5. `choose_nodes` y `address_of` son la única fuente de verdad sobre la
   colocación: ningún otro módulo calcula posiciones ni traduce `peer_id` a
   dirección.
6. `SERVER_URL` ya no existe en el cliente.
7. Ningún módulo lee `os.environ` fuera de `server/config.py` y del arranque del
   cliente.
8. Tres peers arrancados a mano en los puertos 9001-9003 con el mismo bootstrap
   devuelven, en `GET /ring`, la misma membresía, y `choose_nodes` sobre la misma
   clave da el mismo dueño en los tres. Un cuarto peer que se anuncia converge
   con ellos: los cuatro `ring_version` coinciden.
9. No existe ningún endpoint ni función de bloques, metadatos o replicación.
10. Ningún archivo de producción fue escrito antes que su test.
