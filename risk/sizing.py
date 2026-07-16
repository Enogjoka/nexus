"""
NEXUS position sizer — a pure function, no state, no DB, no I/O.

Turns a dollar risk budget (equity * risk_pct%) and a per-unit risk distance
into a lot size, always rounded DOWN to the nearest LOT_STEP so the realized
dollar risk never exceeds the budget. Rounding up is never acceptable here:
it would let a trade risk more than the caller asked for.

Floats are not exact: naive `math.floor(raw_lots / LOT_STEP) * LOT_STEP`
can misfire on values that are conceptually exactly on a step boundary
(e.g. a true 0.30 arriving as the double 0.299999999999999996), silently
flooring down to 0.29 and under-sizing. This module quantizes through
`decimal.Decimal` at a much finer resolution than the step size first, to
strip that float noise, before flooring to the step. See `_floor_to_step`.

SAFETY: correctness beats cleverness. After this task merges, this file is
UNTOUCHABLE (see CLAUDE.md).
"""
import logging
import math
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

import config

logger = logging.getLogger(__name__)

# Quantize the raw float ratio to this many decimal places before flooring to
# LOT_STEP. Far finer than LOT_STEP (0.01), so it only absorbs float noise —
# it never changes a floor decision that would hold at exact precision.
_NOISE_QUANTUM = Decimal("1.00000000")


def _is_valid_number(name: str, value) -> bool:
    """
    True only for a real, finite float/int that is not a bool. Checked before
    any numeric comparison: comparisons against NaN are always False in
    Python, so `nan <= 0` silently passes — type/finiteness must be verified
    first or a NaN could slip past the value guards below.
    """
    if isinstance(value, bool):
        logger.warning("position_size: %s is a bool (%r); rejecting", name, value)
        return False
    if not isinstance(value, (int, float)):
        logger.warning("position_size: %s is not a number (%r); rejecting", name, value)
        return False
    if not math.isfinite(value):
        logger.warning("position_size: %s is not finite (%r); rejecting", name, value)
        return False
    return True


def _floor_to_step(raw_lots: float) -> Decimal:
    """Quantize away float noise, then floor to the nearest LOT_STEP."""
    raw_dec = Decimal(raw_lots).quantize(_NOISE_QUANTUM, rounding=ROUND_HALF_UP)
    step_dec = Decimal(str(config.LOT_STEP))
    steps = (raw_dec / step_dec).to_integral_value(rounding=ROUND_FLOOR)
    return steps * step_dec


def position_size(equity: float, risk_pct: float, risk_per_unit: float) -> float:
    """
    Return a lot size, rounded DOWN to LOT_STEP, such that the dollar risk
    (lots * risk_per_unit * CONTRACT_SIZE_OZ) never exceeds
    equity * (risk_pct / 100). Returns 0.0 (never trade, never round up to
    MIN_LOT) if the floored result is below MIN_LOT, or on any invalid
    input — every rejection is logged with the offending argument.
    """
    if not _is_valid_number("equity", equity):
        return 0.0
    if not _is_valid_number("risk_pct", risk_pct):
        return 0.0
    if not _is_valid_number("risk_per_unit", risk_per_unit):
        return 0.0

    if equity <= 0:
        logger.warning("position_size: equity <= 0 (%r); rejecting", equity)
        return 0.0
    if risk_pct <= 0:
        logger.warning("position_size: risk_pct <= 0 (%r); rejecting", risk_pct)
        return 0.0
    if risk_pct > config.MAX_RISK_PCT:
        logger.warning(
            "position_size: risk_pct %r exceeds MAX_RISK_PCT %r; rejecting",
            risk_pct,
            config.MAX_RISK_PCT,
        )
        return 0.0
    if risk_per_unit <= 0:
        logger.warning("position_size: risk_per_unit <= 0 (%r); rejecting", risk_per_unit)
        return 0.0

    dollar_risk_budget = equity * (risk_pct / 100)
    risk_per_lot = risk_per_unit * config.CONTRACT_SIZE_OZ
    raw_lots = dollar_risk_budget / risk_per_lot

    floored = _floor_to_step(raw_lots)

    min_lot = Decimal(str(config.MIN_LOT))
    max_cap = Decimal(str(config.MAX_LOT_HARD_CAP))

    if floored < min_lot:
        return 0.0
    if floored > max_cap:
        floored = max_cap

    return float(floored.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
