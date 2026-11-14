#!/usr/bin/env bash
# Project Atlas - provision a physical node
# AT-26-L1-002  |  supports FR-01, FR-26
#
# A device needs three things before it can publish, and they live in three
# different places. Missing any one of them produces a different failure, and
# only one of the three is loud:
#
#   1. a broker credential    - missing: connection refused, MQTT state 5
#   2. an ACL entry           - missing: connection SUCCEEDS, publishes are
#                               silently discarded, and nothing is logged at
#                               default verbosity. This is the quiet one.
#   3. a devices row          - missing: telemetry arrives and the ingestion
#                               service discards it as "reading from an
#                               unregistered device"
#
# Broker provisioning is manual at Milestone 2 by design: BROKER_PROVISIONING is
# set to "static" and the Dynamic Security Plugin migration is Milestone 3 work.
#
# Run from the repository root:  ./tools/provision_node.sh atlas-node-01

set -euo pipefail

UID_ARG="${1:-atlas-node-01}"
SITE="${SITE:-lab-a}"
CFG="infra/mosquitto/config"

[[ -d "$CFG" ]] || { echo "run from the repository root (no $CFG)"; exit 1; }

# ------------------------------------------------------ 1. broker credential
PASS="$(openssl rand -base64 18)"
docker compose exec -T broker mosquitto_passwd -b /mosquitto/config/passwd "$UID_ARG" "$PASS"
echo "broker credential created for $UID_ARG"

# ------------------------------------------------------------- 2. ACL entry
# The device may publish only under its own topic prefix and may not subscribe
# to anything. A compromised node therefore cannot read another node's
# telemetry - the property the three-part broker test in the progress report
# verified for the simulator, extended to this device.
if grep -q "^user ${UID_ARG}$" "$CFG/acl"; then
  echo "ACL entry already present, left unchanged"
else
  cat >> "$CFG/acl" <<EOF

user ${UID_ARG}
topic write atlas/${SITE}/${UID_ARG}/telemetry
topic write atlas/${SITE}/${UID_ARG}/status
EOF
  echo "ACL entry appended"
fi

docker compose exec -T broker kill -HUP 1 2>/dev/null || docker compose restart broker
echo "broker reloaded"

# ---------------------------------------------------------- 3. devices row
# Registered through the API rather than by direct INSERT: FR-01 asks for device
# registration as a platform capability, and the API path writes the audit trail
# entry that a manual INSERT would not. The field names are read from the served
# OpenAPI document rather than assumed, so this cannot drift from the schema.
echo
echo "--- the device registration schema currently served by the API ---"
curl -s localhost:8000/openapi.json | python3 - <<'PY'
import json, sys
spec = json.load(sys.stdin)
for path, ops in spec.get("paths", {}).items():
    if path.rstrip("/").endswith("/devices") and "post" in ops:
        ref = (ops["post"].get("requestBody", {})
               .get("content", {}).get("application/json", {})
               .get("schema", {}).get("$ref", ""))
        name = ref.rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"].get(name, {})
        print(f"POST {path}  ->  {name}")
        req = schema.get("required", [])
        for prop, meta in schema.get("properties", {}).items():
            mark = "required" if prop in req else "optional"
            print(f"  {prop:<20} {meta.get('type','?'):<10} {mark}")
        break
else:
    print("no device creation operation found - is the API running?")
PY

echo
echo "==================== SAVE THIS ===================="
echo "device_uid     : $UID_ARG"
echo "mqtt_username  : $UID_ARG"
echo "mqtt_password  : $PASS"
echo "==================================================="
echo
echo "Next: register the device through the API or the dashboard using the"
echo "fields above, then put device_uid and mqtt_password into"
echo "firmware/atlas-node/include/atlas_config.h and flash the node."
