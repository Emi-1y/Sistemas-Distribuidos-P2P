# Despliegue multi-VM de DFSha (preparación AWS Academy)

Guía para desplegar **un SuperPeer de DFSha por máquina virtual**, manteniendo
la arquitectura P2P completamente simétrica. Esta iteración **no despliega AWS**:
prepara el repositorio para que, dadas tres VMs Linux con Docker y conectividad
entre sí (VPN), poner DFSha en marcha sea reproducible con el mismo repo, la
misma imagen y el mismo comando en las tres.

## Objetivo

Que las tres VMs ejecuten el mismo código, la misma imagen Docker y el mismo
`docker-compose.peer.yml`, y que la única diferencia entre ellas sea un archivo
`.env` y su certificado individual. Ningún peer es especial.

## Arquitectura multi-VM

```
Cliente
   │
   │ VPN + HTTPS
   │
   ├───────────────┬───────────────┐
   ▼               ▼               ▼
 peer1           peer2           peer3
 VM1             VM2             VM3
 :9001           :9001           :9001
   │               │               │
 data1           data2           data3

peer ↔ peer: VPN + HTTPS + X-Peer-Token
cliente ↔ peer: VPN + HTTPS + X-User/X-User-Token
```

Cada VM corre su propio contenedor DFSha, con almacenamiento local
independiente. Los peers se descubren por `DFSHA_BOOTSTRAP` y se hablan por las
direcciones de la VPN que cada uno anuncia en `/ring`.

## Por qué los peers siguen siendo simétricos

- **Misma imagen** (`dfsha-peer:latest`), **mismo código**, **mismo**
  `docker-compose.peer.yml`, **mismo comando** en las tres VMs.
- `docker-compose.peer.yml` declara **un único servicio genérico** `peer` — no
  hay `peer1`/`peer2`/`peer3` ni lógica por número de peer.
- La única diferencia proviene de configuración externa: `DFSHA_PEER_ID`,
  `DFSHA_ADDRESS`, `DFSHA_STORAGE_ROOT`, `DFSHA_BOOTSTRAP` y el certificado
  individual.
- El `DFSHA_BOOTSTRAP` es **solo descubrimiento inicial**, no coordinación:
  ningún peer de la lista tiene privilegios. Los tres ejecutan la misma
  reconciliación, descubren el mismo anillo, reciben operaciones del cliente,
  almacenan metadata y bloques, y reparan.
- No hay master, leader, coordinator, NameNode ni peer primario.

## Diferencia entre despliegue local y multi-VM

| | Local (desarrollo/demo) | Multi-VM (AWS) |
|---|---|---|
| Archivos compose | `docker-compose.yml` (+ `docker-compose.tls.yml`) | `docker-compose.peer.yml` |
| Servicios | tres (`peer1/2/3`) + cliente, en una máquina | uno (`peer`) por VM |
| Red | red Docker compartida | VPN entre hosts; sin red Docker compartida |
| Direcciones | nombres de contenedor (`peer2:9002`) | IP/DNS de la VPN (`10.10.0.12:9001`) |
| Puertos | 9001/9002/9003 (misma máquina) | 9001 en cada VM (máquinas distintas) |
| Persistencia | volúmenes con nombre | bind mount a path del host por VM |
| Config | fijada en el compose | `.env` por VM + `--env-file` |

El entorno multi-VM es **aditivo**: `docker-compose.yml`,
`docker-compose.tls.yml` y `demo/guion.py` siguen funcionando igual para los
tests y la demo local.

## Variables de configuración

### Comunes al clúster (idénticas en las tres VMs)

| Variable | Qué es |
|---|---|
| `DFSHA_REQUIRE_AUTH` | Modo seguro: `true` |
| `DFSHA_REQUIRE_TLS` | Exige HTTPS, sin fallback a HTTP: `true` |
| `DFSHA_PEER_TOKEN` | Secreto de autenticación peer ↔ peer |
| `DFSHA_USERS` | Credenciales de usuario `alice=token,bob=token` |
| `DFSHA_MASTER_KEY` | Clave AES-256-GCM (hex 64 / base64, 32 bytes) |
| `DFSHA_CAP_SECRET` | Secreto HMAC de capabilities de bloque |
| `DFSHA_CAP_TTL` | Validez de capability (segundos) |
| `DFSHA_REPLICAS` | Factor de réplica |
| `DFSHA_BLOCK_SIZE` | Tamaño de bloque |
| `DFSHA_VNODES` | Nodos virtuales por peer |

### Específicas de cada peer (distintas en cada VM)

| Variable | Qué es |
|---|---|
| `DFSHA_PEER_ID` | Identidad en el anillo (`peer1`, `peer2`, `peer3`) |
| `DFSHA_ADDRESS` | Dirección VPN que ANUNCIA (`https://10.10.0.1X:9001`) |
| `DFSHA_PORT` | Puerto publicado (9001 en todas) |
| `DFSHA_STORAGE_ROOT` | Raíz de datos dentro del contenedor (`/datos`) |
| `DFSHA_DATA_DIR` | Path del host donde persisten metadata+bloques |
| `DFSHA_CERTS_DIR` | Path del host con `ca.crt` + `peerN.crt` + `peerN.key` |
| `DFSHA_TLS_CERT` / `DFSHA_TLS_KEY` | Cert y clave de ESTE peer (bajo `/certs`) |
| `DFSHA_IMAGE` | Imagen a ejecutar (misma etiqueta en las tres) |

### Comunes de descubrimiento (idénticas en las tres VMs)

| Variable | Qué es |
|---|---|
| `DFSHA_BOOTSTRAP` | Lista de los tres peers con sus direcciones VPN |
| `DFSHA_TLS_CA` | CA que verifica los certificados (`/certs/ca.crt`) |

La plantilla completa está en [`.env.peer.example`](.env.peer.example).

### Ejemplo `.env` de peer1 (VM1)

```ini
# Comunes al clúster
DFSHA_REQUIRE_AUTH=true
DFSHA_REQUIRE_TLS=true
DFSHA_PEER_TOKEN=<secreto-peer-comun>
DFSHA_USERS=alice=<token-alice>,bob=<token-bob>
DFSHA_MASTER_KEY=<32-bytes-hex>
DFSHA_CAP_SECRET=<secreto-cap-comun>
DFSHA_CAP_TTL=900
DFSHA_REPLICAS=2
DFSHA_BLOCK_SIZE=4194304
DFSHA_VNODES=128
# Específicas de peer1
DFSHA_PEER_ID=peer1
DFSHA_ADDRESS=https://10.10.0.11:9001
DFSHA_PORT=9001
DFSHA_STORAGE_ROOT=/datos
DFSHA_DATA_DIR=/opt/dfsha/data
DFSHA_CERTS_DIR=/opt/dfsha/certs
DFSHA_TLS_CERT=/certs/peer1.crt
DFSHA_TLS_KEY=/certs/peer1.key
DFSHA_IMAGE=dfsha-peer:latest
# Descubrimiento
DFSHA_BOOTSTRAP=peer1=https://10.10.0.11:9001,peer2=https://10.10.0.12:9001,peer3=https://10.10.0.13:9001
DFSHA_TLS_CA=/certs/ca.crt
```

### Ejemplo `.env` de peer2 (VM2)

Idéntico al de peer1 **salvo** el bloque específico:

```ini
DFSHA_PEER_ID=peer2
DFSHA_ADDRESS=https://10.10.0.12:9001
DFSHA_TLS_CERT=/certs/peer2.crt
DFSHA_TLS_KEY=/certs/peer2.key
# ...el resto igual que peer1 (comunes y descubrimiento idénticos)
```

### Ejemplo `.env` de peer3 (VM3)

```ini
DFSHA_PEER_ID=peer3
DFSHA_ADDRESS=https://10.10.0.13:9001
DFSHA_TLS_CERT=/certs/peer3.crt
DFSHA_TLS_KEY=/certs/peer3.key
# ...el resto igual que peer1
```

(Las IPs `10.10.0.1X` son ejemplos de documentación; usa las de tu VPN.)

## Almacenamiento persistente

Cada VM monta un path del host (`DFSHA_DATA_DIR`, p. ej. `/opt/dfsha/data`) en
el `DFSHA_STORAGE_ROOT` del contenedor (`/datos`). Reiniciar el contenedor **no**
borra metadata ni bloques. El almacenamiento es **local e independiente por VM**:
no se comparte entre máquinas. En el host:

```
/opt/dfsha/data/
  blocks/     bloques cifrados (AES-GCM) que le tocaron a esta VM
  metadata/   el contenido de los directorios cuya clave le toca
```

## TLS y certificados

- `DFSHA_REQUIRE_TLS=true` exige cert, clave y CA presentes; si falta algo, el
  peer **aborta al arrancar** (sin fallback a HTTP). Nunca se usa `verify=False`.
- Genera una CA privada y un certificado por peer con SAN configurables (DNS +
  IP), sin IPs hardcodeadas, con `scripts/generate_certs.sh`:

```bash
DFSHA_SAN_PEER1="DNS:peer1.dfsha.internal,IP:10.10.0.11" \
DFSHA_SAN_PEER2="DNS:peer2.dfsha.internal,IP:10.10.0.12" \
DFSHA_SAN_PEER3="DNS:peer3.dfsha.internal,IP:10.10.0.13" \
  ./scripts/generate_certs.sh ./certs peer1 peer2 peer3
```

Produce `ca.crt`/`ca.key` y `peerN.crt`/`peerN.key`. La CA se emite con
`CA:TRUE`, `keyCertSign`, `cRLSign` (requisito de verificación de cadena).

### Distribución de certificados (quién recibe qué)

| Archivo | peer1 (VM1) | peer2 (VM2) | peer3 (VM3) | Fuera de las VMs |
|---|---|---|---|---|
| `ca.crt` | ✓ | ✓ | ✓ | ✓ (cliente) |
| `peer1.crt` + `peer1.key` | ✓ | | | |
| `peer2.crt` + `peer2.key` | | ✓ | | |
| `peer3.crt` + `peer3.key` | | | ✓ | |
| `ca.key` (clave de la CA) | | | | ✓ (solo donde se emiten; **nunca** en las VMs) |

Cada VM recibe **únicamente su propia clave privada** y el `ca.crt`. La clave de
la CA (`ca.key`) **no se copia a ninguna VM**. Nada de esto va a Git
(`.gitignore` cubre `certs/`, `*.key`, `*.crt`, ...).

En cada VM, coloca los tres ficheros en `DFSHA_CERTS_DIR` (p. ej.
`/opt/dfsha/certs`), que el compose monta en `/certs:ro`.

## Bootstrap

`DFSHA_BOOTSTRAP` lista los tres peers con sus direcciones de VPN y es idéntico
en las tres VMs. Es solo descubrimiento: como cada peer figura en su propio
bootstrap, coloca a los otros dos al arrancar sin emitir ninguna petición, y el
orden de arranque de las VMs da igual. Ningún peer de la lista coordina.

## Puertos

Las tres VMs usan el **mismo puerto 9001** (`DFSHA_PORT`), cada una en su
máquina. Esto refuerza la simetría: no hay `peer1=9001, peer2=9002`. Debe
coincidir con el puerto de `DFSHA_ADDRESS`.

## Requisitos de red

- Conectividad entre las tres VMs por la VPN (p. ej. 10.10.0.11–13), con el
  puerto 9001/tcp alcanzable entre ellas.
- El cliente alcanza al menos un peer por la VPN.
- La comunicación peer ↔ peer sale por la interfaz del host hacia la VPN,
  usando las direcciones de `DFSHA_ADDRESS` — **no** depende de ninguna red
  Docker compartida entre máquinas.

## Comandos

### Levantar un peer (idéntico en VM1, VM2, VM3)

```bash
# En cada VM, con el repo copiado y su .env + certs en su sitio:
docker build -t dfsha-peer:latest .      # una vez por VM (o distribuir la imagen)
docker compose --env-file .env -f docker-compose.peer.yml up -d
```

### Detenerlo

```bash
docker compose --env-file .env -f docker-compose.peer.yml down       # conserva datos del host
```

### Verificar `/health`

```bash
curl --cacert /opt/dfsha/certs/ca.crt https://10.10.0.11:9001/health
```

### Verificar `/ring` (debe converger a los tres)

```bash
curl --cacert /opt/dfsha/certs/ca.crt \
  -H "X-User: alice" -H "X-User-Token: <token-alice>" \
  https://10.10.0.11:9001/ring
```

Los tres peers deben devolver el **mismo `ring_version`** y ver a `peer1`,
`peer2`, `peer3` con sus direcciones `https://...` de la VPN.

### Ejecutar el cliente desde otra máquina

No necesita Docker Compose. Desde una laptop con el repo y la CA:

```bash
export DFSHA_USER=alice
export DFSHA_USER_TOKEN=<token-alice>
export DFSHA_TLS_CA=/ruta/a/ca.crt
export DFSHA_BOOTSTRAP="peer1=https://10.10.0.11:9001,peer2=https://10.10.0.12:9001,peer3=https://10.10.0.13:9001"
python -m client.client
```

El cliente se conecta al **primer peer vivo** del bootstrap; no hay dirección
fija a peer1.

## Validación realizada (en un solo host, simulando tres VMs)

Se levantaron tres despliegues independientes (tres proyectos compose
separados, misma imagen, mismo `docker-compose.peer.yml`, mismo comando, solo
`.env` distinto) sobre una red que hace de VPN. Resultado:

- Los tres peers quedaron `healthy` sobre HTTPS.
- `/ring` convergió: mismo `ring_version` en los tres, cada uno viendo
  `['peer*','...']` y anunciando sus direcciones `https://...:9001`.
- El cliente externo (solo env + CA, sin compose) hizo `send`/`receive` de
  extremo a extremo sobre HTTPS.
- El bloque se replicó en el almacenamiento **independiente** de dos VMs
  distintas, cifrado en reposo (cabecera `DFS1`).

## Limitaciones conocidas

- Un único `DFSHA_PEER_TOKEN` compartido: confianza plana entre peers (además
  del certificado TLS por peer).
- No hay rotación automática de certificados ni de secretos.
- El `.env` vive junto al compose en cada VM; protégelo con permisos del host.
- La distribución de certificados y secretos a las VMs es manual en esta fase
  (se automatizará en la fase AWS).

## Pendiente para AWS Academy (siguiente fase)

- Crear las tres EC2 (VPC, subred, Security Groups abriendo 9001/tcp solo
  dentro de la VPN).
- Instalar y configurar la VPN overlay (WireGuard/Tailscale) y fijar las IPs
  privadas.
- Distribuir repo + imagen + `.env` + certificados a cada VM.
- Opcional: DNS privado (`peerN.dfsha.internal`) y registro de SAN por DNS.
- Automatización (scripts/IaC) de aprovisionamiento y arranque.
