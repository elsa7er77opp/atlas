#!/usr/bin/env bash
# Project Atlas - migrate the broker to the Dynamic Security Plugin
# AT-26-L1-002 | M3 | SAFE-MIGRATION-V4 | rollback-aware migration
#
# Run from the repository root while the current static broker is healthy:
#   ./tools/migrate_dynsec.sh
#
# The script does not delete a database or Docker volume. On a failed migration
# it restores docker-compose.yml and mosquitto.conf and restarts the broker on
# the previous static configuration. The new dynsec volume, if created, is left
# unused so it can be inspected or removed deliberately after the incident.

set -Eeuo pipefail

SCRIPT_VERSION="SAFE-MIGRATION-V4"
echo "Atlas Dynamic Security migration $SCRIPT_VERSION"

CFG="infra/mosquitto/config"
COMPOSE="docker-compose.yml"
TS="$(date +%Y%m%d-%H%M%S)"
CONFIG_BACKUP="/tmp/atlas-mosquitto.conf.${TS}.bak"
COMPOSE_BACKUP="/tmp/atlas-docker-compose.${TS}.bak"
PLUGIN_PATH=""
MIGRATION_STARTED=0

fail() {
  echo "ERROR: $*" >&2
  return 1
}

broker_running() {
  docker compose ps --status running --services 2>/dev/null \
    | grep -qx broker
}

wait_for_broker() {
  local attempt
  for attempt in $(seq 1 30); do
    if broker_running; then
      return 0
    fi
    sleep 1
  done
  echo "Broker did not become stable. Recent log:" >&2
  docker compose logs --tail=40 broker >&2 || true
  return 1
}

restore_previous_state() {
  local status=$?
  if (( status == 0 || MIGRATION_STARTED == 0 )); then
    return "$status"
  fi

  echo >&2
  echo "Migration failed; restoring the previous broker configuration..." >&2
  if [[ -f "$COMPOSE_BACKUP" ]]; then
    cp "$COMPOSE_BACKUP" "$COMPOSE"
  fi
  if [[ -f "$CONFIG_BACKUP" ]]; then
    cp "$CONFIG_BACKUP" "$CFG/mosquitto.conf"
  fi
  docker compose up -d --force-recreate broker >/dev/null 2>&1 || true
  if wait_for_broker; then
    echo "Rollback completed: broker is running on the previous configuration." >&2
  else
    echo "Rollback was written but broker is not stable; inspect docker logs." >&2
  fi
  return "$status"
}
trap restore_previous_state EXIT

[[ -d "$CFG" ]] || fail "run from the repository root (no $CFG)"
[[ -f "$CFG/mosquitto.conf" ]] || fail "missing $CFG/mosquitto.conf"
[[ -f "$CFG/passwd" ]] || fail "missing $CFG/passwd; restore the static credential file first"
[[ -f "$CFG/acl" ]] || fail "missing $CFG/acl"
[[ -f "infra/mosquitto/certs/server.key" ]] || fail "missing broker TLS key"

if grep -q "dynamic_security" "$CFG/mosquitto.conf"; then
  fail "mosquitto.conf already references Dynamic Security; do not rerun this script"
fi

if ! broker_running; then
  fail "the current broker is not running; fix the static deployment before migration"
fi

# These checks catch the exact class of failure that previously caused the
# broker restart loop before the script changes any file.
docker compose exec -T broker test -r /mosquitto/config/passwd \
  || fail "broker cannot read the static password file"
docker compose exec -T broker test -r /mosquitto/config/acl \
  || fail "broker cannot read the static ACL file"
docker compose exec -T broker test -r /mosquitto/certs/server.key \
  || fail "broker cannot read the TLS private key"

PLUGIN_PATH="$(docker compose exec -T broker sh -c '
  for p in /usr/lib/mosquitto_dynamic_security.so \
           /usr/lib/x86_64-linux-gnu/mosquitto_dynamic_security.so; do
    if [ -f "$p" ]; then printf "%s" "$p"; exit 0; fi
  done
  exit 1
' | tr -d '\r')" || fail "Dynamic Security plugin not found in broker image"

echo "[$SCRIPT_VERSION] Current broker checks: PASS"
echo "Dynamic Security plugin: $PLUGIN_PATH"

# Never overwrite an existing store. If the admin password was not saved, the
# operator must make an explicit, separately backed-up reset decision instead.
if docker compose run --rm --no-deps --entrypoint sh broker -c \
  'test -s /mosquitto/dynsec/dynamic-security.json' >/dev/null 2>&1; then
  fail "Dynamic Security store already exists; refusing to reinitialise it"
fi

cp "$CFG/mosquitto.conf" "$CONFIG_BACKUP"
cp "$COMPOSE" "$COMPOSE_BACKUP"

# From this point onward the script may have changed compose state, so the
# EXIT trap must restore both files if any later command fails.
MIGRATION_STARTED=1

# Add the writable state volume without rewriting unrelated project changes.
python3 - <<'PY'
p = 'docker-compose.yml'
s = open(p).read()
if 'mosquitto_dynsec:/mosquitto/dynsec' not in s:
    old_vol = '      - mosquitto_data:/mosquitto/data\n'
    new_vol = old_vol + '      - mosquitto_dynsec:/mosquitto/dynsec\n'
    if old_vol not in s:
        raise SystemExit('broker volume list not found')
    s = s.replace(old_vol, new_vol, 1)
if '\n  mosquitto_dynsec:\n' not in s:
    old_decl = 'volumes:\n  mosquitto_data:\n'
    new_decl = old_decl + '  mosquitto_dynsec:\n'
    if old_decl not in s:
        raise SystemExit('top-level volume declaration not found')
    s = s.replace(old_decl, new_decl, 1)
open(p, 'w').write(s)
PY

docker compose config >/dev/null || fail "compose file is invalid after adding the dynsec volume"

echo "Recreating broker with the writable dynsec volume..."
docker compose up -d --force-recreate broker >/dev/null
wait_for_broker

# Initialise the store as root, then make the mutable state writable by the
# mosquitto runtime user. The old version created a root-owned store on some
# Docker installations, which fails only on the first runtime write.
DYNSEC_ADMIN_PASSWORD="$(openssl rand -base64 24)"
echo "Creating the Dynamic Security store..."
docker compose exec -T -u 0 broker sh -c \
  "mosquitto_ctrl dynsec init /mosquitto/dynsec/dynamic-security.json \
   atlas_dynsec_admin '$DYNSEC_ADMIN_PASSWORD'"

docker compose exec -T -u 0 broker sh -c \
  'chown -R mosquitto:mosquitto /mosquitto/dynsec &&
   chmod 700 /mosquitto/dynsec &&
   chmod 600 /mosquitto/dynsec/dynamic-security.json'
docker compose exec -T broker test -s /mosquitto/dynsec/dynamic-security.json

# Store the generated credentials only in shell variables and print them once
# at the end. The first init attempt above is intentionally not allowed to
# expose a password in an error message.
if [[ -z "${DYNSEC_ADMIN_PASSWORD:-}" ]]; then
  fail "could not determine the generated Dynamic Security admin password"
fi

python3 - "$PLUGIN_PATH" <<'PY'
import sys
p = 'infra/mosquitto/config/mosquitto.conf'
plugin_path = sys.argv[1]
s = open(p).read()
s = s.replace('password_file /mosquitto/config/passwd\n', '')
s = s.replace('acl_file /mosquitto/config/acl\n', '')
anchor = 'allow_anonymous false\n'
if anchor not in s:
    raise SystemExit('allow_anonymous line not found')
block = f'''{anchor}# Dynamic Security Plugin. Replaces the static password and ACL files.
# The client and role store is mutable state on a writable Docker volume.
plugin {plugin_path}
plugin_opt_config_file /mosquitto/dynsec/dynamic-security.json
'''
s = s.replace(anchor, block, 1)
open(p, 'w').write(s)
PY

echo "Restarting broker with Dynamic Security enabled..."
docker compose restart broker >/dev/null
wait_for_broker

RECENT_LOGS="$(docker compose logs --since=15s broker 2>&1 || true)"
printf '%s\n' "$RECENT_LOGS"
if ! broker_running; then
  printf '%s\n' "$RECENT_LOGS" >&2
  fail "broker is not running after Dynamic Security restart"
fi
if grep -qiE 'error loading dynamic security plugin config|unable to load.*dynamic security|permission denied' <<<"$RECENT_LOGS"; then
  printf '%s\n' "$RECENT_LOGS" >&2
  fail "broker reported a Dynamic Security configuration error"
fi

# Do not silently continue before the operator has the admin credentials.
echo
echo "Migration completed. Save these values in .env before seeding roles:"
echo "DYNSEC_ADMIN_USER=atlas_dynsec_admin"
echo "DYNSEC_ADMIN_PASSWORD=$DYNSEC_ADMIN_PASSWORD"
echo ""
echo "Next: ./tools/dynsec_seed.sh"
