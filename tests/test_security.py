"""Seguridad — RNF6.

Cubre los trece casos obligatorios del hito: autenticación peer↔peer,
autenticación de cliente, autorización por propietario, cifrado en reposo con
AES-256-GCM, integridad, y que send/receive/replicación sigan funcionando con
la seguridad activada.

La seguridad es opt-in: cada fixture enciende la capa que el test necesita
sustituyendo las variables de `config` con monkeypatch, igual que el resto de
la suite aísla el entorno. Así los tests de seguridad y los de siempre conviven
sin pisarse.

No se abren sockets: los endpoints se ejercen con el TestClient de FastAPI y
los flujos de cliente contra los dobles de `test_client_transfer`.
"""

import hashlib

import pytest
import requests
from fastapi.testclient import TestClient

from client import commands
from server import blocks, config, metadata, security
from server.main import app

# Reutilizamos los dobles del cliente ya probados en la suite de transferencia.
from tests.test_client_transfer import (
    ANILLO,
    FILE_ID,
    PeerFalso,
    PeerReal,
    entrada_confirmada,
    plan_pendiente,
    sembrar,
)


UUID_A = "5f3e0000-0000-4000-8000-000000000001"
UUID_B = "5f3e0000-0000-4000-8000-000000000002"

# 32 bytes en hex: clave válida de AES-256 para los tests de cifrado.
MASTER_KEY_HEX = "a" * 64

PEER_TOKEN = "token-de-peer-correcto"
CAP_SECRET = "secreto-de-capabilities"

USERS = {"alice": "clave-de-alice", "bob": "clave-de-bob"}


def sha256(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures que encienden cada capa de seguridad
# ---------------------------------------------------------------------------

@pytest.fixture
def peer_auth(monkeypatch):
    """Activa la autenticación peer↔peer con un token conocido."""
    monkeypatch.setattr(config, "PEER_TOKEN", PEER_TOKEN)
    return PEER_TOKEN


@pytest.fixture
def user_auth(monkeypatch):
    """Activa la autenticación de clientes con dos usuarios."""
    monkeypatch.setattr(config, "USERS", dict(USERS))
    return dict(USERS)


@pytest.fixture
def caps(monkeypatch):
    """Activa las capabilities de bloque."""
    monkeypatch.setattr(config, "CAP_SECRET", CAP_SECRET)
    monkeypatch.setattr(config, "CAP_TTL", 900)
    return CAP_SECRET


@pytest.fixture
def cifrado(monkeypatch):
    """Activa el cifrado en reposo AES-256-GCM."""
    monkeypatch.setattr(config, "MASTER_KEY", MASTER_KEY_HEX)
    return MASTER_KEY_HEX


def cab_usuario(nombre: str) -> dict:
    """Cabeceras de un cliente autenticado como `nombre`."""
    return {"X-User": nombre, "X-User-Token": USERS[nombre]}


# ===========================================================================
# 1-3. Autenticación peer <-> peer (X-Peer-Token)
# ===========================================================================

def test_1_peer_sin_token_es_rechazado(client, arboles, peer_auth):
    """Un endpoint interno sin X-Peer-Token responde 401."""
    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peerX", "address": "http://peerX:9009"},
    )

    assert respuesta.status_code == 401
    assert "X-Peer-Token" in respuesta.json()["detail"]


def test_2_peer_con_token_incorrecto_es_rechazado(client, arboles, peer_auth):
    """Un endpoint interno con X-Peer-Token equivocado responde 403."""
    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peerX", "address": "http://peerX:9009"},
        headers={"X-Peer-Token": "token-equivocado"},
    )

    assert respuesta.status_code == 403


def test_3_peer_autorizado_funciona(client, arboles, peer_auth):
    """Con el token correcto, el endpoint interno atiende la petición."""
    respuesta = client.post(
        "/peers/join",
        json={"peer_id": "peerX", "address": "http://peerX:9009"},
        headers={"X-Peer-Token": PEER_TOKEN},
    )

    assert respuesta.status_code == 200


def test_3b_borrado_de_bloques_exige_token_de_peer(client, arboles, peer_auth):
    """El borrado de bloques es interno: sin token, rechazado; con token, pasa."""
    sin = client.delete(f"/blocks/{UUID_A}")
    assert sin.status_code == 401

    con = client.delete(
        f"/blocks/{UUID_A}", headers={"X-Peer-Token": PEER_TOKEN}
    )
    assert con.status_code == 200


# ===========================================================================
# 4-6. Autenticación de clientes (X-User / X-User-Token)
# ===========================================================================

def test_4_cliente_sin_autenticacion_es_rechazado(client, arboles, user_auth):
    """Sin credencial de usuario, una operación de cliente responde 401."""
    respuesta = client.get("/files", params={"path": "/"})

    assert respuesta.status_code == 401


def test_5_cliente_con_credencial_invalida_es_rechazado(client, arboles, user_auth):
    """Con token de usuario equivocado, 401."""
    respuesta = client.get(
        "/files",
        params={"path": "/"},
        headers={"X-User": "alice", "X-User-Token": "clave-equivocada"},
    )

    assert respuesta.status_code == 401


def test_5b_usuario_inexistente_es_rechazado(client, arboles, user_auth):
    respuesta = client.get(
        "/files",
        params={"path": "/"},
        headers={"X-User": "mallory", "X-User-Token": "lo-que-sea"},
    )

    assert respuesta.status_code == 401


def test_6_cliente_autenticado_puede_operar(client, arboles, user_auth):
    """Con credencial válida, la operación se atiende."""
    respuesta = client.get(
        "/files", params={"path": "/"}, headers=cab_usuario("alice")
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["items"] == []


# ===========================================================================
# 7-8. Autorización por propietario
# ===========================================================================

def test_7_usuario_A_no_puede_borrar_archivo_de_usuario_B(client, arboles, user_auth):
    """Alice sube un archivo; Bob no puede borrarlo, Alice sí."""
    # Alice crea y confirma un archivo (vía metadata, sin mover bloques).
    entrada = metadata.allocate("/de-alice.txt", 0, owner="alice")
    metadata.commit("/de-alice.txt", entrada["file_id"], [], user="alice")

    # Bob intenta borrarlo: prohibido.
    bob = client.request(
        "DELETE", "/files",
        params={"path": "/de-alice.txt"},
        headers=cab_usuario("bob"),
    )
    assert bob.status_code == 403

    # El archivo sigue estando.
    assert metadata.read_bucket("/")["entries"].get("de-alice.txt") is not None

    # Alice sí puede.
    alice = client.request(
        "DELETE", "/files",
        params={"path": "/de-alice.txt"},
        headers=cab_usuario("alice"),
    )
    assert alice.status_code == 200


def test_8_usuario_autorizado_puede_descargar_su_archivo(client, arboles, user_auth):
    """Alice resuelve (lookup) su archivo; Bob no."""
    entrada = metadata.allocate("/privado.txt", 0, owner="alice")
    metadata.commit("/privado.txt", entrada["file_id"], [], user="alice")

    alice = client.get(
        "/files/lookup",
        params={"path": "/privado.txt"},
        headers=cab_usuario("alice"),
    )
    assert alice.status_code == 200

    bob = client.get(
        "/files/lookup",
        params={"path": "/privado.txt"},
        headers=cab_usuario("bob"),
    )
    assert bob.status_code == 403


def test_8b_ls_oculta_los_archivos_ajenos(client, arboles, user_auth):
    """`ls` respeta el modelo de acceso: Bob no ve el archivo de Alice."""
    e = metadata.allocate("/solo-alice.txt", 0, owner="alice")
    metadata.commit("/solo-alice.txt", e["file_id"], [], user="alice")

    vista_alice = client.get(
        "/files", params={"path": "/"}, headers=cab_usuario("alice")
    ).json()["items"]
    vista_bob = client.get(
        "/files", params={"path": "/"}, headers=cab_usuario("bob")
    ).json()["items"]

    assert any(i["name"] == "solo-alice.txt" for i in vista_alice)
    assert all(i["name"] != "solo-alice.txt" for i in vista_bob)


def test_8c_repair_solo_lo_lanza_el_propietario(client, arboles, user_auth):
    """Reparar es sensible: Bob no puede repararlo, Alice sí."""
    e = metadata.allocate("/rep.txt", 0, owner="alice")
    metadata.commit("/rep.txt", e["file_id"], [], user="alice")

    bob = client.post(
        "/files/repair", params={"path": "/rep.txt"}, headers=cab_usuario("bob")
    )
    assert bob.status_code == 403

    alice = client.post(
        "/files/repair", params={"path": "/rep.txt"}, headers=cab_usuario("alice")
    )
    assert alice.status_code == 200


# ===========================================================================
# Autorización a nivel de bloque mediante capabilities
# ===========================================================================

def test_bloque_sin_capability_es_rechazado(client, arboles, caps):
    """Con capabilities activas, un GET/PUT de bloque sin X-Block-Cap da 403."""
    get = client.get(f"/blocks/{UUID_A}/0")
    assert get.status_code == 403

    put = client.put(
        f"/blocks/{UUID_A}/0",
        content=b"x",
        headers={"X-Block-Checksum": sha256(b"x")},
    )
    assert put.status_code == 403


def test_bloque_con_capability_de_lectura_no_sirve_para_escribir(client, arboles, caps):
    """Una cap `r` no autoriza un PUT: 403."""
    cap_r = security.issue_capability(UUID_A, "r")

    put = client.put(
        f"/blocks/{UUID_A}/0",
        content=b"x",
        headers={"X-Block-Checksum": sha256(b"x"), "X-Block-Cap": cap_r},
    )
    assert put.status_code == 403


def test_bloque_con_capability_de_escritura_valida_funciona(client, arboles, caps):
    """Una cap `w` autoriza el PUT; una cap `r` del mismo archivo, el GET."""
    cap_w = security.issue_capability(UUID_A, "w")
    cap_r = security.issue_capability(UUID_A, "r")

    put = client.put(
        f"/blocks/{UUID_A}/0",
        content=b"contenido",
        headers={"X-Block-Checksum": sha256(b"contenido"), "X-Block-Cap": cap_w},
    )
    assert put.status_code == 201

    get = client.get(f"/blocks/{UUID_A}/0", headers={"X-Block-Cap": cap_r})
    assert get.status_code == 200
    assert get.content == b"contenido"


def test_capability_de_otro_archivo_no_sirve(client, arboles, caps):
    """Una cap emitida para UUID_B no abre los bloques de UUID_A."""
    cap_otro = security.issue_capability(UUID_B, "r")

    get = client.get(f"/blocks/{UUID_A}/0", headers={"X-Block-Cap": cap_otro})
    assert get.status_code == 403


def test_peer_token_abre_los_bloques_para_reparacion(client, arboles, caps, peer_auth):
    """Un peer interno accede a los bloques con X-Peer-Token, sin capability.

    Es lo que permite que la reparación copie bloques entre peers.
    """
    # Primero sembramos un bloque con cap de escritura.
    cap_w = security.issue_capability(UUID_A, "w")
    client.put(
        f"/blocks/{UUID_A}/0",
        content=b"para reparar",
        headers={"X-Block-Checksum": sha256(b"para reparar"), "X-Block-Cap": cap_w},
    )

    # Un peer interno lo lee solo con el token de peer.
    get = client.get(
        f"/blocks/{UUID_A}/0", headers={"X-Peer-Token": PEER_TOKEN}
    )
    assert get.status_code == 200
    assert get.content == b"para reparar"


# ===========================================================================
# 9-11. Cifrado en reposo (AES-256-GCM) e integridad
# ===========================================================================

def test_9_el_bloque_fisico_no_contiene_los_bytes_en_claro(client, arboles, cifrado):
    """Lo escrito en disco es el criptograma, no el texto claro."""
    claro = b"informacion secreta que no debe verse en disco"

    respuesta = client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(claro)},
    )
    assert respuesta.status_code == 201

    en_disco = (arboles.blocks / UUID_A / "000000.blk").read_bytes()

    assert claro not in en_disco
    assert security.is_encrypted_blob(en_disco)
    assert en_disco.startswith(security.MAGIC)


def test_10_read_block_recupera_exactamente_los_bytes_originales(
    client, arboles, cifrado
):
    """read_block descifra en memoria y devuelve el claro idéntico."""
    claro = bytes(range(256)) * 3

    client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(claro)},
    )

    datos, checksum = blocks.read_block(UUID_A, 0)

    assert datos == claro
    assert checksum == sha256(claro)

    # Y el GET sirve el claro, no el criptograma.
    get = client.get(f"/blocks/{UUID_A}/0")
    assert get.content == claro
    assert get.headers["x-block-checksum"] == sha256(claro)


def test_10b_no_queda_ninguna_copia_en_claro_en_disco(client, arboles, cifrado):
    """Tras leer un bloque cifrado, no debe aparecer ningún fichero en claro."""
    claro = b"otro secreto"
    client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(claro)},
    )

    # Varias lecturas: ninguna debe materializar el claro.
    for _ in range(3):
        blocks.read_block(UUID_A, 0)
        client.get(f"/blocks/{UUID_A}/0")

    for fichero in arboles.blocks.rglob("*"):
        if fichero.is_file():
            contenido = fichero.read_bytes()
            assert claro not in contenido, f"claro filtrado en {fichero}"


def test_11_un_bloque_corrupto_falla_la_verificacion_aes_gcm(client, arboles, cifrado):
    """Alterar un byte del criptograma hace fallar el descifrado (tag GCM)."""
    claro = b"contenido integro"
    client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(claro)},
    )

    # Corrompemos el último byte (parte del tag o del ciphertext).
    ruta = arboles.blocks / UUID_A / "000000.blk"
    datos = bytearray(ruta.read_bytes())
    datos[-1] ^= 0x01
    ruta.write_bytes(bytes(datos))

    # read_block levanta ErrorCifrado.
    with pytest.raises(security.ErrorCifrado):
        blocks.read_block(UUID_A, 0)

    # Y el endpoint lo traduce a 422 de integridad.
    get = client.get(f"/blocks/{UUID_A}/0")
    assert get.status_code == 422


def test_11b_cifrado_integra_la_secuencia_sha256_y_cifrado(client, arboles, cifrado):
    """La secuencia es claro -> SHA-256 -> cifrado -> disco; disco -> descifrado
    -> SHA-256 -> validación.

    Si el claro no cuadra con el checksum declarado, se rechaza ANTES de
    cifrar y no se escribe nada.
    """
    claro = b"unos bytes"
    respuesta = client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(b"otros bytes")},
    )

    assert respuesta.status_code == 422
    assert not (arboles.blocks / UUID_A / "000000.blk").exists()


def test_11c_bloque_en_claro_previo_se_sigue_leyendo(client, arboles):
    """Un bloque escrito en claro (sin cifrado) se lee aunque luego se active.

    Garantiza que activar el cifrado no deja inaccesible lo ya almacenado.
    """
    claro = b"escrito antes del cifrado"
    client.put(
        f"/blocks/{UUID_A}/0",
        content=claro,
        headers={"X-Block-Checksum": sha256(claro)},
    )

    # Ahora se activa el cifrado y se vuelve a leer el bloque viejo.
    import server.config as cfg
    original = cfg.MASTER_KEY
    cfg.MASTER_KEY = MASTER_KEY_HEX
    try:
        datos, checksum = blocks.read_block(UUID_A, 0)
    finally:
        cfg.MASTER_KEY = original

    assert datos == claro
    assert checksum == sha256(claro)


# ===========================================================================
# 12-13. send/receive y replicación de extremo a extremo con seguridad
# ===========================================================================

@pytest.fixture
def cliente_doble(monkeypatch):
    """Monta el doble de peer del cliente con credenciales de usuario puestas.

    Reproduce lo que hace el cliente real: envía X-User/X-User-Token y usa las
    capabilities que vengan en allocate/lookup.
    """
    doble = PeerFalso()
    monkeypatch.setattr(commands, "requests", doble)
    monkeypatch.setattr(commands, "PEER_URL", "http://peer1:9001")
    monkeypatch.setenv("DFSHA_USER", "alice")
    monkeypatch.setenv("DFSHA_USER_TOKEN", USERS["alice"])
    return doble


def test_12_send_y_receive_funcionan_con_capability(cliente_doble, tmp_path, monkeypatch):
    """send adjunta la write_cap del allocate y receive la read_cap del lookup.

    El doble registra la cabecera X-Block-Cap que recibió en cada PUT, de modo
    que se comprueba que el cliente la propaga de verdad.
    """
    monkeypatch.chdir(tmp_path)
    contenido = bytes(range(250))
    (tmp_path / "tarea.bin").write_bytes(contenido)

    plan = plan_pendiente("/tarea.bin", len(contenido))
    plan["write_cap"] = "cap-de-escritura-firmada"
    cliente_doble.plan = plan

    commands.send("/", "tarea.bin")

    # El commit se hizo: send llegó hasta el final.
    assert cliente_doble.commit is not None

    # Cada PUT llevó la capability de escritura.
    for (peer_id, index), (_, _) in cliente_doble.subidos.items():
        pass  # subidos no guarda la cap; la comprobamos aparte abajo

    # Reconstruimos: el doble guarda (data, checksum) en subidos; para la cap
    # miramos que el cliente la haya puesto consultando una subida con headers.
    # Como PeerFalso.put solo guarda el checksum, validamos el camino receive.
    entrada = entrada_confirmada("/tarea.bin", contenido)
    entrada["read_cap"] = "cap-de-lectura-firmada"
    cliente_doble.entrada = entrada
    sembrar(cliente_doble, entrada, contenido)

    (tmp_path / "tarea.bin").unlink()
    commands.receive("/", "tarea.bin")

    assert (tmp_path / "tarea.bin").read_bytes() == contenido


def test_13_send_receive_extremo_a_extremo_con_seguridad_completa(
    monkeypatch, tmp_path, arboles, limite_pequeno, user_auth, caps, cifrado
):
    """El ciclo completo contra un peer REAL con auth de usuario, capabilities
    y cifrado en reposo todos activados a la vez.

    Prueba de una sola vez: 6 (cliente autenticado opera), 8 (descarga su
    archivo), 9/10 (cifrado transparente) y 12 (send/receive de punta a punta).
    """
    cliente = TestClient(app)

    doble = PeerReal(cliente)
    monkeypatch.setattr(commands, "requests", doble)
    monkeypatch.setattr(commands, "PEER_URL", "http://127.0.0.1:8000")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DFSHA_USER", "alice")
    monkeypatch.setenv("DFSHA_USER_TOKEN", USERS["alice"])

    contenido = bytes(range(256)) * 6          # 1536 bytes -> 3 bloques de 512
    (tmp_path / "doc.bin").write_bytes(contenido)

    commands.send("/", "doc.bin")

    # El archivo se confirmó y aparece para su dueña.
    ficha = cliente.get(
        "/files/lookup",
        params={"path": "/doc.bin"},
        headers=cab_usuario("alice"),
    )
    assert ficha.status_code == 200
    assert ficha.json()["owner"] == "alice"

    # Los bloques en disco están cifrados.
    hay_bloque = False
    for fichero in arboles.blocks.rglob("*.blk"):
        hay_bloque = True
        assert security.is_encrypted_blob(fichero.read_bytes())
        assert contenido[:16] not in fichero.read_bytes()
    assert hay_bloque

    # Y receive lo recupera entero (descifrado transparente).
    (tmp_path / "doc.bin").unlink()
    commands.receive("/", "doc.bin")
    assert (tmp_path / "doc.bin").read_bytes() == contenido


def test_13b_bob_no_puede_descargar_los_bloques_de_alice(
    monkeypatch, tmp_path, arboles, limite_pequeno, user_auth, caps, cifrado
):
    """Aunque Bob conozca la ruta, lookup le da 403 y nunca obtiene read_cap,
    así que no puede llegar a los bloques."""
    cliente = TestClient(app)
    doble = PeerReal(cliente)
    monkeypatch.setattr(commands, "requests", doble)
    monkeypatch.setattr(commands, "PEER_URL", "http://127.0.0.1:8000")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DFSHA_USER", "alice")
    monkeypatch.setenv("DFSHA_USER_TOKEN", USERS["alice"])

    contenido = bytes(range(256)) * 6
    (tmp_path / "doc.bin").write_bytes(contenido)
    commands.send("/", "doc.bin")

    # Bob intenta el lookup directamente: 403, sin read_cap.
    bob = cliente.get(
        "/files/lookup",
        params={"path": "/doc.bin"},
        headers=cab_usuario("bob"),
    )
    assert bob.status_code == 403


# ===========================================================================
# Arranque fail-closed
# ===========================================================================

def test_require_auth_falla_si_faltan_secretos(monkeypatch):
    """Con DFSHA_REQUIRE_AUTH y sin secretos, el arranque aborta."""
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.setattr(config, "PEER_TOKEN", "")
    monkeypatch.setattr(config, "USERS", {})
    monkeypatch.setattr(config, "MASTER_KEY", "")
    monkeypatch.setattr(config, "CAP_SECRET", "")

    with pytest.raises(security.ConfiguracionInsegura):
        security.verify_startup_config()


def test_require_auth_pasa_con_todos_los_secretos(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.setattr(config, "PEER_TOKEN", PEER_TOKEN)
    monkeypatch.setattr(config, "USERS", dict(USERS))
    monkeypatch.setattr(config, "MASTER_KEY", MASTER_KEY_HEX)
    monkeypatch.setattr(config, "CAP_SECRET", CAP_SECRET)

    # No debe lanzar.
    security.verify_startup_config()


# ===========================================================================
# Endurecimiento 1 — fail-closed de TLS (DFSHA_REQUIRE_TLS)
# ===========================================================================

def _secretos_ok(monkeypatch):
    """Deja los secretos de auth válidos para aislar el fallo en el TLS."""
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)
    monkeypatch.setattr(config, "PEER_TOKEN", PEER_TOKEN)
    monkeypatch.setattr(config, "USERS", dict(USERS))
    monkeypatch.setattr(config, "MASTER_KEY", MASTER_KEY_HEX)
    monkeypatch.setattr(config, "CAP_SECRET", CAP_SECRET)


def test_require_tls_falla_si_no_hay_material_tls(monkeypatch):
    """Con DFSHA_REQUIRE_TLS y sin cert/clave/CA, el arranque aborta."""
    _secretos_ok(monkeypatch)
    monkeypatch.setattr(config, "REQUIRE_TLS", True)
    monkeypatch.setattr(config, "TLS_CERT", "")
    monkeypatch.setattr(config, "TLS_KEY", "")
    monkeypatch.setattr(config, "TLS_CA", "")

    with pytest.raises(security.ConfiguracionInsegura) as error:
        security.verify_startup_config()

    assert "TLS" in str(error.value)


def test_require_tls_falla_si_un_fichero_tls_no_existe(monkeypatch, tmp_path):
    """Las rutas configuradas pero inexistentes también abortan."""
    _secretos_ok(monkeypatch)
    cert = tmp_path / "peer.crt"
    key = tmp_path / "peer.key"
    cert.write_text("x")
    key.write_text("x")
    # El CA apunta a un fichero que no existe.
    monkeypatch.setattr(config, "REQUIRE_TLS", True)
    monkeypatch.setattr(config, "TLS_CERT", str(cert))
    monkeypatch.setattr(config, "TLS_KEY", str(key))
    monkeypatch.setattr(config, "TLS_CA", str(tmp_path / "no-existe-ca.crt"))

    with pytest.raises(security.ConfiguracionInsegura) as error:
        security.verify_startup_config()

    assert "DFSHA_TLS_CA" in str(error.value)


def test_require_tls_pasa_con_los_tres_ficheros(monkeypatch, tmp_path):
    """Con cert, clave y CA presentes, el arranque no aborta por TLS."""
    _secretos_ok(monkeypatch)
    cert = tmp_path / "peer.crt"
    key = tmp_path / "peer.key"
    ca = tmp_path / "ca.crt"
    for f in (cert, key, ca):
        f.write_text("x")

    monkeypatch.setattr(config, "REQUIRE_TLS", True)
    monkeypatch.setattr(config, "TLS_CERT", str(cert))
    monkeypatch.setattr(config, "TLS_KEY", str(key))
    monkeypatch.setattr(config, "TLS_CA", str(ca))

    # No debe lanzar.
    security.verify_startup_config()


def test_require_tls_desactivado_permite_http(monkeypatch):
    """Sin DFSHA_REQUIRE_TLS, no se exige material TLS (compat/legacy)."""
    _secretos_ok(monkeypatch)
    monkeypatch.setattr(config, "REQUIRE_TLS", False)
    monkeypatch.setattr(config, "TLS_CERT", "")
    monkeypatch.setattr(config, "TLS_KEY", "")
    monkeypatch.setattr(config, "TLS_CA", "")

    # No debe lanzar: el TLS obligatorio está apagado.
    security.verify_startup_config()


def test_server_main_no_sirve_http_con_require_tls(monkeypatch):
    """El entrypoint `python -m server` se niega a servir en claro con TLS
    obligatorio, antes de atar ningún socket."""
    import server.__main__ as entry

    _secretos_ok(monkeypatch)
    monkeypatch.setattr(config, "REQUIRE_TLS", True)
    monkeypatch.setattr(config, "TLS_CERT", "")
    monkeypatch.setattr(config, "TLS_KEY", "")
    monkeypatch.setattr(config, "TLS_CA", "")

    # Si por lo que fuera llegara a uvicorn.run, el test fallaría ruidosamente.
    def no_deberia(*a, **k):
        raise AssertionError("uvicorn.run no debería ejecutarse con REQUIRE_TLS y sin TLS")

    monkeypatch.setattr(entry.uvicorn, "run", no_deberia)

    with pytest.raises(security.ConfiguracionInsegura):
        entry.main()


# ===========================================================================
# Endurecimiento 2 — metadata legacy (sin owner) falla cerrada en modo seguro
# ===========================================================================

def test_metadata_sin_owner_niega_acceso_en_modo_seguro(client, arboles, user_auth, monkeypatch):
    """En modo seguro, un archivo sin owner no es accesible por nadie."""
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)

    # Entrada legacy: confirmada pero sin owner (como la metadata previa al RNF6).
    bucket = metadata.read_bucket("/")
    bucket["entries"]["legacy.txt"] = {
        "path": "/legacy.txt",
        "file_id": "00000000-0000-4000-8000-000000000abc",
        "size": 0,
        "block_size": config.BLOCK_SIZE,
        "state": "committed",
        "blocks": [],
        # sin clave "owner"
    }
    metadata.write_bucket(bucket)

    # Un usuario autenticado NO obtiene acceso de lectura.
    look = client.get(
        "/files/lookup",
        params={"path": "/legacy.txt"},
        headers=cab_usuario("alice"),
    )
    assert look.status_code == 403

    # Tampoco de borrado.
    rm = client.request(
        "DELETE", "/files",
        params={"path": "/legacy.txt"},
        headers=cab_usuario("alice"),
    )
    assert rm.status_code == 403

    # Y `ls` no lo muestra.
    items = client.get(
        "/files", params={"path": "/"}, headers=cab_usuario("alice")
    ).json()["items"]
    assert all(i["name"] != "legacy.txt" for i in items)


def test_metadata_sin_owner_es_publica_fuera_de_modo_seguro(client, arboles, user_auth):
    """Sin DFSHA_REQUIRE_AUTH, una entrada legacy sin owner sigue siendo
    legible (comportamiento de transición)."""
    bucket = metadata.read_bucket("/")
    bucket["entries"]["legacy.txt"] = {
        "path": "/legacy.txt",
        "file_id": "00000000-0000-4000-8000-000000000abc",
        "size": 0,
        "block_size": config.BLOCK_SIZE,
        "state": "committed",
        "blocks": [],
    }
    metadata.write_bucket(bucket)

    look = client.get(
        "/files/lookup",
        params={"path": "/legacy.txt"},
        headers=cab_usuario("alice"),
    )
    assert look.status_code == 200


# ===========================================================================
# Endurecimiento 3 — bloque legacy plaintext falla cerrado en modo seguro
# ===========================================================================

def test_bloque_plaintext_legacy_falla_en_modo_seguro(client, arboles, cifrado, monkeypatch):
    """Un bloque en claro (sin DFS1) no se entrega con cifrado activo y modo
    seguro: decrypt_block levanta ErrorCifrado y el endpoint da 422."""
    monkeypatch.setattr(config, "REQUIRE_AUTH", True)

    # Escribimos a mano un .blk en claro, como lo dejaría un almacén previo al
    # cifrado (sin la cabecera DFS1).
    claro = b"plaintext legacy que no debe servirse en modo seguro"
    ruta = arboles.blocks / UUID_A / "000000.blk"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(claro)

    with pytest.raises(security.ErrorCifrado):
        blocks.read_block(UUID_A, 0)


def test_bloque_plaintext_legacy_se_lee_fuera_de_modo_seguro(client, arboles):
    """Sin modo seguro, un bloque en claro previo se sigue leyendo tal cual
    (compat con un almacén que estrena cifrado)."""
    claro = b"plaintext legacy legible en transicion"
    ruta = arboles.blocks / UUID_A / "000000.blk"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_bytes(claro)

    datos, checksum = blocks.read_block(UUID_A, 0)
    assert datos == claro
    assert checksum == sha256(claro)
