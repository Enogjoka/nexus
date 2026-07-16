"""
NEXUS pre-flight validator — the last gate before a resolved signal reaches
anything downstream.

SAFETY: correctness beats cleverness. This function only ever RESTRICTS. It
can turn a PASS into a WAIT, downgrade a grade one step, or subtract a
confidence penalty. It can never upgrade a grade, never raise confidence,
and never turn a WAIT into a PASS. Those two invariants are asserted in code
at the end of `validate()` and exercised by the tests.

Every rule decision — pass, block, downgrade, warn, skip — is written to the
validator_log table (INVARIANT: no silent decisions). If the database is
unavailable the evaluation still completes and a Verdict is still returned
(INVARIANT 6), but the Verdict's `reasons` gains "VALIDATOR_LOG_UNAVAILABLE"
so the caller knows the audit trail did not persist.

The clock is supplied by the caller as ctx["utc_now"]; there is deliberately
no datetime.now() in this module. Without a clock we cannot clear the London
fix window, so a missing/None utc_now is an immediate WAIT ("NO_CLOCK") —
we never trade blind on the fix window.

We import ONLY the two pydantic types from ai/ (SignalAnchors,
ResolvedSignal) — type contracts, no logic reuse — per CLAUDE.md.
"""
import json
import logging
import math
from datetime import timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict

import config
from ai.price_resolver import ResolvedSignal, SignalAnchors  # type contracts only
from core import database

logger = logging.getLogger(__name__)

# Symbol label for the audit rows. XAUUSD is the only instrument NEXUS trades;
# a caller may override via ctx["symbol"]. This is a log label, not a tunable
# knob, so it lives here rather than in config.py.
_DEFAULT_SYMBOL = "XAUUSD"

# Grade ladder, best -> worst. Index 0 is the best grade. "Downgrade one step"
# moves toward the end; "cap at X" clamps the index to be no better than X.
# Because every grade operation only ever moves the index toward the worse
# end, a grade can never improve.
_GRADE_LADDER: List[str] = ["A+", "A", "B"]

# rule_result values allowed in validator_log.rule_result.
_PASS = "PASS"
_WAIT = "WAIT"
_DOWNGRADE = "DOWNGRADE"
_WARN = "WARN"
_SKIPPED = "SKIPPED_NO_DATA"
_NO_CLOCK = "NO_CLOCK"


class Verdict(BaseModel):
    """The validator's decision. Strict + extra-forbidden like every NEXUS contract."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["PASS", "WAIT"]
    grade: Literal["A+", "A", "B"]
    confidence: float
    rules_fired: List[str]
    reasons: List[str]


def _is_num(value: Any) -> bool:
    """True only for a real, finite float/int that is not a bool."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _grade_index(grade: str) -> int:
    """
    Index of `grade` in the ladder. An unrecognized grade collapses to the
    worst grade (B) — the conservative choice, since it can only restrict.
    """
    try:
        return _GRADE_LADDER.index(grade)
    except ValueError:
        return len(_GRADE_LADDER) - 1


def _downgrade_one(idx: int) -> int:
    """Move one step toward the worst grade (never past it, never upward)."""
    return min(idx + 1, len(_GRADE_LADDER) - 1)


def _cap_at(idx: int, floor_grade: str) -> int:
    """Clamp so the grade is no better than floor_grade (never improves it)."""
    return max(idx, _GRADE_LADDER.index(floor_grade))


def _minutes_of_day(hour: int, minute: int, second: int = 0) -> float:
    return hour * 60.0 + minute + second / 60.0


def _in_fix_window(utc_now) -> Tuple[bool, Optional[str]]:
    """
    True if utc_now is within FIX_BLOCK_MINUTES *before* (or exactly at) any
    London fix time. Runs off the clock alone — no ctx needed. Returns
    (blocked, matched_fix_time_or_None).
    """
    now = utc_now
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    now_min = _minutes_of_day(now.hour, now.minute, now.second)
    for fix in config.LONDON_FIX_UTC:
        fh, fm = (int(part) for part in fix.split(":"))
        delta = _minutes_of_day(fh, fm) - now_min
        if 0.0 <= delta <= config.FIX_BLOCK_MINUTES:
            return True, fix
    return False, None


def _write_log(symbol: str, ts_value, rows: List[Tuple[str, str, dict]]) -> bool:
    """
    Persist one validator_log row per rule in a single transaction. Returns
    True on success, False if the DB is unavailable (logged, never raised —
    the evaluation must still return a Verdict). `ts_value` is the caller's
    utc_now (or None, in which case the row ts falls back to SQL NOW()).
    """
    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                for rule_name, rule_result, details in rows:
                    cur.execute(
                        """
                        INSERT INTO validator_log (ts, symbol, rule_name, rule_result, details)
                        VALUES (COALESCE(%s, NOW()), %s, %s, %s, %s::jsonb)
                        """,
                        (ts_value, symbol, rule_name, rule_result, json.dumps(details, default=str)),
                    )
        return True
    except Exception:
        logger.error("validator: could not write validator_log rows; continuing", exc_info=True)
        return False


def validate(
    sig: SignalAnchors,
    resolved: ResolvedSignal,
    grade: str,
    confidence: float,
    ctx: dict,
) -> Verdict:
    """
    Run the 7-rule (+ rule 0) pre-flight gate and return a Verdict.

    Every rule is evaluated even after a WAIT has been triggered, so the
    validator_log holds the full picture of one call. The single exception is
    a missing clock: without ctx["utc_now"] we cannot clear the fix window,
    so we short-circuit to an immediate WAIT/NO_CLOCK.

    See module docstring for the two hard invariants (grade never upgrades,
    confidence never increases), asserted at the bottom of this function.
    """
    symbol = ctx.get("symbol") if isinstance(ctx.get("symbol"), str) else _DEFAULT_SYMBOL
    direction = sig.direction  # "LONG" | "SHORT" — from the strict pydantic contract

    initial_grade_idx = _grade_index(grade)
    initial_confidence_clamped = max(0.0, min(100.0, float(confidence)))

    # ---- Clock precondition -------------------------------------------------
    # Required. No default-to-now(): if the caller cannot tell us the time we
    # will not evaluate the fix window blind. Immediate WAIT.
    utc_now = ctx.get("utc_now")
    if utc_now is None or not hasattr(utc_now, "hour"):
        reason = "RULE1 NO_CLOCK: ctx['utc_now'] missing/None; cannot clear fix window; WAIT"
        logger.warning("validator: %s", reason)
        log_ok = _write_log(symbol, None, [("RULE1_EVENT_BLOCK", _NO_CLOCK, {"utc_now": utc_now})])
        reasons = [reason]
        if not log_ok:
            reasons.append("VALIDATOR_LOG_UNAVAILABLE")
        return Verdict(
            action=_WAIT,
            grade=_GRADE_LADDER[initial_grade_idx],
            confidence=initial_confidence_clamped,
            rules_fired=["RULE1_NO_CLOCK"],
            reasons=reasons,
        )

    grade_idx = initial_grade_idx
    conf = initial_confidence_clamped
    action = _PASS
    rules_fired: List[str] = []
    reasons: List[str] = []
    log_rows: List[Tuple[str, str, dict]] = []

    def record(rule_name: str, result: str, details: dict) -> None:
        log_rows.append((rule_name, result, details))

    # ---- RULE 0: ENTRY_DRIFT hard stop -------------------------------------
    # Drift is a hard stop at the validator, never a pass-through.
    if "ENTRY_DRIFT" in resolved.warnings:
        action = _WAIT
        rules_fired.append("RULE0_ENTRY_DRIFT")
        reasons.append("RULE0 ENTRY_DRIFT: resolved signal drifted from live price; WAIT")
        record("RULE0_ENTRY_DRIFT", _WAIT, {"warnings": list(resolved.warnings)})
    else:
        record("RULE0_ENTRY_DRIFT", _PASS, {"warnings": list(resolved.warnings)})

    # ---- RULE 1: EVENT BLOCK + FIX WINDOW ----------------------------------
    fix_blocked, fix_time = _in_fix_window(utc_now)
    events = ctx.get("upcoming_events")
    event_data_present = isinstance(events, list)
    event_blocked = False
    blocking_event = None
    if event_data_present:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            m = ev.get("minutes_until")
            if ev.get("impact") == "HIGH" and _is_num(m) and 0 <= m <= config.EVENT_BLOCK_MINUTES:
                event_blocked = True
                blocking_event = ev
                break

    rule1_details = {
        "fix_blocked": fix_blocked,
        "fix_time": fix_time,
        "event_blocked": event_blocked,
        "blocking_event": blocking_event,
        "event_data_present": event_data_present,
    }
    if event_blocked or fix_blocked:
        action = _WAIT
        rules_fired.append("RULE1_EVENT_BLOCK")
        which = "high-impact event" if event_blocked else f"London fix {fix_time}"
        reasons.append(f"RULE1 EVENT_BLOCK: within blackout of {which}; WAIT")
        record("RULE1_EVENT_BLOCK", _WAIT, rule1_details)
    elif not event_data_present:
        # Fix window is clear, but the event calendar was absent (Task 9). We
        # log the partial evaluation rather than pretend the check was full.
        reasons.append("RULE1 EVENT_BLOCK: no upcoming_events data; fix window clear")
        record("RULE1_EVENT_BLOCK", _SKIPPED, rule1_details)
    else:
        record("RULE1_EVENT_BLOCK", _PASS, rule1_details)

    # ---- RULE 2: RSI EXTREME (dual-timeframe, direction-aware) --------------
    rsi = ctx.get("rsi")
    if not (isinstance(rsi, dict) and _is_num(rsi.get("h1")) and _is_num(rsi.get("h4"))):
        reasons.append("RULE2 RSI: SKIPPED_NO_DATA (rsi.h1/rsi.h4 absent)")
        record("RULE2_RSI_EXTREME", _SKIPPED, {"rsi": rsi})
    else:
        h1, h4 = rsi["h1"], rsi["h4"]
        if direction == "LONG":
            fired = h1 > config.RSI_EXTREME_LONG["h1"] and h4 > config.RSI_EXTREME_LONG["h4"]
        else:  # SHORT
            fired = h1 < config.RSI_EXTREME_SHORT["h1"] and h4 < config.RSI_EXTREME_SHORT["h4"]
        details = {"direction": direction, "h1": h1, "h4": h4}
        if fired:
            grade_idx = _downgrade_one(grade_idx)
            conf -= 10
            rules_fired.append("RULE2_RSI_DOWNGRADE")
            reasons.append(
                f"RULE2 RSI_EXTREME: {direction} dual-TF extreme (h1={h1}, h4={h4}); "
                "downgrade one step, confidence -10"
            )
            record("RULE2_RSI_EXTREME", _DOWNGRADE, details)
        else:
            record("RULE2_RSI_EXTREME", _PASS, details)

    # ---- RULE 3: REGIME CONFLICT -------------------------------------------
    regime = ctx.get("regime")
    if not (isinstance(regime, dict) and "h4" in regime and "d1" in regime):
        reasons.append("RULE3 REGIME: SKIPPED_NO_DATA (regime.h4/regime.d1 absent)")
        record("RULE3_REGIME_CONFLICT", _SKIPPED, {"regime": regime})
    else:
        conflict_value = "TREND_DOWN" if direction == "LONG" else "TREND_UP"
        h4_conflict = regime.get("h4") == conflict_value
        d1_conflict = regime.get("d1") == conflict_value
        details = {"direction": direction, "regime": regime, "conflict_value": conflict_value}
        if h4_conflict and d1_conflict:
            action = _WAIT
            rules_fired.append("RULE3_REGIME_CONFLICT")
            reasons.append(
                f"RULE3 REGIME_CONFLICT: {direction} against both-TF {conflict_value}; WAIT"
            )
            record("RULE3_REGIME_CONFLICT", _WAIT, details)
        elif h4_conflict or d1_conflict:
            tf = "h4" if h4_conflict else "d1"
            rules_fired.append("RULE3_REGIME_WARN")
            reasons.append(
                f"RULE3 REGIME_CONFLICT: single-TF ({tf}) {conflict_value} conflict; warning only"
            )
            record("RULE3_REGIME_CONFLICT", _WARN, details)
        else:
            record("RULE3_REGIME_CONFLICT", _PASS, details)

    # ---- RULE 4: VOLATILE CAP ----------------------------------------------
    if not (isinstance(regime, dict) and ("h4" in regime or "d1" in regime)):
        reasons.append("RULE4 VOLATILE: SKIPPED_NO_DATA (regime absent)")
        record("RULE4_VOLATILE_CAP", _SKIPPED, {"regime": regime})
    else:
        volatile = regime.get("h4") == "VOLATILE" or regime.get("d1") == "VOLATILE"
        details = {"regime": regime, "penalty": config.VOLATILE_PENALTY}
        if volatile:
            grade_idx = _cap_at(grade_idx, "B")
            conf -= config.VOLATILE_PENALTY
            rules_fired.append("RULE4_VOLATILE_CAP")
            reasons.append(
                f"RULE4 VOLATILE_CAP: VOLATILE regime; grade capped at B, "
                f"confidence -{config.VOLATILE_PENALTY}"
            )
            record("RULE4_VOLATILE_CAP", _DOWNGRADE, details)
        else:
            record("RULE4_VOLATILE_CAP", _PASS, details)

    # ---- RULE 5: MACRO DIVERGENCE ------------------------------------------
    if "macro_regime" not in ctx or ctx.get("macro_regime") is None:
        reasons.append("RULE5 MACRO: SKIPPED_NO_DATA (macro_regime absent, until Task 8)")
        record("RULE5_MACRO_DIVERGENCE", _SKIPPED, {"macro_regime": ctx.get("macro_regime")})
    else:
        macro = ctx.get("macro_regime")
        details = {"macro_regime": macro, "direction": direction, "penalty": config.MACRO_DIVERGENCE_PENALTY}
        if macro == "RISK_OFF" and direction == "SHORT":
            conf -= config.MACRO_DIVERGENCE_PENALTY
            grade_idx = _cap_at(grade_idx, "A")  # A+ capped to A
            rules_fired.append("RULE5_MACRO_DIVERGENCE")
            reasons.append(
                "RULE5 MACRO_DIVERGENCE: shorting the safe-haven in RISK_OFF; "
                f"confidence -{config.MACRO_DIVERGENCE_PENALTY}, A+ capped to A"
            )
            record("RULE5_MACRO_DIVERGENCE", _DOWNGRADE, details)
        else:
            record("RULE5_MACRO_DIVERGENCE", _PASS, details)

    # ---- RULE 6: R:R FLOOR -------------------------------------------------
    details = {"tp1_rr": sig.tp1_rr, "tp2_rr": sig.tp2_rr,
               "rr_floor_tp1": config.RR_FLOOR_TP1, "rr_warn_tp2": config.RR_WARN_TP2}
    if sig.tp1_rr < config.RR_FLOOR_TP1:
        action = _WAIT
        rules_fired.append("RULE6_RR_FLOOR")
        reasons.append(
            f"RULE6 RR_FLOOR: tp1_rr {sig.tp1_rr} < floor {config.RR_FLOOR_TP1}; WAIT"
        )
        record("RULE6_RR_FLOOR", _WAIT, details)
    elif sig.tp2_rr < config.RR_WARN_TP2:
        rules_fired.append("RULE6_RR_WARN")
        reasons.append(
            f"RULE6 RR_FLOOR: tp2_rr {sig.tp2_rr} < warn {config.RR_WARN_TP2}; warning only"
        )
        record("RULE6_RR_FLOOR", _WARN, details)
    else:
        record("RULE6_RR_FLOOR", _PASS, details)

    # ---- RULE 7: CONFIDENCE FLOOR (after every penalty above) --------------
    if conf < config.CONFIDENCE_FLOOR:
        action = _WAIT
        rules_fired.append("RULE7_CONFIDENCE_FLOOR")
        reasons.append(
            f"RULE7 CONFIDENCE_FLOOR: final confidence {conf} < floor {config.CONFIDENCE_FLOOR}; WAIT"
        )
        record("RULE7_CONFIDENCE_FLOOR", _WAIT, {"confidence": conf, "floor": config.CONFIDENCE_FLOOR})
    else:
        record("RULE7_CONFIDENCE_FLOOR", _PASS, {"confidence": conf, "floor": config.CONFIDENCE_FLOOR})

    # ---- Persist the audit trail (one row per rule) ------------------------
    if not _write_log(symbol, utc_now, log_rows):
        reasons.append("VALIDATOR_LOG_UNAVAILABLE")

    final_confidence = max(0.0, min(100.0, conf))
    final_grade = _GRADE_LADDER[grade_idx]

    # HARD INVARIANTS — this function only restricts. A grade must never end up
    # better than it started (grade_idx only ever moves toward the worse end),
    # and confidence must never end up above where it started.
    assert grade_idx >= initial_grade_idx, "INVARIANT VIOLATED: grade upgraded"
    assert final_confidence <= initial_confidence_clamped + 1e-9, (
        "INVARIANT VIOLATED: confidence increased"
    )

    return Verdict(
        action=action,
        grade=final_grade,
        confidence=final_confidence,
        rules_fired=rules_fired,
        reasons=reasons,
    )
