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
    assert second["outcome"] == router_mod.DUPLICATE
    assert len(bridge.opens) == 1, "the second submit must not reach the bridge"
    assert kernel.calls == 1, "a duplicate is refused before the kernel is troubled"


def test_client_order_id_is_derived_from_the_signal_id():
    assert router_mod.client_order_id_for(42) == "NEXUS-42"


def test_an_unreadable_ledger_fails_closed(monkeypatch):
    """A database blip must not become a doubled position."""

    def unavailable(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "fetch", unavailable)
    bridge = SpyBridge()

    result = Router(bridge, SpyKernel()).submit(next_signal())

    assert result["outcome"] == router_mod.DUPLICATE
    assert bridge.opens == []


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


def test_a_fill_that_cannot_be_recorded_is_flagged_loudly(monkeypatch, caplog):
    """
    The position is open but the ledger does not know, so idempotency is no
    longer protecting it. That must be impossible to miss.
    """
    caplog.set_level(logging.INFO, logger="exec_.router")

    def unavailable(*a, **k):
        raise RuntimeError("disk full")

    # The ledger READ succeeds (no prior row) but the WRITE fails — the only
    # ordering that gets a real fill past an unwritable ledger.
    monkeypatch.setattr(database, "fetch", lambda *a, **k: [])
    monkeypatch.setattr(database, "get_conn", unavailable)
    bridge = SpyBridge()

    result = Router(bridge, SpyKernel()).submit(next_signal())

    assert result["outcome"] == router_mod.SUBMITTED
    assert len(bridge.opens) == 1
    assert result["ledger_recorded"] is False
    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert criticals and "FILLED BUT NOT RECORDED" in criticals[0].getMessage()


def test_a_recorded_fill_reports_the_ledger_wrote():
    result = Router(SpyBridge(), SpyKernel()).submit(next_signal())
    assert result["ledger_recorded"] is True
