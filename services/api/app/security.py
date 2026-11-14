"""
Project Atlas - authentication primitives
AT-26-L1-002  |  FR-22, FR-23, FR-25, FR-26

Three separate concerns live here and are worth keeping distinct:

1.  Password verification (Argon2id) - slow on purpose.
2.  Access tokens (signed, short-lived, NOT revocable) - fast on purpose.
3.  Refresh tokens (signed, long-lived, revocable, single-use) - the only
    place where server-side session state exists.

The asymmetry between 2 and 3 is the whole design. Checking revocation on
every request would put a database round trip in front of every endpoint and
make the token pointless. Putting revocation on the refresh token instead
means the check happens once per fifteen minutes per session.
"""

import hashlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from . import config
from .logging_setup import log

_hasher = PasswordHasher(
    time_cost=config.ARGON2_TIME_COST,
    memory_cost=config.ARGON2_MEMORY_KIB,
    parallelism=config.ARGON2_PARALLELISM,
    hash_len=32,
    salt_len=16,
    type=Type.ID,          # Argon2id explicitly, not Argon2i or Argon2d
)


# ------------------------------------------------------------------ passwords
def hash_password(plain: str) -> str:
    """
    Returns the PHC-format string: algorithm, parameters, salt and digest in
    one field. The salt is generated per call by argon2-cffi and travels with
    the hash, so FR-23's "per-user salt" is structural rather than a column
    somebody has to remember to populate.
    """
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> tuple[bool, str | None]:
    """
    Returns (ok, rehashed). The second element is a new hash when the stored
    one was made with weaker parameters than the current configuration, so
    raising the cost factor later upgrades existing users on their next login
    instead of requiring a password reset.
    """
    try:
        _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, InvalidHashError):
        return False, None
    except Exception as e:
        log("error", "password verification error", error=str(e)[:120])
        return False, None

    if _hasher.check_needs_rehash(stored_hash):
        return True, _hasher.hash(plain)
    return True, None


# Verifying against a fixed hash when the user does not exist. Without this,
# a missing username returns in microseconds and a real one takes ~50 ms, and
# the difference enumerates the user list. See report 8.8, credential threats.
_DUMMY_HASH = _hasher.hash("timing-equalisation-placeholder")


def waste_time_like_a_real_verification() -> None:
    try:
        _hasher.verify(_DUMMY_HASH, "wrong")
    except Exception:
        pass


# --------------------------------------------------------------------- tokens
def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue_access_token(user_id: int, username: str, role: str) -> tuple[str, int]:
    """
    The role is a claim in the token. Every authorisation decision reads it
    from here, which is what makes report 8.7 true when it says permissions
    are enforced at the API layer and not in the interface.
    """
    expires = _now() + timedelta(minutes=config.ACCESS_TTL_MIN)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    return token, config.ACCESS_TTL_MIN * 60


def issue_refresh_token(user_id: int) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at). The caller persists the digest."""
    jti = str(uuid.uuid4())
    expires = _now() + timedelta(days=config.REFRESH_TTL_DAYS)
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": "refresh",
        # Entropy beyond the jti, so the token cannot be reconstructed by
        # anyone who learns the jti from a log or an error message.
        "nonce": secrets.token_urlsafe(32),
        "iat": int(_now().timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)
    return token, jti, expires


def decode_token(token: str, expected_type: str) -> dict | None:
    """
    Signature, expiry AND type are all checked. The type check is not
    decoration: without it a refresh token is a valid bearer token for every
    endpoint, and its seven-day lifetime becomes the access-token lifetime.
    """
    try:
        claims = jwt.decode(token, config.JWT_SECRET,
                            algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if claims.get("type") != expected_type:
        return None
    return claims


def token_digest(token: str) -> str:
    """SHA-256 hex. See the note in 02_api.sql on why not Argon2id."""
    return hashlib.sha256(token.encode()).hexdigest()


# -------------------------------------------------------------- rate limiting
# Transient, per-process, and bounded. It is not durable state and makes no
# claim to be: report 2.3 says durable state lives in the database, and a
# counter that resets on restart is exactly the kind of state that may not.
#
# The limitation is worth stating rather than hiding: with more than one API
# replica the limit becomes per-replica. At one replica in a laboratory
# deployment that is not a gap; at N replicas it would need Redis or an
# equivalent shared counter, and that is recorded as an M3 item.
_attempts: dict[str, list[float]] = {}


def rate_limited(key: str) -> bool:
    now = time.time()
    window = config.LOGIN_WINDOW_SEC
    hits = [t for t in _attempts.get(key, []) if now - t < window]
    _attempts[key] = hits
    if len(_attempts) > 10_000:            # bound the dictionary itself
        _attempts.clear()
    return len(hits) >= config.LOGIN_MAX_ATTEMPTS


def record_attempt(key: str) -> None:
    _attempts.setdefault(key, []).append(time.time())


def clear_attempts(key: str) -> None:
    _attempts.pop(key, None)


def generate_otp() -> str:
    """Return a six-digit OTP from the operating system CSPRNG."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_device_secret() -> str:
    """
    256 bits from the operating system CSPRNG. Device secrets are never chosen
    by a human, so there is no usability argument for anything shorter.
    """
    return secrets.token_urlsafe(32)
