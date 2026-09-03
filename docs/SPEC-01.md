# SPEC-01 — Operación `send`: subida de archivos al DFS

## 1. Meta

| Campo | Valor |
|---|---|
| **id** | `SPEC-01` |
| **repo** | `Sistemas-Distribuidos-P2P` |
| **rama sugerida** | `feat/send-function` |
| **dependencias** | `resolve_path` en `server/filesystem.py` |
| **dependencias de paquete** | `python-multipart` (requerido por FastAPI para recibir formularios multiparte) |

---

## 2. Objetivo

Permitir que el usuario suba un archivo desde su máquina al sistema de archivos remoto
mediante el comando `send <archivo>`, que el cliente traduce en una petición HTTP hacia el
servidor.

Al cerrar esta SPEC, un archivo válido enviado desde el cliente queda almacenado en el
directorio remoto actual, y todo caso de fallo devuelve un código HTTP que identifica la causa
sin dejar archivos parciales en `storage`.

---

## 3. Contexto y decisiones

El monolito ya cubre la gestión de directorios (`ls`, `mkdir`, `rmdir`, `rm`). `send` es la
primera operación que escribe contenido en el servidor, y por eso es la primera que puede
llenar el disco o escribir fuera del área permitida. Las decisiones de abajo existen para
cerrar esos dos riesgos sin romper la forma en que ya funciona el resto del sistema.

- **Si el archivo ya existe en destino, se responde `409` y no se sobrescribe.** El usuario
  elige otro nombre o lo elimina con `rm`, que ya existe. Sobrescribir en silencio destruiría
  un archivo sin que nadie lo pidiera, y es la misma postura que ya toma `create_directory`
  cuando el directorio existe.
- **Si el directorio destino no existe, se responde `404`. `send` no crea directorios.** Todas
  las operaciones actuales devuelven `404` ante algo inexistente y ninguna crea nada por su
  cuenta. Que `send` fuera la excepción obligaría a recordar en qué operación sí y en cuál no.
- **La ruta de destino se resuelve con `resolve_path`; una ruta fuera de `STORAGE_ROOT`
  responde `403`** No se escribe una validación nueva: una segunda defensa duplicada dejaría
  de actualizarse el día que se corrija la primera, y quedaría vulnerable sin que nadie lo note.
- **El tamaño máximo es 10 MB y la transferencia se corta en cuanto se supera, respondiendo
  `413`.** Diez megas cubren los archivos que maneja el proyecto. Medir el tamaño al final
  implicaría haber recibido y escrito el archivo completo antes de rechazarlo: el gasto de
  cómputo y disco que el límite pretende evitar. Una subida abortada no deja archivo parcial en
  `storage`: o queda completo, o no queda nada.
- **Un nombre de archivo vacío o en blanco responde `400`, y el cliente además avisa antes de
  enviar.** La validación del cliente es comodidad —evita una petición inútil—; la del servidor
  es la que manda, porque el servidor no puede confiar en que la petición venga de este cliente.

---

## 4. Alcance

### Incluye

- El endpoint de subida y su recepción del archivo.
- Validación de ruta destino (existencia y pertenencia a `STORAGE_ROOT`).
- Validación de colisión de nombre, de nombre vacío y de tamaño máximo.
- El comando `send` en el cliente, con su validación previa.

### No incluye

Lo excluido **no se implementa aunque parezca buena idea**:

- Sobrescritura, en cualquier forma: ni parámetro `overwrite`, ni confirmación interactiva.
- Creación automática de directorios intermedios.
- Comparar el contenido de dos archivos con el mismo nombre para detectar duplicados.
- `receive`, que va en su propia SPEC.
- Particionado en bloques y distribución entre nodos, propios de la etapa P2P.

---

## 5. Diseño y contratos

### `POST /files/upload`

**Request** — `Content-Type: multipart/form-data`

| Campo | Tipo | Descripción |
|---|---|---|
| `file` | archivo | El archivo, sus bytes y su nombre |
| `path` | texto | Ruta virtual del directorio destino en el DFS |

**Respuesta `200 OK`:**

```json
{
  "message": "Archivo subido correctamente",
  "path": "/universidad/tarea.pdf"
}
```

**Respuestas de error:**

| Código | Cuándo | Detalle |
|---|---|---|
| `400` | El nombre del archivo está vacío o es solo espacios | `El archivo debe tener un nombre` |
| `403` | La ruta resuelta queda fuera de `STORAGE_ROOT` | `Acceso fuera del sistema DFS no permitido` |
| `404` | El directorio destino no existe | `El directorio no existe` |
| `409` | Ya existe un archivo con ese nombre en el destino | `El archivo ya existe` |
| `413` | El archivo supera los 10 MB | `El archivo supera el tamaño máximo permitido` |

El mensaje de `403` se reutiliza literalmente de `resolve_path`, que es quien lo levanta.

### `server/filesystem.py`

```python
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 MB

def save_file(remote_path: str, uploaded_file): ...
```

Sigue el mismo patrón que las funciones existentes: resolver la ruta, validar, actuar,
devolver un mensaje. El contenido se escribe por fragmentos; si la suma acumulada supera
`MAX_FILE_SIZE`, se interrumpe la escritura, se elimina el archivo parcial y se levanta
`HTTPException(413)`.

### Cliente

`send <archivo>` toma el archivo de la máquina del usuario y lo envía al directorio remoto
actual (`current_path`). Antes de enviar, el cliente comprueba que el archivo local exista y
que el nombre no esté vacío; si falla, informa al usuario sin realizar la petición.

---

## 6. Criterios de aceptación

| ID | Given / When / Then |
|---|---|
| **AC-01** | **Dado** un archivo válido y un directorio destino existente **cuando** se hace `send` **entonces** responde éxito y el archivo queda almacenado en esa ruta |
| **AC-02** | **Dado** un directorio destino inexistente **cuando** se hace `send` **entonces** responde `404` y no se guarda nada |
| **AC-03** | **Dada** una ruta destino fuera de `STORAGE_ROOT` **cuando** se hace `send` **entonces** responde `403` y no se escribe fuera del área permitida |
| **AC-04** | **Dado** un archivo cuyo nombre ya existe en el destino **cuando** se hace `send` **entonces** responde `409` y el archivo original queda intacto |
| **AC-05** | **Dado** un archivo de 10 MB o menos **cuando** se hace `send` **entonces** responde éxito y el archivo queda completo |
| **AC-06** | **Dado** un archivo mayor a 10 MB **cuando** se hace `send` **entonces** responde `413`, la transferencia se interrumpe y no queda archivo parcial en `storage` |
| **AC-07** | **Dado** un archivo con nombre vacío o en blanco **cuando** se hace `send` **entonces** responde `400` |

AC-04 quedaba sin cubrir en la versión anterior pese a estar decidido en la sección 3.

---

## 7. Plan TDD

Orden estricto: escribir el test, verlo fallar, implementar lo mínimo, verde antes de seguir.

**Paso 1 — El caso feliz.** Un archivo válido se sube a un directorio existente y queda
almacenado → AC-01. Obliga a crear el endpoint, la función y la escritura en disco.

**Paso 2 — Validación de ruta.** Directorio inexistente (`404`) y ruta fuera de `STORAGE_ROOT`
(`403`) → AC-02, AC-03. Aquí entra `resolve_path`.

**Paso 3 — Colisión de nombre.** El archivo ya existe → `409`, y el original no se toca →
AC-04.

**Paso 4 — Nombre vacío.** → AC-07.

**Paso 5 — Límite de tamaño.** Un archivo justo por debajo del tope pasa; uno por encima
responde `413` y no deja archivo parcial → AC-05, AC-06. Va al final porque es el único paso
que exige escribir por fragmentos y limpiar tras un fallo.

---

## 8. Tests límite

Fuera de alcance para este ejercicio.

## 9. Tareas

Fuera de alcance para este ejercicio.

---

## 10. Definición de Done

1. Cada uno de los siete criterios de aceptación tiene al menos un test que lo cubre.
2. La suite pasa completa.
3. Tras correr los tests, `server/storage/` no queda con archivos residuales.
4. `send` usa `resolve_path`; no existe ninguna otra validación de ruta en el código.
5. Ningún archivo de producción fue escrito antes que su test.