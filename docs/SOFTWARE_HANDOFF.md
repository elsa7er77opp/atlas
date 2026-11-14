# Atlas Software Handoff

## Release status

The Atlas software release is ready for hardware bring-up. The platform contains the API, PostgreSQL/TimescaleDB storage, Mosquitto over TLS, Dynamic Security provisioning, ingest, anomaly detection, simulator, dashboard, and ESP32 firmware. The simulator and the physical-node firmware use the same telemetry contract.

The remaining work is physical bring-up: connect the sensors, generate the host certificate and CA header for the deployment address, register the device, flash the firmware, and verify the serial boot sequence.

## Completed software capabilities

| Area | Completed behavior |
|---|---|
| Authentication | Login, refresh, logout, role enforcement, audit trail, and rate limiting for the API. |
| Device lifecycle | Register, soft-delete, broker provisioning, one-time MQTT credential return, and deprovisioning. |
| Broker security | Mosquitto TLS plus Dynamic Security roles and service clients with least-privilege ACLs. |
| Telemetry | Simulator and firmware publish the same JSON telemetry contract; ingest validates and persists readings. |
| Monitoring | Device status, historical trends, alarms, events, acknowledgement, and live alarm delivery. |
| Runtime configuration | Operator can update `sample_interval_ms`; the API stores an append-only history and publishes a retained downlink. |
| Device behavior | Simulator and ESP32 subscribe to `atlas/<site>/<uid>/config`, validate the interval, persist it, and publish `config/ack`. |
| Firmware | TLS, NTP-before-TLS, sensor discovery, INA219/MPU6050/BME280 support, degraded readings, NVS runtime interval, and structured serial logs. |

## Deployment after pulling the software changes

Run the following from the repository root on the Kali host:

```bash
set -a
source .env
set +a

# Seed the newly added config/config-ack ACLs. This is idempotent.
./tools/dynsec_seed.sh

docker compose build api simulator dashboard
docker compose up -d api simulator dashboard
sleep 20
docker compose ps
curl -s http://127.0.0.1:8000/health
```

The API container must become `healthy`. The broker must remain `Up`, and the ingest service must continue to report accepted readings with no `not authorised` errors.

## Runtime configuration test without hardware

Open the dashboard at `http://127.0.0.1:5173`, sign in as an operator, open a device, change its sample interval, and select **Apply to device**. Then inspect the simulator and API logs:

```bash
docker compose logs --since=60s simulator | grep -E 'configuration applied|subscribed to'
docker compose logs --since=60s api | grep -E 'device configuration changed|downlink'

docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT device_id, sample_interval_ms, updated_at FROM device_config ORDER BY updated_at DESC LIMIT 5;"
```

The simulator should log the new interval and continue publishing telemetry. The database query should show a new append-only configuration row. If the API cannot publish, it returns a visible 503 error after saving the history row; retrying after broker recovery is safe.

## Hardware bring-up

The physical sequence is documented in `firmware/atlas-node/README.md` and is intentionally performed only after the software test passes:

```bash
./tools/reissue_broker_cert.sh
./tools/make_ca_header.sh
./tools/provision_node.sh atlas-node-01
cp firmware/atlas-node/include/atlas_config.example.h \
   firmware/atlas-node/include/atlas_config.h
$EDITOR firmware/atlas-node/include/atlas_config.h
cd firmware/atlas-node
pio run
pio run -t upload
pio device monitor
```

The firmware build was verified against the pinned PlatformIO environment. In the current bench configuration, INA219 and the AHT20+BMP280 pair are enabled; BME280 and MPU6050 are disabled. The node publishes six metrics: three environmental and three electrical. Set `ENABLE_BME280=1` in `platformio.ini` only after the BME280 is physically installed and its chip ID is confirmed.

## Operational safety

Do not run `docker compose down -v` on the deployment host. Keep the database and broker data volumes. Keep the generated Dynamic Security admin password in the local `.env`, never in Git or in issue/chat messages. Rotate any credentials that were exposed during development before the final field deployment.

## Release verification

The following checks passed in the development workspace after the final software changes:

```text
Python compile: PASS
API syntax: PASS
Dashboard lint: PASS
Dashboard build: PASS
Bash syntax: PASS
ESP32 PlatformIO build: PASS
```

The dashboard build emits only a non-fatal bundle-size advisory. The final hardware acceptance check remains the serial boot sequence, successful TLS connection, configuration subscription, telemetry publication, and ingestion of a real sensor reading.
