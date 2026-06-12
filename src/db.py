"""Lakebase (Autoscaling) connection pool with per-connection OAuth tokens.

Native pg login is OFF on the project, so authentication is token-only and tokens
expire after ~1 hour. We follow the official Databricks Apps pattern: a
psycopg_pool.ConnectionPool whose connection class mints a fresh OAuth token via
`w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME)` each time the
pool creates/recycles a connection, with max_lifetime below the token TTL.

In a Databricks App the `postgres` resource binding auto-injects
PGHOST/PGUSER/PGPORT/PGDATABASE/PGSSLMODE. For local dev, set DATABASE_URL and
token minting is skipped entirely.
"""
from __future__ import annotations

import atexit
import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def _configure(conn: psycopg.Connection) -> None:
    """Run once per physical connection: search_path, autocommit, pgvector."""
    conn.autocommit = True
    sch = config.PG_SCHEMA
    if sch and sch != "public":
        if not sch.isidentifier():
            raise ValueError(f"invalid PG_SCHEMA (must be a simple identifier): {sch!r}")
        # Unqualified table names resolve to the app-owned schema; keep public so
        # extension types (e.g. pgvector's `vector`) still resolve.
        conn.execute(f'SET search_path TO "{sch}", public')
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:
        # vector type may not exist yet (e.g. before schema init) — harmless;
        # we bind vectors as text literals regardless.
        pass


def _oauth_connection_class():
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    endpoint = config.ENDPOINT_NAME

    class OAuthConnection(psycopg.Connection):
        @classmethod
        def connect(cls, conninfo: str = "", **kwargs):
            cred = w.postgres.generate_database_credential(endpoint=endpoint)
            kwargs["password"] = cred.token
            return super().connect(conninfo, **kwargs)

    return OAuthConnection


def _build_pool() -> ConnectionPool:
    common = dict(
        min_size=1,
        max_lifetime=2700,  # 45 min — recycle before the 1-hour OAuth token expires
        open=False,
        configure=_configure,
        kwargs={"row_factory": dict_row},
    )
    if config.DATABASE_URL:
        # Local dev: credentials are in the URL; no token minting.
        return ConnectionPool(conninfo=config.DATABASE_URL, max_size=5, **common)

    conninfo = (
        f"dbname={config.PGDATABASE} user={config.PGUSER} "
        f"host={config.PGHOST} port={config.PGPORT} sslmode={config.PGSSLMODE}"
    )
    return ConnectionPool(
        conninfo=conninfo,
        connection_class=_oauth_connection_class(),
        max_size=10,
        **common,
    )


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                pool = _build_pool()
                pool.open(wait=True, timeout=30.0)  # fail fast if DB unreachable
                _pool = pool
    return _pool


@contextmanager
def connection():
    """Borrow a connection from the pool (returned automatically on exit)."""
    with _get_pool().connection() as conn:
        yield conn


@atexit.register
def _close_pool() -> None:
    """Close the pool cleanly on process exit (avoids worker-thread warnings)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
