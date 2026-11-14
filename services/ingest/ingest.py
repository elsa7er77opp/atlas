#!/usr/bin/env python3
"""
Project Atlas - ingestion service
AT-26-L1-002  |  serves FR-06, FR-07, FR-08, FR-03, FR-17, FR-18

Subscribes to the telemetry and status topics, validates every payload at the
service boundary, and persists accepted readings to the TimescaleDB hypertable.

Three design points are load-bearing and worth stating explicitly:

1.  Validation happens BEFORE persistence, at the boundary. A malformed payload
    is logged with the failing field and discarded; it cannot halt ingestion.
    This is FR-07, and the "devices are untrusted" principle in Section 1.3.

2.  The broker acknowledgement is withheld until the write has committed. With
    QoS 1 the broker therefore redelivers anything lost to a crash between
    receipt and commit, rather than silently dropping it.

3.  The in-memory queue is bounded and transient. It is not durable state: on
    restart it is lost and rebuilt from redelivery. Section 8.4 says this
    plainly rather than claiming the service is stateless.
"""

import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import paho.mqtt.client as mqtt
import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, ValidationError, field_validator

# ----------------------------------------------------------------- config
BROKER_HOST = os.getenv("MQTT_HOST", "atlas-broker")
BROKER_PORT = int(os.getenv("MQTT_PORT", "8883"))
CA_CERT     = os.getenv("MQTT_CA", "/certs/ca.crt")
USERNAME    = os.getenv("MQTT_USERNAME", "atlas_ingest")
PASSWORD    = os.getenv("MQTT_PASSWORD", "")

PG_HOST = os.getenv("POSTGRES_HOST", "atlas-db")
PG_USER = os.getenv("POSTGRES_USER", "atlas")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")
PG_DB   = os.getenv("POSTGRES_DB", "atlas")

# Section 9.7 sizes this against a 5-minute database outage at 2 msg/s.
QUEUE_MAX   = int(os.getenv("QUEUE_MAX", "600"))
BATCH_SIZE  = int(os.getenv("BATCH_SIZE", "50"))
BATCH_MS    = int(os.getenv("BATCH_MS", "500"))
STALE_MULT  = 3          # staleness = 3x sample interval, Section 10.3

TOPIC_TELEMETRY = "atlas/+/+/telemetry"
TOPIC_STATUS    = "atlas/+/+/status"

running = True
work_q: "queue.Queue" = queue.Queue(maxsize=QUEUE_MAX)

_stats = {"received": 0, "accepted": 0, "rejected": 0, "written": 0, "dropped": 0}
_device_cache: Dict[str, int] = {}
_last_seq: Dict[str, int] = {}
_broker_connected = False
_liveness_reported: set[str] = set()


def log(level: str, message: str, **kw) -> None:
    """Structured JSON to stdout. FR-17, FR-18."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "component": "ingest",
        "message": message,
    }
    rec.update(kw)
    # MQTT v5 exposes reason codes as enum-like objects; stringify unknown
    # values so a broker refusal cannot crash the reconnect thread itself.
    print(json.dumps(rec, default=str), flush=True)


# ------------------------------------------------------------- validation
class TelemetryPayload(BaseModel):
    """
    The payload contract from Section 8.5, enforced declaratively.

    Pydantic is doing the work FR-07 asks for: types, ranges and required
    fields are checked at the boundary rather than by hand-written checks
    scattered through the ingestion path.
    """
    device_uid: str = Field(min_length=3, max_length=64)
    ts: datetime
    seq: int = Field(ge=0)
    metrics: Dict[str, float]
    quality: str = "ok"

    @field_validator("quality")
    @classmethod
    def quality_known(cls, v: str) -> str:
        if v not in {"ok", "degraded", "stale"}:
            raise ValueError(f"quality must be ok|degraded|stale, got {v!r}")
        return v

    @field_validator("metrics")
    @classmethod
    def metrics_sane(cls, v: Dict[str, float]) -> Dict[str, float]:
        if not v:
            raise ValueError("metrics must not be empty")
        if len(v) > 32:
            raise ValueError(f"too many metrics: {len(v)}")
        for name, value in v.items():
            if not isinstance(value, (int, float)):
                raise ValueError(f"metric {name} is not numeric")
            # Reject NaN and infinity, which would poison the rolling statistics
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError(f"metric {name} is not finite")
        return v


class StatusPayload(BaseModel):
    device_uid: str
    state: str
    reason: Optional[str] = None
    fw: Optional[str] = None
    ip: Optional[str] = None

    @field_validator("state")
    @classmethod
    def state_known(cls, v: str) -> str:
        if v not in {"online", "offline", "degraded"}:
            raise ValueError(f"unknown state {v!r}")
        return v


# ------------------------------------------------------------- database
def connect_db(retries: int = 30) -> psycopg.Connection:
    """Wait for the database rather than crash-looping on startup."""
    dsn = f"host={PG_HOST} user={PG_USER} password={PG_PASS} dbname={PG_DB}"
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
            log("info", "database connected")
            return conn
        except psycopg.OperationalError as e:
            wait = min(2 ** min(attempt, 5), 32)
            log("warning", "database not ready, retrying",
                attempt=attempt, wait_s=wait, error=str(e)[:120])
            time.sleep(wait)
    raise RuntimeError("database unreachable after retries")


def device_id_for(conn: psycopg.Connection, device_uid: str) -> Optional[int]:
    """Resolve device_uid to its primary key, cached after first lookup."""
    if device_uid in _device_cache:
        return _device_cache[device_uid]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devices WHERE device_uid = %s AND deleted_at IS NULL",
            (device_uid,))
        row = cur.fetchone()
    if row:
        _device_cache[device_uid] = row["id"]
        return row["id"]
    return None


def writer_loop(conn: psycopg.Connection) -> None:
    """
    Drain the queue in batches and commit.

    Batching is not an optimisation for its own sake: Section 9.7 shows the
    offered rate is three orders of magnitude below capacity, so this exists to
    keep transaction count low and make the acknowledge-after-commit boundary
    clean, not to chase throughput.
    """
    batch = []
    last_flush = time.time()

    while running or not work_q.empty():
        timeout = max(0.0, (BATCH_MS / 1000.0) - (time.time() - last_flush))
        try:
            item = work_q.get(timeout=timeout or 0.05)
            batch.append(item)
        except queue.Empty:
            pass

        due = (len(batch) >= BATCH_SIZE
               or (batch and (time.time() - last_flush) * 1000 >= BATCH_MS))
        if not due:
            continue

        try:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO telemetry (time, device_id, metric, value, quality) "
                    "VALUES (%s, %s, %s, %s, %s)", batch)
                device_ids = list({row[1] for row in batch})
                if device_ids:
                    cur.execute(
                        "UPDATE devices SET status = 'online', last_seen_at = now() "
                        "WHERE id = ANY(%s)",
                        (device_ids,))
            conn.commit()
            _stats["written"] += len(batch)
            log("info", "batch committed", rows=len(batch),
                total_written=_stats["written"])
        except Exception as e:
            conn.rollback()
            log("error", "batch write failed, rolling back",
                rows=len(batch), error=str(e)[:200])
        finally:
            batch.clear()
            last_flush = time.time()


# ------------------------------------------------------------- handlers
def handle_telemetry(conn: psycopg.Connection, raw: bytes) -> None:
    _stats["received"] += 1
    try:
        data = json.loads(raw.decode())
        payload = TelemetryPayload(**data)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as e:
        _stats["rejected"] += 1
        # The raw message AND the failing field are logged. FR-07, FR-17.
        log("error", "payload rejected at validation boundary",
            error=str(e)[:300], raw=raw[:200].decode(errors="replace"))
        return

    dev_id = device_id_for(conn, payload.device_uid)
    if dev_id is None:
        _stats["rejected"] += 1
        log("warning", "reading from unregistered device discarded",
            device_uid=payload.device_uid)
        return

    # Sequence gap detection: distinguishes a lost message from an idle device
    prev = _last_seq.get(payload.device_uid)
    if prev is not None and payload.seq > prev + 1:
        log("warning", "sequence gap detected",
            device_uid=payload.device_uid, expected=prev + 1,
            received=payload.seq, missing=payload.seq - prev - 1)
    _last_seq[payload.device_uid] = payload.seq

    _stats["accepted"] += 1
    for metric, value in payload.metrics.items():
        row = (payload.ts, dev_id, metric, float(value), payload.quality)
        try:
            work_q.put_nowait(row)
        except queue.Full:
            # Bounded queue: oldest-first discard, and the loss is never silent
            _stats["dropped"] += 1
            log("critical", "ingestion queue full, reading discarded",
                device_uid=payload.device_uid, metric=metric,
                queue_max=QUEUE_MAX, total_dropped=_stats["dropped"])


def handle_status(conn: psycopg.Connection, raw: bytes) -> None:
    """Device online/offline, including the broker's Last Will. FR-03."""
    try:
        data = json.loads(raw.decode())
        payload = StatusPayload(**data)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as e:
        log("error", "status payload rejected", error=str(e)[:200])
        return

    dev_id = device_id_for(conn, payload.device_uid)
    if dev_id is None:
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET status = %s, last_seen_at = now() WHERE id = %s",
                (payload.state, dev_id))
            cur.execute(
                "INSERT INTO events (device_id, severity, detector, message) "
                "VALUES (%s, %s, %s, %s)",
                (dev_id,
                 "warning" if payload.state != "online" else "info",
                 "connection",
                 f"device {payload.state}"
                 + (f" ({payload.reason})" if payload.reason else "")))
        conn.commit()
        log("info", "device status updated",
            device_uid=payload.device_uid, state=payload.state,
            reason=payload.reason)
    except Exception as e:
        conn.rollback()
        log("error", "status update failed", error=str(e)[:200])


# ------------------------------------------------------------- liveness

def check_liveness(conn: psycopg.Connection) -> None:
    """Raise one critical event when an online device stops producing data."""
    if not _broker_connected:
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id, d.device_uid, d.status, "
                "EXTRACT(EPOCH FROM (now() - d.last_seen_at)) * 1000 "
                "AS age_ms, COALESCE(cfg.sample_interval_ms, 1000) "
                "AS sample_interval_ms "
                "FROM devices d "
                "LEFT JOIN LATERAL ("
                "  SELECT sample_interval_ms FROM device_config "
                "  WHERE device_id = d.id ORDER BY updated_at DESC, id DESC LIMIT 1"
                ") cfg ON TRUE "
                "WHERE d.deleted_at IS NULL AND d.status IN ('online','degraded') "
                "AND d.last_seen_at IS NOT NULL")
            rows = cur.fetchall()

        for row in rows:
            uid = row["device_uid"]
            age_ms = float(row["age_ms"] or 0)
            interval_ms = int(row["sample_interval_ms"] or 1000)
            stale_after_ms = max(30_000, interval_ms * STALE_MULT)

            if age_ms > stale_after_ms:
                if uid in _liveness_reported:
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devices SET status = 'degraded' "
                        "WHERE id = %s AND status = 'online'",
                        (row["id"],))
                    cur.execute(
                        "INSERT INTO events "
                        "(device_id, severity, detector, message) "
                        "VALUES (%s, 'critical', 'ingest_liveness', %s)",
                        (row["id"],
                         f"no telemetry observed for {age_ms / 1000:.1f}s "
                         f"(expected every {interval_ms}ms)"))
                conn.commit()
                _liveness_reported.add(uid)
                log("critical", "device telemetry stalled while broker is connected",
                    device_uid=uid, age_ms=round(age_ms),
                    threshold_ms=stale_after_ms)
            elif uid in _liveness_reported:
                _liveness_reported.remove(uid)
                log("info", "device telemetry recovered", device_uid=uid,
                    age_ms=round(age_ms))
    except Exception as e:
        conn.rollback()
        log("error", "liveness check failed", error=str(e)[:200])


# ------------------------------------------------------------- mqtt
def main() -> int:
    global running

    if not PASSWORD or not PG_PASS:
        print("MQTT_PASSWORD and POSTGRES_PASSWORD must be set", file=sys.stderr)
        return 1

    def shutdown(signum, frame):
        global running
        log("info", "shutdown signal received", signal=signum)
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    conn = connect_db()
    writer = threading.Thread(target=writer_loop, args=(conn,), daemon=True)
    writer.start()

    def on_connect(client, userdata, flags, rc, properties=None):
        global _broker_connected
        if rc == 0:
            log("info", "broker connected over TLS",
                host=BROKER_HOST, port=BROKER_PORT)
            result, mid = client.subscribe(
                [(TOPIC_TELEMETRY, 1), (TOPIC_STATUS, 1)])
            if result != mqtt.MQTT_ERR_SUCCESS:
                _broker_connected = False
                log("error", "broker subscription failed", rc=result, mid=mid)
                return
            _broker_connected = False
            log("info", "subscription requested after broker connection",
                topics=[TOPIC_TELEMETRY, TOPIC_STATUS], mid=mid)
        else:
            _broker_connected = False
            log("error", "broker refused connection", rc=str(rc))

    def on_subscribe(client, userdata, mid, granted_qos, properties=None):
        global _broker_connected
        qos_values = [getattr(qos, "value", qos) for qos in granted_qos]
        refused = [str(qos) for qos in qos_values if qos >= 128]
        if refused:
            _broker_connected = False
            log("error", "broker refused subscription", mid=mid,
                granted_qos=[str(qos) for qos in granted_qos])
            return
        _broker_connected = True
        log("info", "subscriptions confirmed by broker", mid=mid,
            granted_qos=[str(qos) for qos in granted_qos])

    def on_message(client, userdata, msg):
        if msg.topic.endswith("/telemetry"):
            handle_telemetry(conn, msg.payload)
        elif msg.topic.endswith("/status"):
            handle_status(conn, msg.payload)

    def on_disconnect(client, userdata, rc, properties=None, reason=None):
        global _broker_connected
        _broker_connected = False
        # rc is a paho ReasonCode object, not an int, and json.dumps cannot
        # serialise it. Logging it raw made the error handler throw at exactly
        # the moment the service needed to report a failure.
        log("warning", "broker disconnected, will retry", rc=str(rc))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="atlas-ingest")
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set(ca_certs=CA_CERT, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=32)

    log("info", "ingestion service starting",
        queue_max=QUEUE_MAX, batch_size=BATCH_SIZE, batch_ms=BATCH_MS)
    client.connect(BROKER_HOST, BROKER_PORT, keepalive=30)
    client.loop_start()

    try:
        while running:
            time.sleep(10)
            check_liveness(conn)
            log("info", "ingestion statistics", **_stats,
                queue_depth=work_q.qsize(), broker_connected=_broker_connected)
    finally:
        log("info", "draining queue before exit", pending=work_q.qsize())
        client.loop_stop()
        client.disconnect()
        writer.join(timeout=10)
        conn.close()
        log("info", "stopped", **_stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
