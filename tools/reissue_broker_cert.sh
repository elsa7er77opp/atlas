#!/usr/bin/env bash
# Project Atlas - reissue the broker certificate with a Subject Alternative Name
# AT-26-L1-002  |  closes Deviation #4 in the Milestone 2 progress report
#
# Why this exists
# ---------------
# The commissioning certificate carries CN=atlas-broker and nothing else. That
# works for containers on the atlas network, which resolve the broker by its
# container name. A physical ESP32 is not on that network: it connects to the
# host by IP address, and every conforming TLS client - including mbedTLS on
# the ESP32 - matches the presented name against the Subject Alternative Name
# extension. RFC 6125 deprecated CN fallback in 2011. With no SAN the handshake
# fails, and it fails with an opaque error long before any MQTT frame is sent.
#
# This script issues a replacement signed by the SAME certificate authority, so
# every existing client keeps working: ca.crt is unchanged and it is ca.crt that
# the simulator, the ingestion service and the firmware trust.
#
# Run from the repository root:  ./tools/reissue_broker_cert.sh

set -euo pipefail

CERT_DIR="infra/mosquitto/certs"
DAYS=825          # CA/Browser Forum maximum for server certificates
BACKUP_TS="$(date +%Y%m%d-%H%M%S)"

[[ -d "$CERT_DIR" ]] || { echo "run this from the repository root (no $CERT_DIR)"; exit 1; }
[[ -f "$CERT_DIR/ca.crt" && -f "$CERT_DIR/ca.key" ]] || { echo "CA material missing in $CERT_DIR"; exit 1; }

# --------------------------------------------------------------- host address
# The ESP32 dials this address. It must appear in the SAN or the handshake fails.
DETECTED_IP="$(ip -4 -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')"
HOST_IP="${ATLAS_HOST_IP:-$DETECTED_IP}"

if [[ -z "$HOST_IP" ]]; then
  echo "could not detect the host LAN address; set it explicitly:"
  echo "  ATLAS_HOST_IP=192.168.1.20 $0"
  exit 1
fi

echo "certificate will be valid for:"
echo "  DNS  atlas-broker      (container network)"
echo "  DNS  localhost         (host-side tooling)"
echo "  IP   $HOST_IP          (physical devices)"
echo "  IP   127.0.0.1"
echo
read -r -p "is $HOST_IP the address the ESP32 will connect to? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "aborted; set ATLAS_HOST_IP and rerun"; exit 1; }

# ------------------------------------------------------------------- backup
mkdir -p "$CERT_DIR/superseded"
for f in broker.crt broker.key broker.csr; do
  [[ -f "$CERT_DIR/$f" ]] && cp "$CERT_DIR/$f" "$CERT_DIR/superseded/${f}.${BACKUP_TS}"
done
echo "previous material copied to $CERT_DIR/superseded/ (suffix $BACKUP_TS)"

# ------------------------------------------------------------------- issue
EXT_CNF="$(mktemp)"
trap 'rm -f "$EXT_CNF"' EXIT

cat > "$EXT_CNF" <<EOF
basicConstraints = critical, CA:FALSE
keyUsage         = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName   = DNS:atlas-broker, DNS:localhost, IP:${HOST_IP}, IP:127.0.0.1
EOF

openssl genrsa -out "$CERT_DIR/broker.key" 2048 2>/dev/null
chmod 600 "$CERT_DIR/broker.key"

openssl req -new -key "$CERT_DIR/broker.key" \
  -out "$CERT_DIR/broker.csr" \
  -subj "/C=EG/ST=Cairo/O=Project Atlas/CN=atlas-broker"

openssl x509 -req -in "$CERT_DIR/broker.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/broker.crt" -days "$DAYS" -sha256 \
  -extfile "$EXT_CNF" 2>/dev/null

# The container reads these as an unprivileged mosquitto user
chmod 644 "$CERT_DIR/broker.crt"

# ------------------------------------------------------------------- verify
echo
echo "--- subject alternative name, as issued ---"
openssl x509 -in "$CERT_DIR/broker.crt" -noout -text \
  | grep -A1 "Subject Alternative Name"

echo
echo "--- chain verifies against the existing CA ---"
openssl verify -CAfile "$CERT_DIR/ca.crt" "$CERT_DIR/broker.crt"

# ------------------------------------------------------------------- restart
echo
echo "restarting the broker..."
docker compose restart broker
sleep 4

echo
echo "--- handshake from the host, presenting the host IP ---"
echo | openssl s_client -connect "${HOST_IP}:8883" \
        -CAfile "$CERT_DIR/ca.crt" -verify_return_error 2>&1 \
      | grep -E "Verify return code|Protocol *:|Cipher *:" || true

echo
echo "--- existing clients must still connect (same CA, so they should) ---"
docker compose restart ingest simulator >/dev/null
sleep 6
docker compose logs --tail=4 ingest | grep -i "broker connected" \
  && echo "PASS  ingestion service reconnected over TLS" \
  || echo "CHECK ingestion did not log a connection - inspect: docker compose logs ingest"

echo
echo "done. Capture this output as docs/evidence/m2_cert_san.txt"
