"""
Project Atlas - device management
AT-26-L1-002  |  FR-01, FR-02, FR-03, FR-04, FR-19, FR-24

Role floor per operation follows report 8.7 exactly:
    register / remove   administrator
    configure           operator
    read                viewer
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import audit, db, security
from ..broker import provision_device, deprovision_device, publish_device_config
from ..deps import CurrentUser, client_ip, require_role
from ..logging_setup import log
from ..schemas import (DeviceConfigUpdate, DeviceCreate, DeviceDetail, DeviceOut,
                       DeviceRegistered, ThresholdOut, ThresholdUpdate)

router = APIRouter(prefix="/api/devices", tags=["devices"])

_DEVICE_COLS = ("d.id, d.device_uid, d.name, d.site, d.location, d.status, "
                "d.last_seen_at, d.created_at, d.broker_provisioned")


@router.get("", response_model=list[DeviceOut])
def list_devices(
    site: str | None = Query(default=None, max_length=64),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(require_role("viewer")),
) -> list[DeviceOut]:
    """
    FR-01, FR-03. Deleted devices are excluded: `deleted_at` is a soft delete,
    because telemetry rows carry a foreign key to the device and a hard delete
    would either fail or take history with it.
    """
    clauses = ["d.deleted_at IS NULL"]
    params: list = []
    if site:
        clauses.append("d.site = %s")
        params.append(site)
    if status_filter:
        clauses.append("d.status = %s")
        params.append(status_filter)
    params += [limit, offset]

    rows = db.fetch_all(
        f"SELECT {_DEVICE_COLS} FROM devices d WHERE {' AND '.join(clauses)} "
        "ORDER BY d.device_uid LIMIT %s OFFSET %s", tuple(params))
    return [DeviceOut(**r) for r in rows]


@router.get("/{device_id}", response_model=DeviceDetail)
def get_device(device_id: int,
               _: CurrentUser = Depends(require_role("viewer"))) -> DeviceDetail:
    row = db.fetch_one(
        f"SELECT {_DEVICE_COLS}, d.mqtt_username, "
        "       c.sample_interval_ms, c.enabled_metrics "
        "FROM devices d "
        "LEFT JOIN LATERAL (SELECT sample_interval_ms, enabled_metrics "
        "                   FROM device_config WHERE device_id = d.id "
        "                   ORDER BY updated_at DESC LIMIT 1) c ON TRUE "
        "WHERE d.id = %s AND d.deleted_at IS NULL", (device_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    row["enabled_metrics"] = row.get("enabled_metrics") or []
    return DeviceDetail(**row)


@router.post("", response_model=DeviceRegistered,
             status_code=status.HTTP_201_CREATED)
def register_device(body: DeviceCreate, request: Request,
                    user: CurrentUser = Depends(require_role("administrator"))
                    ) -> DeviceRegistered:
    """
    FR-01, FR-19.

    The secret is generated here, handed to the broker, returned once, and
    never persisted by the platform. Report 8.7 puts device credential
    material in the broker's own store precisely so that a stolen application
    database yields no device credentials - the "credential theft from
    database" row of the threat table depends on this being true in code.
    """
    ip = client_ip(request)
    secret = security.generate_device_secret()

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, deleted_at FROM devices WHERE device_uid = %s",
                        (body.device_uid,))
            if cur.fetchone() is not None:
                conn.rollback()
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "device_uid already exists")

            cur.execute(
                "INSERT INTO devices (device_uid, name, site, location, mqtt_username) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_DEVICE_COLS.replace('d.', '')}",
                (body.device_uid, body.name, body.site, body.location,
                 body.device_uid))
            device = cur.fetchone()

            cur.execute(
                "INSERT INTO device_config "
                "(device_id, sample_interval_ms, enabled_metrics, updated_by) "
                "VALUES (%s, %s, %s, %s)",
                (device["id"], body.sample_interval_ms,
                 body.enabled_metrics, user.username))

        provisioned, note = provision_device(body.device_uid, secret)
        if provisioned:
            with conn.cursor() as cur:
                cur.execute("UPDATE devices SET broker_provisioned = TRUE "
                            "WHERE id = %s", (device["id"],))
            device["broker_provisioned"] = True

        audit.record(conn, user, action="device.register",
                     entity_type="device", entity_id=device["id"],
                     before=None,
                     after={"device_uid": body.device_uid, "name": body.name,
                            "site": body.site,
                            "sample_interval_ms": body.sample_interval_ms,
                            "enabled_metrics": body.enabled_metrics,
                            "mqtt_password": secret},   # redacted by audit.scrub
                     ip=ip)
        conn.commit()

    if provisioned:
        try:
            publish_device_config(
                device_uid=body.device_uid,
                site=body.site,
                sample_interval_ms=body.sample_interval_ms,
                enabled_metrics=body.enabled_metrics,
            )
        except Exception as exc:
            # Registration remains committed and the device can still be
            # configured again through PATCH after the broker recovers.
            log("warning", "device registered but initial config downlink failed",
                device_uid=body.device_uid,
                error=f"{type(exc).__name__}: {exc}"[:300])

    log("info", "device registered", device_uid=body.device_uid,
        actor=user.username, broker_provisioned=provisioned)

    return DeviceRegistered(
        device=DeviceOut(**device),
        mqtt_username=body.device_uid,
        mqtt_password=secret,
        broker_provisioned=provisioned,
        provisioning_note=note)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_device(device_id: int, request: Request,
                  user: CurrentUser = Depends(require_role("administrator"))) -> None:
    """FR-02, FR-19. Soft delete; the broker client is removed for real."""
    ip = client_ip(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, device_uid, name, site, status FROM devices "
                "WHERE id = %s AND deleted_at IS NULL FOR UPDATE", (device_id,))
            before = cur.fetchone()
            if before is None:
                conn.rollback()
                raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")

        # Revoke broker access before committing the soft delete. If the broker
        # cannot revoke the client, leave the device visible and retryable.
        removed, note = deprovision_device(before["device_uid"])
        if not removed:
            conn.rollback()
            log("error", "device removal blocked because broker revocation failed",
                device_uid=before["device_uid"], actor=user.username)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "device found but broker credential revocation failed",
            )

        with conn.cursor() as cur:
            cur.execute("UPDATE devices SET deleted_at = now(), status = 'offline' "
                        "WHERE id = %s", (device_id,))

        audit.record(conn, user, action="device.remove", entity_type="device",
                     entity_id=device_id, before=dict(before),
                     after={"deleted_at": "now()", "broker_client_removed": True,
                            "note": note},
                     ip=ip)
        conn.commit()

    log("info", "device removed", device_uid=before["device_uid"],
        actor=user.username, broker_client_removed=True)


@router.patch("/{device_id}/config", response_model=DeviceDetail)
def update_config(device_id: int, body: DeviceConfigUpdate, request: Request,
                  user: CurrentUser = Depends(require_role("operator"))
                  ) -> DeviceDetail:
    """
    FR-04, FR-19.

    device_config is append-only: each change writes a new row rather than
    updating one. The audit trail records what changed and who changed it; the
    config history records what the device was actually told at each point in
    time, which is what a reading from last Tuesday has to be interpreted
    against. Those are two different questions and both get answered.
    """
    if body.sample_interval_ms is None and body.enabled_metrics is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to update")

    ip = client_ip(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, device_uid, site FROM devices "
                        "WHERE id = %s AND deleted_at IS NULL", (device_id,))
            device_row = cur.fetchone()
            if device_row is None:
                conn.rollback()
                raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")

            cur.execute(
                "SELECT sample_interval_ms, enabled_metrics FROM device_config "
                "WHERE device_id = %s ORDER BY updated_at DESC LIMIT 1",
                (device_id,))
            current = cur.fetchone() or {"sample_interval_ms": 1000,
                                         "enabled_metrics": []}

            after = {
                "sample_interval_ms": (body.sample_interval_ms
                                       if body.sample_interval_ms is not None
                                       else current["sample_interval_ms"]),
                "enabled_metrics": (body.enabled_metrics
                                    if body.enabled_metrics is not None
                                    else current["enabled_metrics"]),
            }

            cur.execute(
                "INSERT INTO device_config "
                "(device_id, sample_interval_ms, enabled_metrics, updated_by) "
                "VALUES (%s, %s, %s, %s)",
                (device_id, after["sample_interval_ms"],
                 after["enabled_metrics"], user.username))

        audit.record(conn, user, action="device.config.update",
                     entity_type="device", entity_id=device_id,
                     before=dict(current), after=after, ip=ip)
        conn.commit()

    try:
        publish_device_config(
            device_uid=device_row["device_uid"],
            site=device_row["site"],
            sample_interval_ms=after["sample_interval_ms"],
            enabled_metrics=after["enabled_metrics"] or [],
        )
    except Exception as exc:
        # The append-only history is already committed. Returning a 503 makes
        # the delivery failure visible to the operator and allows a retry,
        # rather than claiming the device received a configuration it did not.
        log("error", "device configuration saved but downlink failed",
            device_id=device_id, error=f"{type(exc).__name__}: {exc}"[:300])
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "configuration saved but could not be delivered to the device",
        ) from exc

    log("info", "device configuration changed", device_id=device_id,
        actor=user.username, changed=audit.diff(dict(current), after))
    return get_device(device_id)          # type: ignore[arg-type]


@router.get("/{device_id}/thresholds", response_model=list[ThresholdOut])
def list_thresholds(device_id: int,
                    _: CurrentUser = Depends(require_role("viewer"))
                    ) -> list[ThresholdOut]:
    rows = db.fetch_all(
        "SELECT device_id, metric, min_value, max_value, max_rate_of_change, "
        "       z_window, z_sigma, confirm_samples "
        "FROM thresholds WHERE device_id = %s ORDER BY metric", (device_id,))
    return [ThresholdOut(**r) for r in rows]


@router.put("/{device_id}/thresholds/{metric}", response_model=ThresholdOut)
def set_threshold(device_id: int, metric: str, body: ThresholdUpdate,
                  request: Request,
                  user: CurrentUser = Depends(require_role("operator"))
                  ) -> ThresholdOut:
    """
    FR-04, FR-19. This is the endpoint the FR-19 verification method names
    directly: change a threshold, confirm previous and new values recorded.
    """
    ip = client_ip(request)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no fields to update")

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM devices WHERE id = %s AND deleted_at IS NULL",
                        (device_id,))
            if cur.fetchone() is None:
                conn.rollback()
                raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")

            cur.execute(
                "SELECT device_id, metric, min_value, max_value, max_rate_of_change, "
                "       z_window, z_sigma, confirm_samples FROM thresholds "
                "WHERE device_id = %s AND metric = %s FOR UPDATE",
                (device_id, metric))
            before = cur.fetchone()

            # Column names come from the model's own field set, never from user
            # input, so the assignment list cannot be influenced by the request.
            assignable = [c for c in ("min_value", "max_value", "max_rate_of_change",
                                      "z_window", "z_sigma", "confirm_samples")
                          if c in fields]
            sets = ", ".join(f"{c} = %s" for c in assignable)
            values = [fields[c] for c in assignable]

            if before is None:
                cols = ", ".join(assignable)
                marks = ", ".join(["%s"] * len(assignable))
                cur.execute(
                    f"INSERT INTO thresholds (device_id, metric, {cols}) "
                    f"VALUES (%s, %s, {marks}) RETURNING device_id, metric, "
                    "min_value, max_value, max_rate_of_change, z_window, "
                    "z_sigma, confirm_samples",
                    (device_id, metric, *values))
            else:
                cur.execute(
                    f"UPDATE thresholds SET {sets} "
                    "WHERE device_id = %s AND metric = %s "
                    "RETURNING device_id, metric, min_value, max_value, "
                    "max_rate_of_change, z_window, z_sigma, confirm_samples",
                    (*values, device_id, metric))
            after = cur.fetchone()

        audit.record(conn, user, action="threshold.update",
                     entity_type="threshold", entity_id=device_id,
                     before=dict(before) if before else None,
                     after=dict(after), ip=ip)
        conn.commit()

    log("info", "threshold changed", device_id=device_id, metric=metric,
        actor=user.username,
        changed=audit.diff(dict(before) if before else None, dict(after)))
    return ThresholdOut(**after)
