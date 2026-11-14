"""
Project Atlas - telemetry, events and alarms
AT-26-L1-002  |  FR-11, FR-13, FR-14, FR-15, FR-21, FR-24

Every route here is read-only except the alarm acknowledgement, which is the
one operator action report 8.7 places in this area.

Two decisions recur and are stated once:

* Every list endpoint is bounded by a `limit` with a hard ceiling. An
  unbounded query against a hypertable is a denial of service the caller does
  not have to intend - one dashboard asking for a year of 1 Hz data would pull
  31 million rows through the API process.

* Time ranges default to a recent window rather than to all of history, so the
  cheap query is the one you get by not thinking about it.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import audit, db
from ..deps import CurrentUser, client_ip, require_role
from ..logging_setup import log
from ..schemas import AggregatePoint, AlarmOut, EventOut, TelemetryPoint

router = APIRouter(tags=["data"])


def _window(start: datetime | None, end: datetime | None,
            default_hours: int) -> tuple[datetime, datetime]:
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(hours=default_hours))
    if start >= end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "start must be earlier than end")
    return start, end


@router.get("/api/telemetry", response_model=list[TelemetryPoint])
def query_telemetry(
    device_id: int | None = Query(default=None),
    metric: str | None = Query(default=None, max_length=64),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=50_000),
    _: CurrentUser = Depends(require_role("viewer")),
) -> list[TelemetryPoint]:
    """
    FR-13. Raw readings.

    The index on (device_id, time DESC) created in 01_schema.sql is what makes
    this cheap; the WHERE clause is ordered to use it, and TimescaleDB excludes
    chunks outside the time range before reading any of them.
    """
    start, end = _window(start, end, default_hours=1)
    clauses = ["time >= %s", "time < %s"]
    params: list = [start, end]
    if device_id is not None:
        clauses.append("device_id = %s")
        params.append(device_id)
    if metric:
        clauses.append("metric = %s")
        params.append(metric)
    params.append(limit)

    rows = db.fetch_all(
        "SELECT time, device_id, metric, value, quality FROM telemetry "
        f"WHERE {' AND '.join(clauses)} ORDER BY time DESC LIMIT %s",
        tuple(params))
    return [TelemetryPoint(**r) for r in rows]


@router.get("/api/telemetry/aggregate", response_model=list[AggregatePoint])
def query_aggregate(
    device_id: int | None = Query(default=None),
    metric: str | None = Query(default=None, max_length=64),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=20_000),
    _: CurrentUser = Depends(require_role("viewer")),
) -> list[AggregatePoint]:
    """
    FR-13 trend charts. Served from the continuous aggregate in 02_api.sql,
    which is why a 24-hour trend is 1,440 rows rather than 86,400.
    """
    start, end = _window(start, end, default_hours=24)
    clauses = ["bucket >= %s", "bucket < %s"]
    params: list = [start, end]
    if device_id is not None:
        clauses.append("device_id = %s")
        params.append(device_id)
    if metric:
        clauses.append("metric = %s")
        params.append(metric)
    params.append(limit)

    rows = db.fetch_all(
        "SELECT bucket, device_id, metric, avg_value, min_value, max_value, "
        "       sample_count FROM telemetry_1m "
        f"WHERE {' AND '.join(clauses)} ORDER BY bucket DESC LIMIT %s",
        tuple(params))
    return [AggregatePoint(**r) for r in rows]


@router.get("/api/events", response_model=list[EventOut])
def list_events(
    device_id: int | None = Query(default=None),
    severity: str | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(require_role("viewer")),
) -> list[EventOut]:
    """FR-15, FR-21."""
    start, end = _window(start, end, default_hours=24)
    clauses = ["occurred_at >= %s", "occurred_at < %s"]
    params: list = [start, end]
    if device_id is not None:
        clauses.append("device_id = %s")
        params.append(device_id)
    if severity:
        if severity not in ("info", "warning", "critical"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown severity")
        clauses.append("severity = %s")
        params.append(severity)
    params += [limit, offset]

    rows = db.fetch_all(
        "SELECT id, device_id, severity, detector, metric, observed_value, "
        "       message, occurred_at FROM events "
        f"WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC "
        "LIMIT %s OFFSET %s", tuple(params))
    return [EventOut(**r) for r in rows]


@router.get("/api/alarms", response_model=list[AlarmOut])
def list_alarms(
    state: str | None = Query(default="active"),
    device_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    _: CurrentUser = Depends(require_role("viewer")),
) -> list[AlarmOut]:
    """
    FR-14. Defaults to active only, because the question a dashboard asks is
    "what is wrong now", not "what has ever been wrong".
    """
    clauses = []
    params: list = []
    if state:
        if state not in ("active", "acknowledged", "cleared"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown state")
        clauses.append("state = %s")
        params.append(state)
    if device_id is not None:
        clauses.append("device_id = %s")
        params.append(device_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = db.fetch_all(
        "SELECT id, event_id, device_id, state, severity, raised_at, "
        f"       acknowledged_by, cleared_at FROM alarms {where} "
        "ORDER BY raised_at DESC LIMIT %s", tuple(params))
    return [AlarmOut(**r) for r in rows]


@router.post("/api/alarms/{alarm_id}/ack", response_model=AlarmOut)
def acknowledge_alarm(alarm_id: int, request: Request,
                      user: CurrentUser = Depends(require_role("operator"))
                      ) -> AlarmOut:
    """
    FR-14, FR-20.

    Acknowledging is not clearing. The alarm stays raised until the condition
    that raised it stops, which is the anomaly engine's decision, not the
    operator's - report 10 makes clearing a function of the data. What the
    operator asserts here is that a human has seen it. Conflating the two would
    let an operator silence an active fault by pressing a button.
    """
    ip = client_ip(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, event_id, device_id, state, severity, raised_at, "
                "       acknowledged_by, cleared_at FROM alarms "
                "WHERE id = %s FOR UPDATE", (alarm_id,))
            before = cur.fetchone()
            if before is None:
                conn.rollback()
                raise HTTPException(status.HTTP_404_NOT_FOUND, "alarm not found")
            if before["state"] != "active":
                conn.rollback()
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"alarm is {before['state']}, only active alarms can be "
                    "acknowledged")

            cur.execute(
                "UPDATE alarms SET state = 'acknowledged', acknowledged_by = %s "
                "WHERE id = %s RETURNING id, event_id, device_id, state, "
                "severity, raised_at, acknowledged_by, cleared_at",
                (user.username, alarm_id))
            after = cur.fetchone()

        audit.record(conn, user, action="alarm.acknowledge",
                     entity_type="alarm", entity_id=alarm_id,
                     before=dict(before), after=dict(after), ip=ip)
        conn.commit()

    log("info", "alarm acknowledged", alarm_id=alarm_id,
        actor=user.username, device_id=after["device_id"])
    return AlarmOut(**after)
