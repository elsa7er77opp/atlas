"""
Project Atlas - database access
AT-26-L1-002

A pool rather than the single connection the ingestion service uses. The
difference is not stylistic: ingest is one thread draining one queue, so one
connection is exactly right. The API serves concurrent requests, and sharing a
single connection across them would serialise every request behind the slowest
query and make transaction boundaries ambiguous.

Every query in this service is parameterised. No caller builds SQL by string
concatenation, which is the mitigation recorded against the injection threat in
report 8.8.
"""

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config
from .logging_setup import log

_pool: ConnectionPool | None = None


def open_pool(retries: int = 30) -> ConnectionPool:
    """
    Wait for the database instead of crash-looping.

    Compose already gates on the healthcheck, but a healthy container is not
    the same as an accepting server during a restart, and an API that dies on
    a transient refusal turns a two-second blip into a restart loop.
    """
    global _pool
    for attempt in range(1, retries + 1):
        try:
            pool = ConnectionPool(
                conninfo=config.DSN,
                min_size=config.POOL_MIN,
                max_size=config.POOL_MAX,
                kwargs={"row_factory": dict_row, "autocommit": False},
                open=True,
                timeout=10,
            )
            pool.wait(timeout=15)
            _pool = pool
            log("info", "database pool ready",
                min_size=config.POOL_MIN, max_size=config.POOL_MAX)
            return pool
        except Exception as e:
            wait = min(2 ** min(attempt, 5), 32)
            log("warning", "database not ready, retrying",
                attempt=attempt, wait_s=wait, error=str(e)[:120])
            time.sleep(wait)
    raise RuntimeError("database unreachable after retries")


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
        log("info", "database pool closed")


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """
    Hand out a pooled connection.

    Commit is explicit at the call site. Autocommit is off so that a write and
    its audit entry land in the same transaction: an audit trail that can be
    absent while the change it describes succeeded is not an audit trail.
    """
    if _pool is None:
        raise RuntimeError("pool not open")
    with _pool.connection() as conn:
        yield conn


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
