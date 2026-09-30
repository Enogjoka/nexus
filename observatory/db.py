"""
Read-only database access for the observatory.

Every connection is opened with:
  * default_transaction_read_only = on   (the session cannot write)
  * statement_timeout = 5 s              (no query can hold the API hostage)
  * connect_timeout = 5 s                (an unreachable server fails fast)
  * timezone = UTC

and then set_session(readonly=True) as a second belt. The real protection is
the `observatory` role itself, which holds SELECT and nothing else.

Every helper returns plain JSON-safe values: Decimal -> float, datetime -> ISO
8601 UTC. Handing a Decimal to code that expects a float silently disabled the
regime model for months (plan F-35); nothing leaves this module as Decimal.
"""
import logging
import math
import threading
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger("observatory.db")

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5000
SESSION_OPTIONS = (
    "-c default_transaction_read_only=on "
    f"-c statement_timeout={STATEMENT_TIMEOUT_MS} "
    "-c timezone=UTC"
)
# The deploy role has CONNECTION LIMIT 5; stay under it and leave one for psql.
MAX_CONNECTIONS = 4
# How long a request waits for a free connection before answering 503.
CHECKOUT_WAIT_S = 5.0


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached or the pool is exhausted."""


def jsonable(value: Any) -> Any:
    """Recursively convert a DB value into something json.dumps accepts."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)  # the session runs in UTC
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None  # nothing binary belongs in the API
    return str(value)


class ReadOnlyDB:
    """A small thread-safe pool of read-only connections, opened lazily."""

    def __init__(self, dsn: str, max_connections: int = MAX_CONNECTIONS) -> None:
        self._dsn = dsn
        self._max = max_connections
        self._pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
        self._pool_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max_connections)

    def __repr__(self) -> str:  # the DSN may carry credentials
        return f"ReadOnlyDB(max_connections={self._max})"

    def _get_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        with self._pool_lock:
            if self._pool is None:
                try:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        0,
                        self._max,
                        dsn=self._dsn,
                        connect_timeout=CONNECT_TIMEOUT_S,
                        options=SESSION_OPTIONS,
                        application_name="nexus-observatory",
                    )
                except psycopg2.Error as exc:
                    raise DatabaseUnavailable(f"cannot open the observatory pool: {type(exc).__name__}") from exc
            return self._pool

    def _checkout(self):
        if not self._slots.acquire(timeout=CHECKOUT_WAIT_S):
            raise DatabaseUnavailable("all observatory connections are busy")
        try:
            pool = self._get_pool()
            conn = pool.getconn()
            if conn.closed:
                pool.putconn(conn, close=True)
                conn = pool.getconn()
            conn.set_session(readonly=True, autocommit=True)
            return pool, conn
        except DatabaseUnavailable:
            self._slots.release()
            raise
        except psycopg2.Error as exc:
            self._slots.release()
            raise DatabaseUnavailable(f"cannot connect: {type(exc).__name__}") from exc

    def _checkin(self, pool, conn, broken: bool) -> None:
        try:
            pool.putconn(conn, close=broken or bool(conn.closed))
        finally:
            self._slots.release()

    def rows(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """Run one read and return its rows as JSON-safe dicts."""
        pool, conn = self._checkout()
        broken = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [jsonable(dict(row)) for row in cur.fetchall()]
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            broken = True
            raise DatabaseUnavailable(f"database error: {type(exc).__name__}") from exc
        finally:
            self._checkin(pool, conn, broken)

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        found = self.rows(sql, params)
        return found[0] if found else None

    def table_exists(self, table: str) -> bool:
        row = self.one("SELECT to_regclass(%s) IS NOT NULL AS present LIMIT 1", (f"public.{table}",))
        return bool(row and row["present"])

    def close(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None
