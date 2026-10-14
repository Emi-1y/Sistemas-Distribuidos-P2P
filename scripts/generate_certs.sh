#!/usr/bin/env bash
#
# Genera una CA local y certificados de servidor para desarrollo de DFSha.
#
# Produce, en el directorio de salida (por defecto ./certs):
#
#   ca.key / ca.crt           La CA local. ca.crt es lo que apunta
#                             DFSHA_TLS_CA en cliente y peers para verificar.
#   peerN.key / peerN.crt     Certificado de cada peer, firmado por la CA.
#
# El certificado de cada peer incluye como SAN tanto el nombre de contenedor
# (peerN) como localhost y 127.0.0.1, de modo que vale para hablar por el
# nombre del anillo dentro de la red de Docker y por localhost en el anfitrión.
#
# Esto es SOLO para desarrollo: una CA local y certificados autofirmados. En
# producción se usarían certificados emitidos por una CA de confianza.
#
# Uso:
#   ./scripts/generate_certs.sh                 # peer1 peer2 peer3 en ./certs
#   ./scripts/generate_certs.sh ./mis-certs peerA peerB

set -euo pipefail

OUT_DIR="${1:-./certs}"
shift || true
PEERS=("$@")

if [ ${#PEERS[@]} -eq 0 ]; then
  PEERS=(peer1 peer2 peer3)
fi

mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

DAYS=825

echo ">> CA local"
if [ ! -f ca.key ]; then
  openssl genrsa -out ca.key 4096

  # La CA necesita las extensiones de CA explícitas: OpenSSL/Python modernos
  # rechazan verificar contra un certificado raíz sin basicConstraints CA:TRUE
  # ni keyUsage de firma de certificados.
  cat > ca.ext <<EOF
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF

  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
    -subj "/C=CO/O=DFSha/CN=DFSha Local Dev CA" \
    -extensions v3_ca -config <(cat <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_ca
[dn]
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
EOF
) \
    -out ca.crt

  rm -f ca.ext
else
  echo "   ca.key ya existe, se reutiliza"
fi

for PEER in "${PEERS[@]}"; do
  echo ">> Certificado de ${PEER}"

  openssl genrsa -out "${PEER}.key" 2048

  openssl req -new -key "${PEER}.key" \
    -subj "/C=CO/O=DFSha/CN=${PEER}" \
    -out "${PEER}.csr"

  cat > "${PEER}.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = ${PEER}
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF

  openssl x509 -req -in "${PEER}.csr" -CA ca.crt -CAkey ca.key \
    -CAcreateserial -out "${PEER}.crt" -days "$DAYS" -sha256 \
    -extfile "${PEER}.ext"

  rm -f "${PEER}.csr" "${PEER}.ext"
done

echo
echo "Listo. Certificados en: $(pwd)"
echo "  CA (para verificar):   $(pwd)/ca.crt   -> DFSHA_TLS_CA"
for PEER in "${PEERS[@]}"; do
  echo "  ${PEER}: $(pwd)/${PEER}.crt / ${PEER}.key -> DFSHA_TLS_CERT / DFSHA_TLS_KEY"
done
