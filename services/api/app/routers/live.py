"""
Project Atlas - live channel
AT-26-L1-002  |  FR-12, FR-14, FR-24

One listening database connection, held by a background thread, fanned out to
every connected WebSocket client. The database cost is therefore constant in
the number of clients, which is the reason for choosing NOTIFY over polling:
polling costs one query per client per interval whether or not anything
changed, and it is precisely wrong at both ends - too slow when something
happens, wasteful when nothing does.

The listener thread is deliberately separate from the pooled connections. A
connection sitting in LISTEN is blocked and unavailable for queries, so taking
one from the pool would silently reduce pool capacity by one.
"""

import asyncio
import json
import threading
from typing import Any

import psycopg
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from .. import config, db, security
from ..logging_setup import log

router = APIRouter(tags=["live"])

_CHANNEL = "atlas_alarms"

_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_stop = threading.Event()


def _broadcast(payload: dict[str, Any]) -> None:
    """Called from the listener thread; hops onto the event loop to deliver."""
    if _loop is None:
        return
    with _lock:
        targets = list(_subscribers)

    def deliver() -> None:
        for q in targets:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # A client too slow to drain its own queue is dropped rather
                # than allowed to grow it without bound. Live updates are
                # perishable: replaying a backlog of stale alarm states to a
                # reconnecting dashboard is worse than letting it re-query.
                pass

    _loop.call_soon_threadsafe(deliver)


def _listen_loop() -> None:
    while not _stop.is_set():
        try:
            with psycopg.connect(config.DSN, autocommit=True) as conn:
                conn.execute(f"LISTEN {_CHANNEL}")
                log("info", "listening for database notifications",
                    channel=_CHANNEL)
                # notifies(timeout=) returns a generator that ENDS when the
                # timeout elapses, so it is polled in an inner loop rather than
                # iterated once. Without the inner loop the connection is torn
                # down and rebuilt every five seconds, which works but churns a
                # database connection forever and fills the log.
                while not _stop.is_set():
                    for note in conn.notifies(timeout=5, stop_after=None):
                        if _stop.is_set():
                            break
                        try:
                            _broadcast(json.loads(note.payload))
                        except json.JSONDecodeError:
                            log("error", "unparseable notification payload",
                                channel=_CHANNEL, raw=note.payload[:200])
        except Exception as e:
            if _stop.is_set():
                return
            log("warning", "notification listener dropped, reconnecting",
                error=str(e)[:160])
            _stop.wait(2)


def start_listener(loop: asyncio.AbstractEventLoop) -> threading.Thread:
    global _loop
    _loop = loop
    _stop.clear()
    t = threading.Thread(target=_listen_loop, name="atlas-listen", daemon=True)
    t.start()
    return t


def stop_listener() -> None:
    _stop.set()


@router.websocket("/ws/live")
async def live(websocket: WebSocket, token: str = Query(default="")) -> None:
    """
    Authenticated with the same access token as every other endpoint, passed as
    a query parameter.

    This is a deviation from the header-based scheme and it is deliberate: the
    browser WebSocket API cannot set an Authorization header, so the only
    alternatives are a query parameter or a cookie, and a cookie would
    reintroduce CSRF exposure that bearer tokens avoid. The cost is that the
    token can appear in access logs, which is bounded by the fifteen-minute
    lifetime and is why the lifetime is short. Recorded for the as-built
    revision as a divergence from ATL-M1-008.
    """
    claims = security.decode_token(token, expected_type="access")
    if claims is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Viewer is the floor, per report 8.7. The check is here rather than in a
    # dependency because HTTPBearer does not apply to the WebSocket handshake.
    row = db.fetch_one(
        "SELECT u.username, u.is_active, r.name AS role FROM users u "
        "JOIN roles r ON r.id = u.role_id WHERE u.id = %s",
        (int(claims["sub"]),))
    if row is None or not row["is_active"]:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    with _lock:
        _subscribers.add(q)
    log("info", "live client connected", username=row["username"],
        subscribers=len(_subscribers))

    try:
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30)
            except asyncio.TimeoutError:
                # Keepalive. A silent connection through a NAT or proxy is
                # reaped after an idle timeout, and a dashboard that appears
                # connected but receives nothing is the worst failure mode here.
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json({"type": "alarm", "data": payload})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log("warning", "live client error", error=str(e)[:160])
    finally:
        with _lock:
            _subscribers.discard(q)
        log("info", "live client disconnected", username=row["username"],
            subscribers=len(_subscribers))
