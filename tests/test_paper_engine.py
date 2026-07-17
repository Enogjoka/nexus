"""
Acceptance tests for Task 6 part 1: the paper engine (exec_/paper_engine.py).

Scripted signal lifecycles against nexus_dev. No network. Each test uses a
TST_PE-prefixed symbol so evaluate_signals() (which filters by symbol) sees
only its own rows, and the autouse fixture purges them afterward.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from core import database
from exec_ import paper_engine

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

T0 = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if config.DATABASE_URL:
        database.execute("DELETE FROM signals WHERE symbol LIKE 'TST_PE%'")


def _long_prices():
    # entry 100, stop 90 (risk 10), tp1 120 (2R), tp2 140 (4R)
    return {
        "entry": 100.0, "stop": 90.0, "tp1": 120.0, "tp2": 140.0,
        "risk_per_unit": 10.0, "tp1_rr": 2.0, "tp2_rr": 4.0, "lots": 0.10,
    }


def _insert(symbol, prices, ts=T0, direction="LONG", status="PENDING"):
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO signals (ts, symbol, direction, prices, grade, confidence, "
                "thesis, market_snapshot, status, execution_mode, lots) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
                (ts, symbol, direction, json.dumps(prices), "A", 80, "t",
                 json.dumps({}), status, "PAPER", prices.get("lots")),
            )
            return cur.fetchone()[0]


def _row(sig_id, *cols):
    return database.fetch(
        f"SELECT {', '.join(cols)} FROM signals WHERE id=%s", (sig_id,)
    )[0]


# --------------------------------------------------------------------------
# full lifecycle: PENDING -> OPEN -> TP1 partial -> BE
# --------------------------------------------------------------------------


@requires_db
def test_lifecycle_open_tp1_partial_then_break_even():
    sym = "TST_PE_LIFE"
    sig_id = _insert(sym, _long_prices())

    # 1) fill on the H1 close crossing entry
    events = paper_engine.evaluate_signals({"close": 99.0, "high": 101.0, "low": 98.0}, T0 + timedelta(hours=1), sym)
    assert ("signal_opened", {"id": sig_id, "direction": "LONG", "grade": "A",
                              "entry": 100.0, "stop": 90.0, "tp1": 120.0, "tp2": 140.0,
                              "lots": 0.1, "thesis": "t"}) in events
    status, filled_at = _row(sig_id, "status", "filled_at")
    assert status == "OPEN"
    assert filled_at is not None

    # 2) TP1 tagged -> partial, stop to break-even, still OPEN
    paper_engine.evaluate_signals({"close": 119.0, "high": 121.0, "low": 118.0}, T0 + timedelta(hours=2), sym)
    status, ms = _row(sig_id, "status", "market_snapshot")
    assert status == "OPEN"
    assert ms["paper_engine"]["partial_filled"] is True
    assert float(ms["paper_engine"]["tracked_stop"]) == 100.0

    # 3) price returns to entry (BE stop) -> BE close
    events = paper_engine.evaluate_signals({"close": 100.0, "high": 105.0, "low": 99.0}, T0 + timedelta(hours=3), sym)
    assert ("signal_closed", {"id": sig_id, "status": "BE", "outcome_r": 1.0}) in events
    status, outcome_r, outcome_pips = _row(sig_id, "status", "outcome_r", "outcome_pips")
    assert status == "BE"
    assert float(outcome_r) == 1.0           # tp1_rr * 0.5
    assert float(outcome_pips) == 100.0      # 1.0 * 10 / 0.1


# --------------------------------------------------------------------------
# stop-first ordering: a bar spanning both stop and tp2 books the STOP
# --------------------------------------------------------------------------


@requires_db
def test_stop_first_when_bar_hits_both_stop_and_tp2():
    sym = "TST_PE_STOP"
    sig_id = _insert(sym, _long_prices())
    paper_engine.evaluate_signals({"close": 99.0, "high": 101.0, "low": 98.0}, T0 + timedelta(hours=1), sym)

    # one wide bar: low 88 <= stop 90 AND high 141 >= tp2 140
    events = paper_engine.evaluate_signals({"close": 130.0, "high": 141.0, "low": 88.0}, T0 + timedelta(hours=2), sym)
    assert ("signal_closed", {"id": sig_id, "status": "STOPPED", "outcome_r": -1.0}) in events
    status, outcome_r = _row(sig_id, "status", "outcome_r")
    assert status == "STOPPED"
    assert float(outcome_r) == -1.0


# --------------------------------------------------------------------------
# TTL expiry for an unfilled PENDING
# --------------------------------------------------------------------------


@requires_db
def test_pending_expires_after_ttl():
    sym = "TST_PE_EXP"
    old_ts = T0 - timedelta(hours=config.PAPER_TTL_HOURS + 1)
    sig_id = _insert(sym, _long_prices(), ts=old_ts)

    # entry never crossed, but the signal is now older than the TTL
    events = paper_engine.evaluate_signals({"close": 150.0, "high": 151.0, "low": 149.0}, T0, sym)
    assert ("signal_closed", {"id": sig_id, "status": "EXPIRED", "outcome_r": None}) in events
    assert _row(sig_id, "status")[0] == "EXPIRED"


# --------------------------------------------------------------------------
# MAE/MFE tracked in R against each bar's high/low
# --------------------------------------------------------------------------


@requires_db
def test_mae_mfe_tracked_on_hand_computed_sequence():
    sym = "TST_PE_MAE"
    sig_id = _insert(sym, _long_prices())
    paper_engine.evaluate_signals({"close": 99.0, "high": 100.0, "low": 99.0}, T0 + timedelta(hours=1), sym)

    # Bar A: high 105 -> +0.5R, low 98 -> -0.2R
    paper_engine.evaluate_signals({"close": 102.0, "high": 105.0, "low": 98.0}, T0 + timedelta(hours=2), sym)
    # Bar B: high 103 -> +0.3R, low 95 -> -0.5R (new worst)
    paper_engine.evaluate_signals({"close": 101.0, "high": 103.0, "low": 95.0}, T0 + timedelta(hours=3), sym)

    status, mae_r, mfe_r = _row(sig_id, "status", "mae_r", "mfe_r")
    assert status == "OPEN"
    assert float(mfe_r) == 0.5    # best favorable excursion (105)
    assert float(mae_r) == -0.5   # worst adverse excursion (95)


# --------------------------------------------------------------------------
# short-side entry fill + stop
# --------------------------------------------------------------------------


@requires_db
def test_short_fills_and_stops():
    sym = "TST_PE_SHORT"
    prices = {"entry": 100.0, "stop": 110.0, "tp1": 80.0, "tp2": 60.0,
              "risk_per_unit": 10.0, "tp1_rr": 2.0, "tp2_rr": 4.0, "lots": 0.10}
    sig_id = _insert(sym, prices, direction="SHORT")

    # SHORT fills when close >= entry
    paper_engine.evaluate_signals({"close": 101.0, "high": 102.0, "low": 99.0}, T0 + timedelta(hours=1), sym)
    assert _row(sig_id, "status")[0] == "OPEN"

    # SHORT stop hit when high >= stop (110)
    events = paper_engine.evaluate_signals({"close": 108.0, "high": 111.0, "low": 107.0}, T0 + timedelta(hours=2), sym)
    assert ("signal_closed", {"id": sig_id, "status": "STOPPED", "outcome_r": -1.0}) in events
    assert _row(sig_id, "status")[0] == "STOPPED"
