"""
Project Atlas - authentication endpoints
AT-26-L1-002  |  FR-20, FR-22, FR-23, FR-26
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import audit, db, emailer, security
from ..config import OTP_MAX_ATTEMPTS, OTP_TTL_MIN
from ..deps import CurrentUser, client_ip, get_current_user
from ..logging_setup import log
from ..schemas import (
    EmailCodeRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageOut,
    PublicRegistration,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPair,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _new_challenge(conn, user_id: int, purpose: str, ip: str) -> str:
    code = security.generate_otp()
    expires = datetime.now(timezone.utc) + timedelta(minutes=OTP_TTL_MIN)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_challenges SET consumed_at = now() "
            "WHERE user_id = %s AND purpose = %s AND consumed_at IS NULL",
            (user_id, purpose),
        )
        cur.execute(
            "INSERT INTO auth_challenges "
            "(user_id, purpose, code_hash, expires_at, requested_ip) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, purpose, security.token_digest(code), expires, ip),
        )
    return code


def _generic_auth_message() -> MessageOut:
    return MessageOut(message="If the account is eligible, a verification email has been sent.")


@router.post("/register", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def register(body: PublicRegistration, request: Request) -> MessageOut:
    """Create a viewer account; activation requires email verification."""
    ip = client_ip(request)
    existing = db.fetch_one(
        "SELECT id FROM users WHERE lower(username) = lower(%s) "
        "OR lower(email) = lower(%s)",
        (body.username, body.email),
    )
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "username or email already in use",
        )

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM roles WHERE name = 'viewer'")
            role = cur.fetchone()
            cur.execute(
                "INSERT INTO users (username, password_hash, email, role_id, is_active) "
                "VALUES (%s, %s, %s, %s, FALSE) RETURNING id",
                (body.username, security.hash_password(body.password), body.email, role["id"]),
            )
            user_id = cur.fetchone()["id"]
            code = _new_challenge(conn, user_id, "email_verify", ip)
            audit.record(conn, None, action="auth.registration_requested",
                         entity_type="user", entity_id=user_id,
                         after={"username": body.username, "email": body.email, "role": "viewer"}, ip=ip)
        conn.commit()

    try:
        emailer.send_otp(recipient=body.email, code=code, purpose="email_verify")
    except emailer.EmailDeliveryError:
        log("error", "registration email delivery failed", username=body.username)
    return _generic_auth_message()


@router.post("/verify-email", response_model=MessageOut)
def verify_email(body: EmailCodeRequest, request: Request) -> MessageOut:
    ip = client_ip(request)
    key = f"verify|{body.email}|{ip}"
    if security.rate_limited(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many verification attempts")

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.user_id, c.code_hash, c.expires_at, c.attempts, "
                "u.username, u.is_active, r.name AS role "
                "FROM auth_challenges c JOIN users u ON u.id = c.user_id "
                "JOIN roles r ON r.id = u.role_id "
                "WHERE lower(u.email) = lower(%s) AND c.purpose = 'email_verify' "
                "AND c.consumed_at IS NULL ORDER BY c.created_at DESC LIMIT 1 "
                "FOR UPDATE OF c",
                (body.email,),
            )
            row = cur.fetchone()
            if row is None or row["expires_at"] < datetime.now(timezone.utc):
                conn.rollback()
                security.record_attempt(key)
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code")
            if row["attempts"] >= OTP_MAX_ATTEMPTS:
                conn.rollback()
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many verification attempts")
            if row["code_hash"] != security.token_digest(body.code):
                cur.execute("UPDATE auth_challenges SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
                conn.commit()
                security.record_attempt(key)
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code")

            cur.execute("UPDATE auth_challenges SET consumed_at = now() WHERE id = %s", (row["id"],))
            cur.execute("UPDATE users SET email_verified_at = now(), is_active = TRUE WHERE id = %s", (row["user_id"],))
            audit.record(conn, None, action="auth.email_verified", entity_type="user",
                         entity_id=row["user_id"], after={"email_verified": True}, ip=ip)
        conn.commit()
    security.clear_attempts(key)
    return MessageOut(message="Email verified. You can sign in now.")


@router.post("/resend-verification", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def resend_verification(body: ForgotPasswordRequest, request: Request) -> MessageOut:
    ip = client_ip(request)
    key = f"resend|{body.email}|{ip}"
    if security.rate_limited(key):
        return _generic_auth_message()
    security.record_attempt(key)
    row = db.fetch_one(
        "SELECT id, email, is_active FROM users WHERE lower(email) = lower(%s)",
        (body.email,),
    )
    if not row or row["is_active"]:
        return _generic_auth_message()
    with db.connection() as conn:
        code = _new_challenge(conn, row["id"], "email_verify", ip)
        conn.commit()
    try:
        emailer.send_otp(recipient=row["email"], code=code, purpose="email_verify")
    except emailer.EmailDeliveryError:
        log("error", "verification email delivery failed", user_id=row["id"])
    return _generic_auth_message()


@router.post("/forgot-password", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def forgot_password(body: ForgotPasswordRequest, request: Request) -> MessageOut:
    ip = client_ip(request)
    key = f"forgot|{body.email}|{ip}"
    if security.rate_limited(key):
        return _generic_auth_message()
    security.record_attempt(key)
    row = db.fetch_one(
        "SELECT id, email, is_active FROM users WHERE lower(email) = lower(%s)",
        (body.email,),
    )
    if not row or not row["is_active"]:
        security.waste_time_like_a_real_verification()
        return _generic_auth_message()
    with db.connection() as conn:
        code = _new_challenge(conn, row["id"], "password_reset", ip)
        conn.commit()
    try:
        emailer.send_otp(recipient=row["email"], code=code, purpose="password_reset")
    except emailer.EmailDeliveryError:
        log("error", "password reset email delivery failed", user_id=row["id"])
    return _generic_auth_message()


@router.post("/reset-password", response_model=MessageOut)
def reset_password(body: ResetPasswordRequest, request: Request) -> MessageOut:
    ip = client_ip(request)
    key = f"reset|{body.email}|{ip}"
    if security.rate_limited(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many reset attempts")
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.user_id, c.code_hash, c.expires_at, c.attempts, "
                "u.username, r.name AS role FROM auth_challenges c "
                "JOIN users u ON u.id = c.user_id JOIN roles r ON r.id = u.role_id "
                "WHERE lower(u.email) = lower(%s) AND c.purpose = 'password_reset' "
                "AND c.consumed_at IS NULL ORDER BY c.created_at DESC LIMIT 1 FOR UPDATE OF c",
                (body.email,),
            )
            row = cur.fetchone()
            if row is None or row["expires_at"] < datetime.now(timezone.utc) or row["attempts"] >= OTP_MAX_ATTEMPTS:
                conn.rollback()
                security.record_attempt(key)
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code")
            if row["code_hash"] != security.token_digest(body.code):
                cur.execute("UPDATE auth_challenges SET attempts = attempts + 1 WHERE id = %s", (row["id"],))
                conn.commit()
                security.record_attempt(key)
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code")

            cur.execute("UPDATE auth_challenges SET consumed_at = now() WHERE id = %s", (row["id"],))
            cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                        (security.hash_password(body.new_password), row["user_id"]))
            cur.execute("UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (row["user_id"],))
            audit.record(conn, None, action="auth.password_reset", entity_type="user",
                         entity_id=row["user_id"], after={"password_reset": True}, ip=ip)
        conn.commit()
    security.clear_attempts(key)
    return MessageOut(message="Password reset. You can sign in now.")


@router.post("/login", response_model=TokenPair)
def login(body: LoginRequest, request: Request) -> TokenPair:
    ip = client_ip(request)
    # Keyed on username AND address. Keying on address alone lets one office
    # NAT lock out everyone behind it; on username alone it lets an attacker
    # lock a known account out deliberately. Both together limits guessing
    # without giving either denial-of-service for free.
    key = f"{body.username.lower()}|{ip}"

    if security.rate_limited(key):
        log("warning", "login rate limit exceeded",
            username=body.username, ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, try again shortly")

    row = db.fetch_one(
        "SELECT u.id, u.username, u.password_hash, u.is_active, r.name AS role "
        "FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE lower(u.username) = lower(%s)",
        (body.username,))

    if row is None:
        # Equalise timing so a missing account is indistinguishable from a
        # wrong password. See security.py.
        security.waste_time_like_a_real_verification()
        security.record_attempt(key)
        log("warning", "login failed", username=body.username, ip=ip,
            reason="no such account")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid username or password")

    ok, rehashed = security.verify_password(row["password_hash"], body.password)
    if not ok:
        security.record_attempt(key)
        log("warning", "login failed", username=row["username"], ip=ip,
            reason="bad password")
        # Identical message and status for both failure modes. A distinct
        # "no such user" reply is a free account-enumeration oracle.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid username or password")

    if not row["is_active"]:
        log("warning", "login refused for inactive account",
            username=row["username"], ip=ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account is disabled")

    security.clear_attempts(key)

    access, ttl = security.issue_access_token(row["id"], row["username"], row["role"])
    refresh, jti, expires = security.issue_refresh_token(row["id"])

    with db.connection() as conn:
        with conn.cursor() as cur:
            if rehashed:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                            (rehashed, row["id"]))
                log("info", "password hash upgraded to current parameters",
                    username=row["username"])
            cur.execute(
                "INSERT INTO refresh_tokens "
                "(user_id, jti, token_hash, expires_at, user_agent, ip_address) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (row["id"], jti, security.token_digest(refresh), expires,
                 request.headers.get("user-agent", "")[:256], ip))
        audit.record(conn,
                     CurrentUser(row["id"], row["username"], row["role"]),
                     action="auth.login", entity_type="user",
                     entity_id=row["id"], ip=ip)
        conn.commit()

    log("info", "login succeeded", username=row["username"],
        role=row["role"], ip=ip)
    return TokenPair(access_token=access, refresh_token=refresh, expires_in=ttl)


@router.post("/refresh", response_model=TokenPair)
def refresh_tokens(body: RefreshRequest, request: Request) -> TokenPair:
    """
    Refresh tokens are single-use and rotated.

    The reason is detection, not tidiness. If a refresh token is stolen, both
    the attacker and the legitimate client eventually present the same token.
    With rotation the second presentation is of an already-used token, which
    cannot happen in normal operation - so it is treated as evidence of theft
    and the whole token family for that user is revoked, forcing a re-login.
    """
    claims = security.decode_token(body.refresh_token, expected_type="refresh")
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "invalid or expired refresh token")

    ip = client_ip(request)
    digest = security.token_digest(body.refresh_token)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.user_id, t.revoked_at, t.expires_at, "
                "       u.username, u.is_active, r.name AS role "
                "FROM refresh_tokens t "
                "JOIN users u ON u.id = t.user_id "
                "JOIN roles r ON r.id = u.role_id "
                "WHERE t.jti = %s AND t.token_hash = %s "
                "FOR UPDATE OF t",
                (claims["jti"], digest))
            row = cur.fetchone()

            if row is None:
                conn.rollback()
                log("error", "refresh token not recognised",
                    jti=claims.get("jti"), ip=ip)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                    "invalid refresh token")

            if row["revoked_at"] is not None:
                # Reuse of a revoked token. Revoke everything this user holds.
                cur.execute(
                    "UPDATE refresh_tokens SET revoked_at = now() "
                    "WHERE user_id = %s AND revoked_at IS NULL",
                    (row["user_id"],))
                conn.commit()
                log("critical", "revoked refresh token reused, all sessions revoked",
                    username=row["username"], jti=claims["jti"], ip=ip)
                raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                    "session revoked, please sign in again")

            if row["expires_at"] < datetime.now(timezone.utc):
                conn.rollback()
                raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                    "refresh token expired")

            if not row["is_active"]:
                conn.rollback()
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "account is disabled")

            access, ttl = security.issue_access_token(
                row["user_id"], row["username"], row["role"])
            new_token, new_jti, expires = security.issue_refresh_token(row["user_id"])

            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = now(), replaced_by = %s "
                "WHERE id = %s", (new_jti, row["id"]))
            cur.execute(
                "INSERT INTO refresh_tokens "
                "(user_id, jti, token_hash, expires_at, user_agent, ip_address) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (row["user_id"], new_jti, security.token_digest(new_token),
                 expires, request.headers.get("user-agent", "")[:256], ip))
        conn.commit()

    log("info", "token refreshed", username=row["username"], ip=ip)
    return TokenPair(access_token=access, refresh_token=new_token, expires_in=ttl)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshRequest, request: Request,
           user: CurrentUser = Depends(get_current_user)) -> None:
    """
    Revokes the presented refresh token.

    The access token stays valid until it expires. This is stated rather than
    papered over: a stateless token cannot be recalled, and pretending
    otherwise would mean a revocation lookup on every request. The bound is
    the fifteen-minute lifetime, which is why the lifetime is short.
    """
    ip = client_ip(request)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refresh_tokens SET revoked_at = now() "
                "WHERE token_hash = %s AND user_id = %s AND revoked_at IS NULL",
                (security.token_digest(body.refresh_token), user.id))
            affected = cur.rowcount
        audit.record(conn, user, action="auth.logout",
                     entity_type="user", entity_id=user.id, ip=ip)
        conn.commit()

    log("info", "logout", username=user.username, revoked=affected, ip=ip)


@router.get("/me", response_model=UserOut)
def whoami(user: CurrentUser = Depends(get_current_user)) -> UserOut:
    row = db.fetch_one(
        "SELECT u.id, u.username, u.email, u.is_active, u.created_at, "
        "       r.name AS role FROM users u JOIN roles r ON r.id = u.role_id "
        "WHERE u.id = %s", (user.id,))
    return UserOut(**row)
