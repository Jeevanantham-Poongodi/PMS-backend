"""Database access. Every user request runs in one transaction as the `authenticated` role with
the verified token's claims, so Supabase RLS applies exactly as for that user (research R1)."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import get_settings
from app.core.errors import AppError

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        url = get_settings().database_url
        if not url:
            raise AppError(503, "DATABASE_NOT_CONFIGURED", "The database is not configured.")
        # Check connections before use and keep them alive: dropped connections to the hosted
        # database otherwise surface as requests that hang until the socket times out.
        _pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=True,
            timeout=10,
            check=ConnectionPool.check_connection,
            kwargs={"keepalives": 1, "keepalives_idle": 20, "keepalives_interval": 10},
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def user_transaction(claims: dict[str, Any]) -> Iterator[Cursor]:
    """One transaction as the signed-in user. Commits on success, rolls back on any error."""
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                # One round trip: set_config('role', …, true) is SET LOCAL ROLE.
                cur.execute(
                    "select set_config('role', 'authenticated', true),"
                    " set_config('request.jwt.claims', %s, true),"
                    " set_config('request.jwt.claim.sub', %s, true)",
                    (json.dumps(claims), str(claims["sub"])),
                )
                yield cur


def ping() -> bool:
    with get_pool().connection() as conn:
        return conn.execute("select 1").fetchone() == (1,)
