"""
Project Atlas - user management and audit trail
AT-26-L1-002  |  FR-20, FR-23, FR-24

Administrator only, both of them. The audit trail in particular: a trail
readable by the people it records is a weaker control, and report 8.7 places
"read the audit log" in the administrator row alone.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from .. import audit, db, security
from ..deps import CurrentUser, client_ip, require_role
from ..logging_setup import log
from ..schemas import AuditOut, UserCreate, UserOut

router = APIRouter(tags=["administration"])


@router.get("/api/users", response_model=list[UserOut])
def list_users(_: CurrentUser = Depends(require_role("administrator"))
               ) -> list[UserOut]:
    rows = db.fetch_all(
        "SELECT u.id, u.username, u.email, u.is_active, u.created_at, "
        "       r.name AS role FROM users u JOIN roles r ON r.id = u.role_id "
        "ORDER BY u.username")
    return [UserOut(**r) for r in rows]


@router.post("/api/users", response_model=UserOut,
             status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, request: Request,
                user: CurrentUser = Depends(require_role("administrator"))
                ) -> UserOut:
    """
    FR-23. The plaintext password exists only as a local variable for the
    duration of the hash call. It is not logged, not audited, and not stored:
    `audit.scrub` removes the key even if a future edit passes it in.
    """
    ip = client_ip(request)
    password_hash = security.hash_password(body.password)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM roles WHERE name = %s", (body.role,))
            role = cur.fetchone()
            if role is None:
                conn.rollback()
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown role")

            cur.execute("SELECT id FROM users WHERE lower(username) = lower(%s)",
                        (body.username,))
            if cur.fetchone() is not None:
                conn.rollback()
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "username already exists")

            cur.execute(
                "INSERT INTO users (username, password_hash, email, role_id) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING id, username, email, is_active, created_at",
                (body.username, password_hash, body.email, role["id"]))
            created = cur.fetchone()

        audit.record(conn, user, action="user.create", entity_type="user",
                     entity_id=created["id"],
                     after={"username": body.username, "role": body.role,
                            "email": body.email, "password": body.password},
                     ip=ip)
        conn.commit()

    log("info", "user created", username=body.username, role=body.role,
        actor=user.username)
    return UserOut(**created, role=body.role)


@router.post("/api/users/{user_id}/deactivate", response_model=UserOut)
def deactivate_user(user_id: int, request: Request,
                    user: CurrentUser = Depends(require_role("administrator"))
                    ) -> UserOut:
    """
    Deactivation rather than deletion: audit_log holds a foreign key to the
    user, and deleting the account would either fail or orphan the record of
    what that account did. FR-20 requires the second not to happen.

    Refresh tokens are revoked in the same transaction. Without that, a
    deactivated user keeps a working session for up to seven days; the
    per-request active check in deps.py already covers the access token.
    """
    if user_id == user.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "an administrator cannot deactivate their own account")

    ip = client_ip(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.username, u.email, u.is_active, u.created_at, "
                "       r.name AS role FROM users u JOIN roles r ON r.id = u.role_id "
                "WHERE u.id = %s FOR UPDATE OF u", (user_id,))
            before = cur.fetchone()
            if before is None:
                conn.rollback()
                raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")

            cur.execute("UPDATE users SET is_active = FALSE WHERE id = %s",
                        (user_id,))
            cur.execute("UPDATE refresh_tokens SET revoked_at = now() "
                        "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
            revoked = cur.rowcount

        after = dict(before) | {"is_active": False}
        audit.record(conn, user, action="user.deactivate", entity_type="user",
                     entity_id=user_id, before=dict(before), after=after, ip=ip)
        conn.commit()

    log("warning", "user deactivated", username=before["username"],
        actor=user.username, sessions_revoked=revoked)
    return UserOut(**after)


@router.get("/api/audit", response_model=list[AuditOut])
def read_audit(
    action: str | None = Query(default=None, max_length=64),
    entity_type: str | None = Query(default=None, max_length=64),
    user_id: int | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    _: CurrentUser = Depends(require_role("administrator")),
) -> list[AuditOut]:
    """FR-19, FR-20. Read-only: there is no endpoint that writes or edits this."""
    end = end or datetime.now(timezone.utc)
    start = start or (end - timedelta(days=7))
    clauses = ["a.occurred_at >= %s", "a.occurred_at < %s"]
    params: list = [start, end]
    if action:
        clauses.append("a.action = %s")
        params.append(action)
    if entity_type:
        clauses.append("a.entity_type = %s")
        params.append(entity_type)
    if user_id is not None:
        clauses.append("a.user_id = %s")
        params.append(user_id)
    params += [limit, offset]

    rows = db.fetch_all(
        "SELECT a.id, a.user_id, u.username, a.action, a.entity_type, "
        "       a.entity_id, a.before, a.after, a.ip_address, a.occurred_at "
        "FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY a.occurred_at DESC "
        "LIMIT %s OFFSET %s", tuple(params))
    return [AuditOut(**r) for r in rows]
