#!/usr/bin/env python3
"""
Project Atlas - device simulator
AT-26-L1-002  |  serves FR-05

Publishes on the identical topic taxonomy and payload contract as the physical
ESP32 node, so that the platform cannot distinguish it structurally from real
hardware. This is what allows the ingestion, storage, anomaly and dashboard
layers to be built and tested before the sensors arrive.

Contract reference: Consolidated Engineering Report, Section 8.5.

BME280 note
-----------
The BME280 had not arrived at the time of writing. Set ENABLE_BME280=false to
emit the six metrics the bench can currently produce (INA219 + MPU6050), or
true to emit the full nine-metric contract. The default is true so that the
platform is exercised against the contract it will run in production.
"""

import json
import math
import os
import random
import signal
import ssl
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ----------------------------------------------------------------- config
BROKER_HOST = os.getenv("MQTT_HOST", "atlas-broker")
BROKER_PORT = int(os.getenv("MQTT_PORT", "8883"))
CA_CERT     = os.getenv("MQTT_CA", "/certs/ca.crt")
USERNAME    = os.getenv("MQTT_USERNAME", "atlas-sim-01")
PASSWORD    = os.getenv("MQTT_PASSWORD", "")
DEVICE_UID  = os.getenv("DEVICE_UID", "atlas-sim-01")
SITE        = os.getenv("SITE", "lab-a")
INTERVAL_MS = int(os.getenv("SAMPLE_INTERVAL_MS", "1000"))
ENABLE_BME  = os.getenv("ENABLE_BME280", "true").lower() == "true"

TOPIC_TELEMETRY = f"atlas/{SITE}/{DEVICE_UID}/telemetry"
TOPIC_STATUS    = f"atlas/{SITE}/{DEVICE_UID}/status"
TOPIC_CMD       = f"atlas/{SITE}/{DEVICE_UID}/cmd"
TOPIC_CMD_ACK   = f"atlas/{SITE}/{DEVICE_UID}/cmd/ack"
TOPIC_CONFIG    = f"atlas/{SITE}/{DEVICE_UID}/config"
TOPIC_CONFIG_ACK = f"atlas/{SITE}/{DEVICE_UID}/config/ack"

KEEPALIVE = 30          # matches the Last Will detection bound in Section 10.3

# ----------------------------------------------------------------- state
seq = 0
running = True
load_closed = False     # relay state, driven by the command channel
t0 = time.time()


def log(msg: str) -> None:
    """Single-line structured log to stdout, captured by the Docker driver."""
    print(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": "info",
        "component": "simulator",
        "device": DEVICE_UID,
        "message": msg,
    }), flush=True)


def sample() -> dict:
    """
    Generate one physically plausible reading set.

    The signals are not random noise: each is modelled on how the real sensor
    behaves, so that the anomaly detectors are exercised against something
    representative rather than against white noise, which would make the
    false-alarm figures in Section 10 meaningless.
    """
    t = time.time() - t0
    m = {}

    if ENABLE_BME:
        # BME280: slow thermal drift plus small measurement noise
        m["temperature_c"] = round(24.5 + 1.5 * math.sin(t / 300) + random.gauss(0, 0.05), 2)
        m["humidity_pct"]  = round(47.0 + 3.0 * math.sin(t / 420) + random.gauss(0, 0.15), 2)
        m["pressure_hpa"]  = round(1012.0 + 0.8 * math.sin(t / 900) + random.gauss(0, 0.05), 2)

    # INA219: the load step is the demonstration anomaly. Closing the relay
    # adds 500 mA, which Section 9.6 sizes as 10 ohm across 5 V.
    base_current = 810.0 if load_closed else 310.0
    bus_v   = round(5.02 + random.gauss(0, 0.01), 3)
    cur_ma  = round(base_current + random.gauss(0, 4.0), 1)
    m["bus_voltage_v"] = bus_v
    m["current_ma"]    = cur_ma
    m["power_mw"]      = round(bus_v * cur_ma, 2)

    # MPU6050 at rest: z reads ~1 g, x and y near zero. Deliberately not
    # Gaussian-symmetric, which is the caveat recorded in Section 10.1.
    m["accel_x_g"] = round(random.gauss(0, 0.004), 4)
    m["accel_y_g"] = round(random.gauss(0, 0.004), 4)
    m["accel_z_g"] = round(0.998 + random.gauss(0, 0.003), 4)

    return m


def build_payload() -> str:
    global seq
    seq += 1
    return json.dumps({
        "device_uid": DEVICE_UID,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "seq": seq,
        "metrics": sample(),
        "quality": "ok",
    })


# ----------------------------------------------------------------- mqtt
def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log(f"connected to {BROKER_HOST}:{BROKER_PORT} over TLS")
        client.publish(TOPIC_STATUS,
                       json.dumps({"device_uid": DEVICE_UID, "state": "online",
                                   "fw": "sim-1.0.0"}),
                       qos=1, retain=True)
        client.subscribe([(TOPIC_CMD, 1), (TOPIC_CONFIG, 1)])
        log(f"subscribed to {TOPIC_CMD} and {TOPIC_CONFIG}")
    else:
        log(f"connection refused, rc={rc}")


def on_message(client, userdata, msg):
    """Handle commands and retained configuration downlinks."""
    global load_closed, INTERVAL_MS
    try:
        command = json.loads(msg.payload.decode())
    except json.JSONDecodeError:
        log("malformed downlink ignored")
        return

    if msg.topic == TOPIC_CONFIG:
        requested = command.get("sample_interval_ms", INTERVAL_MS)
        if not isinstance(requested, int) or not 100 <= requested <= 3_600_000:
            result = "rejected"
            reason = "sample_interval_ms must be between 100 and 3600000"
        else:
            INTERVAL_MS = requested
            result = "ok"
            reason = None
            log(f"configuration applied: interval={INTERVAL_MS} ms")
        ack = {"device_uid": DEVICE_UID, "result": result,
               "sample_interval_ms": INTERVAL_MS}
        if reason:
            ack["reason"] = reason
        client.publish(TOPIC_CONFIG_ACK, json.dumps(ack), qos=1, retain=True)
        return

    action = command.get("action")
    if action == "set_load":
        load_closed = bool(command.get("closed", False))
        log(f"load {'closed' if load_closed else 'opened'} by command")
        client.publish(TOPIC_CMD_ACK,
                       json.dumps({"device_uid": DEVICE_UID, "action": action,
                                   "result": "ok", "load_closed": load_closed}),
                       qos=1)
    else:
        log(f"unknown command: {action}")


def on_disconnect(client, userdata, rc, properties=None, reason=None):
    log(f"disconnected, rc={rc}")


def shutdown(signum, frame):
    global running
    running = False


def main() -> int:
    if not PASSWORD:
        print("MQTT_PASSWORD is not set", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"{DEVICE_UID}-sim")
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set(ca_certs=CA_CERT, tls_version=ssl.PROTOCOL_TLS_CLIENT)

    # Last Will: the broker publishes this if the connection drops without a
    # clean disconnect. This is what satisfies FR-03 at protocol level.
    client.will_set(TOPIC_STATUS,
                    json.dumps({"device_uid": DEVICE_UID, "state": "offline",
                                "reason": "lwt"}),
                    qos=1, retain=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    # Exponential backoff, matching the firmware resilience requirement
    client.reconnect_delay_set(min_delay=1, max_delay=32)

    metrics_n = 9 if ENABLE_BME else 6
    log(f"starting: {metrics_n} metrics at {INTERVAL_MS} ms "
        f"(BME280 {'enabled' if ENABLE_BME else 'disabled - not yet delivered'})")

    client.connect(BROKER_HOST, BROKER_PORT, keepalive=KEEPALIVE)
    client.loop_start()

    interval = INTERVAL_MS / 1000.0
    published = 0
    try:
        while running:
            start = time.time()
            client.publish(TOPIC_TELEMETRY, build_payload(), qos=1)
            published += 1
            if published % 60 == 0:
                log(f"{published} readings published")
            drift = time.time() - start
            time.sleep(max(0.0, interval - drift))
    finally:
        log("shutting down cleanly")
        client.publish(TOPIC_STATUS,
                       json.dumps({"device_uid": DEVICE_UID, "state": "offline",
                                   "reason": "shutdown"}),
                       qos=1, retain=True)
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
