"""
Project Atlas - audit trail
AT-26-L1-002  |  FR-19, FR-20

A note on why this is not middleware.

FR-19 asks for configuration changes to be recorded with their previous and
new values. Middleware sits outside the handler: it sees the request body and
the response, and it never sees the row as it was before the write. It can
report that a PATCH occurred; it cannot report what was replaced. Reconstructing
the previous value from a later query is worse than useless, because by then it
has already changed.

So the before/after capture is explicit, inside the handler, inside the same
transaction as the change it describes. The middleware in main.py does the part
middleware can actually do - attributing every authenticated request to a user
for FR-20 - and nothing more.

The transaction point is load-bearing. An audit row committed separately from
its change can be lost while the change succeeds, which produces a silent
modification: precisely the thing the requirement exists to prevent.
"""

import json
from typing import Any

import psycopg

from .deps import CurrentUser
from .logging_setup import log

# Never written to the trail, whatever the caller passes.
_REDACT = {"password", "password_hash", "secret", "mqtt_password",
           "token", "refresh_token", "access_token", "otp", "otp_code",
           "verification_code", "reset_code", "code"}


def scrub(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if data is None:
        return None
    return {k: ("[redacted]" if k.lower() in _REDACT else v)
            for k, v in data.items()}


def record(conn: psycopg.Connection,
           user: CurrentUser | None,
           action: str,
           entity_type: str | None = None,
           entity_id: int | None = None,
           before: dict | None = None,
           after: dict | None = None,
           ip: str | None = None) -> None:
    """
    Append to the audit trail on the CALLER'S connection.

    The caller must not have committed yet. This does not commit either: the
    handler commits once, and the change and its record land together or
    neither does.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log "
            "(user_id, action, entity_type, entity_id, before, after, ip_address) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (user.id if user else None,
             action,
             entity_type,
             entity_id,
             json.dumps(scrub(before), default=str) if before is not None else None,
             json.dumps(scrub(after), default=str) if after is not None else None,
             ip))

    log("info", "audit recorded",
        actor=user.username if user else "anonymous",
        action=action, entity_type=entity_type, entity_id=entity_id)


def diff(before: dict | None, after: dict | None) -> dict[str, Any]:
    """
    Changed fields only.

    Storing the whole row twice makes the trail large and the actual change
    hard to see. Storing only what moved makes 'who raised the current
    threshold' answerable by reading one row.
    """
    if before is None or after is None:
        return {}
    return {k: {"from": before.get(k), "to": after.get(k)}
            for k in after
            if k in before and before.get(k) != after.get(k)}
