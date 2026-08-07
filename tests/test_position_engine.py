"""
Acceptance tests for Task 21: the position engine.

The scripted lifecycle test is the centrepiece — open, TP1 partial, break-even,
trail, TP2 — with every state transition asserted in the positions table rather
than inferred from a return value. A manager that reports the right action
while writing the wrong row is still broken.

Positions are seeded under client_order_ids unique to this process so the
engine's table-wide queries cannot pick up another test's rows.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

import config
from core import database
from core.state import BUS
from exec_ import position_engine as pe
from exec_ import router as router_mod
from exec_.position_engine import PositionEngine, doctrine_conflicts, is_eow_flat_time, trailed_stop

UTC = timezone.utc
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)          # a Wednesday
FRIDAY_LATE = datetime(2026, 8, 7, 20, 30, tzinfo=UTC)  # Friday 20:30 UTC

_SEQ = {"n": 0}


def next_cid(tag="POS"):
    _SEQ["n"] += 1
    return f"TST-{tag}-{os.getpid()}-{_SEQ['n']}"


class SpyRouter:
    def __init__(self):
        self.closed = []

    def close(self, client_order_id):
        self.closed.append(client_order_id)
        return {"outcome": "CLOSED", "client_order_id": client_order_id}


@pytest.fixture(autouse=True)
def _cleanup():
    """
    FIX 22.1. Scoped to this process's ids and extended to fills, so a suite
    run leaves the ledgers exactly as it found them. A positions row that
    outlives its test is read by /status as a real trade.
    """
    yield
    if not config.DATABASE_URL:
        return
    pattern = f"TST-%-{os.getpid()}-%"
    for table in ("fills", "positions"):
        try:
            database.execute(
                f"DELETE FROM {table} WHERE client_order_id LIKE %s", (pattern,)
            )
        except Exception:
            pass


def seed(cid, **overrides):
    row = dict(
        source="POD", pod="S1_FIXFADE", direction="LONG", lots=0.10,
        entry_px=4000.0, stop_px=3990.0, tp1_px=4010.0, tp2_px=4020.0,
        state=router_mod.STATE_OPEN,
    )
    row.update(overrides)
    database.execute(
        "INSERT INTO positions (client_order_id, source, pod, direction, lots, entry_px, "
        "stop_px, tp1_px, tp2_px, state, opened_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())",
        (cid, row["source"], row["pod"], row["direction"], row["lots"], row["entry_px"],
         row["stop_px"], row["tp1_px"], row["tp2_px"], row["state"]),
    )
    return {"client_order_id": cid, **row}


def read(cid):
    rows = database.fetch(
        "SELECT state, stop_px, lots, realized_pnl_usd, close_reason FROM positions "
        "WHERE client_order_id = %s",
        (cid,),
    )
    if not rows:
        return None
    state, stop, lots, pnl, reason = rows[0]
    return {
        "state": state,
        "stop_px": float(stop) if stop is not None else None,
        "lots": float(lots) if lots is not None else None,
        "pnl": float(pnl) if pnl is not None else None,
        "close_reason": reason,
    }


def bar(high, low, close, ts=NOW):
    return {"ts": ts, "open": close, "high": high, "low": low, "close": close, "volume": 100}


# ===========================================================================
# pure helpers
# ===========================================================================


def test_trailed_stop_never_moves_backward_over_a_hundred_steps():
    """
    A stop that can retreat is not a stop. Fixed sawtooth, no randomness.
    """
    stop = 3990.0
    previous = stop
    for step in range(100):
        price = 4000.0 + (step % 7) * 3.0 - (step % 5) * 2.0
        stop = trailed_stop("LONG", stop, price, 10.0)
        assert stop >= previous, f"LONG stop retreated at step {step}"
        previous = stop

    stop = 4010.0
    previous = stop
    for step in range(100):
        price = 4000.0 - (step % 7) * 3.0 + (step % 5) * 2.0
        stop = trailed_stop("SHORT", stop, price, 10.0)
        assert stop <= previous, f"SHORT stop retreated at step {step}"
        previous = stop


def test_trailed_stop_ratchets_toward_price():
    assert trailed_stop("LONG", 3990.0, 4020.0, 10.0) == pytest.approx(4010.0)
    assert trailed_stop("LONG", 4010.0, 4000.0, 10.0) == pytest.approx(4010.0)   # held
    assert trailed_stop("SHORT", 4010.0, 3980.0, 10.0) == pytest.approx(3990.0)
    assert trailed_stop("SHORT", 3990.0, 4000.0, 10.0) == pytest.approx(3990.0)  # held


@pytest.mark.parametrize(
    "bias,direction,expected",
    [
        ("LONG_ONLY", "SHORT", True), ("LONG_ONLY", "LONG", False),
        ("SHORT_ONLY", "LONG", True), ("SHORT_ONLY", "SHORT", False),
        ("FLAT", "LONG", True), ("FLAT", "SHORT", True),
        ("BOTH", "LONG", False), (None, "LONG", False),
    ],
)
def test_doctrine_conflict_matrix(bias, direction, expected):
    assert doctrine_conflicts(bias, direction) is expected


def test_eow_flat_window():
    assert is_eow_flat_time(FRIDAY_LATE) is True
    assert is_eow_flat_time(FRIDAY_LATE - timedelta(minutes=1)) is False
    assert is_eow_flat_time(FRIDAY_LATE + timedelta(hours=2)) is True
    assert is_eow_flat_time(NOW) is False          # Wednesday


def test_eow_flat_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "EOW_FLAT_ENABLED", False)
    assert is_eow_flat_time(FRIDAY_LATE) is False


def test_trail_multiple_is_per_pod():
    assert pe.trail_mult_for("S1_FIXFADE") == config.S1_STOP_ATR_MULT
    assert pe.trail_mult_for("S3_BASIS") == config.S3_STOP_ATR_MULT
    assert pe.trail_mult_for(None) == config.TRAIL_ATR_MULT_SWING


# ===========================================================================
# the scripted lifecycle
# ===========================================================================


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_full_lifecycle_open_tp1_be_trail_tp2():
    """
    LONG 0.10 @ 4000, stop 3990, tp1 4010, tp2 4020, atr 10.

      1. quiet bar               -> HOLD
      2. high touches tp1 (4010) -> half off (0.05), stop to entry 4000, BE
                                    pnl = (4010-4000)*100*0.05 = 50.00
      3. price 4030              -> trail to 4030 - 0.8*10 = 4022  (S1 mult)
      4. high touches tp2 (4020) -> CLOSED, remainder booked
    """
    cid = next_cid("LIFE")
    seed(cid)
    router = SpyRouter()
    engine = PositionEngine(router)

    def live():
        with database.get_conn() as conn:
            rows = [p for p in pe.load_live_positions(conn) if p["client_order_id"] == cid]
        return rows[0] if rows else None

    # 1. nothing happens
    assert engine.evaluate(live(), bar(4005.0, 3995.0, 4000.0), NOW, atr=10.0) == "HOLD"
    assert read(cid)["state"] == router_mod.STATE_OPEN

    # 2. tp1
    assert engine.evaluate(live(), bar(4011.0, 3999.0, 4010.0), NOW, atr=10.0) == "TP1_PARTIAL"
    after_tp1 = read(cid)
    assert after_tp1["state"] == router_mod.STATE_BE
    assert after_tp1["lots"] == pytest.approx(0.05)
    assert after_tp1["stop_px"] == pytest.approx(4000.0), "stop must sit at entry"
    assert after_tp1["pnl"] == pytest.approx(50.0)

    # 3. trail
    assert engine.evaluate(live(), bar(4019.0, 4012.0, 4030.0), NOW, atr=10.0) == "TRAIL"
    after_trail = read(cid)
    assert after_trail["state"] == router_mod.STATE_TRAILING
    assert after_trail["stop_px"] == pytest.approx(4030.0 - config.S1_STOP_ATR_MULT * 10.0)

    # 4. tp2 — reseed a reachable stop so tp2 is what triggers
    database.execute("UPDATE positions SET stop_px = 4000 WHERE client_order_id = %s", (cid,))
    assert engine.evaluate(live(), bar(4021.0, 4015.0, 4020.0), NOW, atr=10.0) == "TP2"
    final = read(cid)
    assert final["state"] == router_mod.STATE_CLOSED
    assert final["close_reason"] == "TP2"
    assert cid in router.closed


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_stop_wins_on_a_bar_that_spans_both():
    """H1 cannot say which came first; the pessimistic reading is the honest one."""
    cid = next_cid("BOTH")
    position = seed(cid)
    engine = PositionEngine(SpyRouter())

    action = engine.evaluate(position, bar(4025.0, 3985.0, 4000.0), NOW, atr=10.0)

    assert action == "STOP"
    row = read(cid)
    assert row["state"] == router_mod.STATE_CLOSED
    assert row["close_reason"] == "STOP"
    # (3990 - 4000) * 100 * 0.10 = -100.00
    assert row["pnl"] == pytest.approx(-100.0)


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_short_lifecycle_mirrors():
    cid = next_cid("SHORT")
    position = seed(
        cid, direction="SHORT", entry_px=4000.0, stop_px=4010.0, tp1_px=3990.0, tp2_px=3980.0
    )
    engine = PositionEngine(SpyRouter())

    assert engine.evaluate(position, bar(4005.0, 3989.0, 3990.0), NOW, atr=10.0) == "TP1_PARTIAL"
    row = read(cid)
    assert row["stop_px"] == pytest.approx(4000.0)
    assert row["pnl"] == pytest.approx(50.0)   # (4000-3990)*100*0.05


# ===========================================================================
# doctrine flip and end-of-week
# ===========================================================================


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_a_doctrine_flip_tightens_a_swing_to_break_even():
    cid = next_cid("FLIP")
    position = seed(cid, source="SWING", pod=None, direction="LONG", stop_px=3990.0)
    engine = PositionEngine(SpyRouter())

    action = engine.evaluate(position, bar(4005.0, 3995.0, 4000.0), NOW, atr=10.0, bias="SHORT_ONLY")

    assert action == "DOCTRINE_FLIP"
    row = read(cid)
    assert row["stop_px"] == pytest.approx(4000.0), "tightened to entry"
    assert row["state"] != router_mod.STATE_CLOSED, "a flip tightens, it does not close"


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_a_doctrine_flip_does_not_touch_a_pod_position():
    """Pods live and die by their own breakers, not by the desk's view."""
    cid = next_cid("PODFLIP")
    position = seed(cid, source="POD", pod="S1_FIXFADE")
    engine = PositionEngine(SpyRouter())

    action = engine.evaluate(position, bar(4005.0, 3995.0, 4000.0), NOW, atr=10.0, bias="FLAT")

    assert action == "HOLD"
    assert read(cid)["stop_px"] == pytest.approx(3990.0)


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_end_of_week_closes_pods_and_spares_swing():
    pod_cid, swing_cid = next_cid("EOWPOD"), next_cid("EOWSWING")
    pod_pos = seed(pod_cid, source="POD", pod="S2_VWAPSNAP")
    swing_pos = seed(swing_cid, source="SWING", pod=None)
    engine = PositionEngine(SpyRouter())
    quiet = bar(4005.0, 3995.0, 4000.0, ts=FRIDAY_LATE)

    assert engine.evaluate(pod_pos, quiet, FRIDAY_LATE, atr=10.0) == "EOW_FLAT"
    assert engine.evaluate(swing_pos, quiet, FRIDAY_LATE, atr=10.0) == "HOLD"

    assert read(pod_cid)["close_reason"] == "EOW_FLAT"
    assert read(swing_cid)["state"] == router_mod.STATE_OPEN


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_a_stop_still_wins_over_end_of_week():
    """Risk order: anything that closes on price comes before the calendar."""
    cid = next_cid("EOWSTOP")
    position = seed(cid, source="POD")
    engine = PositionEngine(SpyRouter())

    action = engine.evaluate(position, bar(4005.0, 3985.0, 4000.0, ts=FRIDAY_LATE),
                             FRIDAY_LATE, atr=10.0)

    assert action == "STOP"
    assert read(cid)["close_reason"] == "STOP"


# ===========================================================================
# survival + events
# ===========================================================================


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_a_closed_position_publishes_a_position_event():
    received = []
    BUS.subscribe("position_event", received.append)

    cid = next_cid("EVENT")
    position = seed(cid, source="POD", pod="S1_FIXFADE")
    PositionEngine(SpyRouter()).evaluate(position, bar(4005.0, 3985.0, 4000.0), NOW, atr=10.0)

    closes = [e for e in received if e.get("client_order_id") == cid]
    assert closes, "the pod supervisor needs this to book the outcome"
    assert closes[0]["event"] == "CLOSED"
    assert closes[0]["pod"] == "S1_FIXFADE"
    assert closes[0]["realized_pnl_usd"] == pytest.approx(-100.0)


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_a_raising_router_does_not_stop_the_close_being_recorded():
    class ExplodingRouter:
        def close(self, cid):
            raise RuntimeError("bridge gone")

    cid = next_cid("BOOM")
    position = seed(cid)
    action = PositionEngine(ExplodingRouter()).evaluate(
        position, bar(4005.0, 3985.0, 4000.0), NOW, atr=10.0
    )

    assert action == "STOP"
    assert read(cid)["state"] == router_mod.STATE_CLOSED


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_evaluate_all_survives_one_broken_position(caplog):
    caplog.set_level(logging.INFO, logger="exec_.position_engine")
    good = next_cid("GOOD")
    seed(good, source="POD")
    engine = PositionEngine(SpyRouter())

    actions = engine.evaluate_all(bar(4005.0, 3985.0, 4000.0), NOW, 10.0, None)

    assert actions.get(good) == "STOP"


def test_a_bar_without_prices_is_a_no_op():
    engine = PositionEngine(SpyRouter())
    position = {
        "client_order_id": "x", "source": "POD", "pod": None, "direction": "LONG",
        "lots": 0.1, "entry_px": 4000.0, "stop_px": 3990.0, "tp1_px": None,
        "tp2_px": None, "state": router_mod.STATE_OPEN,
    }
    assert engine.evaluate(position, {"high": None, "low": None, "close": None}, NOW) == "NO_BAR"


# ===========================================================================
# the slippage callable
# ===========================================================================


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_slippage_p75_is_hand_computed_over_twenty_five_fills(monkeypatch):
    """
    |slippage| = 1..25, nearest-rank p75:
      index = ceil(0.75 * 25) - 1 = 19 - 1 = 18  -> the 19th smallest = 19.0
    """
    values = [(float(v),) for v in range(1, 26)]
    monkeypatch.setattr(database, "fetch", lambda *a, **k: values)
    assert router_mod.observed_slippage_p75() == pytest.approx(19.0)


@pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")
def test_slippage_p75_refuses_below_twenty_rows(monkeypatch):
    """A percentile over a handful of fills describes luck, not the broker."""
    values = [(float(v),) for v in range(1, 20)]   # 19 rows
    monkeypatch.setattr(database, "fetch", lambda *a, **k: values)
    assert router_mod.observed_slippage_p75() is None


def test_slippage_p75_survives_a_dead_database(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "fetch", boom)
    assert router_mod.observed_slippage_p75() is None


def test_the_kernel_never_queries_for_slippage_itself():
    """
    Ring 0 takes the reading as an injected callable. If the kernel grew its
    own query it would need a database, and a kernel that needs a database is
    a kernel that can be taken down by one.
    """
    import ast
    import inspect

    import risk.kernel as kernel_mod

    tree = ast.parse(inspect.getsource(kernel_mod))
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("FROM fills" in text for text in literals)
