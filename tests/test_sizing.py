"""
Acceptance tests for Task 4: the position sizer (risk/sizing.py).

position_size() is a pure function — no DB, no state, no I/O — so every
test here runs with no external dependencies.
"""
import logging
import random

import config
from risk.sizing import position_size


# --------------------------------------------------------------------------
# hand-computed cases
# --------------------------------------------------------------------------


def test_hand_computed_010_lots_exact():
    # $5000 * 1% = $50 budget / ($5.00 * 100oz = $500/lot) = 0.10 lots exactly.
    assert position_size(5000, 1, 5.00) == 0.10


def test_hand_computed_015_lots_rounds_down():
    # budget $50 / ($3.30*100=$330/lot) = raw 0.151515... -> floors to 0.15.
    assert position_size(5000, 1, 3.30) == 0.15


def test_hand_computed_001_lots_rounds_down():
    # budget $50 / ($40*100=$4000/lot) = raw 0.0125 -> floors to 0.01.
    assert position_size(5000, 1, 40) == 0.01


def test_hand_computed_below_min_returns_zero():
    # budget $50 / ($60*100=$6000/lot) = raw 0.00833... -> below MIN_LOT -> 0.0.
    assert position_size(5000, 1, 60) == 0.0


def test_hand_computed_clamped_to_hard_cap():
    # budget $10000 / ($2*100=$200/lot) = raw 5.0 -> clamped to MAX_LOT_HARD_CAP (1.0).
    assert position_size(1_000_000, 1, 2) == 1.0


# --------------------------------------------------------------------------
# float-artifact case: a true step-boundary value must not floor down
# --------------------------------------------------------------------------


def test_float_artifact_exact_boundary_029_not_028():
    # equity=2900, risk_pct=1 -> budget=29; risk_per_unit=1 -> risk_per_lot=100.
    # raw_lots = 29/100 = 0.29 (the double), but 0.29/0.01 == 28.999999999999996
    # in floating point -- a naive floor(raw/step)*step misfires to 0.28. This
    # must return the true step-boundary value, 0.29.
    naive_raw = 29 / 100
    assert naive_raw / config.LOT_STEP != 29  # confirms the artifact is live
    assert position_size(2900, 1, 1) == 0.29


# --------------------------------------------------------------------------
# guard cases: every invalid arg -> 0.0 + a logged warning naming the arg
# --------------------------------------------------------------------------


def test_guard_equity_zero(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(0, 1, 5)
    assert result == 0.0
    assert "equity" in caplog.text


def test_guard_equity_negative(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(-5000, 1, 5)
    assert result == 0.0
    assert "equity" in caplog.text


def test_guard_risk_pct_zero(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, 0, 5)
    assert result == 0.0
    assert "risk_pct" in caplog.text


def test_guard_risk_pct_negative(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, -1, 5)
    assert result == 0.0
    assert "risk_pct" in caplog.text


def test_guard_risk_pct_above_max(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, config.MAX_RISK_PCT + 0.5, 5)
    assert result == 0.0
    assert "risk_pct" in caplog.text
    assert "MAX_RISK_PCT" in caplog.text


def test_guard_risk_per_unit_zero(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, 1, 0)
    assert result == 0.0
    assert "risk_per_unit" in caplog.text


def test_guard_risk_per_unit_negative(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, 1, -5)
    assert result == 0.0
    assert "risk_per_unit" in caplog.text


def test_guard_equity_nan(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(float("nan"), 1, 5)
    assert result == 0.0
    assert "equity" in caplog.text


def test_guard_equity_inf(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(float("inf"), 1, 5)
    assert result == 0.0
    assert "equity" in caplog.text


def test_guard_risk_pct_nan(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, float("nan"), 5)
    assert result == 0.0
    assert "risk_pct" in caplog.text


def test_guard_risk_per_unit_inf(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, 1, float("inf"))
    assert result == 0.0
    assert "risk_per_unit" in caplog.text


def test_guard_equity_bool_rejected(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(True, 1, 5)
    assert result == 0.0
    assert "bool" in caplog.text


def test_guard_risk_pct_bool_rejected(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, True, 5)
    assert result == 0.0
    assert "bool" in caplog.text


def test_guard_risk_per_unit_bool_rejected(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size(5000, 1, True)
    assert result == 0.0
    assert "bool" in caplog.text


def test_guard_non_numeric_string_rejected(caplog):
    with caplog.at_level(logging.WARNING):
        result = position_size("5000", 1, 5)
    assert result == 0.0
    assert "equity" in caplog.text


# --------------------------------------------------------------------------
# property test: 1000 seeded random valid inputs
# --------------------------------------------------------------------------


def test_property_dollar_risk_never_exceeds_budget():
    rng = random.Random(20260716)  # fixed seed -> reproducible
    for _ in range(1000):
        equity = rng.uniform(100, 2_000_000)
        risk_pct = rng.uniform(0.001, config.MAX_RISK_PCT)
        risk_per_unit = rng.uniform(0.01, 200)

        lots = position_size(equity, risk_pct, risk_per_unit)

        budget = equity * (risk_pct / 100)
        realized_risk = lots * risk_per_unit * config.CONTRACT_SIZE_OZ
        assert realized_risk <= budget + 1e-9, (
            f"dollar risk {realized_risk} exceeded budget {budget} "
            f"(equity={equity}, risk_pct={risk_pct}, risk_per_unit={risk_per_unit})"
        )

        # lots must be a multiple of LOT_STEP within tight tolerance.
        step_ratio = lots / config.LOT_STEP
        assert abs(step_ratio - round(step_ratio)) < 1e-6, (
            f"lots {lots} is not a multiple of LOT_STEP {config.LOT_STEP}"
        )

        assert 0.0 <= lots <= config.MAX_LOT_HARD_CAP
