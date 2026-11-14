"""
Project Atlas - API service
AT-26-L1-002  |  serves FR-01..FR-04, FR-11..FR-15, FR-19..FR-26

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Docs: /docs   Specification: /openapi.json

Report 8.9 records that ATL-M1-008 is a design-stage contract and that the
generated document becomes authoritative once this service exists. That
generated document is produced from the models in schemas.py, so the two
cannot drift apart within this service; divergence from the issued M1
specification is recorded as a deviation rather than silently corrected.
"""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import config, db, security
from .logging_setup import log
from .routers import admin, auth, data, devices, live
from .schemas import HealthOut

VERSION = "0.1.0"


def bootstrap_admin() -> None:
    """
    Create the first administrator, only when there is no user at all.

    The condition is 'no users', not 'no administrator'. If somebody has
    deleted the last administrator while other accounts exist, silently
    minting a new one from an environment variable would be a privilege
    escalation path, not a convenience.
    """
    if not (config.BOOTSTRAP_ADMIN_USER and config.BOOTSTRAP_ADMIN_PASSWORD):
        return
    if len(config.BOOTSTRAP_ADMIN_PASSWORD) < config.PASSWORD_MIN_LENGTH:
        log("error", "bootstrap admin password below minimum length, skipped",
            minimum=config.PASSWORD_MIN_LENGTH)
        return

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM users")
        if cur.fetchone()["n"] > 0:
            return
        cur.execute("SELECT id FROM roles WHERE name = 'administrator'")
        role = cur.fetchone()
        cur.execute(
            "INSERT INTO users (username, password_hash, role_id) "
            "VALUES (%s, %s, %s) RETURNING id",
            (config.BOOTSTRAP_ADMIN_USER,
             security.hash_password(config.BOOTSTRAP_ADMIN_PASSWORD),
             role["id"]))
        new_id = cur.fetchone()["id"]
        conn.commit()

    log("warning", "bootstrap administrator created from environment - "
                   "change this password and remove the variables",
        username=config.BOOTSTRAP_ADMIN_USER, user_id=new_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    db.open_pool()
    bootstrap_admin()
    live.start_listener(asyncio.get_running_loop())
    log("info", "api service started", version=VERSION,
        access_ttl_min=config.ACCESS_TTL_MIN,
        refresh_ttl_days=config.REFRESH_TTL_DAYS)
    try:
        yield
    finally:
        live.stop_listener()
        db.close_pool()
        log("info", "api service stopped")


app = FastAPI(
    title="Project Atlas API",
    version=VERSION,
    description=("Cyber-physical monitoring platform. Assessment AT-26-L1-002. "
                 "Every endpoint states the minimum role it requires; the "
                 "check is enforced here, not in the interface."),
    lifespan=lifespan,
)

# Locked to an explicit origin list from the environment. A wildcard would
# permit any page the operator happens to visit to call this API with their
# browser's credentials, which is the browser-side version of the privilege
# escalation threat in report 8.8.
if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def request_log(request: Request, call_next):
    """
    FR-20, the part middleware can honestly do: attribute every request to a
    user with its outcome and duration. The before/after capture that FR-19
    requires happens inside the handlers - see the note at the top of audit.py.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    user = getattr(request.state, "user", None)
    level = ("warning" if response.status_code >= 400 else "info")
    if response.status_code >= 500:
        level = "error"

    log(level, "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=elapsed_ms,
        actor=user.username if user else None,
        role=user.role if user else None)

    response.headers["X-Response-Time-ms"] = str(elapsed_ms)
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """
    The message goes to the log, not to the client.

    A stack trace or a database error string in an HTTP response tells an
    attacker the schema, the driver and often the query. The correlation is
    kept by path and timestamp in the log instead.
    """
    log("error", "unhandled exception",
        path=request.url.path, method=request.method,
        error=f"{type(exc).__name__}: {exc}"[:300])
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        content={"detail": "internal error"})


@app.get("/health", response_model=HealthOut, tags=["operations"])
def health() -> HealthOut:
    """
    Public, and reports the dependency rather than just the process.

    A health check that returns 200 because the web server is running tells
    the operator nothing: the failure that matters is the database being
    unreachable, and this service is useless in that state.
    """
    try:
        db.fetch_one("SELECT 1 AS ok")
        return HealthOut(status="ok", database="reachable", version=VERSION)
    except Exception as e:
        log("error", "health check failed", error=str(e)[:160])
        return HealthOut(status="degraded", database="unreachable",
                         version=VERSION)


app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(data.router)
app.include_router(admin.router)
app.include_router(live.router)
