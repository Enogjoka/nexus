"""
Acceptance tests for ai/price_resolver.py (Task 2) — the wall between AI
text and real prices. This is the heart of the task: the tests exist to
prove that a hallucinated price is structurally impossible to pass through,
that no malformed input escapes as an exception, and that resolved prices
obey the trade geometry exactly.

No network, no AI calls, no DB. Everything is a hand-built dict / string.
"""
import json
import random

import pytest

from ai.price_resolver import (
    Anchor,
    ResolvedSignal,
    SignalAnchors,
    build_anchor_map,
    parse_ai_output,
    resolve,
)

# A minimal, fully-valid raw signal used as the base for injection / mutation
# fuzz cases. Kept as a dict so we can corrupt individual fields cleanly.
_VALID_SIGNAL = {
    "direction": "LONG",
    "entry_anchor": "H1_EMA20",
    "entry_offset_pips": 10,
    "stop_anchor": "H1_EMA50",
    "stop_offset_pips": 0,
    "tp1_rr": 2.0,
    "tp2_rr": 3.0,
}


def _mutate(**overrides):
    """Return a JSON string of _VALID_SIGNAL with fields overridden/added."""
    d = dict(_VALID_SIGNAL)
    d.update(overrides)
    return json.dumps(d)


# --------------------------------------------------------------------------
# Happy path — exact hand-computed prices
# --------------------------------------------------------------------------


def test_happy_path_long_exact_prices():
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 3980.0}
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,
        entry_offset_pips=10.0,   # +10 pips * 0.1 = +1.0
        stop_anchor=Anchor.H1_EMA50,
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    r = resolve(sig, anchor_map, live_price=4001.0)

    assert r is not None
    # entry = 4000 + 1.0 = 4001; stop = 3980; risk = 21
    assert r.entry_price == 4001.0
    assert r.stop_price == 3980.0
    assert r.risk_per_unit == 21.0
    # tp1 = 4001 + 21*2 = 4043; tp2 = 4001 + 21*3 = 4064
    assert r.tp1_price == 4043.0
    assert r.tp2_price == 4064.0
    assert r.warnings == []


def test_happy_path_short_exact_prices():
    anchor_map = {Anchor.H4_SWING_HIGH: 4100.0, Anchor.H1_EMA20: 4110.0}
    sig = SignalAnchors(
        direction="SHORT",
        entry_anchor=Anchor.H4_SWING_HIGH,
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA20,   # 4110 > entry 4100, correct for SHORT
        stop_offset_pips=0.0,
        tp1_rr=1.5,
        tp2_rr=2.0,
    )
    r = resolve(sig, anchor_map, live_price=4100.0)

    assert r is not None
    # entry = 4100; stop = 4110; risk = 10
    assert r.entry_price == 4100.0
    assert r.stop_price == 4110.0
    assert r.risk_per_unit == 10.0
    # SHORT profit is downward: tp1 = 4100 - 10*1.5 = 4085; tp2 = 4100 - 10*2 = 4080
    assert r.tp1_price == 4085.0
    assert r.tp2_price == 4080.0
    assert r.warnings == []


# --------------------------------------------------------------------------
# FUZZ — 20+ malformed raw strings must never raise, must return None;
# a markdown-fenced VALID signal must parse.
# --------------------------------------------------------------------------

_VALID_FENCED = "```json\n" + json.dumps(_VALID_SIGNAL) + "\n```"

# Each entry: (label, raw_string, expected_is_signal)
_FUZZ_CASES = [
    ("broken_json", "{not valid json", False),
    ("prose_around_json", 'Here is the trade: {"direction": "LONG"} hope it helps!', False),
    ("unknown_anchor", _mutate(entry_anchor="H1_EMA21"), False),
    ("raw_price_only", '{"entry_price": 4100}', False),
    ("raw_price_injected_into_valid", _mutate(entry_price=4100), False),
    ("entry_offset_out_of_range", _mutate(entry_offset_pips=999), False),
    ("stop_offset_out_of_range", _mutate(stop_offset_pips=-500), False),
    ("tp1_rr_too_low", _mutate(tp1_rr=0.5), False),
    ("tp2_rr_too_high", _mutate(tp2_rr=11), False),
    ("tp2_not_greater_than_tp1", _mutate(tp1_rr=3.0, tp2_rr=2.0), False),
    ("offset_wrong_type_string", _mutate(entry_offset_pips="10"), False),
    ("direction_wrong_type_number", _mutate(direction=123), False),
    ("empty_string", "", False),
    ("whitespace_only", "   \n  ", False),
    ("literal_null", "null", False),
    ("null_field", _mutate(direction=None), False),
    ("nested_json", json.dumps({"signal": _VALID_SIGNAL}), False),
    ("json_array", "[1, 2, 3]", False),
    ("bare_number", "42", False),
    ("bool_for_float", _mutate(tp1_rr=True), False),
    ("missing_stop_anchor", json.dumps({k: v for k, v in _VALID_SIGNAL.items() if k != "stop_anchor"}), False),
    ("bad_direction_literal", _mutate(direction="BUY"), False),
    ("markdown_fenced_valid", _VALID_FENCED, True),  # <-- SHOULD pass
]


@pytest.mark.parametrize("label,raw,expected_is_signal", _FUZZ_CASES, ids=[c[0] for c in _FUZZ_CASES])
def test_fuzz_never_raises_and_rejects_garbage(label, raw, expected_is_signal):
    # No exception may escape parse_ai_output, ever.
    try:
        result = parse_ai_output(raw)
    except Exception as exc:  # noqa: BLE001 - the whole point is to prove none escape
        pytest.fail(f"parse_ai_output raised {type(exc).__name__} on case {label!r}: {exc}")

    if expected_is_signal:
        assert isinstance(result, SignalAnchors), f"case {label!r} should have parsed to a SignalAnchors"
    else:
        assert result is None, f"case {label!r} should have been rejected (None), got {result!r}"


def test_fuzz_count_at_least_20_malformed():
    malformed = [c for c in _FUZZ_CASES if c[2] is False]
    assert len(malformed) >= 20


def test_raw_price_injection_into_valid_signal_is_rejected():
    # The strongest injection: a perfectly valid signal with a raw price
    # appended. extra="forbid" must reject the whole thing.
    assert parse_ai_output(_mutate(entry_price=4100)) is None


def test_markdown_fenced_valid_parses_to_expected_fields():
    sig = parse_ai_output(_VALID_FENCED)
    assert isinstance(sig, SignalAnchors)
    assert sig.direction == "LONG"
    assert sig.entry_anchor is Anchor.H1_EMA20
    assert sig.stop_anchor is Anchor.H1_EMA50


# --------------------------------------------------------------------------
# Stop-side violations
# --------------------------------------------------------------------------


def test_long_stop_above_entry_rejected():
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 4020.0}
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,   # entry 4000
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,    # stop 4020 -> ABOVE entry, invalid for LONG
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    assert resolve(sig, anchor_map, live_price=4000.0) is None


def test_short_stop_below_entry_rejected():
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 3980.0}
    sig = SignalAnchors(
        direction="SHORT",
        entry_anchor=Anchor.H1_EMA20,   # entry 4000
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,    # stop 3980 -> BELOW entry, invalid for SHORT
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    assert resolve(sig, anchor_map, live_price=4000.0) is None


def test_stop_too_tight_5_pips_rejected():
    # 5 pips * 0.1 = 0.5 risk, below MIN_STOP_DISTANCE_PIPS (15) * 0.1 = 1.5
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 3999.5}
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,   # entry 4000
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,    # stop 3999.5 -> only 0.5 below
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    assert resolve(sig, anchor_map, live_price=4000.0) is None


# --------------------------------------------------------------------------
# Missing anchor in the map
# --------------------------------------------------------------------------


def test_entry_anchor_missing_from_map_rejected():
    anchor_map = {Anchor.H1_EMA50: 3980.0}  # no H1_EMA20
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    assert resolve(sig, anchor_map, live_price=4000.0) is None


def test_stop_anchor_missing_from_map_rejected():
    anchor_map = {Anchor.H1_EMA20: 4000.0}  # no H1_EMA50
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    assert resolve(sig, anchor_map, live_price=4000.0) is None


# --------------------------------------------------------------------------
# Drift
# --------------------------------------------------------------------------


def test_entry_drift_resolves_with_warning():
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 3980.0}
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,   # entry 4000
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    # live_price ~1% away from entry 4000 -> drift > MAX_ENTRY_DRIFT_PCT (0.5%)
    r = resolve(sig, anchor_map, live_price=3960.0)
    assert r is not None
    assert "ENTRY_DRIFT" in r.warnings
    # Prices are still fully resolved despite the warning.
    assert r.entry_price == 4000.0
    assert r.stop_price == 3980.0


def test_no_drift_no_warning():
    anchor_map = {Anchor.H1_EMA20: 4000.0, Anchor.H1_EMA50: 3980.0}
    sig = SignalAnchors(
        direction="LONG",
        entry_anchor=Anchor.H1_EMA20,
        entry_offset_pips=0.0,
        stop_anchor=Anchor.H1_EMA50,
        stop_offset_pips=0.0,
        tp1_rr=2.0,
        tp2_rr=3.0,
    )
    r = resolve(sig, anchor_map, live_price=4001.0)  # 0.025% away, under threshold
    assert r is not None
    assert r.warnings == []


# --------------------------------------------------------------------------
# build_anchor_map: structure + stale/missing handling
# --------------------------------------------------------------------------


def test_build_anchor_map_from_gold_agent_shape():
    state = {
        "1h": {
            "price": 4005.0,
            "indicators": {
                "ema20": 4001.0, "ema50": 3999.0,
                "bb_upper": 4020.0, "bb_lower": 3980.0, "bb_mid": 4000.0,
            },
            "stale": False,
        },
        "4h": {
            "price": 4005.0,
            "indicators": {"ema20": 3998.0, "ema50": 3990.0, "swing_high": 4100.0, "swing_low": 3900.0},
            "stale": False,
        },
        "1d": {
            "price": 4005.0,
            "indicators": {"ema20": 3995.0, "ema50": 3980.0, "swing_high": 4200.0, "swing_low": 3800.0},
            "stale": False,
        },
        "session": {"high": 4050.0, "low": 3950.0},
    }
    amap = build_anchor_map(state)

    # CURRENT_* all collapse to the 1h close for now
    assert amap[Anchor.CURRENT_BID] == 4005.0
    assert amap[Anchor.CURRENT_ASK] == 4005.0
    assert amap[Anchor.CURRENT_MID] == 4005.0
    assert amap[Anchor.H1_EMA20] == 4001.0
    assert amap[Anchor.H1_BB_UPPER] == 4020.0
    assert amap[Anchor.H4_SWING_HIGH] == 4100.0
    assert amap[Anchor.D1_SWING_LOW] == 3800.0
    assert amap[Anchor.SESSION_HIGH] == 4050.0
    assert amap[Anchor.SESSION_LOW] == 3950.0


def test_build_anchor_map_stale_tf_absent():
    state = {
        "1h": {"price": 4005.0, "indicators": {"ema20": 4001.0}, "stale": True},  # stale
        "4h": {"price": 4005.0, "indicators": {"ema20": 3998.0}, "stale": False},
    }
    amap = build_anchor_map(state)
    assert Anchor.H1_EMA20 not in amap       # stale tf -> absent
    assert Anchor.CURRENT_MID not in amap     # stale 1h -> no current price
    assert amap[Anchor.H4_EMA20] == 3998.0    # fresh tf still present


def test_build_anchor_map_none_and_missing_values_absent():
    state = {
        "1h": {"price": 4005.0, "indicators": {"ema20": None, "ema50": float("nan")}, "stale": False},
    }
    amap = build_anchor_map(state)
    assert Anchor.H1_EMA20 not in amap   # None -> absent
    assert Anchor.H1_EMA50 not in amap   # NaN -> absent
    assert amap[Anchor.CURRENT_MID] == 4005.0


def test_build_anchor_map_empty_state():
    assert build_anchor_map({}) == {}


# --------------------------------------------------------------------------
# Property test — random valid maps/signals; invariants must always hold.
# (hypothesis is not installed here, so this is the spec's 200-iteration
# random-loop fallback with a fixed seed for reproducibility.)
# --------------------------------------------------------------------------


def test_property_resolved_geometry_invariants():
    rng = random.Random(20260708)
    checked = 0

    for _ in range(200):
        direction = rng.choice(["LONG", "SHORT"])
        base = rng.uniform(1000.0, 5000.0)
        entry_offset = rng.uniform(-50.0, 50.0)
        distance = rng.uniform(16.0, 50.0)  # comfortably above the 15-pip floor
        stop_offset = entry_offset - distance if direction == "LONG" else entry_offset + distance

        tp1_rr = rng.uniform(1.0, 5.0)
        tp2_rr = rng.uniform(max(tp1_rr, 1.5) + 0.01, 10.0)

        # entry and stop share one anchor so the geometry is fully controlled.
        anchor_map = {Anchor.CURRENT_MID: base}
        sig = SignalAnchors(
            direction=direction,
            entry_anchor=Anchor.CURRENT_MID,
            entry_offset_pips=entry_offset,
            stop_anchor=Anchor.CURRENT_MID,
            stop_offset_pips=stop_offset,
            tp1_rr=tp1_rr,
            tp2_rr=tp2_rr,
        )

        r = resolve(sig, anchor_map, live_price=base)
        assert r is not None, f"expected resolution for {direction} d={distance}"
        checked += 1

        # risk equals |entry - stop| exactly (same float ops)
        assert r.risk_per_unit == abs(r.entry_price - r.stop_price)

        # tp1/tp2 always on the profit side of entry, tp2 further than tp1
        if direction == "LONG":
            assert r.tp1_price > r.entry_price
            assert r.tp2_price > r.tp1_price
            assert r.stop_price < r.entry_price
        else:
            assert r.tp1_price < r.entry_price
            assert r.tp2_price < r.tp1_price
            assert r.stop_price > r.entry_price

    assert checked == 200
