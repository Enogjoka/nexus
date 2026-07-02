"""
Database connection pool and migration runner.

Hard architectural rule: configuration is never stored in the database.
This module deliberately has no system_config table and no functions for
toggling paper trading, a kill switch, or any other config knob. Config
knobs live in config.py; secrets live in the environment.
"""
import glob
import os
from typing import Optional

import psycopg2
from psycopg2 import pool as pg_pool

import config

_pool: Optional[pg_pool.SimpleConnectionPool] = None

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _get_pool() -> pg_pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set")
        _pool = pg_pool.SimpleConnectionPool(1, 10, dsn=config.DATABASE_URL)
    return _pool


def init() -> None:
    """Test the database connection. Raises if it cannot be established."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        pool.putconn(conn)


def run_migrations() -> list[str]:
    """
    Apply migrations/*.sql in filename order, recording each applied
    filename in schema_migrations. Returns the list of versions applied
    during this call (already-applied migrations are skipped).
    """
    files = sorted(glob.glob(os.path.join(_MIGRATIONS_DIR, "*.sql")))

    pool = _get_pool()
    conn = pool.getconn()
    applied: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.commit()

            for filepath in files:
                version = os.path.basename(filepath)
                cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
                )
                if cur.fetchone():
                    continue
                with open(filepath, "r") as f:
                    sql = f.read()
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
                conn.commit()
                applied.append(version)
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
    return applied


def close() -> None:
    """Close the connection pool. Intended for graceful shutdown / tests."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
