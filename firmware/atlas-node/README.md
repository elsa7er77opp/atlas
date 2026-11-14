# Atlas Node Firmware

ESP32-WROOM-32 (NodeMCU ESP-32S, 38-pin DevKit). Publishes telemetry to the platform over
MQTT/TLS on the same payload contract as the software simulator.

Serves **FR-03**, **FR-05**, **FR-06**, and supplies the Milestone 2 exit
criterion: a physical device publishing a real reading that is received and
persisted.

---

## Wiring

All three breakouts share one I2C bus. The ESP32 is a 3.3 V part and every
breakout here is 3.3 V tolerant, so no level shifting is required.

| Signal | ESP32 pin | Breadboard hole | INA219 | AHT20+BMP280 |
|---|---|---|---|---|
| 3V3 | 3V3 | `a5` | VCC | VDD |
| GND | GND | `a18` | GND | GND |
| SDA | GPIO25 | `a13` | SDA | SDA |
| SCL | GPIO13 | `a19` | SCL | SCL |

Addresses in use: AHT20 `0x38`, INA219 `0x40`, BMP280 `0x77`. All distinct, so
they coexist without address straps.

The 38-pin DevKit is exactly 1.0 inch wide, so its pins occupy breadboard rows
`b` and `j` rather than `e` and `f`. Row `a` is the only accessible row, and the
buried header carries GPIO21 and GPIO22 — the default I2C pins. The bus is
therefore assigned to GPIO25 and GPIO13, both general-purpose pins with no boot
function and no flash bonding, six breadboard positions apart to preclude
accidental shorting. Recorded as Deviation 10.

Supply the breakouts from 3V3, not 5V. The pull-up resistors on each board are
tied to that board's VCC and hold SDA and SCL high; at 5 V both bus lines would
sit at 5 V against pins rated 3.3 V with an absolute maximum near 3.6 V. Total
bus consumption is under 2 mA, so there is no power argument for the higher rail.

The INA219 measures the load in series: `VIN+` from the supply, `VIN-` to the
load. Putting it in parallel measures nothing and may short the supply.

Most breakouts ship with headers unsoldered. Solder before powering.

---

## Build configuration

Sensor selection is a build flag in `platformio.ini`, not a source edit:

```ini
-D ENABLE_INA219=1
-D ENABLE_MPU6050=0     ; out of scope for this milestone
-D ENABLE_BME280=0     ; on order - set to 1 on arrival
```

With the BME280 disabled the node emits six metrics rather than nine. The
platform requires no change for this: the ingestion path stores whatever
metrics a device sends. It does mean the bandwidth and storage figures in
report Section 9 describe a nine-metric configuration while the bench runs six,
and the weekly progress summary should say so rather than leave the
discrepancy to be found at review.

A BMP280 alone is not a substitute for a BME280: it has no humidity channel,
the breakouts are visually identical, and the firmware refuses one on chip ID
(`0x60` = BME280, `0x58` = BMP280) rather than publishing a fabricated
`humidity_pct`. The adopted solution is the AHT20 and BMP280 together, where
the AHT20 supplies the missing humidity channel — two chips at two addresses
covering the same three metrics one BME280 would have covered alone.

---

## Bring-up

Order matters. Steps 1 and 2 must precede the first connection attempt.

```bash
# 1. certificate valid for the host IP, not just the container name
./tools/reissue_broker_cert.sh

# 2. embed the CA into the firmware
./tools/make_ca_header.sh

# 3. broker credential, ACL entry, and the registration schema
./tools/provision_node.sh atlas-node-01

# 4. configuration
cp firmware/atlas-node/include/atlas_config.example.h \
   firmware/atlas-node/include/atlas_config.h
$EDITOR firmware/atlas-node/include/atlas_config.h    # wifi, host IP, password

# 5. flash and watch
cd firmware/atlas-node && pio run -t upload && pio device monitor
```

Add to `.gitignore`:

```
firmware/atlas-node/include/atlas_config.h
firmware/atlas-node/include/ca_cert.h
.pio/
```

---

## Expected boot sequence

```
atlas node starting
i2c survey
  0x40  INA219
  0x68  MPU6050
INA219 initialised
MPU6050 initialised
wifi connected
clock set
broker connected, online status published
publishing  seq=30 metrics=6 quality=ok
```

Confirm on the platform side:

```bash
docker compose logs --tail=20 ingest | grep atlas-node-01
docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -c \
  "SELECT metric, count(*), max(time) FROM telemetry t
     JOIN devices d ON d.id=t.device_id
    WHERE d.device_uid='atlas-node-01' GROUP BY metric;"
```

---

## Diagnosis

| Symptom | Cause |
|---|---|
| `broker connection refused, state: -2` | TLS. Certificate has no SAN for the host IP, or the CA header is stale, or the clock is unset. |
| `broker connection refused, state: 5` | Authorisation. The password file has no entry for this `device_uid`. |
| Connects, then nothing arrives | ACL. The device may connect but not publish. Set `log_type all` on the broker to see the refusal. |
| `reading from unregistered device discarded` in the ingest log | No `devices` row. Register through the API. |
| `publish failed, bytes: 3xx` | MQTT buffer. `setBufferSize(512)` was removed or overridden. |
| `no i2c devices responded` | Wiring, or the breakout is unpowered. Check 3V3 before suspecting the driver. |

The first three are the ones this bench will actually hit, in that order.

---

## Runtime configuration

The platform can update `sample_interval_ms` and `enabled_metrics` without
reflashing the node. The API publishes a retained JSON document to:

```text
atlas/<site>/<uid>/config
```

The firmware subscribes after every TLS reconnect, validates the interval
between 100 ms and 3,600,000 ms, persists it in ESP32 NVS, and publishes a
retained acknowledgement to:

```text
atlas/<site>/<uid>/config/ack
```

A retained configuration is delivered immediately after a device reconnects,
so a power cycle does not revert the node to its compile-time default. The
compile-time `SAMPLE_INTERVAL_MS` remains the safe fallback used on first boot
or if NVS is unavailable. `enabled_metrics` is persisted for forward
compatibility; the current firmware continues to publish every sensor enabled
by its build flags.

## Software complete before hardware

The software path is complete when the API, broker, simulator, ingest service,
and dashboard are running and the simulator receives a configuration update and
returns an acknowledgement. The remaining work is physical: wire the sensors,
flash the generated firmware configuration, and verify the serial bring-up
sequence in the section above.
