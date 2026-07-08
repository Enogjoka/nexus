"""
Acceptance tests for Task 1.5 core plumbing:

  - core/database.py's get_conn()/execute()/fetch()
  - core/state.py's shared BUS/STATE singletons and exception-safe
    EventBus.publish()
  - the per-timeframe anomaly threshold used by data/gold_agent.py

The get_conn() commit/rollback tests talk to the real nexus_dev database
(DATABASE_URL must be set) — that's the only way to actually prove
transactional behavior end to end. They're skipped (not failed) when
DATABASE_URL is unset, so the rest of the suite stays runnable without a
live Postgres.
"""
import numpy as np
import pandas as pd
import pytest

import config
from core import database
from core.state import BUS as bus_from_core_state
from data.gold_agent import BUS as bus_from_gold_agent
from data.gold_agent import detect_anomalies

# candles.symbol is VARCHAR(10) — keep test symbols within that limit.
_TEST_SYMBOL_ROLLBACK = "TST_RB1_5"
_TEST_SYMBOL_COMMIT = "TST_CM1_5"

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _cleanup_test_rows():
    yield
    if not config.DATABASE_URL:
        return
    database.execute(
        "DELETE FROM candles WHERE symbol IN (%s, %s)",
        (_TEST_SYMBOL_ROLLBACK, _TEST_SYMBOL_COMMIT),
    )


# --------------------------------------------------------------------------
# get_conn(): commit / rollback
# --------------------------------------------------------------------------


@requires_db
def test_get_conn_rolls_back_on_exception():
    with pytest.raises(RuntimeError):
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume, is_anomaly)
                    VALUES (%s, '1h', NOW(), 1, 1, 1, 1, 1, false)
                    """,
                    (_TEST_SYMBOL_ROLLBACK,),
                )
            raise RuntimeError("boom - force rollback")

    rows = database.fetch("SELECT 1 FROM candles WHERE symbol = %s", (_TEST_SYMBOL_ROLLBACK,))
    assert rows == []


@requires_db
def test_get_conn_commits_on_clean_exit():
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume, is_anomaly)
                VALUES (%s, '1h', NOW(), 1, 1, 1, 1, 1, false)
                """,
                (_TEST_SYMBOL_COMMIT,),
            )

    rows = database.fetch("SELECT 1 FROM candles WHERE symbol = %s", (_TEST_SYMBOL_COMMIT,))
    assert len(rows) == 1


# --------------------------------------------------------------------------
# BUS: shared singleton, exception-safe publish
# --------------------------------------------------------------------------


def test_bus_is_a_shared_singleton_across_modules():
    assert bus_from_core_state is bus_from_gold_agent
    assert id(bus_from_core_state) == id(bus_from_gold_agent)


def test_publish_survives_a_raising_subscriber():
    received = []

    def bad_subscriber(payload):
        raise ValueError("subscriber blew up")

    def good_subscriber(payload):
        received.append(payload)

    event_name = "test_core_plumbing_event"
    bus_from_core_state.subscribe(event_name, bad_subscriber)
    bus_from_core_state.subscribe(event_name, good_subscriber)

    bus_from_core_state.publish(event_name, {"ok": True})

    assert received == [{"ok": True}]


# --------------------------------------------------------------------------
# per-timeframe anomaly threshold
# --------------------------------------------------------------------------


def _make_ohlcv(close: np.ndarray) -> pd.DataFrame:
    n = len(close)
    ts = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "open": close.copy(),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1000.0),
        }
    )


def test_anomaly_4pct_daily_move_not_flagged():
    # config.ANOMALY_MAX_PCT_JUMP["1d"] == 8.0; a 4% jump must NOT trip it.
    closes = np.array([2000.0, 2000.0, 2080.0, 2080.0])
    df = _make_ohlcv(closes)
    flags = detect_anomalies(df, "1d")
    assert not flags.any()


def test_anomaly_4pct_hourly_move_flagged():
    # config.ANOMALY_MAX_PCT_JUMP["1h"] == 3.0; a 4% jump must trip it.
    closes = np.array([2000.0, 2000.0, 2080.0, 2080.0])
    df = _make_ohlcv(closes)
    flags = detect_anomalies(df, "1h")
    assert bool(flags.iloc[2]) is True
