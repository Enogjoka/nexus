"""
Acceptance tests for Task 3: the pre-flight validator (risk/validator.py).

Coverage:
  - one fire + one near-miss per rule (rules 0-7)
  - dual-timeframe RSI requirement (single-TF extreme does nothing)
  - regime conflict: one-TF -> warning only; both-TF -> WAIT
  - penalty stacking + confidence-floor WAIT
  - ENTRY_DRIFT hard stop regardless of everything else
  - missing utc_now -> WAIT/NO_CLOCK; missing rsi -> SKIPPED_NO_DATA, no crash
  - London fix window (10:15 UTC blocks, 10:31 UTC does not)
  - grade can never rise; confidence can never increase
  - validator_log: a full call writes exactly 8 rows (rule 0 + rules 1-7)
  - DB-down path: Verdict still returned with VALIDATOR_LOG_UNAVAILABLE

The two DB tests talk to nexus_dev and are skipped (not failed) when
DATABASE_URL is unset. Every other test runs without a live Postgres — the
validator persists its log best-effort and returns a Verdict regardless
(INVARIANT 6), so the pure-logic assertions never depend on the DB.
"""
from datetime import datetime, timezone

import pytest

import config
from ai.price_resolver import ResolvedSignal, SignalAnchors
from core import database
from risk.validator import Verdict, validate

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

# All test rows use a TST-prefixed symbol so the cleanup fixture can purge them.
_TEST_SYMBOL = "TST_VALIDATOR"


@pytest.fixture(autouse=True)
def _cleanup_validator_log():
    yield
    if config.DATABASE_URL:
        database.execute("DELETE FROM validator_log WHERE symbol LIKE 'TST%'")


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def make_sig(direction="LONG", tp1_rr=1.5, tp2_rr=3.0):
    """A structurally valid SignalAnchors. Anchor values are irrelevant to the
    validator (it does not resolve prices), so any valid anchors will do."""
    return SignalAnchors(
        direction=direction,
        entry_anchor="H1_EMA20",
        entry_offset_pips=0.0,
        stop_anchor="H1_EMA50",
        stop_offset_pips=0.0,
        tp1_rr=tp1_rr,
        tp2_rr=tp2_rr,
    )


def make_resolved(warnings=None):
    return ResolvedSignal(
        entry_price=4100.0,
        stop_price=4090.0,
        tp1_price=4110.0,
        tp2_price=4130.0,
        risk_per_unit=10.0,
        warnings=list(warnings or []),
    )


def make_ctx(**overrides):
    """A fully-populated, non-blocking context (LONG-clean by default):
    08:00 UTC (far from any fix), neutral RSI, non-conflicting/non-volatile
    regime, RISK_ON macro, no events."""
    ctx = {
        "utc_now": datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        "rsi": {"h1": 50.0, "h4": 50.0},
        "regime": {"h4": "TREND_UP", "d1": "RANGE"},
        "macro_regime": "RISK_ON",
        "upcoming_events": [],
        "symbol": _TEST_SYMBOL,
    }
    ctx.update(overrides)
    return ctx


# --------------------------------------------------------------------------
# RULE 0 — ENTRY_DRIFT hard stop
# --------------------------------------------------------------------------


def test_rule0_entry_drift_forces_wait_regardless():
    # Everything else is clean; ENTRY_DRIFT alone must force WAIT.
    v = validate(make_sig(), make_resolved(["ENTRY_DRIFT"]), "A+", 90, make_ctx())
    assert v.action == "WAIT"
    assert "RULE0_ENTRY_DRIFT" in v.rules_fired


def test_rule0_no_drift_does_not_block():
    v = validate(make_sig(), make_resolved([]), "A+", 90, make_ctx())
    assert v.action == "PASS"
    assert "RULE0_ENTRY_DRIFT" not in v.rules_fired


# --------------------------------------------------------------------------
# RULE 1 — event block + London fix window
# --------------------------------------------------------------------------


def test_rule1_high_impact_event_within_window_waits():
    ctx = make_ctx(upcoming_events=[{"minutes_until": 15, "impact": "HIGH"}])
    v = validate(make_sig(), make_resolved(), "A", 80, ctx)
    assert v.action == "WAIT"
    assert "RULE1_EVENT_BLOCK" in v.rules_fired


def test_rule1_high_impact_event_outside_window_no_block():
    # 31 minutes out > EVENT_BLOCK_MINUTES (30) -> no block.
    ctx = make_ctx(upcoming_events=[{"minutes_until": 31, "impact": "HIGH"}])
    v = validate(make_sig(), make_resolved(), "A", 80, ctx)
    assert v.action == "PASS"
    assert "RULE1_EVENT_BLOCK" not in v.rules_fired


def test_rule1_low_impact_event_never_blocks():
    ctx = make_ctx(upcoming_events=[{"minutes_until": 5, "impact": "LOW"}])
    v = validate(make_sig(), make_resolved(), "A", 80, ctx)
    assert v.action == "PASS"


def test_rule1_fix_window_1015_utc_waits():
    ctx = make_ctx(utc_now=datetime(2026, 7, 16, 10, 15, tzinfo=timezone.utc))
    v = validate(make_sig(), make_resolved(), "A", 80, ctx)
    assert v.action == "WAIT"
    assert "RULE1_EVENT_BLOCK" in v.rules_fired


def test_rule1_fix_window_1031_utc_no_block():
    # 10:31 is *after* the 10:30 fix -> no longer inside the pre-fix window.
    ctx = make_ctx(utc_now=datetime(2026, 7, 16, 10, 31, tzinfo=timezone.utc))
    v = validate(make_sig(), make_resolved(), "A", 80, ctx)
    assert v.action == "PASS"
    assert "RULE1_EVENT_BLOCK" not in v.rules_fired


# --------------------------------------------------------------------------
# RULE 2 — dual-timeframe, direction-aware RSI extreme
# --------------------------------------------------------------------------


def test_rule2_dual_tf_extreme_downgrades_long():
    ctx = make_ctx(rsi={"h1": 80.0, "h4": 72.0})  # both past 78 / 70
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, ctx)
    assert "RULE2_RSI_DOWNGRADE" in v.rules_fired
    assert v.grade == "A"  # A+ -> A
    assert v.confidence == 60.0  # 70 - 10


def test_rule2_single_tf_extreme_does_nothing():
    # H1 RSI 85 alone (H4 neutral) must NOT downgrade — the dual-TF gate.
    ctx = make_ctx(rsi={"h1": 85.0, "h4": 50.0})
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, ctx)
    assert "RULE2_RSI_DOWNGRADE" not in v.rules_fired
    assert v.grade == "A+"
    assert v.confidence == 70.0


def test_rule2_short_mirrored_extreme_downgrades():
    ctx = make_ctx(
        rsi={"h1": 20.0, "h4": 25.0},  # both below 22 / 30
        regime={"h4": "TREND_DOWN", "d1": "RANGE"},  # clean for SHORT
    )
    v = validate(make_sig("SHORT"), make_resolved(), "A", 70, ctx)
    assert "RULE2_RSI_DOWNGRADE" in v.rules_fired
    assert v.grade == "B"  # A -> B


# --------------------------------------------------------------------------
# RULE 3 — regime conflict
# --------------------------------------------------------------------------


def test_rule3_both_tf_conflict_waits():
    ctx = make_ctx(regime={"h4": "TREND_DOWN", "d1": "TREND_DOWN"})
    v = validate(make_sig("LONG"), make_resolved(), "A", 80, ctx)
    assert v.action == "WAIT"
    assert "RULE3_REGIME_CONFLICT" in v.rules_fired


def test_rule3_single_tf_conflict_warns_only():
    ctx = make_ctx(regime={"h4": "TREND_DOWN", "d1": "RANGE"})
    v = validate(make_sig("LONG"), make_resolved(), "A", 80, ctx)
    assert v.action == "PASS"  # warning only, no block
    assert "RULE3_REGIME_WARN" in v.rules_fired
    assert "RULE3_REGIME_CONFLICT" not in v.rules_fired


# --------------------------------------------------------------------------
# RULE 4 — volatile cap
# --------------------------------------------------------------------------


def test_rule4_volatile_caps_grade_and_penalizes():
    ctx = make_ctx(regime={"h4": "VOLATILE", "d1": "RANGE"})
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, ctx)
    assert "RULE4_VOLATILE_CAP" in v.rules_fired
    assert v.grade == "B"  # capped at B
    assert v.confidence == 65.0  # 70 - VOLATILE_PENALTY (5)


def test_rule4_non_volatile_no_action():
    ctx = make_ctx(regime={"h4": "TREND_UP", "d1": "RANGE"})
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, ctx)
    assert "RULE4_VOLATILE_CAP" not in v.rules_fired
    assert v.grade == "A+"


# --------------------------------------------------------------------------
# RULE 5 — macro divergence (short the safe-haven in risk-off)
# --------------------------------------------------------------------------


def test_rule5_short_in_risk_off_penalized():
    ctx = make_ctx(macro_regime="RISK_OFF", regime={"h4": "TREND_DOWN", "d1": "RANGE"})
    v = validate(make_sig("SHORT"), make_resolved(), "A+", 70, ctx)
    assert "RULE5_MACRO_DIVERGENCE" in v.rules_fired
    assert v.grade == "A"  # A+ capped to A
    assert v.confidence == 62.0  # 70 - MACRO_DIVERGENCE_PENALTY (8)


def test_rule5_long_in_risk_off_no_action():
    ctx = make_ctx(macro_regime="RISK_OFF")  # LONG -> rule does not apply
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, ctx)
    assert "RULE5_MACRO_DIVERGENCE" not in v.rules_fired
    assert v.confidence == 70.0


# --------------------------------------------------------------------------
# RULE 6 — R:R floor / warn
# --------------------------------------------------------------------------


def test_rule6_tp1_below_floor_waits():
    v = validate(make_sig("LONG", tp1_rr=1.1, tp2_rr=1.6), make_resolved(), "A", 80, make_ctx())
    assert v.action == "WAIT"
    assert "RULE6_RR_FLOOR" in v.rules_fired


def test_rule6_tp2_below_warn_is_warning_only():
    v = validate(make_sig("LONG", tp1_rr=1.3, tp2_rr=1.8), make_resolved(), "A", 80, make_ctx())
    assert v.action == "PASS"
    assert "RULE6_RR_WARN" in v.rules_fired
    assert "RULE6_RR_FLOOR" not in v.rules_fired


def test_rule6_healthy_rr_no_action():
    v = validate(make_sig("LONG", tp1_rr=1.5, tp2_rr=3.0), make_resolved(), "A", 80, make_ctx())
    assert "RULE6_RR_FLOOR" not in v.rules_fired
    assert "RULE6_RR_WARN" not in v.rules_fired


# --------------------------------------------------------------------------
# RULE 7 — confidence floor
# --------------------------------------------------------------------------


def test_rule7_confidence_below_floor_waits():
    v = validate(make_sig("LONG"), make_resolved(), "A", 30, make_ctx())
    assert v.action == "WAIT"
    assert "RULE7_CONFIDENCE_FLOOR" in v.rules_fired


def test_rule7_confidence_above_floor_passes():
    v = validate(make_sig("LONG"), make_resolved(), "A", 45, make_ctx())
    assert v.action == "PASS"
    assert "RULE7_CONFIDENCE_FLOOR" not in v.rules_fired


# --------------------------------------------------------------------------
# stacking + invariants
# --------------------------------------------------------------------------


def test_penalties_stack_then_confidence_floor_waits():
    # SHORT, A+, 60: RSI extreme (-10, downgrade), VOLATILE (-5, cap B),
    # macro divergence (-8, cap A). 60-23 = 37 < 40 -> RULE 7 WAIT. Grade B.
    ctx = make_ctx(
        rsi={"h1": 20.0, "h4": 25.0},
        regime={"h4": "VOLATILE", "d1": "RANGE"},
        macro_regime="RISK_OFF",
    )
    v = validate(make_sig("SHORT"), make_resolved(), "A+", 60, ctx)
    assert v.confidence == 37.0
    assert v.grade == "B"
    assert v.action == "WAIT"
    for token in ("RULE2_RSI_DOWNGRADE", "RULE4_VOLATILE_CAP",
                  "RULE5_MACRO_DIVERGENCE", "RULE7_CONFIDENCE_FLOOR"):
        assert token in v.rules_fired


def test_grade_never_upgrades_on_clean_pass():
    # Feed B through a clean pass -> still B (never rises).
    v = validate(make_sig("LONG"), make_resolved(), "B", 70, make_ctx())
    assert v.action == "PASS"
    assert v.grade == "B"
    assert v.confidence == 70.0


def test_clean_top_grade_stays_top():
    v = validate(make_sig("LONG"), make_resolved(), "A+", 70, make_ctx())
    assert v.grade == "A+"
    assert v.action == "PASS"
    assert v.rules_fired == []


# --------------------------------------------------------------------------
# clock + skip semantics
# --------------------------------------------------------------------------


def test_missing_utc_now_is_no_clock_wait():
    ctx = make_ctx()
    ctx.pop("utc_now")
    v = validate(make_sig(), make_resolved(), "A", 90, ctx)
    assert v.action == "WAIT"
    assert "RULE1_NO_CLOCK" in v.rules_fired
    assert any("NO_CLOCK" in r for r in v.reasons)


def test_none_utc_now_is_no_clock_wait():
    v = validate(make_sig(), make_resolved(), "A", 90, make_ctx(utc_now=None))
    assert v.action == "WAIT"
    assert "RULE1_NO_CLOCK" in v.rules_fired


def test_missing_rsi_is_skipped_no_data_no_crash():
    ctx = make_ctx()
    ctx.pop("rsi")
    v = validate(make_sig("LONG"), make_resolved(), "A", 70, ctx)
    assert isinstance(v, Verdict)
    assert v.action == "PASS"  # skip takes no action
    assert any("RULE2" in r and "SKIPPED" in r for r in v.reasons)


def test_missing_regime_and_macro_skip_cleanly():
    ctx = make_ctx()
    ctx.pop("regime")
    ctx.pop("macro_regime")
    v = validate(make_sig("LONG"), make_resolved(), "A", 70, ctx)
    assert v.action == "PASS"
    assert any("RULE3" in r and "SKIPPED" in r for r in v.reasons)
    assert any("RULE4" in r and "SKIPPED" in r for r in v.reasons)
    assert any("RULE5" in r and "SKIPPED" in r for r in v.reasons)


# --------------------------------------------------------------------------
# audit trail (DB)
# --------------------------------------------------------------------------


@requires_db
def test_validator_log_writes_exactly_eight_rows():
    symbol = "TST_VAL_8ROWS"
    v = validate(make_sig("LONG"), make_resolved(), "A", 70, make_ctx(symbol=symbol))
    assert isinstance(v, Verdict)
    assert "VALIDATOR_LOG_UNAVAILABLE" not in v.reasons

    rows = database.fetch(
        "SELECT rule_name, rule_result FROM validator_log WHERE symbol = %s", (symbol,)
    )
    assert len(rows) == 8
    assert {r[0] for r in rows} == {
        "RULE0_ENTRY_DRIFT",
        "RULE1_EVENT_BLOCK",
        "RULE2_RSI_EXTREME",
        "RULE3_REGIME_CONFLICT",
        "RULE4_VOLATILE_CAP",
        "RULE5_MACRO_DIVERGENCE",
        "RULE6_RR_FLOOR",
        "RULE7_CONFIDENCE_FLOOR",
    }


def test_db_down_still_returns_verdict(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "get_conn", boom)
    v = validate(make_sig("LONG"), make_resolved(), "A", 70, make_ctx())
    assert isinstance(v, Verdict)
    assert v.action == "PASS"  # evaluation completes despite no audit sink
    assert "VALIDATOR_LOG_UNAVAILABLE" in v.reasons
