"""
Acceptance tests for Task 10: the fusion state vector (fusion/state_vector.py)
plus the session high/low tracking that finally lights up the SESSION_* anchors.

NO network. DB-backed tests use FAR-FUTURE (year 2099) timestamps so they sort
ahead of and never collide with real assembled rows; the autouse fixture purges
them afterward.
"""
from datetime import datetime, timezone

import pytest

import config
from ai.analysis import build_prompt
from ai.price_resolver import Anchor, build_anchor_map
from core import database
from core.state import STATE
from data import gold_agent
from fusion import state_vector as sv

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE = datetime(2099, 6, 1, 8, 0, tzinfo=timezone.utc)  # 08:00 UTC -> outside any fix window


@pytest.fixture(autouse=True)
def _cleanup_rows():
    yield
    if config.DATABASE_URL:
        database.execute(
            "DELETE FROM state_vectors WHERE ts >= %s", (datetime(2099, 1, 1, tzinfo=timezone.utc),)
        )


@pytest.fixture(autouse=True)
def _isolate_state():
    """Save/restore the AppState keys these tests write, and reset the
    module-local session tracker so one test cannot leak into another."""
    touched = ("session_hilo", "session", "session_label", "state_vector")
    saved = {key: STATE.market_data.get(key) for key in touched}
    gold_agent._session_hilo.update({"session": None, "high": None, "low": None})
    yield
    for key, value in saved.items():
        if value is None:
            STATE.market_data.pop(key, None)
        else:
            STATE.update_market_data(key, value)
    gold_agent._session_hilo.update({"session": None, "high": None, "low": None})


def make_full_state():
    """Every sensor present and healthy."""
    return {
        "1h": {
            "price": 4100.0, "regime": "TREND_UP", "stale": False,
            "indicators": {"atr14": 5.0, "rsi14": 55.0, "bb_position_pct": 62.5},
        },
        "4h": {"price": 4100.0, "regime": "TREND_UP", "indicators": {"atr14": 8.0, "rsi14": 52.0}},
        "1d": {"price": 4100.0, "regime": "RANGE", "indicators": {"atr14": 15.0, "rsi14": 50.0}},
        "dxy": {"close": 103.5, "trend": "DOWN"},
        "macro": {
            "real_yield": 1.85, "real_yield_5d_delta": 0.25,
            "curve_2s10s": 0.25, "breakeven_10y": 2.3,
        },
        "positioning": {"cot_mm_net": 124831.0, "cot_mm_net_pctile": 48.72, "comex_coverage": 20.0},
        "news_heat": 0.75,
        "upcoming_events": [
            {"minutes_until": 10.0, "impact": "LOW", "name": "noise"},
            {"minutes_until": 45.0, "impact": "HIGH", "name": "CPI"},
            {"minutes_until": 90.0, "impact": "HIGH", "name": "FOMC"},
        ],
        "session_hilo": {"high": 4130.0, "low": 4070.0},
    }


# --------------------------------------------------------------------------
# assemble: every dim exact, and NULL-tolerant when sensors are absent
# --------------------------------------------------------------------------


def test_assemble_full_state_every_dim_exact():
    vector = sv.assemble(make_full_state(), None, _FUTURE)

    assert vector["ts"] == _FUTURE
    # L0
    assert vector["price"] == 4100.0
    assert vector["atr_h1"] == 5.0
    assert vector["atr_h4"] == 8.0
    assert vector["rsi_h1"] == 55.0
    assert vector["rsi_h4"] == 52.0
    assert vector["rsi_d1"] == 50.0
    assert vector["bb_position_h1"] == 62.5
    assert vector["regime_h1"] == "TREND_UP"
    assert vector["regime_h4"] == "TREND_UP"
    assert vector["regime_d1"] == "RANGE"
    assert vector["session"] == "LONDON"           # 08:00 UTC
    assert vector["session_high"] == 4130.0
    assert vector["session_low"] == 4070.0
    # L1
    assert vector["real_yield"] == 1.85
    assert vector["real_yield_5d_delta"] == 0.25
    assert vector["curve_2s10s"] == 0.25
    assert vector["breakeven_10y"] == 2.3
    assert vector["dxy"] == 103.5
    assert vector["dxy_trend"] == "DOWN"
    # L2
    assert vector["cot_mm_net"] == 124831
    assert vector["cot_mm_net_pctile"] == 48.72
    assert vector["comex_coverage"] == 20.0
    # L4 / L5
    assert vector["news_heat"] == 0.75
    assert vector["minutes_to_next_high_event"] == 45.0  # soonest HIGH, LOW ignored
    assert vector["fix_window"] is False

    assert set(vector) == set(sv._COLUMNS)  # exactly the persisted dims, no extras


def test_assemble_partial_state_yields_nulls_without_crashing():
    partial = {"1h": {"price": 4100.0, "regime": "RANGE", "indicators": {"rsi14": 55.0}}}

    vector = sv.assemble(partial, None, _FUTURE)

    assert vector["price"] == 4100.0
    assert vector["rsi_h1"] == 55.0
    for missing in (
        "atr_h1", "atr_h4", "rsi_h4", "rsi_d1", "bb_position_h1", "regime_h4", "regime_d1",
        "session_high", "session_low", "real_yield", "real_yield_5d_delta", "curve_2s10s",
        "breakeven_10y", "dxy", "dxy_trend", "cot_mm_net", "cot_mm_net_pctile",
        "comex_coverage", "news_heat", "minutes_to_next_high_event",
    ):
        assert vector[missing] is None, f"{missing} should be NULL when its sensor is absent"


def test_assemble_empty_state_is_all_null_but_shaped():
    vector = sv.assemble({}, None, _FUTURE)
    assert set(vector) == set(sv._COLUMNS)
    assert vector["price"] is None
    assert vector["session"] == "LONDON"   # derived from the clock, not a sensor
    assert vector["fix_window"] is False


def test_assemble_rejects_non_finite_and_bool_readings():
    # A NaN/inf reading is a MISSING reading, never a persisted number.
    poisoned = {
        "1h": {"price": float("nan"), "indicators": {"rsi14": float("inf"), "atr14": True}},
        "news_heat": float("nan"),
    }
    vector = sv.assemble(poisoned, None, _FUTURE)
    assert vector["price"] is None
    assert vector["rsi_h1"] is None
    assert vector["atr_h1"] is None
    assert vector["news_heat"] is None


# --------------------------------------------------------------------------
# fix_window — hand-computed around the 10:30 UTC London fix
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour, minute, expected",
    [
        (10, 15, True),    # 15 min before the 10:30 fix -> inside the 20-min block
        (10, 10, True),    # exactly 20 min before -> inclusive boundary
        (10, 9, False),    # 21 min before -> outside
        (10, 31, False),   # 1 min AFTER the fix -> no longer a pre-fix window
        (14, 45, True),    # 15 min before the 15:00 fix
        (8, 0, False),     # nowhere near either fix
    ],
)
def test_in_fix_window_hand_computed(hour, minute, expected):
    now = datetime(2099, 6, 1, hour, minute, tzinfo=timezone.utc)
    assert sv.in_fix_window(now) is expected
    assert sv.assemble({}, None, now)["fix_window"] is expected


# --------------------------------------------------------------------------
# minutes_to_next_high_event
# --------------------------------------------------------------------------


def test_minutes_to_next_high_event_picks_soonest_high_only():
    events = [
        {"minutes_until": 5.0, "impact": "MEDIUM", "name": "ignored"},
        {"minutes_until": 80.0, "impact": "HIGH", "name": "later"},
        {"minutes_until": 30.0, "impact": "HIGH", "name": "soonest"},
    ]
    assert sv.minutes_to_next_high_event(events) == 30.0


def test_minutes_to_next_high_event_none_when_no_high_or_no_calendar():
    assert sv.minutes_to_next_high_event([{"minutes_until": 5.0, "impact": "LOW"}]) is None
    assert sv.minutes_to_next_high_event([]) is None
    assert sv.minutes_to_next_high_event(None) is None  # calendar sensor absent


# --------------------------------------------------------------------------
# hourly persist dedup
# --------------------------------------------------------------------------


@requires_db
def test_persist_is_hourly_deduped():
    state = make_full_state()
    first = sv.assemble(state, None, datetime(2099, 6, 1, 12, 5, tzinfo=timezone.utc))
    second = sv.assemble(state, None, datetime(2099, 6, 1, 12, 55, tzinfo=timezone.utc))

    with database.get_conn() as conn:
        wrote_first = sv.persist(first, conn)
        wrote_second = sv.persist(second, conn)

    assert wrote_first is True
    assert wrote_second is False  # same hour bucket -> ON CONFLICT DO NOTHING

    rows = database.fetch(
        "SELECT ts, price, cot_mm_net, fix_window FROM state_vectors WHERE ts >= %s",
        (datetime(2099, 1, 1, tzinfo=timezone.utc),),
    )
    assert len(rows) == 1
    ts, price, cot_mm_net, fix_window = rows[0]
    assert ts == datetime(2099, 6, 1, 12, 0, tzinfo=timezone.utc)  # truncated to the hour
    assert float(price) == 4100.0
    assert cot_mm_net == 124831
    assert fix_window is False


@requires_db
def test_persist_writes_nulls_for_absent_sensors():
    vector = sv.assemble({}, None, datetime(2099, 6, 2, 9, 30, tzinfo=timezone.utc))
    with database.get_conn() as conn:
        assert sv.persist(vector, conn) is True

    rows = database.fetch(
        "SELECT price, real_yield, cot_mm_net, news_heat FROM state_vectors WHERE ts = %s",
        (datetime(2099, 6, 2, 9, 0, tzinfo=timezone.utc),),
    )
    assert rows == [(None, None, None, None)]  # NULL, never fabricated


# --------------------------------------------------------------------------
# session high/low tracking (data/gold_agent.py) + the anchors it lights up
# --------------------------------------------------------------------------


def test_session_hilo_extends_then_resets_on_session_flip():
    first = gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)
    assert first == {"high": 4130.0, "low": 4070.0}

    # same session -> extremes widen
    widened = gold_agent.update_session_hilo("LONDON", 4140.0, 4080.0)
    assert widened == {"high": 4140.0, "low": 4070.0}

    # an inside bar must not shrink the running extremes
    inside = gold_agent.update_session_hilo("LONDON", 4135.0, 4075.0)
    assert inside == {"high": 4140.0, "low": 4070.0}

    # session flips -> reset to the new bar entirely
    flipped = gold_agent.update_session_hilo("NY", 4100.0, 4090.0)
    assert flipped == {"high": 4100.0, "low": 4090.0}


def test_session_hilo_ignores_non_finite_bars():
    gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)
    unchanged = gold_agent.update_session_hilo("LONDON", float("nan"), 4060.0)
    assert unchanged == {"high": 4130.0, "low": 4070.0}


def test_session_hilo_publishes_both_state_keys():
    gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)
    # the descriptive key this task's readers use...
    assert STATE.get_market_data("session_hilo") == {"high": 4130.0, "low": 4070.0}
    # ...and the shape ai/price_resolver.build_anchor_map already reads.
    assert STATE.get_market_data("session") == {"high": 4130.0, "low": 4070.0}


def test_resolver_now_resolves_session_anchors_from_gold_agent_shape():
    """Integration: the gold_agent-produced AppState shape must satisfy the
    UNTOUCHABLE resolver, which has read state["session"] = {high, low} since
    Task 2 but never had a producer until now."""
    gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)

    anchor_map = build_anchor_map(STATE.market_data)

    assert anchor_map[Anchor.SESSION_HIGH] == 4130.0
    assert anchor_map[Anchor.SESSION_LOW] == 4070.0


def test_assemble_prefers_state_session_label_over_the_clock():
    # The data agent owns the authoritative session boundaries; when it has
    # published a label, the clock-derived fallback must not override it.
    state = make_full_state()
    state["session_label"] = "ASIA"                 # data agent says ASIA...
    vector = sv.assemble(state, None, _FUTURE)      # ...while 08:00 UTC reads LONDON
    assert vector["session"] == "ASIA"


def test_assemble_falls_back_to_clock_without_a_session_label():
    # No data agent in this process -> derive from the clock rather than NULL.
    state = make_full_state()
    state.pop("session_label", None)
    assert sv.assemble(state, None, _FUTURE)["session"] == "LONDON"


def test_session_label_and_resolver_contract_coexist():
    """The label and the resolver's {high, low} live under DIFFERENT keys, so
    populating one can never clobber the other."""
    gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)
    STATE.update_market_data("session_label", "LONDON")

    # resolver contract intact...
    anchor_map = build_anchor_map(STATE.market_data)
    assert anchor_map[Anchor.SESSION_HIGH] == 4130.0
    # ...and the label is readable alongside it
    assert STATE.get_market_data("session_label") == "LONDON"
    assert "Session: LONDON" in build_prompt(STATE.market_data)


def test_state_vector_reads_the_session_hilo_gold_agent_writes():
    gold_agent.update_session_hilo("LONDON", 4130.0, 4070.0)
    vector = sv.assemble(STATE.market_data, None, _FUTURE)
    assert vector["session_high"] == 4130.0
    assert vector["session_low"] == 4070.0


# --------------------------------------------------------------------------
# prompt: MACRO & POSITIONING section
# --------------------------------------------------------------------------


def test_prompt_renders_session_label_from_its_own_key():
    state = make_full_state()
    state["session_label"] = "OVERLAP"
    state["session"] = {"high": 4130.0, "low": 4070.0}  # resolver's contract, not a label
    assert "Session: OVERLAP" in build_prompt(state)


def test_prompt_session_is_unknown_without_a_label():
    state = make_full_state()
    state["session"] = {"high": 4130.0, "low": 4070.0}  # dict must never render as a label
    assert "Session: UNKNOWN" in build_prompt(state)


def test_prompt_renders_macro_section_with_values():
    prompt = build_prompt(make_full_state())

    assert "## MACRO & POSITIONING" in prompt
    assert "1.8500" in prompt   # real_yield
    assert "0.2500" in prompt   # real_yield_5d_delta / curve_2s10s
    assert "103.500" in prompt  # dxy
    assert "48.72" in prompt    # cot_mm_net_pctile
    assert "0.7500" in prompt   # news_heat


def test_prompt_macro_section_renders_na_when_sensors_absent():
    # No macro/positioning/news keys at all -- every field must read n/a,
    # never a fabricated number, and build_prompt must not raise.
    prompt = build_prompt({"1h": {"price": 4100.0, "indicators": {}}})

    assert "## MACRO & POSITIONING" in prompt
    macro_section = prompt.split("## MACRO & POSITIONING")[1].split("##")[0]
    assert "n/a" in macro_section
    for forbidden in ("None", "nan"):
        assert forbidden not in macro_section
