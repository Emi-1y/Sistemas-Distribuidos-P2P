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
# El certificado de cada peer incluye como SAN, por defecto, su propio nombre
# (peerN), localhost y 127.0.0.1, de modo que vale para hablar por el nombre
# del anillo dentro de la red de Docker y por localhost en el anfitrión.
#
# Para multi-VM, cada peer necesita además un SAN con su DNS privado o su IP de
# la VPN. Esos SAN se pasan por variable de entorno, sin hardcodear ninguna IP
# en este script:
#
#   DFSHA_SAN_<PEER>="DNS:peerN.dfsha.internal,IP:10.10.0.11"
#
# donde <PEER> es el nombre del peer en MAYÚSCULAS (p. ej. DFSHA_SAN_PEER1).
# Las entradas se añaden a los SAN por defecto. Formato de cada entrada:
# `DNS:nombre` o `IP:1.2.3.4`, separadas por comas.
#
# Esto es SOLO para desarrollo/laboratorio: una CA local y certificados
# firmados por ella. En producción se usarían certificados de una CA de
# confianza. La CA se emite con CA:TRUE / keyCertSign / cRLSign y no se
# debilita.
#
# Uso:
#   ./scripts/generate_certs.sh                      # peer1 peer2 peer3 en ./certs
#   ./scripts/generate_certs.sh ./mis-certs peerA peerB
#   DFSHA_SAN_PEER1="DNS:peer1.dfsha.internal,IP:10.10.0.11" \
#   DFSHA_SAN_PEER2="DNS:peer2.dfsha.internal,IP:10.10.0.12" \
#   DFSHA_SAN_PEER3="DNS:peer3.dfsha.internal,IP:10.10.0.13" \
#     ./scripts/generate_certs.sh ./certs peer1 peer2 peer3

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

  # SAN por defecto: el nombre del peer, localhost y 127.0.0.1.
  dns_n=1
  ip_n=1
  alt_names="DNS.${dns_n} = ${PEER}"
  dns_n=$((dns_n + 1))
  alt_names="${alt_names}
DNS.${dns_n} = localhost"
  dns_n=$((dns_n + 1))
  alt_names="${alt_names}
IP.${ip_n} = 127.0.0.1"
  ip_n=$((ip_n + 1))

  # SAN extra para multi-VM, leídos de DFSHA_SAN_<PEER> (sin IPs hardcodeadas).
  peer_upper=$(printf '%s' "$PEER" | tr '[:lower:]-' '[:upper:]_')
  san_var="DFSHA_SAN_${peer_upper}"
  extra_sans="${!san_var:-}"

  if [ -n "$extra_sans" ]; then
    IFS=',' read -ra entradas <<< "$extra_sans"
    for entrada in "${entradas[@]}"; do
      entrada="$(printf '%s' "$entrada" | xargs)"   # trim
      tipo="${entrada%%:*}"
      valor="${entrada#*:}"

      case "$tipo" in
        DNS|dns)
          alt_names="${alt_names}
DNS.${dns_n} = ${valor}"
          dns_n=$((dns_n + 1))
          ;;
        IP|ip)
          alt_names="${alt_names}
IP.${ip_n} = ${valor}"
          ip_n=$((ip_n + 1))
          ;;
        *)
          echo "   aviso: SAN ignorado (usa DNS:... o IP:...): ${entrada}" >&2
          ;;
      esac
    done
  fi

  cat > "${PEER}.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, keyEncipherment
subjectAltName = @alt_names

[alt_names]
${alt_names}
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
