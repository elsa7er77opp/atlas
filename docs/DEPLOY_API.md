# Deploying the API service

Assessment AT-26-L1-002 · Software release candidate

## 1. Copy the files in

```
services/api/                      -> new
infra/postgres/init/02_api.sql     -> new
docs/evidence/verify_api.sh        -> new
```

Merge the `api:` block from `docker-compose.api-fragment.yml` into the
`services:` section of your existing `docker-compose.yml`, then delete the
fragment file.

## 2. Add the new secrets

```bash
echo "JWT_SECRET=$(openssl rand -base64 48)"            >> .env
echo "BOOTSTRAP_ADMIN_USER=admin"                       >> .env
echo "BOOTSTRAP_ADMIN_PASSWORD=$(openssl rand -base64 18)" >> .env
```

Add the **names only** to `.env.example`. Confirm nothing leaked:

```bash
git check-ignore -v .env && git ls-files | grep -c '^\.env$'   # expect 0
```

## 3. Apply the migration

`/docker-entrypoint-initdb.d` runs once, on an empty data directory. Your
`pgdata` volume already exists, so `02_api.sql` will not run by itself:

```bash
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    < infra/postgres/init/02_api.sql
```

Every statement is idempotent, so re-running is harmless.

## 4. Build and start

```bash
docker compose build api
docker compose up -d api
docker compose logs -f api
```

Expect, in order: `database pool ready`, `bootstrap administrator created`,
`listening for database notifications`, `api service started`.

## 5. Verify

```bash
curl -s localhost:8000/health
set -a; source .env; set +a
./docs/evidence/verify_api.sh | tee docs/evidence/m2_api_proof.txt
```

Interactive documentation: <http://localhost:8000/docs>

## 6. Freeze the generated specification

Report 8.9 says the generated document becomes authoritative once the service
exists, and that divergence from ATL-M1-008 is recorded rather than corrected
silently:

```bash
curl -s localhost:8000/openapi.json > docs/openapi_generated.json
```

Diff it against ATL-M1-008 and record each difference for the as-built revision.

## 7. Enable Dynamic Security broker provisioning

Milestone 3 replaces the static password/ACL edit path with the Mosquitto
Dynamic Security Plugin. The plugin state is mutable runtime data and is kept
in the `mosquitto_dynsec` Docker volume; it must not be committed to Git.

Run the migration from the repository root while the broker is running:

```bash
./tools/migrate_dynsec.sh
```

Save the generated `DYNSEC_ADMIN_USER` and `DYNSEC_ADMIN_PASSWORD` in the
local `.env`, then seed the least-privilege roles and service clients:

```bash
set -a; source .env; set +a
./tools/dynsec_seed.sh
```

Set `BROKER_PROVISIONING=dynsec` and restart the API. The API connects to the
broker's TLS listener using `/certs/ca.crt`, creates each device client with
the one-time password, and deletes the client when the device is removed. A
failed broker operation is reported as `broker_provisioned: false`; it never
returns a password or broker error detail in the HTTP response.

The simulator and ingest credentials must also be present in `.env` before
starting their containers. Do not run `docker compose down -v` in production:
it deletes the database and broker credential volumes.

## 8. Verify runtime configuration downlink

The device configuration path is now part of the software release. The API
stores an append-only configuration history and publishes the latest desired
configuration retained to `atlas/<site>/<device_uid>/config`. The simulator
subscribes to that topic, applies `sample_interval_ms`, and publishes an
acknowledgement to `atlas/<site>/<device_uid>/config/ack`. The ESP32 firmware
implements the same protocol and persists the interval in NVS.

After rebuilding the API and simulator, log into the dashboard as an operator,
open a device, change its sample interval, and apply it. Confirm the following:

```bash
docker compose logs --since=60s simulator | grep -E 'configuration applied|subscribed to'
docker compose logs --since=60s api | grep -E 'device configuration changed|downlink'
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT device_id, sample_interval_ms, updated_at FROM device_config ORDER BY updated_at DESC LIMIT 5;"
```

The simulator should report the new interval and continue publishing telemetry;
`ingest` should continue to show accepted readings with no `not authorised`
errors. For the physical node, the remaining bring-up work is wiring, flashing,
and observing the serial sequence in `firmware/atlas-node/README.md`.

## 9. Deliberate release limitations

| Item | Where | Why |
|---|---|---|
| Rate limiting is per-process | `app/security.py` | Correct at one replica. Needs a shared counter at N. |
| One uvicorn worker | `Dockerfile` | The `LISTEN` thread is per-process; fan-out across workers needs a shared broker. |
| PlatformIO binary build in CI | `firmware/atlas-node` | The source is pinned and syntax-reviewed; CI or a developer workstation should run the final cross-build because the sandbox may not have the ESP32 toolchain cache. |
