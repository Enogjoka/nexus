"""
Acceptance tests for Task 16: the order router.

The governing property is INVARIANT 5: EVERY order passes kernel.permit(),
exactly once, and a denial never reaches the bridge. The spy kernel below
counts calls rather than merely recording them, because "permit was called"
and "permit was called once, before any send" are different guarantees and
only the second one is worth anything.

No network, and no MetaTrader5 import: every test drives SimBridge or a spy.
"""
import logging
import os
import time

import pytest

import config
from core import database
from core.state import STATE
from data.mt5_bridge import SimBridge
from exec_ import router as router_mod
from exec_.router import Router, build_kernel
from risk.kernel import Verdict

MID = 4000.0
EXPECTED_LONG_FILL = 4000.225
EXPECTED_SHORT_FILL = 3999.775

_SEQ = {"n": 0}


def next_signal(**overrides):
    """A signal row with an id unique to this process and test run."""
    _SEQ["n"] += 1
    signal = {
        "id": f"{os.getpid()}{_SEQ['n']:04d}",
        "direction": "LONG",
        "lots": 0.05,
        "prices": {"entry": 4000.0, "stop": 3990.0, "tp1": 4020.0, "tp2": 4040.0},
    }
    signal.update(overrides)
    return signal


@pytest.fixture(autouse=True)
def _clean_state():
    saved_market = dict(STATE.market_data)
    saved_ts = STATE.last_analysis_ts
    STATE.market_data.clear()
    STATE.update_market_data("1h", {"price": MID})
    STATE.last_analysis_ts = time.time()
    yield
    STATE.market_data.clear()
    STATE.market_data.update(saved_market)
    STATE.last_analysis_ts = saved_ts


class SpyKernel:
    """Counts permit() calls and returns a canned verdict."""

    def __init__(self, verdict=None):
        self.verdict = verdict or Verdict(allowed=True, reason="spy allows")
        self.orders = []

    @property
    def calls(self):
        return len(self.orders)

    def permit(self, order):
        self.orders.append(order)
        return self.verdict


class SpyBridge:
    """Records every send; returns a scripted sequence of results."""

    def __init__(self, results=None, spread=0.35):
        self.results = list(results) if results is not None else [{"fill_px": 4000.25, "ts": None}]
        self.opens = []
        self.closes = []
        self.flattens = []
        self._spread = spread
        self._book = []

    def market_open(self, direction, lots, sl, tp, client_order_id):
        self.opens.append(
            {"direction": direction, "lots": lots, "sl": sl, "tp": tp, "cid": client_order_id}
        )
        return self.results.pop(0) if self.results else None

    def market_close(self, client_order_id):
        self.closes.append(client_order_id)
        return {"fill_px": 3999.0, "ts": None}

    def flatten_all(self, reason):
        self.flattens.append(reason)
        self._book = []
        return True

    def positions(self):
        return list(self._book)

    def get_spread(self):
        return self._spread

    def get_tick_age_seconds(self):
        return 1.0

    def equity(self):
        return 5000.0


def position_row(client_order_id):
    rows = database.fetch(
        "SELECT state, entry_px, close_reason FROM positions WHERE client_order_id = %s",
        (client_order_id,),
    )
    return rows[0] if rows else None


def fills_row(client_order_id):
    rows = database.fetch(
        "SELECT status, kernel_reason, lots, requested_px, fill_px, slippage, fill_mode "
        "FROM fills WHERE client_order_id = %s",
        (client_order_id,),
    )
    return rows[0] if rows else None


# ===========================================================================
# INVARIANT 5 — every order passes the kernel, exactly once
# ===========================================================================


def test_submit_consults_the_kernel_exactly_once():
    kernel = SpyKernel()
    bridge = SpyBridge()
    result = Router(bridge, kernel).submit(next_signal())

    assert kernel.calls == 1, "permit() must be called exactly once per submit"
    assert result["outcome"] == router_mod.SUBMITTED


def test_a_denied_order_never_reaches_the_bridge():
    kernel = SpyKernel(Verdict(allowed=False, breaker="SPREAD_CEILING", reason="too wide"))
    bridge = SpyBridge()

    signal = next_signal()
    result = Router(bridge, kernel).submit(signal)

    assert result["outcome"] == router_mod.KERNEL_DENIED
    assert result["breaker"] == "SPREAD_CEILING"
    assert bridge.opens == [], "a denied order must not touch the bridge at all"

    status, reason, *_ = fills_row(f"NEXUS-{signal['id']}")
    assert status == "REJECTED"
    assert "SPREAD_CEILING" in reason and "too wide" in reason


def test_the_kernel_is_consulted_before_the_bridge():
    """Ordering, not just presence: permit() cannot run after a send."""
    order_of_events = []

    class OrderingKernel(SpyKernel):
        def permit(self, order):
            order_of_events.append("permit")
            return super().permit(order)

    class OrderingBridge(SpyBridge):
        def market_open(self, *a, **k):
            order_of_events.append("send")
            return super().market_open(*a, **k)

    Router(OrderingBridge(), OrderingKernel()).submit(next_signal())
    assert order_of_events == ["permit", "send"]


# ===========================================================================
# idempotency
# ===========================================================================


def test_a_duplicate_client_order_id_is_not_sent_twice():
    kernel = SpyKernel()
    bridge = SpyBridge(results=[{"fill_px": 4000.25, "ts": None}])
    router = Router(bridge, kernel)
    signal = next_signal()

    first = router.submit(signal)
    second = router.submit(signal)

    assert first["outcome"] == router_mod.SUBMITTED
    assert second["outcome"] == router_mod.DUPLICATE  # UNIQUE(client_order_id) in positions
    assert len(bridge.opens) == 1, "the second submit must not reach the bridge"
    assert kernel.calls == 1, "a duplicate is refused before the kernel is troubled"


def test_client_order_id_is_derived_from_the_signal_id():
    assert router_mod.client_order_id_for(42) == "NEXUS-42"


def test_an_unreservable_ledger_stops_the_order_dead(monkeypatch):
    """
    THE TASK 16 REGRESSION. The old flow read the ledger, then sent, then
    wrote — so a write failure left a position open and unrecorded, and the
    next submit would open a second one. Now the row is written FIRST, and a
    ledger that cannot take it means no order at all.
    """

    def unavailable(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "get_conn", unavailable)
    bridge = SpyBridge()

    result = Router(bridge, SpyKernel()).submit(next_signal())

    assert result["outcome"] == router_mod.LEDGER_UNAVAILABLE
    assert bridge.opens == [], "no ledger, no order — the bridge is never touched"


# ===========================================================================
# clamping
# ===========================================================================


def test_the_bridge_receives_the_clamped_lots_not_the_requested_lots():
    kernel = SpyKernel(Verdict(allowed=True, reason="clamped", clamped_lots=0.10))
    bridge = SpyBridge()

    result = Router(bridge, kernel).submit(next_signal(lots=0.5))

    assert bridge.opens[0]["lots"] == 0.10, "the kernel's number wins"
    assert result["lots"] == 0.10
    assert result["clamped"] is True


def test_an_unclamped_order_sends_its_requested_size():
    bridge = SpyBridge()
    result = Router(bridge, SpyKernel()).submit(next_signal(lots=0.05))

    assert bridge.opens[0]["lots"] == 0.05
    assert result["clamped"] is False


# ===========================================================================
# retry
# ===========================================================================


def test_one_retry_then_success(caplog):
    caplog.set_level(logging.INFO, logger="exec_.router")
    bridge = SpyBridge(results=[None, {"fill_px": 4000.30, "ts": None}])

    result = Router(bridge, SpyKernel(), retry_delay_seconds=0).submit(next_signal())

    assert result["outcome"] == router_mod.SUBMITTED
    assert len(bridge.opens) == 2
    assert any("retrying once" in r.getMessage() for r in caplog.records)
    assert any("filled on retry" in r.getMessage() for r in caplog.records)


def test_two_failures_give_up():
    bridge = SpyBridge(results=[None, None])
    signal = next_signal()

    result = Router(bridge, SpyKernel(), retry_delay_seconds=0).submit(signal)

    assert result["outcome"] == router_mod.BRIDGE_FAILED
    assert len(bridge.opens) == 2, "exactly one retry, not a storm"
    status, reason, *_ = fills_row(f"NEXUS-{signal['id']}")
    assert status == "REJECTED"
    assert "after one retry" in reason


def test_a_raising_bridge_is_treated_as_transient():
    class ExplodingBridge(SpyBridge):
        def market_open(self, *a, **k):
            self.opens.append(a)
            raise RuntimeError("socket died")

    bridge = ExplodingBridge()
    result = Router(bridge, SpyKernel(), retry_delay_seconds=0).submit(next_signal())

    assert result["outcome"] == router_mod.BRIDGE_FAILED
    assert len(bridge.opens) == 2


def test_the_retry_does_not_re_ask_the_kernel():
    kernel = SpyKernel()
    bridge = SpyBridge(results=[None, {"fill_px": 4000.30, "ts": None}])

    Router(bridge, kernel, retry_delay_seconds=0).submit(next_signal())

    assert kernel.calls == 1


# ===========================================================================
# slippage sign
# ===========================================================================


def test_long_slippage_is_positive_when_filled_above_the_request():
    bridge = SpyBridge(results=[{"fill_px": 4000.25, "ts": None}])
    result = Router(bridge, SpyKernel()).submit(
        next_signal(direction="LONG", prices={"entry": 4000.0, "stop": 3990.0})
    )
    assert result["slippage"] == pytest.approx(0.25), "paying up is adverse"


def test_short_slippage_is_positive_when_filled_below_the_request():
    bridge = SpyBridge(results=[{"fill_px": 3999.75, "ts": None}])
    result = Router(bridge, SpyKernel()).submit(
        next_signal(direction="SHORT", prices={"entry": 4000.0, "stop": 4010.0})
    )
    assert result["slippage"] == pytest.approx(0.25), "receiving less is adverse"


def test_a_favourable_long_fill_reads_negative():
    bridge = SpyBridge(results=[{"fill_px": 3999.90, "ts": None}])
    result = Router(bridge, SpyKernel()).submit(
        next_signal(direction="LONG", prices={"entry": 4000.0, "stop": 3990.0})
    )
    assert result["slippage"] == pytest.approx(-0.10)


# ===========================================================================
# malformed signals
# ===========================================================================


@pytest.mark.parametrize(
    "overrides",
    [
        {"lots": 0},
        {"lots": None},
        {"direction": "SIDEWAYS"},
        {"prices": {"entry": None, "stop": 3990.0}},
        {"prices": {"entry": 4000.0, "stop": None}},
        {"prices": {}},
    ],
)
def test_a_malformed_signal_is_refused_before_the_kernel(overrides):
    kernel = SpyKernel()
    bridge = SpyBridge()

    result = Router(bridge, kernel).submit(next_signal(**overrides))

    assert result["outcome"] == router_mod.INVALID_ORDER
    assert bridge.opens == []
    assert kernel.calls == 0


# ===========================================================================
# exits
# ===========================================================================


def test_close_records_a_separate_row_and_preserves_the_open_one():
    bridge = SpyBridge()
    router = Router(bridge, SpyKernel())
    signal = next_signal()
    router.submit(signal)

    cid = f"NEXUS-{signal['id']}"
    result = router.close(cid)

    assert result["outcome"] == "CLOSED"
    assert bridge.closes == [cid]
    assert fills_row(cid)[0] == "FILLED", "the opening row must survive"
    assert fills_row(f"{cid}-CLOSE")[0] == "CLOSED"


def test_flatten_all_records_every_open_position():
    bridge = SpyBridge()
    bridge._book = [{"client_order_id": "NEXUS-A1"}, {"client_order_id": "NEXUS-A2"}]

    result = Router(bridge, SpyKernel()).flatten_all("kernel halt")

    assert result["success"] is True
    assert result["closed"] == 2
    assert bridge.flattens == ["kernel halt"]


# ===========================================================================
# build_kernel — the wiring
# ===========================================================================


def test_build_kernel_wires_every_bridge_callable():
    bridge = SimBridge()
    kernel = build_kernel(bridge)

    assert kernel.policy is not None
    # Equity flows from the bridge, not from config directly.
    assert kernel.peak_equity == pytest.approx(config.ACCOUNT_SIZE)


def test_build_kernel_gives_sim_no_broker_to_reconcile_against():
    """Reconciling a simulator against itself is a tautology, not a check."""
    kernel = build_kernel(SimBridge())
    assert kernel.reconcile([{"lots": 0.5, "direction": "LONG"}], []) is True
    assert kernel.halted is False


def test_kill_file_denies_a_submit_end_to_end(monkeypatch, tmp_path):
    """Integration: KILL on disk -> kernel -> router -> REJECTED row."""
    monkeypatch.setattr(config, "KILL_FILE_PATH", str(tmp_path / "KILL"))
    (tmp_path / "KILL").write_text("stop", encoding="utf-8")

    bridge = SimBridge()
    router = Router(bridge, build_kernel(bridge))
    signal = next_signal()

    result = router.submit(signal)

    assert result["outcome"] == router_mod.KERNEL_DENIED
    assert result["breaker"] == "KILL_SWITCH"
    assert bridge.positions() == [], "nothing may have been opened"
    status, reason, *_ = fills_row(f"NEXUS-{signal['id']}")
    assert status == "REJECTED"
    assert "KILL_SWITCH" in reason


def test_a_real_submit_through_the_sim_stack_fills_and_records(monkeypatch, tmp_path):
    """The full path with no spies: SimBridge + real Kernel + Router."""
    monkeypatch.setattr(config, "KILL_FILE_PATH", str(tmp_path / "KILL"))
    bridge = SimBridge()
    router = Router(bridge, build_kernel(bridge))
    signal = next_signal(lots=0.05)

    result = router.submit(signal)

    assert result["outcome"] == router_mod.SUBMITTED, result
    assert result["fill_px"] == pytest.approx(EXPECTED_LONG_FILL)
    assert result["slippage"] == pytest.approx(EXPECTED_LONG_FILL - 4000.0)
    assert result["spread_at_send"] == config.SIM_SPREAD_USD
    assert len(bridge.positions()) == 1

    status, _, lots, requested, fill, slippage, fill_mode = fills_row(f"NEXUS-{signal['id']}")
    assert status == "FILLED"
    assert float(lots) == 0.05
    assert float(requested) == pytest.approx(4000.0)
    assert float(fill) == pytest.approx(EXPECTED_LONG_FILL)
    assert float(slippage) == pytest.approx(0.225)
    assert fill_mode == "MODELED", "PAPER stage models its fills"


def test_the_filled_but_not_recorded_window_no_longer_exists():
    """
    Task 16 shipped a CRITICAL for the case where a fill landed but the ledger
    write failed. Reserve-before-send removes the window entirely: the row is
    committed before the bridge is called, so there is no ordering in which a
    position exists and the ledger does not know. The warning is gone because
    the condition is gone.
    """
    import ast
    import inspect

    # String LITERALS, not raw source: a comment explaining why the warning is
    # gone naturally contains the words it is explaining.
    tree = ast.parse(inspect.getsource(router_mod))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not any("FILLED BUT NOT RECORDED" in text for text in literals)


def test_a_position_row_is_reserved_before_the_bridge_is_touched():
    order_of_events = []

    class OrderingBridge(SpyBridge):
        def market_open(self, *a, **k):
            order_of_events.append("send")
            return super().market_open(*a, **k)

    signal = next_signal()
    Router(OrderingBridge(), SpyKernel()).submit(signal)

    row = position_row(f"NEXUS-{signal['id']}")
    assert row is not None, "the reservation must exist"
    assert order_of_events == ["send"]


def test_a_filled_order_moves_its_row_to_open():
    signal = next_signal()
    result = Router(SpyBridge(), SpyKernel()).submit(signal)

    assert result["outcome"] == router_mod.SUBMITTED
    state, entry_px, _ = position_row(f"NEXUS-{signal['id']}")
    assert state == router_mod.STATE_OPEN
    assert float(entry_px) == pytest.approx(4000.25)


def test_a_kernel_denial_marks_the_row_failed():
    kernel = SpyKernel(Verdict(allowed=False, breaker="SPREAD_CEILING", reason="too wide"))
    signal = next_signal()

    Router(SpyBridge(), kernel).submit(signal)

    state, _, close_reason = position_row(f"NEXUS-{signal['id']}")
    assert state == router_mod.STATE_FAILED
    assert "SPREAD_CEILING" in close_reason


def test_a_bridge_failure_marks_the_row_failed():
    signal = next_signal()
    Router(SpyBridge(results=[None, None]), SpyKernel(), retry_delay_seconds=0).submit(signal)

    state, _, close_reason = position_row(f"NEXUS-{signal['id']}")
    assert state == router_mod.STATE_FAILED
    assert close_reason == "BRIDGE_FAILED"


def test_a_pod_order_with_a_non_numeric_id_still_writes_its_fills_row():
    """
    REGRESSION (found in the Task 21 live run). Pod signal ids are strings
    like "S1_FIXFADE-1786015714" and fills.signal_id is BIGINT, so the INSERT
    failed and — the fills write being best-effort — the audit row vanished
    silently for every pod trade. NULL is the honest value: a pod order has no
    row in `signals` to point at.
    """
    signal = next_signal(source="POD", pod="S1_FIXFADE", expected_edge_usd=100.0)
    signal["id"] = f"S1_FIXFADE-{os.getpid()}-{_SEQ['n']}"

    result = Router(SpyBridge(), SpyKernel()).submit(signal)

    assert result["outcome"] == router_mod.SUBMITTED
    assert result["ledger_recorded"] is True, "the pod's fill must be audited"

    rows = database.fetch(
        "SELECT signal_id, status FROM fills WHERE client_order_id = %s",
        (f"NEXUS-{signal['id']}",),
    )
    assert rows, "a fills row must exist for a pod order"
    assert rows[0][0] is None, "a pod order points at no signal row"
    assert rows[0][1] == "FILLED"


def test_a_numeric_signal_id_is_still_recorded():
    signal = next_signal()
    Router(SpyBridge(), SpyKernel()).submit(signal)
    rows = database.fetch(
        "SELECT signal_id FROM fills WHERE client_order_id = %s",
        (f"NEXUS-{signal['id']}",),
    )
    assert int(rows[0][0]) == int(signal["id"])
