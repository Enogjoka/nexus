"""
Acceptance tests for Task 19: S1, S2, the shared VWAP, and the replay judge.

Every expected number is hand-computed from the config constants rather than
read back from the code, so a change to the fill maths or the cost arithmetic
fails HERE instead of quietly re-teaching the replay harness what a strategy
is worth.

No randomness anywhere: the "fuzz" and "seeded property" sweeps below are
fixed grids and fixed lists. A backtest with a random seed is a backtest that
cannot be reproduced when it matters.
"""
import math
from datetime import datetime, timedelta, timezone

import pytest

import config
from pods.s1_fixfade import S1FixFade, in_fix_window
from pods.s2_vwapsnap import S2VwapSnap
from pods.vwap import MIN_BARS, SessionVWAP, session_of
from tests import replay as replay_mod

UTC = timezone.utc


def at(hour, minute=0, day=5):
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def tick(mid, ts=None):
    spread = config.SIM_SPREAD_USD
    ts = ts or at(15, 0)
    return {"ts": ts, "mid": mid, "bid": mid - spread / 2, "ask": mid + spread / 2}


def bar(ts, high, low, close, volume=100, open_=None):
    return {
        "ts": ts,
        "open": close if open_ is None else open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


# ===========================================================================
# SessionVWAP
# ===========================================================================


def test_hand_computed_three_bar_vwap_and_stdev():
    """
    typicals (H+L+C)/3:  95, 105, 115     volumes: 10, 20, 30
    vwap  = (95*10 + 105*20 + 115*30) / 60 = 6500/60 = 108.3333...
    mean  = 105
    var   = ((-10)^2 + 0 + 10^2)/3 = 66.6667   (population)
    stdev = 8.16497
    """
    tracker = SessionVWAP()
    tracker.update(bar(at(12), 100, 90, 95, volume=10), "OVERLAP")
    tracker.update(bar(at(13), 110, 100, 105, volume=20), "OVERLAP")
    tracker.update(bar(at(14), 120, 110, 115, volume=30), "OVERLAP")

    assert tracker.bars_seen == 3
    assert tracker.vwap() == pytest.approx(6500 / 60)
    assert tracker.stdev() == pytest.approx(math.sqrt(200 / 3))


def test_fewer_than_three_bars_is_not_a_session():
    tracker = SessionVWAP()
    for i in range(MIN_BARS - 1):
        tracker.update(bar(at(12 + i), 100, 90, 95, volume=10), "OVERLAP")
        assert tracker.vwap() is None
        assert tracker.stdev() is None


def test_zero_and_missing_volume_fall_back_to_equal_weight():
    """A session with no usable volume degrades to a plain mean, not to None."""
    tracker = SessionVWAP()
    tracker.update(bar(at(12), 100, 90, 95, volume=0), "OVERLAP")
    tracker.update(bar(at(13), 110, 100, 105, volume=None), "OVERLAP")
    tracker.update(bar(at(14), 120, 110, 115, volume=0), "OVERLAP")

    assert tracker.vwap() == pytest.approx(105.0)   # (95 + 105 + 115) / 3


def test_session_change_resets_the_window():
    tracker = SessionVWAP()
    for i in range(3):
        tracker.update(bar(at(12 + i), 100, 90, 95, volume=10), "OVERLAP")
    assert tracker.vwap() is not None

    tracker.update(bar(at(16), 200, 190, 195, volume=10), "NY")

    assert tracker.session == "NY"
    assert tracker.bars_seen == 1
    assert tracker.vwap() is None, "a fresh session starts thin again"


def test_session_label_is_derived_from_the_bar_when_not_supplied():
    tracker = SessionVWAP()
    tracker.update(bar(at(13), 100, 90, 95))
    assert tracker.session == "OVERLAP"
    tracker.update(bar(at(17), 100, 90, 95))
    assert tracker.session == "NY"


def test_a_bar_missing_prices_is_skipped_not_guessed():
    tracker = SessionVWAP()
    tracker.update({"ts": at(12), "high": 100, "low": None, "close": 95}, "OVERLAP")
    assert tracker.bars_seen == 0


@pytest.mark.parametrize(
    "hour,expected",
    [(0, "ASIA"), (6, "ASIA"), (7, "LONDON"), (11, "LONDON"), (12, "OVERLAP"),
     (15, "OVERLAP"), (16, "NY"), (20, "NY"), (21, "OFF"), (22, "OFF"), (23, "ASIA")],
)
def test_session_boundaries_mirror_the_data_agent(hour, expected):
    assert session_of(at(hour)) == expected


# ===========================================================================
# S1 FIX-FADE
# ===========================================================================


S1_STATE = {"atr_h1": 10.0, "vwap": 4000.0}


def test_s1_hand_computed_short():
    """
    atr 10, vwap 4000, mid 4020
      stretch   = +20 > 1.2*10 = 12          -> fade -> SHORT
      entry     = 4020
      stop      = 4020 + 0.8*10 = 4028
      tp        = max(4020 - 1.0*10, 4000) = 4010
      edge      = |4010-4020| * 100 * 0.01 * 0.5 = 5.00
    """
    intent = S1FixFade().evaluate(tick(4020.0), dict(S1_STATE))

    assert intent is not None
    assert intent.pod == "S1_FIXFADE"
    assert intent.direction == "SHORT"
    assert intent.entry_price == pytest.approx(4020.0)
    assert intent.stop_price == pytest.approx(4028.0)
    assert intent.tp_price == pytest.approx(4010.0)
    assert intent.lots == config.S1_LOTS
    assert intent.expected_edge_usd == pytest.approx(5.00)


def test_s1_hand_computed_long():
    """Mirrored below VWAP: mid 3980 -> LONG, stop 3972, tp 3990."""
    intent = S1FixFade().evaluate(tick(3980.0), dict(S1_STATE))

    assert intent.direction == "LONG"
    assert intent.stop_price == pytest.approx(3972.0)
    assert intent.tp_price == pytest.approx(3990.0)
    assert intent.expected_edge_usd == pytest.approx(5.00)


@pytest.mark.parametrize("hour,minute", [(14, 15), (14, 30), (15, 0), (15, 30)])
def test_s1_is_armed_inside_the_window(hour, minute):
    assert in_fix_window(at(hour, minute)) is True
    assert S1FixFade().evaluate(tick(4020.0, at(hour, minute)), dict(S1_STATE)) is not None


@pytest.mark.parametrize(
    "hour,minute", [(14, 14), (15, 31), (0, 0), (9, 0), (16, 0), (23, 59)]
)
def test_s1_is_silent_outside_the_window(hour, minute):
    """The window IS the thesis — the same stretch elsewhere is a trend."""
    assert in_fix_window(at(hour, minute)) is False
    assert S1FixFade().evaluate(tick(4020.0, at(hour, minute)), dict(S1_STATE)) is None


def test_s1_ignores_a_stretch_below_the_multiple():
    # 1.2 * 10 = 12; a 12.0 stretch is NOT strictly greater.
    assert S1FixFade().evaluate(tick(4012.0), dict(S1_STATE)) is None
    assert S1FixFade().evaluate(tick(4012.01), dict(S1_STATE)) is not None


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"atr_h1": None, "vwap": 4000.0},
        {"atr_h1": 10.0, "vwap": None},
        {"atr_h1": 0, "vwap": 4000.0},
        {"atr_h1": -1.0, "vwap": 4000.0},
        {"atr_h1": float("nan"), "vwap": 4000.0},
        {"atr_h1": 10.0, "vwap": float("inf")},
        {"atr_h1": "ten", "vwap": 4000.0},
    ],
)
def test_s1_declines_on_missing_or_broken_inputs(state):
    assert S1FixFade().evaluate(tick(4020.0), state) is None


def test_s1_declines_without_a_usable_timestamp():
    assert S1FixFade().evaluate({"mid": 4020.0, "ts": "15:00"}, dict(S1_STATE)) is None


def test_s1_take_profit_never_targets_past_vwap():
    """
    200 seeded cases (a fixed grid, no randomness). The pod is reverting TO the
    mean; asking for more than the mean is asking for the trend it just faded.
    """
    pod = S1FixFade()
    checked = 0
    for atr in [1.0, 2.5, 5.0, 7.5, 10.0, 12.5, 15.0, 20.0, 25.0, 30.0]:
        for step in range(-10, 10):
            if step == 0:
                continue
            vwap = 4000.0
            mid = vwap + step * atr * 0.4
            intent = pod.evaluate(tick(mid), {"atr_h1": atr, "vwap": vwap})
            if intent is None:
                continue
            checked += 1
            if intent.direction == "SHORT":
                assert intent.tp_price >= vwap, "SHORT tp must not target below vwap"
                assert intent.tp_price <= intent.entry_price
            else:
                assert intent.tp_price <= vwap, "LONG tp must not target above vwap"
                assert intent.tp_price >= intent.entry_price
    assert checked >= 100, f"the grid only produced {checked} intents"


def test_s1_take_profit_cap_actually_binds_when_it_can(monkeypatch):
    """
    With the shipped constants the cap can NEVER bind: the trigger needs a
    stretch > 1.2*atr while the target reaches only 1.0*atr. Raise the target
    multiple past the trigger and the cap must engage.
    """
    monkeypatch.setattr(config, "S1_TP_ATR_MULT", 3.0)
    intent = S1FixFade().evaluate(tick(4020.0), dict(S1_STATE))

    # Uncapped this would be 4020 - 30 = 3990, well past vwap.
    assert intent.tp_price == pytest.approx(4000.0)


# ===========================================================================
# S2 VWAP-SNAP
# ===========================================================================


S2_STATE = {
    "regime_h1": "RANGE",
    "atr_h1": 10.0,
    "vwap": 4000.0,
    "vwap_stdev": 5.0,
    "volume": 100.0,
    "prev_volume": 200.0,
}


def test_s2_hand_computed_short():
    """
    RANGE, vwap 4000, sigma 5, atr 10, mid 4012, volume 100 < prev 200
      displacement = +12 > 2.0*5 = 10        -> SHORT
      entry = 4012
      stop  = 4012 + 0.7*10 = 4019
      tp    = 4012 + 0.8*(4000 - 4012) = 4012 - 9.6 = 4002.4
      edge  = 9.6 * 100 * 0.01 * 0.5 = 4.80
    """
    intent = S2VwapSnap().evaluate(tick(4012.0), dict(S2_STATE))

    assert intent is not None
    assert intent.pod == "S2_VWAPSNAP"
    assert intent.direction == "SHORT"
    assert intent.stop_price == pytest.approx(4019.0)
    assert intent.tp_price == pytest.approx(4002.4)
    assert intent.expected_edge_usd == pytest.approx(4.80)


def test_s2_hand_computed_long():
    intent = S2VwapSnap().evaluate(tick(3988.0), dict(S2_STATE))

    assert intent.direction == "LONG"
    assert intent.stop_price == pytest.approx(3981.0)
    assert intent.tp_price == pytest.approx(3997.6)


@pytest.mark.parametrize("regime", ["TREND_UP", "TREND_DOWN", "VOLATILE", None, "", "range"])
def test_s2_refuses_outside_a_range_regime(regime):
    """Mean-reversion in a trend is a losing trade with a good story."""
    state = {**S2_STATE, "regime_h1": regime}
    huge = tick(4500.0)   # a colossal displacement must still not tempt it
    assert S2VwapSnap().evaluate(huge, state) is None


def test_s2_requires_falling_volume():
    """Displacement on RISING volume is a breakout, and fading it is wrong."""
    rising = {**S2_STATE, "volume": 300.0, "prev_volume": 200.0}
    assert S2VwapSnap().evaluate(tick(4012.0), rising) is None

    equal = {**S2_STATE, "volume": 200.0, "prev_volume": 200.0}
    assert S2VwapSnap().evaluate(tick(4012.0), equal) is None


def test_s2_ignores_displacement_below_the_sigma_multiple():
    # 2.0 * 5 = 10; exactly 10 is not strictly greater.
    assert S2VwapSnap().evaluate(tick(4010.0), dict(S2_STATE)) is None
    assert S2VwapSnap().evaluate(tick(4010.01), dict(S2_STATE)) is not None


def test_s2_declines_on_a_thin_session():
    """Fewer than three bars means no stdev, and the stdev IS the trigger."""
    thin = {**S2_STATE, "vwap_stdev": None}
    assert S2VwapSnap().evaluate(tick(4012.0), thin) is None


@pytest.mark.parametrize(
    "override",
    [
        {"vwap_stdev": 0.0},
        {"vwap_stdev": float("nan")},
        {"atr_h1": None},
        {"vwap": None},
        {"volume": None},
        {"prev_volume": None},
    ],
)
def test_s2_declines_on_broken_inputs(override):
    assert S2VwapSnap().evaluate(tick(4012.0), {**S2_STATE, **override}) is None


def test_s2_ignores_the_fix_window():
    """S2 has no time gate — only S1's thesis is clock-bound."""
    assert S2VwapSnap().evaluate(tick(4012.0, at(3, 0)), dict(S2_STATE)) is not None


# ===========================================================================
# neither pod may ever raise
# ===========================================================================


GARBAGE = [
    (None, None),
    ({}, {}),
    ({"ts": "nonsense"}, {"atr_h1": "nope"}),
    ({"ts": at(15), "mid": None}, {"atr_h1": 10.0, "vwap": 4000.0}),
    ({"ts": at(15), "mid": float("nan")}, S1_STATE),
    ({"ts": at(15), "mid": -5.0}, S1_STATE),
    ({"ts": at(15), "mid": 4020.0}, {"atr_h1": [1, 2], "vwap": {"a": 1}}),
    ({"ts": 12345, "mid": 4020.0}, S1_STATE),
    ({"ts": at(15), "mid": 4020.0}, {"regime_h1": "RANGE", "vwap_stdev": "wide"}),
    ({"ts": at(15), "mid": 0.0}, {**S2_STATE}),
]


@pytest.mark.parametrize("bad_tick,bad_state", GARBAGE)
def test_pods_never_raise_on_garbage(bad_tick, bad_state):
    """
    A pod that can crash the sweep can silence every other pod. The assertion
    is that the call RETURNS — an exception fails the test by propagating —
    and that whatever comes back is a usable answer rather than a half-object.
    """
    from pods.base import Intent

    for pod in (S1FixFade(), S2VwapSnap()):
        result = pod.evaluate(bad_tick, bad_state)
        assert result is None or isinstance(result, Intent)


# ===========================================================================
# the replay judge
# ===========================================================================


COST_PER_TRADE = 0.62   # (0.35 + 2*0.10)*100*0.01 + 7.0*0.01 = 0.55 + 0.07


def test_round_turn_cost_is_hand_computed():
    assert replay_mod.round_turn_cost_usd(0.01) == pytest.approx(COST_PER_TRADE)


def s1_script(final_bar, tail=None):
    """
    20 hourly bars on 2026-01-05, flat until the 15:00 UTC fix bar.

    bars 0-14 : high 4001 / low 3999 / close 4000  -> TR 2, typical 4000
    bar 15    : high 4020 / low 4000 / close 4020  -> the fix stretch
    bar 16    : supplied by the caller, to resolve the trade
    bars 17-19: flat again
    """
    bars = [bar(at(h), 4001.0, 3999.0, 4000.0) for h in range(15)]
    bars.append(bar(at(15), 4020.0, 4000.0, 4020.0, open_=4000.0))
    bars.append(final_bar)
    if tail is None:
        tail = [bar(at(h), 4001.0, 3999.0, 4000.0) for h in (17, 18, 19)]
    bars += tail
    return bars


# Hand-computed state at bar 15:
#   ATR(14) over bars 2..15 = (13 * 2 + 20) / 14 = 46/14 = 3.2857142857
#   VWAP over OVERLAP bars 12,13,14,15 (equal volume 100):
#     typicals 4000, 4000, 4000, (4020+4000+4020)/3 = 4013.3333
#     vwap = (4000*3 + 4013.3333)/4 = 4003.3333
#   stretch = 4020 - 4003.3333 = 16.6667 > 1.2 * 3.2857 = 3.9429   -> SHORT
#   entry = 4020
#   stop  = 4020 + 0.8 * 3.2857 = 4022.6286
#   tp    = max(4020 - 3.2857, 4003.3333) = 4016.7143
ATR_AT_FIX = 46 / 14
EXPECTED_ENTRY = 4020.0
EXPECTED_STOP = 4020.0 + config.S1_STOP_ATR_MULT * ATR_AT_FIX
EXPECTED_TP = 4020.0 - config.S1_TP_ATR_MULT * ATR_AT_FIX


def test_replay_finds_exactly_one_engineered_setup_and_prices_it():
    # bar 16 reaches the target without touching the stop.
    resolver = bar(at(16), 4021.0, 4010.0, 4012.0, open_=4020.0)
    result = replay_mod.replay(S1FixFade(), conn=None, bars=s1_script(resolver))

    assert result["n"] == 1, result["trades"]
    trade = result["trades"][0]

    assert trade["direction"] == "SHORT"
    assert trade["entry"] == pytest.approx(EXPECTED_ENTRY)
    assert trade["stop"] == pytest.approx(EXPECTED_STOP)
    assert trade["tp"] == pytest.approx(EXPECTED_TP)
    assert trade["outcome"] == "TP"

    # gross = (entry - exit) * oz * lots = 3.2857 * 100 * 0.01
    expected_gross = (EXPECTED_ENTRY - EXPECTED_TP) * config.CONTRACT_SIZE_OZ * config.S1_LOTS
    assert trade["gross_pnl"] == pytest.approx(expected_gross)
    assert trade["cost"] == pytest.approx(COST_PER_TRADE)
    assert trade["net_pnl"] == pytest.approx(expected_gross - COST_PER_TRADE)

    assert result["wins"] == 1
    assert result["expectancy_usd"] == pytest.approx(expected_gross - COST_PER_TRADE)


def test_a_bar_spanning_both_stop_and_target_is_counted_as_a_loss():
    """H1 cannot say which came first; assuming the good one invents an edge."""
    both = bar(at(16), 4025.0, 4010.0, 4012.0, open_=4020.0)   # > stop AND < tp
    result = replay_mod.replay(S1FixFade(), conn=None, bars=s1_script(both))

    assert result["n"] == 1
    trade = result["trades"][0]
    assert trade["outcome"] == "STOP"

    expected_gross = (EXPECTED_ENTRY - EXPECTED_STOP) * config.CONTRACT_SIZE_OZ * config.S1_LOTS
    assert expected_gross < 0
    assert trade["net_pnl"] == pytest.approx(expected_gross - COST_PER_TRADE)
    assert result["wins"] == 0
    assert result["max_consecutive_losses"] == 1


def test_costs_are_always_charged_even_on_a_winner():
    resolver = bar(at(16), 4021.0, 4010.0, 4012.0, open_=4020.0)
    result = replay_mod.replay(S1FixFade(), conn=None, bars=s1_script(resolver))

    assert result["cost_usd"] == pytest.approx(COST_PER_TRADE)
    assert result["net_usd"] < result["gross_usd"], "net must always trail gross"


def test_replay_holds_only_one_open_trade_at_a_time():
    """The 15:00 bar is the only armed bar, so a second cannot open anyway —
    but an unresolved trade must also block the sweep."""
    # Every remaining bar must stay strictly between tp (4016.71) and stop
    # (4022.63), or the trade simply resolves later — the flat 4000 bars in the
    # default tail are BELOW the short's target and would close it as a win.
    stuck = bar(at(16), 4020.5, 4019.5, 4020.0, open_=4020.0)
    tail = [bar(at(h), 4020.5, 4019.5, 4020.0, open_=4020.0) for h in (17, 18, 19)]
    result = replay_mod.replay(S1FixFade(), conn=None, bars=s1_script(stuck, tail=tail))

    assert result["n"] == 0
    assert result["open_at_end"] is True


def test_replay_over_a_flat_script_finds_nothing():
    """A pod that fires on nothing is an honest answer, not a failure."""
    flat = [bar(at(h), 4001.0, 3999.0, 4000.0) for h in range(20)]
    result = replay_mod.replay(S1FixFade(), conn=None, bars=flat)

    assert result["n"] == 0
    assert result["expectancy_usd"] is None
    assert result["win_rate"] is None
    assert "NO TRADES" in replay_mod.format_report("S1_FIXFADE", 30, result)


def test_replay_does_not_read_indicator_snapshots():
    """
    ATR must come from the bars, so a replay runs on bare candles.

    Checks the SQL the module actually contains rather than its prose — the
    docstring says the words "indicator_snapshots" precisely to explain why it
    does not query them.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(replay_mod))
    sql = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        # "SELECT ... FROM", not merely the English word "from" — the module
        # docstring contains that and is not SQL.
        and "SELECT " in node.value.upper()
        and " FROM " in node.value.upper()
    ]
    assert sql, "expected to find at least one SQL literal"
    for statement in sql:
        assert "indicator_snapshot" not in statement.lower()
    # The ATR still comes from bare candles. Task 21 added a second query for
    # S3's basis readings, so "every query hits candles" is no longer the
    # property — "the bars are the only indicator source" is.
    assert any("from candles" in statement.lower() for statement in sql)


def test_replay_report_renders_a_traded_window():
    resolver = bar(at(16), 4021.0, 4010.0, 4012.0, open_=4020.0)
    result = replay_mod.replay(S1FixFade(), conn=None, bars=s1_script(resolver))

    report = replay_mod.format_report("S1_FIXFADE", 30, result)

    assert "trades             : 1" in report
    assert "gross" in report and "costs" in report and "net" in report
    assert "VERDICT" in report


def test_build_pod_maps_names():
    assert replay_mod.build_pod("S1").name == "S1_FIXFADE"
    assert replay_mod.build_pod("s2").name == "S2_VWAPSNAP"
    with pytest.raises(SystemExit):
        replay_mod.build_pod("S9")


def test_atr_tracker_needs_a_full_period():
    tracker = replay_mod._AtrTracker(period=3)
    assert tracker.value() is None
    for _ in range(2):
        tracker.update(bar(at(0), 101.0, 99.0, 100.0))
    assert tracker.value() is None
    tracker.update(bar(at(0), 101.0, 99.0, 100.0))
    assert tracker.value() == pytest.approx(2.0)
