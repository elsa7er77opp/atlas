"""
Project Atlas - API configuration
AT-26-L1-002  |  FR-25

Every secret arrives from the environment. Nothing is defaulted to a working
value: a missing secret stops the process at startup rather than letting the
service run on a placeholder that happens to be in the source tree. That is the
FR-25 verification method ("scan repository history; confirm no secret material
present") made structurally true rather than remembered.
"""

import os
import sys

# ------------------------------------------------------------------ database
PG_HOST = os.getenv("POSTGRES_HOST", "atlas-db")
PG_USER = os.getenv("POSTGRES_USER", "atlas")
PG_PASS = os.getenv("POSTGRES_PASSWORD", "")
PG_DB   = os.getenv("POSTGRES_DB", "atlas")

DSN = f"host={PG_HOST} user={PG_USER} password={PG_PASS} dbname={PG_DB}"

# Pool sizing. The API is I/O bound on the database, so the useful ceiling is
# set by concurrent in-flight queries, not by CPU. Four is comfortably above
# the offered rate in report 9.7 and far below the PostgreSQL default limit of
# 100, leaving room for the ingest and anomaly services.
POOL_MIN = int(os.getenv("POOL_MIN", "2"))
POOL_MAX = int(os.getenv("POOL_MAX", "8"))

# ----------------------------------------------------------------- tokens
JWT_SECRET    = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"

# Access tokens are not revocable, so their lifetime IS the bound on damage
# from a stolen token. Fifteen minutes is short enough to make replay a narrow
# window and long enough that a dashboard session is not refreshing constantly.
ACCESS_TTL_MIN    = int(os.getenv("ACCESS_TTL_MIN", "15"))
REFRESH_TTL_DAYS  = int(os.getenv("REFRESH_TTL_DAYS", "7"))

# ------------------------------------------------------------------ Argon2
# RFC 9106 second recommended option: 64 MiB, three passes, four lanes. The
# first option (2 GiB) is not usable in a container sized for a laboratory
# host. Memory cost is the parameter that matters against GPU attack, which is
# why it is preferred over raising iterations.
ARGON2_TIME_COST   = int(os.getenv("ARGON2_TIME_COST", "3"))
ARGON2_MEMORY_KIB  = int(os.getenv("ARGON2_MEMORY_KIB", "65536"))
ARGON2_PARALLELISM = int(os.getenv("ARGON2_PARALLELISM", "4"))

# --------------------------------------------------------------- bootstrap
# The first administrator has to come from somewhere. It comes from the
# environment, is created only when the users table is empty, and is logged
# loudly so it cannot be forgotten in place.
BOOTSTRAP_ADMIN_USER     = os.getenv("BOOTSTRAP_ADMIN_USER", "")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")

# ------------------------------------------------------------ MQTT broker
BROKER_PROVISIONING = os.getenv("BROKER_PROVISIONING", "static").lower()
BROKER_HOST = os.getenv("BROKER_HOST", "atlas-broker")
BROKER_PORT = int(os.getenv("BROKER_PORT", "8883"))
BROKER_CA = os.getenv("BROKER_CA", "/certs/ca.crt")
DYNSEC_ADMIN_USER = os.getenv("DYNSEC_ADMIN_USER", "")
DYNSEC_ADMIN_PASSWORD = os.getenv("DYNSEC_ADMIN_PASSWORD", "")

# ------------------------------------------------------------- email / OTP
EMAIL_MODE = os.getenv("EMAIL_MODE", "console").lower()
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
EMAIL_DEV_LOG_CODE = os.getenv("EMAIL_DEV_LOG_CODE", "false").lower() in {"1", "true", "yes"}
OTP_TTL_MIN = int(os.getenv("OTP_TTL_MIN", "10"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))

# ------------------------------------------------------------ rate limiting
# FR-26. Applies to the login endpoint only: it is the one unauthenticated
# route where guessing is the attack.
LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SEC   = int(os.getenv("LOGIN_WINDOW_SEC", "60"))

# ------------------------------------------------------------------- cors
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

PASSWORD_MIN_LENGTH = 12


def validate() -> None:
    """Fail fast, with the missing name stated."""
    missing = [n for n, v in (
        ("POSTGRES_PASSWORD", PG_PASS),
        ("JWT_SECRET", JWT_SECRET),
    ) if not v]
    if missing:
        print(f"missing required environment variables: {', '.join(missing)}",
              file=sys.stderr)
        raise SystemExit(1)

    # A short signing key makes HS256 brute-forceable offline from any token.
    if len(JWT_SECRET) < 32:
        print("JWT_SECRET must be at least 32 characters", file=sys.stderr)
        raise SystemExit(1)

    if EMAIL_MODE == "smtp":
        smtp_missing = [n for n, v in (
            ("SMTP_HOST", SMTP_HOST),
            ("SMTP_USER", SMTP_USER),
            ("SMTP_PASSWORD", SMTP_PASSWORD),
            ("SMTP_FROM", SMTP_FROM),
        ) if not v]
        if smtp_missing:
            print(
                "missing required SMTP environment variables: "
                + ", ".join(smtp_missing),
                file=sys.stderr,
            )
            raise SystemExit(1)

    if BROKER_PROVISIONING == "dynsec":
        dynsec_missing = [n for n, v in (
            ("DYNSEC_ADMIN_USER", DYNSEC_ADMIN_USER),
            ("DYNSEC_ADMIN_PASSWORD", DYNSEC_ADMIN_PASSWORD),
        ) if not v]
        if dynsec_missing:
            print(
                "missing required Dynamic Security environment variables: "
                + ", ".join(dynsec_missing),
                file=sys.stderr,
            )
            raise SystemExit(1)
