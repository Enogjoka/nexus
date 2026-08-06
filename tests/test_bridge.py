"""
Acceptance tests for Task 16: the broker seam.

SimBridge is a MODEL, and the property that makes it useful is determinism:
the same AppState must produce the same fill, every time. Every expected price
below is hand-computed from the constants rather than read back from the code,
so a change to the fill maths fails here instead of silently re-teaching the
learning loop what a strategy is worth.

No MetaTrader5 import is ever executed: RealMT5Bridge is only ever constructed
in the one test that asserts it refuses to construct.
"""
import math

import pytest

import config
from core.state import STATE
from data.mt5_bridge import Bridge, RealMT5Bridge, SimBridge, make_bridge

MID = 4000.0
# Hand-computed from config: spread 0.35 straddles the mid, slippage 0.05 is
# always against us.
EXPECTED_BID = 3999.825   # 4000.00 - 0.35/2
EXPECTED_ASK = 4000.175   # 4000.00 + 0.35/2
EXPECTED_LONG_FILL = 4000.225   # ask + 0.05
EXPECTED_SHORT_FILL = 3999.775  # bid - 0.05


@pytest.fixture(autouse=True)
def _clean_state():
    """AppState is a process-wide singleton; leave it as we found it."""
    saved_market = dict(STATE.market_data)
    saved_ts = STATE.last_analysis_ts
    STATE.market_data.clear()
    yield
    STATE.market_data.clear()
    STATE.market_data.update(saved_market)
    STATE.last_analysis_ts = saved_ts


@pytest.fixture
def market():
    """A live 1h price and a fresh data cycle."""
    import time

    STATE.update_market_data("1h", {"price": MID, "ts": None})
    STATE.last_analysis_ts = time.time()


# ===========================================================================
# quotes
# ===========================================================================


def test_bid_ask_straddle_the_mid_by_exactly_the_spread(market):
    tick = SimBridge().get_tick()
    assert tick["bid"] == pytest.approx(EXPECTED_BID)
    assert tick["ask"] == pytest.approx(EXPECTED_ASK)
    assert tick["ask"] - tick["bid"] == pytest.approx(config.SIM_SPREAD_USD)


def test_spread_is_exactly_the_configured_constant(market):
    """Not ask-bid: that computes to 0.35000000000000003 and reads oddly."""
    spread = SimBridge().get_spread()
    assert spread == config.SIM_SPREAD_USD
    assert spread < 0.45, "SIM must sit inside the kernel's spread ceiling"


def test_no_market_means_none_not_a_guess():
    bridge = SimBridge()
    assert bridge.get_tick() is None
    assert bridge.get_spread() is None
    assert bridge.market_open("LONG", 0.1, None, None, "x") is None


@pytest.mark.parametrize("bad", [{"price": None}, {"price": "4000"}, {"price": 0}, {"price": -1}, {}, "nonsense"])
def test_malformed_price_state_yields_no_market(bad):
    STATE.update_market_data("1h", bad)
    assert SimBridge().get_tick() is None


def test_tick_age_comes_from_the_last_data_cycle(market):
    import time

    STATE.last_analysis_ts = time.time() - 12.0
    age = SimBridge().get_tick_age_seconds()
    assert 11.0 <= age <= 14.0


def test_tick_age_is_none_when_no_cycle_has_run(market):
    STATE.last_analysis_ts = None
    assert SimBridge().get_tick_age_seconds() is None


# ===========================================================================
# fills — hand-computed, deterministic
# ===========================================================================


def test_long_fills_at_ask_plus_slippage(market):
    result = SimBridge().market_open("LONG", 0.10, 3990.0, 4020.0, "NEXUS-1")
    assert result["fill_px"] == pytest.approx(EXPECTED_LONG_FILL)


def test_short_fills_at_bid_minus_slippage(market):
    result = SimBridge().market_open("SHORT", 0.10, 4010.0, 3980.0, "NEXUS-2")
    assert result["fill_px"] == pytest.approx(EXPECTED_SHORT_FILL)


def test_slippage_is_never_favourable(market):
    """A simulator that sometimes gives a good price teaches a lie."""
    bridge = SimBridge()
    tick = bridge.get_tick()
    long_fill = bridge.market_open("LONG", 0.1, None, None, "a")["fill_px"]
    short_fill = SimBridge().market_open("SHORT", 0.1, None, None, "b")["fill_px"]

    assert long_fill > tick["ask"], "a buy must never fill below the ask"
    assert short_fill < tick["bid"], "a sell must never fill above the bid"


def test_fills_are_deterministic(market):
    """Same state, same fill — no randomness anywhere."""
    fills = [
        SimBridge().market_open("LONG", 0.10, None, None, f"det-{i}")["fill_px"]
        for i in range(20)
    ]
    assert len(set(fills)) == 1
    assert fills[0] == pytest.approx(EXPECTED_LONG_FILL)


def test_unknown_direction_is_refused(market):
    assert SimBridge().market_open("SIDEWAYS", 0.1, None, None, "x") is None


# ===========================================================================
# position book
# ===========================================================================


def test_open_position_appears_in_the_book(market):
    bridge = SimBridge()
    bridge.market_open("LONG", 0.10, None, None, "NEXUS-1")

    book = bridge.positions()
    assert len(book) == 1
    assert book[0]["direction"] == "LONG"
    assert book[0]["lots"] == 0.10
    assert book[0]["entry"] == pytest.approx(EXPECTED_LONG_FILL)


def test_round_trip_realizes_the_spread_and_slippage_as_a_loss(market):
    """
    Open LONG at ask+slip, close at bid-slip. Hand-computed:
      (3999.775 - 4000.225) * 0.10 lots * 100 oz = -4.50
    """
    bridge = SimBridge()
    bridge.market_open("LONG", 0.10, None, None, "NEXUS-1")
    result = bridge.market_close("NEXUS-1")

    assert result["fill_px"] == pytest.approx(EXPECTED_SHORT_FILL)
    assert result["pnl"] == pytest.approx(-4.50)
    assert bridge.positions() == []
    assert bridge.realized_pnl == pytest.approx(-4.50)


def test_equity_reflects_realized_and_unrealized(market):
    bridge = SimBridge()
    assert bridge.equity() == pytest.approx(config.ACCOUNT_SIZE)

    bridge.market_open("LONG", 0.10, None, None, "NEXUS-1")
    # Marked to mid: entry 4000.225 vs mid 4000.00 = -0.225 * 10 = -2.25
    assert bridge.equity() == pytest.approx(config.ACCOUNT_SIZE - 2.25)

    bridge.market_close("NEXUS-1")
    assert bridge.equity() == pytest.approx(config.ACCOUNT_SIZE - 4.50)


def test_short_profits_when_price_falls(market):
    bridge = SimBridge()
    bridge.market_open("SHORT", 0.10, None, None, "NEXUS-1")
    STATE.update_market_data("1h", {"price": 3900.0})

    book = bridge.positions()
    assert book[0]["unrealized_pnl"] > 0
    # entry 3999.775, mid 3900 -> 99.775 * 10 = 997.75
    assert book[0]["unrealized_pnl"] == pytest.approx(997.75)


def test_closing_an_unknown_position_is_refused(market):
    assert SimBridge().market_close("never-opened") is None


def test_flatten_all_empties_the_book(market):
    bridge = SimBridge()
    bridge.market_open("LONG", 0.05, None, None, "a")
    bridge.market_open("SHORT", 0.05, None, None, "b")

    assert bridge.flatten_all("test") is True
    assert bridge.positions() == []


def test_flatten_all_on_an_empty_book_succeeds(market):
    assert SimBridge().flatten_all("nothing to do") is True


def test_flatten_all_fails_honestly_with_no_market():
    """No market means positions cannot be closed — that is not a success."""
    bridge = SimBridge()
    STATE.update_market_data("1h", {"price": MID})
    bridge.market_open("LONG", 0.05, None, None, "a")
    STATE.market_data.clear()  # market vanishes

    assert bridge.flatten_all("market gone") is False


# ===========================================================================
# the real bridge — never executed, only refused
# ===========================================================================


def test_real_mt5_bridge_refuses_to_construct_on_this_platform():
    with pytest.raises(RuntimeError, match="MetaTrader5 unavailable on this platform"):
        RealMT5Bridge()


def test_real_mt5_bridge_leaves_no_partial_object():
    """
    A half-built bridge returning None from every method would be
    indistinguishable from a quiet market. It must not exist at all.
    """
    try:
        bridge = RealMT5Bridge()
    except RuntimeError:
        bridge = None
    assert bridge is None


def test_real_mt5_bridge_binds_nothing_before_the_import():
    """The import is the first statement; no attribute survives the failure."""
    import ast
    import inspect

    source = inspect.getsource(RealMT5Bridge.__init__)
    tree = ast.parse(source.strip())
    body = tree.body[0].body
    first_real = [n for n in body if not isinstance(n, ast.Expr)][0]
    assert isinstance(first_real, ast.Try), "__init__ must open with the guarded import"


# ===========================================================================
# factory
# ===========================================================================


def test_make_bridge_returns_sim_by_default():
    bridge = make_bridge()
    assert isinstance(bridge, SimBridge)


def test_make_bridge_rejects_an_unknown_kind(monkeypatch):
    """A typo must not silently boot a live box into a simulator."""
    monkeypatch.setattr(config, "BRIDGE_KIND", "SIMM")
    with pytest.raises(RuntimeError, match="unknown BRIDGE_KIND"):
        make_bridge()


def test_sim_bridge_satisfies_the_protocol():
    assert isinstance(SimBridge(), Bridge)
