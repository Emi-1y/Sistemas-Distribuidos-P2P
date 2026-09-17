# SPEC-06 — Despliegue con Docker y guion de demostración

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-06` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/despliegue-demo` |
| **dependencias** | Las cinco SPECs anteriores, cerradas y verdes. `DFSHA_PEER_ID`, `DFSHA_ADDRESS`, `DFSHA_BOOTSTRAP`, `DFSHA_STORAGE_ROOT`, `DFSHA_BLOCK_SIZE` y `DFSHA_VNODES` (SPEC-02). `GET /health` y `GET /ring` (SPEC-02). `client.commands` completo (SPEC-05). |
| **dependencias de paquete** | Sale `python-multipart`, que nadie importa desde que la SPEC-04 retiró la subida multiparte. Entra `pyyaml`, que solo usan los tests para leer el compose. Neto: cero. |
| **dependencias de herramienta** | Docker con `docker compose` v2. Es la primera SPEC que necesita algo que no sea Python. |

---

## 2. Objetivo

Que el sistema **se levante entero con un comando** y que un guion demuestre, sin
que nadie toque nada, las cuatro cosas que el hito 2 tenía que conseguir.

Al cerrar esta SPEC:

- `docker compose up` deja tres peers en 9001, 9002 y 9003, cada uno con su
  propia raíz de datos.
- `python demo/guion.py` se ejecuta de principio a fin y termina diciendo si
  todo fue bien. Muestra, en este orden: los tres peers convergiendo al mismo
  `ring_version`, un archivo repartido con bloques en los tres, la lectura
  idéntica byte a byte, y el ciclo del fallo con su `rm`.
- La suite completa pasa **dentro del contenedor**, que es la única forma de
  saber que el arreglo de la jaula de la SPEC-05 vale también en Linux.

Es la SPEC más pequeña del hito y no añade ni una regla de negocio: todo lo que
el guion demuestra ya funciona. Lo que aquí se construye es la forma de
enseñarlo y de arrancarlo sin un manual.

---

## 3. Contexto y decisiones

Las notas que se fueron acumulando en este archivo mientras se cerraban las
SPECs 02 a 05 están recogidas abajo, cada una en la decisión que resuelve. Se
mantienen sus conclusiones y se corrige una: el reparto ya no hace falta
calcularlo por adelantado ni mirarlo en disco.

- **Una sola imagen para los tres peers.** Son simétricos —esa es la propiedad
  central del sistema— y tres Dockerfiles la dejarían de garantizar por
  construcción: bastaría que uno se quedara sin actualizar para tener un anillo
  con un peer que se comporta distinto y no saber por qué. Lo único que cambia
  entre los tres son variables de entorno, que es exactamente lo que la SPEC-02
  decidió cuando sacó la configuración del código.

- **La dirección que va en el anillo tiene que servirle al peer y al cliente, y
  en Docker no hay ninguna que sirva a los dos.** Es la decisión que ordena todo
  lo demás, y no es evidente hasta que se intenta.

  El anillo guarda direcciones, y desde la SPEC-05 el cliente **las usa**: pide
  `GET /ring` y manda cada bloque directo a su peer. Así que la dirección de
  `peer2` tiene que ser alcanzable desde `peer1` (para el reenvío) y desde el
  cliente (para los bloques). Dentro de la red de compose eso es `peer2:9002`;
  desde la máquina de fuera es `127.0.0.1:9002`. No coinciden, y no hay una
  tercera forma que valga para ambos:

  | Alternativa | Por qué no |
  |---|---|
  | `host.docker.internal:900N` | Lo resuelven los contenedores, no el anfitrión |
  | `network_mode: host` | Solo Linux; el equipo trabaja en Windows |
  | Añadir `peer1 peer2 peer3` a los `hosts` del anfitrión | Pide permisos de administrador y hay que deshacerlo a mano |

  Así que el anillo habla **nombres de contenedor**, y la consecuencia se escribe
  entera: **todo lo que mueve bytes se ejecuta dentro de la red.** Los puertos
  9001-9003 se publican igual, y desde el anfitrión funcionan `curl /health`,
  `/ring`, `/files` y los cuatro comandos de directorio del CLI —son metadatos, y
  el peer que los recibe reenvía por dentro—; lo que no funciona desde fuera es
  `send` y `receive`, porque son lo único que usa las direcciones del anillo. Por
  eso el compose trae un servicio `cliente` con el que se entra a la red:
  `docker compose run --rm cliente`.

- **El guion tiene dos mitades, y no es por gusto.** Parar un peer es cosa del
  anfitrión —`docker compose stop` no existe dentro de la red— y mover bytes es
  cosa de dentro. Un guion que viviera solo en el contenedor necesitaría el
  socket de Docker montado dentro, que es un permiso enorme para un proyecto de
  clase; uno que viviera solo fuera no podría subir un bloque. Se parte por esa
  frontera y no por otra: `demo/guion.py` orquesta desde fuera, `demo/actos.py`
  ejecuta cada acto desde dentro.

- **El reparto se demuestra preguntándoselo a los peers, no listando
  directorios.** La nota de la SPEC-02 pedía enseñar el contenido de
  `storage/<file_id>/` en los tres. Con los peers en contenedores eso obligaría a
  montar los tres volúmenes en el guion solo para mirarlos, y demostraría menos:
  que hay un archivo en un disco.

  El guion pide `GET /blocks/{file_id}/{index}` **a los tres peers, para cada
  bloque**, y dibuja la matriz. Un `200` prueba que el bloque está *y* que se
  puede leer; un `404` prueba que ese peer no lo tiene. Es la propia API
  respondiendo, no una inspección por detrás, y no necesita acceso al disco de
  nadie.

- **El reparto se ve con muchos bloques, y la palanca es el número de bloques,
  no el tamaño del archivo.** La nota de la SPEC-02 avisaba de que tres bloques
  pueden caer 2/1/0 y parecer que el sistema no reparte; pedía calcular el
  tamaño por adelantado con `choose_nodes`. La verificación de la SPEC-05 lo
  resolvió mejor: con 24 bloques el reparto salió 8/10/6 y no hace falta calcular
  nada: la probabilidad de que **algún** peer se quede vacío no llega a
  `3 · (2/3)²⁴ ≈ 0,018 %`.

  De ahí que el despliegue fije `DFSHA_BLOCK_SIZE=65536` y el guion genere un
  archivo de 1,5 MiB: 24 bloques, un archivo que no pesa nada y un reparto
  visible. El tamaño de bloque es un parámetro de despliegue desde la SPEC-02,
  no una constante del código; un despliegue de verdad usaría los 4 MiB del
  `.env.example`, y con ellos harían falta 96 MiB para ver lo mismo.

  El guion imprime **por qué** el reparto no es un tercio exacto: es un hash, no
  un turno, y con dos docenas de claves la varianza se ve. Sin esa línea, la
  demo parece que reparte mal.

- **El fallo se provoca parando los dos peers que no son dueños del directorio.**
  La nota de la SPEC-05 pedía enseñar el ciclo del fallo, y el guion tiene que
  correr sin intervención, así que hay que romper algo a propósito y saber que se
  rompe.

  Parar un peer cualquiera casi siempre basta, pero «casi» no es una demo
  repetible: la colocación depende de un `file_id` que se sortea en cada
  `allocate`, y la probabilidad de que el peer parado no tenga ningún bloque es
  `(2/3)²⁴`: una entre diecisiete mil. Parando **dos** baja a `(1/3)²⁴`, una entre
  doscientos ochenta y dos mil millones: determinista para cualquier uso
  práctico.

  Cuáles dos no es arbitrario: se para todo **menos el dueño del directorio de la
  demo**, calculado con `choose_nodes` antes de apagar nada —que es, ahora sí, el
  «calcular antes» que pedía la nota de la SPEC-02—. Con el dueño vivo, el
  `allocate` funciona, el `ls` funciona, el `lookup` funciona y el `rm` funciona:
  todo lo que el acto necesita afirmar sigue en pie, y lo único que falla es
  subir los bloques, que es justo lo que se quiere enseñar.

  Y encaja con el borrado: los bloques que llegaron a subirse están todos en el
  superviviente, porque los otros dos estaban apagados, así que el `rm` se los
  lleva de verdad y el guion puede comprobarlo con un `404`.

- **El `403` de la jaula: qué variante puede aparecer en Linux y cuál no.** El
  arreglo de la SPEC-05 se verificó en Windows, y la nota pedía revisarlo aquí.

  La variante de Windows —`Path.resolve()` devolviendo la forma larga
  `\\?\C:\...`— **no puede darse en Linux**: no existe ese prefijo. La que sí
  puede darse es la otra mitad del mismo error, y es más peligrosa porque no es
  intermitente sino constante: si la raíz de datos se alcanza a través de un
  **enlace simbólico**, `Path.resolve()` la sigue, y comparar la ruta resuelta
  con una raíz sin resolver haría que la jaula rechazara su propio árbol
  **siempre**. En un contenedor eso no es teórico: un volumen puede montarse en
  una ruta que sea un enlace.

  El arreglo de la SPEC-05 ya cubre las dos, porque normaliza **los dos lados**
  de la comparación en vez de parchear un síntoma. Pero eso hay que
  comprobarlo, no suponerlo: la suite entera corre dentro del contenedor (AC-04)
  y se añade un test con la raíz alcanzada por un enlace simbólico (AC-05).

- **Volúmenes con nombre, y el guion empieza tirándolos.** Cada peer monta su
  volumen en `/datos`: los datos sobreviven a un `restart` —que es lo que la
  SPEC-04 prometió sobre la persistencia— y se tiran con `down -v`. El guion
  arranca siempre con `down -v` porque una demo que depende de lo que quedó de la
  vez anterior no es repetible, y la segunda vez fallaría por el `409` de un
  archivo que ya existe.

- **El healthcheck lo hace Python, no `curl`.** Instalar un paquete en la imagen
  solo para preguntar si el peer está vivo, cuando dentro ya hay un intérprete
  con `urllib`, es peso sin motivo. Y sirve para algo más que mirar: con
  `depends_on: condition: service_healthy`, el servicio del cliente no arranca
  hasta que los tres peers responden, y el guion no tiene que dormir a ciegas.

- **La convergencia se enseña, no se provoca.** Los tres peers están en su propio
  `DFSHA_BOOTSTRAP`, así que colocan a los otros dos en el anillo al arrancar y
  **ninguno emite una sola petición** (SPEC-02). Que los tres `ring_version`
  coincidan es la prueba de que el anillo es una función pura de la membresía y
  no un acuerdo negociado. Un cuarto peer uniéndose en caliente sería otra demo
  —la de `POST /peers/join`— y no entra aquí.

- **Sale `python-multipart`.** La SPEC-01 lo añadió para recibir el formulario de
  `POST /files/upload`; la SPEC-04 retiró ese endpoint y nadie volvió a importar
  nada suyo. Una imagen que instala una dependencia que nadie usa es una
  invitación a que alguien reintroduzca las subidas multiparte «porque ya está».

---

## 4. Alcance

### Incluye

- `Dockerfile`: una imagen para los tres peers y para el cliente.
- `docker-compose.yml`: tres peers en 9001-9003, volúmenes con nombre,
  healthchecks, y un servicio `cliente` para entrar en la red.
- `demo/guion.py`: el orquestador, desde el anfitrión.
- `demo/actos.py`: los cuatro actos, desde dentro de la red.
- El test de la variante Linux de la jaula, y el test que lee el compose.
- La sección del `README.md` con cómo se levanta y cómo se ejecuta la demo.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Cualquier cambio en `server/` o en `client/`. El guion usa el CLI tal y como
  está; si necesitara tocarlo, sería que la SPEC-05 no cerró.
- Un cuarto peer que se une en caliente, y cualquier demostración de
  `POST /peers/join`.
- Reinicio automático, réplicas, escalado, `restart: always` y política de
  reintentos. Un peer que se cae en la demo se levanta porque el guion lo
  levanta.
- Publicar la imagen en un registro, etiquetarla con versiones o construirla en
  CI.
- TLS, autenticación, límites de recursos y usuarios no privilegiados dentro del
  contenedor.
- Kubernetes, en cualquiera de sus formas.
- Un `.env` para el compose: los valores van explícitos en el YAML, porque son
  la topología de la demo y no un secreto.

---

## 5. Diseño

### `Dockerfile`

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/ server/
COPY client/ client/
COPY demo/ demo/
COPY tests/ tests/
```

`python:3.13` para no estrenar en el contenedor una versión distinta de la que
corrió la suite. `tests/` entra en la imagen a propósito: correr la suite dentro
es un criterio de aceptación, no un extra.

Sin `CMD`: cada servicio pone el suyo en el compose, porque el puerto es lo único
que cambia y verlo al lado de la identidad del peer evita el clásico peer3
escuchando en el puerto de peer2.

### `docker-compose.yml`

Tres servicios iguales salvo identidad, más el cliente. Lo común va en un ancla
YAML, que es lo que impide que las tres copias del bootstrap se separen:

```yaml
x-peer: &peer
  build: .
  environment: &entorno
    DFSHA_BOOTSTRAP: peer1=http://peer1:9001,peer2=http://peer2:9002,peer3=http://peer3:9003
    DFSHA_STORAGE_ROOT: /datos
    DFSHA_BLOCK_SIZE: "65536"
    DFSHA_VNODES: "128"

services:
  peer1:
    <<: *peer
    environment:
      <<: *entorno
      DFSHA_PEER_ID: peer1
      DFSHA_ADDRESS: http://peer1:9001
    command: uvicorn server.main:app --host 0.0.0.0 --port 9001
    ports: ["9001:9001"]
    volumes: ["datos-peer1:/datos"]
    healthcheck: ...
```

`--host 0.0.0.0` no es opcional: dentro de un contenedor, `127.0.0.1` es el
propio contenedor y nadie llegaría.

El servicio `cliente` comparte imagen y bootstrap, no publica puertos, no tiene
volumen de datos y depende de que los tres peers estén sanos. No arranca con
`up`: se invoca con `run`.

### `demo/actos.py` — dentro de la red

```python
def convergencia() -> dict[str, str]:      # peer_id -> ring_version
def reparto(file_id, bloques, direcciones) -> dict[int, list[str]]
def matriz(reparto) -> str                 # el dibujo, para imprimir
def dueno_de(directorio: str) -> str       # choose_nodes, sin red
```

`reparto` pregunta `GET /blocks/{file_id}/{index}` a los tres peers por cada
bloque y devuelve quién contestó `200`. `matriz` solo dibuja. `dueno_de` importa
`server.ring` —es un guion, no el cliente— y no habla con nadie: el anillo es una
función pura de la membresía.

Cada acto es un subcomando: `python -m demo.actos convergencia`, `... enviar`,
`... recibir`, `... fallo-envio`, `... fallo-rm`, `... reintento`.

### `demo/guion.py` — desde el anfitrión

Orquesta y no calcula nada:

1. `docker compose down -v` y `docker compose up -d --build`.
2. Espera a que los tres estén sanos.
3. **Acto 1** — convergencia: imprime los tres `ring_version` y comprueba que
   coinciden.
4. **Acto 2** — reparto: crea el archivo de 1,5 MiB, `send`, y dibuja la matriz
   bloque × peer. Comprueba que cada bloque tiene exactamente un tenedor, que
   coincide con lo que dice `lookup`, y que los tres peers tienen alguno.
5. **Acto 3** — lectura: `receive` y comparación del SHA-256 con el original.
6. **Acto 4** — el ciclo del fallo: calcula el dueño del directorio, para los
   otros dos peers, `send` (falla), comprueba que no aparece en `ls`, que
   `lookup` da `404` y que repetir el `send` da `409`; `rm`, y comprueba que los
   bloques que sí subieron ya no están; levanta los dos peers, espera a que
   converjan y repite el `send`, que ahora funciona.
7. Resumen final con una línea por acto y un `TODO BIEN: True|False`.

Salida esperada del acto 2, en corto:

```
bloque  peer1 peer2 peer3
   0      ·     X     ·
   1      X     ·     ·
  ...
reparto: peer1=8  peer2=10  peer3=6
No es un tercio exacto y no tiene que serlo: la colocacion es un hash, no un
turno, y con 24 claves la varianza se ve.
```

---

## 6. Criterios de aceptación

### Despliegue

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dado** el repositorio limpio **cuando** se hace `docker compose up -d` **entonces** los tres peers responden `GET /health` con `200` en 9001, 9002 y 9003 desde el anfitrión |
| **AC-02** | **Dados** los tres peers arrancados en cualquier orden **cuando** se les pregunta **entonces** los tres devuelven el mismo `ring_version` y los mismos tres peers en `GET /ring`, sin que ninguno haya emitido una petición para conseguirlo |
| **AC-03** | **Dado** un archivo subido **cuando** se hace `docker compose restart` **entonces** el `ls` y el `receive` siguen funcionando: cada peer conserva su raíz de datos, y ninguna es la de otro |
| **AC-04** | **Dada** la imagen construida **cuando** se ejecuta la suite **dentro** del contenedor **entonces** pasa completa, incluidos los dos tests de la jaula de la SPEC-05 |
| **AC-05** | **Dada** una raíz de datos alcanzada a través de un enlace simbólico **cuando** se resuelve una ruta dentro de ella **entonces** la jaula la acepta, y sigue respondiendo `403` a la que se sale |
| **AC-06** | **Dado** el `docker-compose.yml` **cuando** se lee **entonces** los tres peers comparten exactamente el mismo `DFSHA_BOOTSTRAP`, y no repiten entre ellos ni `DFSHA_PEER_ID`, ni `DFSHA_ADDRESS`, ni volumen, ni puerto |

### Guion

| ID | Given / When / Then |
|---|---|
| **AC-07** | **Dado** Docker levantado **cuando** se ejecuta `python demo/guion.py` **entonces** recorre los cuatro actos sin intervención y termina con un resumen que dice si todo fue bien |
| **AC-08** | **Acto 1.** Muestra el `ring_version` de los tres y comprueba que es el mismo |
| **AC-09** | **Acto 2.** Sube un archivo de 24 bloques y dibuja la matriz bloque × peer preguntándosela a los peers; cada bloque tiene **exactamente un** tenedor, coincide con lo que dice `lookup`, y los tres peers guardan al menos uno |
| **AC-10** | **Acto 3.** Baja el archivo y su SHA-256 coincide con el del original |
| **AC-11** | **Acto 4.** Con los dos peers que no son dueños parados: el `send` falla, el archivo no aparece en `ls`, `lookup` da `404`, repetir el `send` da `409`, el `rm` responde `200` y deja sin bloques al peer que sí los tenía; y al volver los dos peers, el mismo `send` funciona |
| **AC-12** | **Dado** un guion que ya se ejecutó **cuando** se vuelve a ejecutar **entonces** hace lo mismo: empieza tirando los volúmenes y no depende de nada que quedara |

---

## 7. Plan TDD

Es la SPEC con menos código y más configuración, así que la mitad de lo que hay
que comprobar no se comprueba con `pytest` sino levantando el despliegue. Se
escribe primero lo que sí es un test, y los actos se construyen de uno en uno,
verificando cada uno contra el despliegue antes de seguir con el siguiente.

**Paso 1 — El test que lee el compose** → AC-06. Antes que el compose. Es el
único invariante del despliegue que se puede fijar sin Docker, y protege el
error más caro y más silencioso: dos peers con el mismo `DFSHA_STORAGE_ROOT`
comparten disco, se pisan los bloques y el sistema parece funcionar hasta que
uno borra los del otro.

**Paso 2 — La variante Linux de la jaula** → AC-05. Puede nacer verde, y no
pasa nada: el arreglo de la SPEC-05 normaliza los dos lados de la comparación,
así que debería cubrirla ya. Se escribe igual, y por eso: si nace verde queda
como test de regresión de una variante que nadie había probado; si nace rojo,
hay que arreglar la jaula antes de seguir. Las dos respuestas valen, y hasta que
el test exista no se sabe cuál es. Se salta donde el sistema no deje crear
enlaces simbólicos, y se dice que se salta.

**Paso 3 — Imagen y compose** → AC-01, AC-02. Se levanta, se pregunta a los tres
por `/health` y `/ring` y se comparan las respuestas a mano. Aquí no hay test
automático: el criterio es que se levanta y responde.

**Paso 4 — La suite dentro del contenedor** → AC-04. Antes de escribir ni un
acto. Si la jaula, la escritura atómica de los metadatos o el paralelismo del
cliente se comportan distinto en Linux, hay que saberlo ahora y no cuando el
guion falle por un motivo que parecerá del guion.

**Paso 5 — Los ayudantes del guion** → parte de AC-09. `reparto`, `matriz` y
`dueno_de` se prueban con `pytest`, con respuestas de mentira: que un bloque con
dos tenedores o con ninguno se detecte como error, que la matriz los dibuje, y
que `dueno_de` no hable con nadie.

**Paso 6 — Actos 1, 2 y 3** → AC-08, AC-09, AC-10. En orden, verificando cada
uno contra el despliegue levantado. El acto 2 es el que obliga a casi todo.

**Paso 7 — El acto 4** → AC-11. El último porque es el único que apaga cosas, y
porque necesita que los tres anteriores ya funcionen para que su fallo se
distinga de un fallo de verdad.

**Paso 8 — El guion completo, dos veces seguidas** → AC-07, AC-12, AC-03. La
segunda ejecución es la que prueba que es repetible; el `restart` de AC-03 se
comprueba aquí, entre las dos.

---

## 8. Tests límite

| Caso | Esperado | Por qué importa |
|---|---|---|
| Arrancar los peers de uno en uno, con minutos de diferencia | Los tres convergen igual | El anillo se deriva del bootstrap, no de quién llegó antes: el orden de arranque tiene que dar igual |
| `docker compose restart peer2` con un archivo ya subido | El archivo se sigue leyendo entero | Los metadatos y los bloques están en el volumen, no en memoria |
| `docker compose down` sin `-v` y volver a subir | Los datos siguen ahí | Es la diferencia entre parar y borrar, y conviene que esté probada antes de la demo |
| `send` desde el CLI ejecutado en el **anfitrión** | Falla al subir el primer bloque | Es la consecuencia documentada de que el anillo hable nombres de contenedor; tiene que fallar de forma entendible y estar escrito en el README |
| El guion con Docker parado | Un mensaje claro y salida distinta de cero | Es el error más probable de quien clone el repo |
| Un peer que se para en mitad del acto 2 | El guion falla y lo dice | El acto 2 no tiene tolerancia a fallos y no debe fingir que sí |
| El archivo de la demo ya existe en el DFS de una ejecución anterior | No puede pasar: el guion empieza con `down -v` | Es exactamente el fallo que hace que una demo funcione solo la primera vez |

---

## 9. Tareas

1. Escribir `tests/test_despliegue.py` con los invariantes del compose (paso 1).
2. Añadir a `tests/test_jaulas.py` el caso del enlace simbólico (paso 2).
3. Escribir el `Dockerfile` y el `docker-compose.yml`.
4. Quitar `python-multipart` de `requirements.txt` y añadir `pyyaml`.
5. Correr la suite dentro del contenedor y dejar anotado el resultado.
6. Escribir `demo/actos.py` con sus ayudantes y sus subcomandos.
7. Escribir `demo/guion.py` con los cuatro actos y el resumen.
8. Añadir al `README.md` cómo se levanta, cómo se ejecuta la demo, y la nota de
   por qué el CLI se ejecuta dentro de la red.
9. Ejecutar el guion dos veces seguidas y dejar su salida en el PR.

---

## 10. Definición de Done

1. Cada uno de los doce criterios de aceptación está comprobado: los que son
   `pytest`, con un test; los del despliegue, ejecutando lo que dicen y dejando
   la salida en el PR.
2. Los siete casos de la sección 8 se han probado.
3. La suite pasa completa en el anfitrión y **dentro del contenedor**, con el
   mismo número de tests en los dos sitios.
4. No se ha tocado `server/` ni `client/`. El guion usa el CLI tal y como quedó
   en la SPEC-05.
5. `docker compose up -d` deja los tres peers respondiendo, y `docker compose
   down -v` no deja nada.
6. `python demo/guion.py` se ejecuta dos veces seguidas con el mismo resultado,
   sin intervención y sin editar nada entre medias.
7. El guion no oculta un fallo: si un acto no cumple lo que afirma, lo dice y el
   resumen final sale en falso.
8. El `docker-compose.yml` no repite el bootstrap escrito a mano tres veces, y
   ningún par de peers comparte identidad, dirección, puerto ni volumen.
9. `requirements.txt` no instala nada que el código no importe.
10. El `README.md` permite levantar el sistema y ver la demo sin leer ninguna
    SPEC.
11. Ningún archivo de producción fue escrito antes que su test, en las partes que
    tienen test.
