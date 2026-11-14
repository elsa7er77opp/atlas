#!/usr/bin/env bash
# Project Atlas - seed Dynamic Security roles and service clients
# Run from the repository root after ./tools/migrate_dynsec.sh.

set -euo pipefail

: "${DYNSEC_ADMIN_USER:?set DYNSEC_ADMIN_USER}"
: "${DYNSEC_ADMIN_PASSWORD:?set DYNSEC_ADMIN_PASSWORD}"

BROKER_HOST="${BROKER_HOST:-atlas-broker}"
BROKER_PORT="${BROKER_PORT:-8883}"
CAFILE="${CAFILE:-/mosquitto/certs/ca.crt}"
CTRL_TIMEOUT="${CTRL_TIMEOUT:-20s}"

ctrl() {
  local output rc
  set +e
  output="$(timeout "$CTRL_TIMEOUT" docker compose exec -T broker mosquitto_ctrl \
    --cafile "$CAFILE" -h "$BROKER_HOST" -p "$BROKER_PORT" \
    -u "$DYNSEC_ADMIN_USER" -P "$DYNSEC_ADMIN_PASSWORD" dynsec "$@" </dev/null 2>&1)"
  rc=$?
  if [ "$rc" -eq 124 ]; then
    output="mosquitto_ctrl timed out after $CTRL_TIMEOUT"
  fi
  set -e
  printf '%s\n' "$output"
  # mosquitto_ctrl may print `Error: ...` while returning zero. Treat the
  # protocol-level error as failure so missing roles/clients are created.
  if [ "$rc" -ne 0 ] || grep -qiE '(^|[[:space:]])(error|failed|denied|not found):' <<<"$output"; then
    return 1
  fi
}

# Existing roles/clients are idempotent. The .env values are the source of
# truth for service credentials, so an existing client's password is explicitly
# synchronized before services are restarted. A failed command still stops the
# script so an incomplete security policy is never reported as done.
create_role() {
  local role="$1"
  if ctrl getRole "$role" >/dev/null 2>&1; then
    echo "role exists: $role"
  else
    ctrl createRole "$role"
    echo "role created: $role"
  fi
}

add_acl() {
  local role="$1" acltype="$2" topic="$3" output
  if output="$(ctrl addRoleACL "$role" "$acltype" "$topic" allow 2>&1)"; then
    echo "ACL added: $role $acltype $topic"
  elif grep -qiE 'already exists|duplicate|already present' <<<"$output"; then
    echo "ACL exists: $role $acltype $topic"
  else
    printf '%s\n' "$output" >&2
    return 1
  fi
}

create_client() {
  local username="$1" password="$2" role="$3"
  if ctrl getClient "$username" >/dev/null 2>&1; then
    echo "client exists: $username"
    if ! ctrl setClientPassword "$username" "$password"; then
      echo "client password synchronization failed: $username" >&2
      return 1
    fi
    # Existing clients were already assigned by the successful seed. Avoid
    # addClientRole here: Mosquitto returns an internal error for a duplicate
    # client-role relation even though the relation is already correct.
    echo "client password synchronized: $username"
  else
    ctrl createClient "$username" "$password" >/dev/null
    ctrl addClientRole "$username" "$role" >/dev/null
    echo "client created: $username"
  fi
}

echo "Creating least-privilege roles..."
create_role atlas-device
add_acl atlas-device publishClientSend 'atlas/+/%u/telemetry'
add_acl atlas-device publishClientSend 'atlas/+/%u/status'
add_acl atlas-device publishClientSend 'atlas/+/%u/cmd/ack'
add_acl atlas-device subscribePattern 'atlas/+/%u/cmd'
add_acl atlas-device subscribePattern 'atlas/+/%u/config'
add_acl atlas-device publishClientSend 'atlas/+/%u/config/ack'

create_role atlas-ingest
add_acl atlas-ingest subscribePattern 'atlas/+/+/telemetry'
add_acl atlas-ingest subscribePattern 'atlas/+/+/status'
add_acl atlas-ingest subscribePattern 'atlas/+/+/cmd/ack'

create_role atlas-api
add_acl atlas-api publishClientSend 'atlas/+/+/cmd'
add_acl atlas-api subscribePattern 'atlas/+/+/status'
add_acl atlas-api publishClientSend 'atlas/+/+/config'
add_acl atlas-api subscribePattern 'atlas/+/+/config/ack'

echo
echo "Creating service clients from the existing .env values..."
: "${MQTT_INGEST_USER:?set MQTT_INGEST_USER}"
: "${MQTT_INGEST_PASSWORD:?set MQTT_INGEST_PASSWORD}"
: "${MQTT_API_USER:?set MQTT_API_USER}"
: "${MQTT_API_PASSWORD:?set MQTT_API_PASSWORD}"
create_client "$MQTT_INGEST_USER" "$MQTT_INGEST_PASSWORD" atlas-ingest
create_client "$MQTT_API_USER" "$MQTT_API_PASSWORD" atlas-api

if [[ -n "${MQTT_SIM01_USER:-}" && -n "${MQTT_SIM01_PASSWORD:-}" ]]; then
  create_client "$MQTT_SIM01_USER" "$MQTT_SIM01_PASSWORD" atlas-device
else
  echo "simulator credentials not set; seed them before starting simulator"
fi

echo
echo "Dynamic Security seed complete. Existing static password/ACL files are no"
echo "longer authoritative; keep them only for rollback until the migration is"
echo "verified."
