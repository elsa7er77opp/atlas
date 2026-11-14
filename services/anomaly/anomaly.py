#!/usr/bin/env python3
"""
Project Atlas - anomaly detection engine
AT-26-L1-002  |  serves FR-09, FR-10, FR-14, FR-21

Four deterministic detectors run on every accepted reading. Their ordering is
the substance of the design: it guarantees that every alarm presented at
technical review can be explained in one sentence.

    threshold        value outside a configured min/max
    rate_of_change   step between consecutive samples exceeds a bound
    statistical      value outside rolling mean +/- z_sigma standard deviations
    staleness        no reading within 3x the sample interval

The statistical detector requires N consecutive qualifying samples before it
raises an alarm. Section 10.1 of the report shows why: at 1 Hz across eighteen
series, a bare 3-sigma rule produces approximately 4199 false alarms per day.
With N=3 confirmation that falls to roughly one every 33 days. A specification
that said "alarm at three standard deviations" and stopped there would have
produced an unusable system.

Alarm lifecycle
---------------
An event records that a condition occurred. An alarm is a state that persists
until the condition clears. The first qualifying event raises an alarm;
subsequent qualifying events update it rather than raising duplicates; a
configurable number of consecutive normal readings clears it. This is what
stops a flapping sensor generating hundreds of alarm rows.
"""

import json
import math
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

PG_HOST = os.getenv("POSTGRES_HOST", "atlas-db")
PG_USER = os.getenv("POSTGRES_USER", "atlas")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")
PG_DB   = os.getenv("POSTGRES_DB", "atlas")

POLL_MS         = int(os.getenv("POLL_MS", "1000"))
DEFAULT_WINDOW  = int(os.getenv("Z_WINDOW", "300"))
DEFAULT_SIGMA   = float(os.getenv("Z_SIGMA", "3.0"))
DEFAULT_CONFIRM = int(os.getenv("CONFIRM_SAMPLES", "3"))
CLEAR_SAMPLES   = int(os.getenv("CLEAR_SAMPLES", "3"))
STALE_MULTIPLIER = 3

running = True
_stats = {"evaluated": 0, "events": 0, "alarms_raised": 0, "alarms_cleared": 0}


def log(level: str, message: str, **kw) -> None:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "level": level,
        "component": "anomaly",
        "message": message,
    }
    rec.update(kw)
    print(json.dumps(rec), flush=True)


class RollingWindow:
    """
    Fixed-size ring buffer with incremental sum and sum-of-squares.

    The samples themselves must be retained. Cumulative Welford state gives
    mean and variance incrementally but provides no way to remove the oldest
    sample as the window advances, so the buffer is not an optimisation that
    could be dropped: it is what makes the window rolling rather than
    cumulative. Memory is 300 x 8 B plus 24 B of accumulators = 2424 B per
    series, or 42.6 KiB across all eighteen series (report Section 10.3).
    """

    __slots__ = ("n", "buf", "s", "s2")

    def __init__(self, n: int):
        self.n = n
        self.buf: deque = deque(maxlen=n)
        self.s = 0.0
        self.s2 = 0.0

    def push(self, x: float) -> None:
        if len(self.buf) == self.n:
            old = self.buf[0]
            self.s -= old
            self.s2 -= old * old
        self.buf.append(x)
        self.s += x
        self.s2 += x * x

    @property
    def count(self) -> int:
        return len(self.buf)

    def stats(self) -> Tuple[Optional[float], Optional[float]]:
        c = len(self.buf)
        if c < 2:
            return None, None
        mean = self.s / c
        var = max(0.0, (self.s2 - c * mean * mean) / (c - 1))
        return mean, math.sqrt(var)


class SeriesState:
    """Per device-metric detector state."""

    __slots__ = ("window", "last_value", "last_time", "breach_run",
                 "normal_run", "active_alarm_id")

    def __init__(self, window_len: int):
        self.window = RollingWindow(window_len)
        self.last_value: Optional[float] = None
        self.last_time: Optional[datetime] = None
        self.breach_run = 0
        self.normal_run = 0
        self.active_alarm_id: Optional[int] = None


def connect_db(retries: int = 30) -> psycopg.Connection:
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
    raise RuntimeError("database unreachable")


def load_thresholds(conn) -> Dict[Tuple[int, str], dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM thresholds")
        rows = cur.fetchall()
    return {(r["device_id"], r["metric"]): r for r in rows}


def raise_event(conn, device_id: int, severity: str, detector: str,
                metric: Optional[str], observed: Optional[float],
                message: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (device_id, severity, detector, metric, "
            "observed_value, message) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (device_id, severity, detector, metric, observed, message))
        event_id = cur.fetchone()["id"]
    _stats["events"] += 1
    return event_id


def raise_alarm(conn, event_id: int, device_id: int, severity: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO alarms (event_id, device_id, severity) "
            "VALUES (%s,%s,%s) RETURNING id", (event_id, device_id, severity))
        alarm_id = cur.fetchone()["id"]
    _stats["alarms_raised"] += 1
    return alarm_id


def clear_alarm(conn, alarm_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alarms SET state='cleared', cleared_at=now() "
            "WHERE id=%s AND state <> 'cleared'", (alarm_id,))
    _stats["alarms_cleared"] += 1


def evaluate(conn, state: SeriesState, cfg: Optional[dict],
             device_id: int, metric: str, value: float,
             ts: datetime) -> None:
    """Run the deterministic detectors, in order, on one reading."""
    _stats["evaluated"] += 1

    sigma   = float(cfg["z_sigma"]) if cfg and cfg.get("z_sigma") else DEFAULT_SIGMA
    confirm = int(cfg["confirm_samples"]) if cfg and cfg.get("confirm_samples") else DEFAULT_CONFIRM
    vmin    = cfg.get("min_value") if cfg else None
    vmax    = cfg.get("max_value") if cfg else None
    maxroc  = cfg.get("max_rate_of_change") if cfg else None

    breached = False
    detector = None
    message = ""
    severity = "warning"

    # ---- detector 1: fixed threshold -------------------------------------
    if vmin is not None and value < float(vmin):
        breached, detector = True, "threshold"
        severity = "critical"
        message = f"{metric}={value:.4g} below minimum {float(vmin):.4g}"
    elif vmax is not None and value > float(vmax):
        breached, detector = True, "threshold"
        severity = "critical"
        message = f"{metric}={value:.4g} above maximum {float(vmax):.4g}"

    # ---- detector 2: rate of change --------------------------------------
    # Runs without confirmation: a step is a single-sample event by nature,
    # and requiring N consecutive samples would defeat the purpose.
    if not breached and maxroc is not None and state.last_value is not None:
        delta = abs(value - state.last_value)
        if delta > float(maxroc):
            breached, detector = True, "rate_of_change"
            severity = "warning"
            message = (f"{metric} changed by {delta:.4g} in one sample, "
                       f"limit {float(maxroc):.4g}")

    # ---- detector 3: statistical, with N-consecutive confirmation --------
    stat_breach = False
    if not breached and state.window.count >= 30:
        mean, std = state.window.stats()
        if std and std > 1e-9:
            z = abs(value - mean) / std
            if z > sigma:
                stat_breach = True
                state.breach_run += 1
                if state.breach_run >= confirm:
                    breached, detector = True, "statistical"
                    severity = "warning"
                    message = (f"{metric}={value:.4g} is {z:.2f} sigma from "
                               f"rolling mean {mean:.4g}, confirmed over "
                               f"{state.breach_run} consecutive samples")
    if not stat_breach:
        state.breach_run = 0

    # ---- alarm lifecycle -------------------------------------------------
    if breached:
        state.normal_run = 0
        if state.active_alarm_id is None:
            ev = raise_event(conn, device_id, severity, detector,
                             metric, value, message)
            state.active_alarm_id = raise_alarm(conn, ev, device_id, severity)
            conn.commit()
            log("warning", "alarm raised", device_id=device_id, metric=metric,
                detector=detector, value=value,
                alarm_id=state.active_alarm_id, detail=message)
        else:
            # Alarm already active: record the event, do not duplicate the alarm
            raise_event(conn, device_id, severity, detector, metric, value, message)
            conn.commit()
    else:
        if state.active_alarm_id is not None:
            state.normal_run += 1
            if state.normal_run >= CLEAR_SAMPLES:
                clear_alarm(conn, state.active_alarm_id)
                raise_event(conn, device_id, "info", "clear", metric, value,
                            f"{metric} returned to normal for "
                            f"{state.normal_run} consecutive samples")
                conn.commit()
                log("info", "alarm cleared", device_id=device_id, metric=metric,
                    alarm_id=state.active_alarm_id)
                state.active_alarm_id = None
                state.normal_run = 0

    # window is updated only with the value, after evaluation, so a breach
    # does not immediately contaminate the baseline it was measured against
    state.window.push(value)
    state.last_value = value
    state.last_time = ts


def check_staleness(conn, states: Dict[Tuple[int, str], SeriesState],
                    intervals: Dict[int, int]) -> None:
    """
    Detector 4: a device that is still connected but has stopped sampling.

    Deliberately redundant with the broker Last Will path, and detects a
    different failure: the Last Will fires on a lost TCP connection, this
    fires when the connection is healthy but readings have stopped. Neither
    alone covers both (report Section 10.3).
    """
    now = datetime.now(timezone.utc)
    for (device_id, metric), st in states.items():
        if st.last_time is None:
            continue
        interval_s = intervals.get(device_id, 1000) / 1000.0
        limit = interval_s * STALE_MULTIPLIER
        age = (now - st.last_time).total_seconds()
        if age > limit and st.active_alarm_id is None:
            msg = (f"no reading for {metric} in {age:.1f}s, "
                   f"limit {limit:.1f}s")
            ev = raise_event(conn, device_id, "critical", "staleness",
                             metric, None, msg)
            st.active_alarm_id = raise_alarm(conn, ev, device_id, "critical")
            conn.commit()
            log("critical", "staleness alarm", device_id=device_id,
                metric=metric, age_s=round(age, 1))


def main() -> int:
    global running

    if not PG_PASS:
        print("POSTGRES_PASSWORD must be set", file=sys.stderr)
        return 1

    def shutdown(signum, frame):
        global running
        log("info", "shutdown signal received", signal=signum)
        running = False

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    conn = connect_db()
    states: Dict[Tuple[int, str], SeriesState] = {}

    # Track progress by timestamp: index-backed by (device_id, time DESC)
    cursor_time = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute("SELECT device_id, sample_interval_ms FROM device_config")
        intervals = {r["device_id"]: r["sample_interval_ms"] for r in cur.fetchall()}

    thresholds = load_thresholds(conn)
    log("info", "anomaly engine starting",
        z_window=DEFAULT_WINDOW, z_sigma=DEFAULT_SIGMA,
        confirm_samples=DEFAULT_CONFIRM, clear_samples=CLEAR_SAMPLES,
        thresholds_loaded=len(thresholds))

    last_reload = time.time()
    last_report = time.time()

    try:
        while running:
            # Reload thresholds periodically so operator changes take effect
            if time.time() - last_reload > 30:
                thresholds = load_thresholds(conn)
                with conn.cursor() as cur:
                    cur.execute("SELECT device_id, sample_interval_ms FROM device_config")
                    intervals = {r["device_id"]: r["sample_interval_ms"]
                                 for r in cur.fetchall()}
                last_reload = time.time()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT time, device_id, metric, value FROM telemetry "
                    "WHERE time > %s ORDER BY time ASC LIMIT 2000",
                    (cursor_time,))
                rows = cur.fetchall()
            conn.commit()

            for r in rows:
                key = (r["device_id"], r["metric"])
                if key not in states:
                    cfg = thresholds.get(key)
                    win = int(cfg["z_window"]) if cfg and cfg.get("z_window") else DEFAULT_WINDOW
                    states[key] = SeriesState(win)
                evaluate(conn, states[key], thresholds.get(key),
                         r["device_id"], r["metric"], float(r["value"]), r["time"])
                cursor_time = max(cursor_time, r["time"])

            check_staleness(conn, states, intervals)

            if time.time() - last_report > 30:
                log("info", "anomaly statistics", **_stats,
                    series_tracked=len(states))
                last_report = time.time()

            time.sleep(POLL_MS / 1000.0)
    finally:
        log("info", "stopped", **_stats)
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
