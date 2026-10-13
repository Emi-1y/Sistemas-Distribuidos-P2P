# Hito 3 — Resumen para el compañero

Este documento resume, de forma breve y teórica, qué se implementó en el Hito 3 del proyecto DFSha. La intención es que quien vaya a continuar el trabajo (especialmente la parte de seguridad) entienda los mecanismos ya disponibles sin tener que leer todo el código.

---

## 1. Replicación

### Concepto
Cada archivo se divide en bloques de 64 KiB y cada bloque se replica en `DFSHA_REPLICAS` peers distintos. Así el sistema tolera la pérdida de hasta `DFSHA_REPLICAS - 1` copias de un mismo bloque sin perder datos.

### Cómo se eligieron los destinos
Se usa el anillo de hashing consistente. Para cada bloque se calculan los `DFSHA_REPLICAS` sucesores más cercanos en el anillo. Esto hace que la colocación sea determinista: dos peers con el mismo anillo siempre pondrán un bloque en los mismos destinos.

### Reintento y atomicidad
Si un destino falla durante el `send`, el cliente intenta con el siguiente candidato en el anillo. Si al final no consigue `DFSHA_REPLICAS` copias, el archivo se rechaza por completo: no queda parcialmente subido. El cliente hace `rm` implícito para liberar el nombre y borrar los bloques que sí llegaron.

### Archivos clave
- `server/ring.py` — anillo de hashing consistente.
- `server/placement.py` — cálculo de candidatos y destinos.
- `server/block_transfer.py` — envío y recepción de bloques.
- `server/metadata.py` — registro de qué peer guarda cada bloque.
- `client/commands.py` — comando `send` con reintentos.

---

## 2. Alta Disponibilidad

### Concepto
El sistema detecta automáticamente cuando un peer deja de responder y repara las réplicas que se perdieron por esa caída.

### Heartbeat
Cada peer ejecuta periódicamente (cada 30 segundos) un ciclo de tres pasos:

1. **Reconciliación** — se explica en la sección de consistencia.
2. **Health check** — pregunta `/health` a todos los peers conocidos.
3. **Reparación** — para cada peer que no responde, lanza `repair_for_peer(peer_id)`.

### Reparación automática
Cuando un peer es detectado como caído, el sistema:

- Busca todos los archivos que tenían bloques en ese peer.
- Comprueba cuántas réplicas viven realmente de cada bloque afectado.
- Si faltan réplicas, copia los bloques desde peers vivos a nuevos candidatos.
- Actualiza la metadata del archivo con los nuevos responsables.

Esto asegura que el factor de replicación configurado se mantenga incluso después de fallos.

### Reparación manual
Desde el cliente también se puede forzar la reparación:

```bash
repair <archivo>
repair-all
```

Y por API:

```bash
POST /files/repair
POST /files/repair-all
```

### Archivos clave
- `server/membership.py` — health check y reconciliación.
- `server/repair.py` — lógica de reparación de bloques.
- `server/main.py` — heartbeat y endpoints de reparación.

---

## 3. Consistencia

### Concepto
En un sistema P2P es fácil que distintos peers tengan visiones ligeramente diferentes del anillo. Un peer puede no conocer a uno nuevo porque se unió tarde, o puede perderse un `join` por una red congestionada. Si los anillos difieren, el hashing consistente produce dueños distintos y el sistema deja de ser coherente.

### Reconciliación de anillos
Cada peer pregunta periódicamente `GET /ring` a todos los demás. Si descubre peers que no tenía, los añade a su anillo. No sobrescribe direcciones de peers ya conocidos para evitar conflictos maliciosos o transitorios.

Además existe un endpoint manual:

```bash
POST /peers/reconcile
```

Este mecanismo hace que la membresía converja eventualmente: si la red está conectada, todos los peers acaban viendo el mismo conjunto de nodos.

### Archivos clave
- `server/membership.py` — `reconcile_with()` y `reconcile_all()`.
- `server/main.py` — endpoint `POST /peers/reconcile` e integración en heartbeat.

---

## 4. Seguridad — ideas para la implementación

Esta sección propone un camino posible para cerrar el Hito 3 con seguridad. Cada item puede implementarse de forma independiente, aunque lo ideal es hacerlo en el orden indicado.

### 4.1 Autenticación entre peers
Actualmente cualquier nodo que conozca una dirección puede unirse al anillo y leer/escribir bloques. Se podría exigir un token compartido:

- Variable de entorno `DFSHA_PEER_TOKEN`.
- Header `X-Peer-Token` en las peticiones entre peers (`/peers/join`, `/blocks`, `/files/repair`, etc.).
- FastAPI puede rechazar con `403` si el token no coincide.

Esto evita que nodos arbitrarios se unan a la red privada.

### 4.2 Autenticación del cliente
El cliente REPL debería autenticarse contra el peer al que se conecta:

- Variable de entorno `DFSHA_CLIENT_TOKEN`.
- Header `X-Client-Token` en todas las peticiones del cliente (`/files/send`, `/files/get`, `/files/ls`, etc.).
- El peer valida el token antes de ejecutar cualquier comando.

Opcionalmente se podrían tener dos tokens: uno de solo lectura y otro de lectura/escritura.

### 4.3 Comunicación cifrada (TLS)
Reemplazar `http://` por `https://`:

- Generar certificados autofirmados o usar un mini-CA interno.
- Montar los certificados en los contenedores mediante volúmenes.
- Configurar Uvicorn con `ssl_keyfile` y `ssl_certfile`.
- El cliente `httpx` debe confiar en el certificado raíz.

Con TLS se evita que un atacante en la misma red pueda leer bloques o tokens en tránsito.

### 4.4 Integridad de bloques
Actualmente los bloques se guardan tal cual. Se podría agregar:

- Hash SHA-256 de cada bloque en la metadata.
- Verificación del hash al leer o reparar.
- Rechazo de bloques cuyo hash no coincida con el esperado.

Esto detecta corrupción accidental o manipulación de archivos en disco.

### 4.5 Cifrado de bloques en reposo
Para proteger la confidencialidad de los archivos almacenados:

- Clave maestra derivada de una contraseña (`DFSHA_MASTER_KEY`) o gestión de secretos.
- Cifrar cada bloque con AES-256-GCM antes de escribirlo a disco.
- Descifrar al leer.
- El nonce/IV se puede almacenar junto al bloque cifrado.

Esto implica que un atacante con acceso físico al volumen del contenedor no pueda leer los archivos.

### 4.6 Lista de control de acceso (opcional)
Si el proyecto lo permite, se podría asociar cada archivo a un usuario propietario:

- Header `X-User` en las operaciones del cliente.
- Solo el propietario puede hacer `rm` o `repair` sobre sus archivos.
- `ls` muestra solo archivos del usuario (o todos, según se decida).

### Archivos donde probablemente toque tocar
- `server/main.py` — añadir dependencias de seguridad a los endpoints.
- `server/config.py` — cargar tokens y rutas de certificados.
- `client/client.py` / `client/commands.py` — enviar headers de autenticación.
- `server/metadata.py` — guardar hashes de bloques y, opcionalmente, propietarios.
- `server/block_storage.py` — cifrado/descifrado de bloques en disco.
- `docker-compose.yml` / `Dockerfile` — montar certificados y variables de entorno.

### Tests sugeridos
- Un peer sin token no puede hacer `POST /peers/join`.
- Un cliente sin token no puede subir ni descargar.
- La descarga de un bloque cuyo hash no coincide falla.
- Un bloque cifrado en disco no es legible sin la clave maestra.

---

## Estado de las pruebas (última ejecución)

- Host (Windows): `295 passed, 4 skipped`
- Contenedor Docker: `299 passed`
- Demo completa (`python demo/guion.py`): TODO BIEN
