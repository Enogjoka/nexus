"""
NEXUS price resolver — the wall between AI text output and real prices.

This module enforces INVARIANT 2: the AI never emits raw prices or raw
orders. It emits an *anchor name* (a member of the fixed `Anchor` enum) plus
bounded pip offsets. This module resolves those anchors against live market
state to produce concrete prices, OR rejects. A hallucinated price is
structurally impossible to pass through because:

  * `SignalAnchors` forbids extra keys (`extra="forbid"`) — a stray
    `{"entry_price": 4100}` is rejected outright, never read.
  * Numeric fields are strict — a string like "4100" is NOT coerced to a
    float; it is rejected.
  * `entry_anchor` / `stop_anchor` must be members of `Anchor`. An unknown
    or invented anchor name is rejected. There is no fuzzy matching and no
    "closest anchor" — reject means reject.
  * The only numbers the AI supplies are bounded pip offsets and R:R
    multiples, which are turned into prices *here*, from real anchor
    values, not from anything the AI typed.

`None` everywhere means WAIT (do not trade). No function in this module
raises on bad input; every rejection logs a one-line reason first.

SAFETY: correctness beats cleverness. Do not add repair logic, fuzzy
matching, extra anchors, or a try/except that swallows without logging.
After this task merges, this file is UNTOUCHABLE (see CLAUDE.md).
"""
import json
import logging
import math
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

import config

logger = logging.getLogger(__name__)


class Anchor(str, Enum):
    """
    The complete, closed set of price anchors the AI may reference. Adding a
    member here is a deliberate contract change, not a convenience — the
    whole point is that this set is fixed and small.
    """

    CURRENT_BID = "CURRENT_BID"
    CURRENT_ASK = "CURRENT_ASK"
    CURRENT_MID = "CURRENT_MID"
    H1_EMA20 = "H1_EMA20"
    H1_EMA50 = "H1_EMA50"
    H1_BB_UPPER = "H1_BB_UPPER"
    H1_BB_LOWER = "H1_BB_LOWER"
    H1_BB_MID = "H1_BB_MID"
    H4_EMA20 = "H4_EMA20"
    H4_EMA50 = "H4_EMA50"
    H4_SWING_HIGH = "H4_SWING_HIGH"
    H4_SWING_LOW = "H4_SWING_LOW"
    D1_EMA20 = "D1_EMA20"
    D1_EMA50 = "D1_EMA50"
    D1_SWING_HIGH = "D1_SWING_HIGH"
    D1_SWING_LOW = "D1_SWING_LOW"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"


class SignalAnchors(BaseModel):
    """
    The AI's structured output contract. Strict + extra-forbidden: this is
    the pydantic gate that makes a raw price literally unrepresentable.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    direction: Literal["LONG", "SHORT"]
    entry_anchor: Anchor
    entry_offset_pips: float = Field(ge=-50, le=50)
    stop_anchor: Anchor
    stop_offset_pips: float = Field(ge=-100, le=100)
    tp1_rr: float = Field(ge=1.0, le=5.0)
    tp2_rr: float = Field(ge=1.5, le=10.0)

    @field_validator("entry_anchor", "stop_anchor", mode="before")
    @classmethod
    def _coerce_anchor(cls, value):
        """
        Accept the canonical anchor *string* the AI emits (e.g. "H1_EMA20")
        and convert it to the enum member, which is the entire purpose of a
        str-Enum contract. Under strict mode pydantic will not coerce
        str->enum on its own, so we do it here — and `Anchor(value)` raises
        for any name that is not an exact, existing member. No fuzzy
        matching: an unknown or non-string anchor is rejected.
        """
        if isinstance(value, Anchor):
            return value
        if isinstance(value, str):
            return Anchor(value)  # ValueError on unknown member -> validation fails
        raise ValueError(f"anchor must be a known anchor name string, got {type(value).__name__}")

    @model_validator(mode="after")
    def _tp2_greater_than_tp1(self) -> "SignalAnchors":
        if not self.tp2_rr > self.tp1_rr:
            raise ValueError(f"tp2_rr ({self.tp2_rr}) must be > tp1_rr ({self.tp1_rr})")
        return self


class ResolvedSignal(BaseModel):
    """Concrete prices produced from a validated SignalAnchors + live state."""

    model_config = ConfigDict(extra="forbid")

    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    risk_per_unit: float
    warnings: List[str] = Field(default_factory=list)


def _strip_markdown_fences(raw: str) -> str:
    """
    Remove a single leading ```/```json fence and its matching trailing
    fence if present. This is the ONLY normalization applied — we do not
    hunt for JSON embedded in prose (that would be fuzzy repair). Prose
    around bare JSON therefore fails json.loads and is rejected.
    """
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    lines = lines[1:]  # drop opening fence line (``` or ```json)
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]  # drop closing fence line
    return "\n".join(lines).strip()


def parse_ai_output(raw: str) -> Optional[SignalAnchors]:
    """
    Parse raw AI text into a validated SignalAnchors, or return None (WAIT).

    Steps: strip markdown fences -> json.loads -> validate through the
    strict pydantic model. ANY failure (bad json, non-object top level,
    unknown anchor, out-of-range, extra keys, wrong types, tp2<=tp1) logs
    the exact reason and returns None. This function never raises.
    """
    if not isinstance(raw, str):
        logger.warning("parse_ai_output: input is not a string (got %s); WAIT", type(raw).__name__)
        return None

    text = _strip_markdown_fences(raw)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("parse_ai_output: JSON decode failed (%s); WAIT", exc)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "parse_ai_output: top-level JSON is %s, expected an object; WAIT", type(data).__name__
        )
        return None

    try:
        return SignalAnchors.model_validate(data)
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
        )
        logger.warning("parse_ai_output: schema validation failed [%s]; WAIT", reasons)
        return None


def _finite_number(value) -> bool:
    """True only for a real, finite float/int that is not a bool."""
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def build_anchor_map(state: dict) -> Dict[Anchor, float]:
    """
    Build the {Anchor: price} map from an AppState.market_data-shaped dict.

    `state` is the dict AppState.market_data holds: keyed by timeframe
    ("1h"/"4h"/"1d"), each value a tf_state dict with a "price" (last close),
    an "indicators" sub-dict, and a "stale" flag. A missing timeframe, a
    timeframe marked stale, or a missing/None/non-finite value simply means
    the corresponding anchor is ABSENT from the returned map. Absent ->
    resolve() returns None (WAIT). We never fabricate a value.

    Session high/low: gold_agent does not populate session high/low into
    market_data yet (a later task will). We read them from an optional
    state["session"] = {"high": float, "low": float}; until that exists,
    SESSION_HIGH / SESSION_LOW are simply absent — which is the safe default
    (a signal anchored to them resolves to WAIT, never to a wrong price).
    """
    anchor_map: Dict[Anchor, float] = {}

    def _live_tf(tf_key: str) -> Optional[dict]:
        tf = state.get(tf_key)
        if not isinstance(tf, dict):
            return None
        if tf.get("stale"):  # stale data is treated as absent, on purpose
            return None
        return tf

    def _indicators(tf_key: str) -> Optional[dict]:
        tf = _live_tf(tf_key)
        if tf is None:
            return None
        indicators = tf.get("indicators")
        return indicators if isinstance(indicators, dict) else None

    # CURRENT_BID / ASK / MID all map to the freshest last close (the 1h
    # close) for now. MT5 will provide real bid/ask in a later task; until
    # then there is no spread and all three collapse to the same value. The
    # live_price passed to resolve() is a separate, independent cross-check.
    h1 = _live_tf("1h")
    if h1 is not None and _finite_number(h1.get("price")):
        current = float(h1["price"])
        anchor_map[Anchor.CURRENT_BID] = current
        anchor_map[Anchor.CURRENT_ASK] = current
        anchor_map[Anchor.CURRENT_MID] = current

    # (anchor, timeframe key, indicator key within tf["indicators"])
    indicator_anchors = [
        (Anchor.H1_EMA20, "1h", "ema20"),
        (Anchor.H1_EMA50, "1h", "ema50"),
        (Anchor.H1_BB_UPPER, "1h", "bb_upper"),
        (Anchor.H1_BB_LOWER, "1h", "bb_lower"),
        (Anchor.H1_BB_MID, "1h", "bb_mid"),
        (Anchor.H4_EMA20, "4h", "ema20"),
        (Anchor.H4_EMA50, "4h", "ema50"),
        (Anchor.H4_SWING_HIGH, "4h", "swing_high"),
        (Anchor.H4_SWING_LOW, "4h", "swing_low"),
        (Anchor.D1_EMA20, "1d", "ema20"),
        (Anchor.D1_EMA50, "1d", "ema50"),
        (Anchor.D1_SWING_HIGH, "1d", "swing_high"),
        (Anchor.D1_SWING_LOW, "1d", "swing_low"),
    ]
    for anchor, tf_key, indicator_key in indicator_anchors:
        indicators = _indicators(tf_key)
        if indicators is None:
            continue
        value = indicators.get(indicator_key)
        if _finite_number(value):
            anchor_map[anchor] = float(value)

    session = state.get("session")
    if isinstance(session, dict):
        if _finite_number(session.get("high")):
            anchor_map[Anchor.SESSION_HIGH] = float(session["high"])
        if _finite_number(session.get("low")):
            anchor_map[Anchor.SESSION_LOW] = float(session["low"])

    return anchor_map


def resolve(
    sig: SignalAnchors, anchor_map: Dict[Anchor, float], live_price: float
) -> Optional[ResolvedSignal]:
    """
    Turn a validated SignalAnchors into concrete prices, or return None
    (WAIT). Every None path logs a one-line reason naming the offending
    field. This function does not raise on ordinary bad inputs.

    Rules:
      entry = entry_anchor_value + entry_offset_pips * PIP_SIZE
      stop  = stop_anchor_value  + stop_offset_pips  * PIP_SIZE
      LONG requires stop < entry; SHORT requires stop > entry.
      risk = abs(entry - stop); reject if risk < MIN_STOP_DISTANCE_PIPS*PIP_SIZE.
      tp1 = entry + risk * tp1_rr * dir_sign; tp2 = entry + risk * tp2_rr * dir_sign.
      Anchor absent from the map -> None.
      |entry - live_price| / live_price > MAX_ENTRY_DRIFT_PCT% -> resolve
      but append the warning "ENTRY_DRIFT".
    """
    entry_anchor_value = anchor_map.get(sig.entry_anchor)
    if entry_anchor_value is None:
        logger.warning(
            "resolve: entry_anchor %s absent from anchor map; WAIT", sig.entry_anchor.value
        )
        return None

    stop_anchor_value = anchor_map.get(sig.stop_anchor)
    if stop_anchor_value is None:
        logger.warning(
            "resolve: stop_anchor %s absent from anchor map; WAIT", sig.stop_anchor.value
        )
        return None

    dir_sign = 1.0 if sig.direction == "LONG" else -1.0

    entry_price = entry_anchor_value + sig.entry_offset_pips * config.PIP_SIZE
    stop_price = stop_anchor_value + sig.stop_offset_pips * config.PIP_SIZE

    # Stop must be on the losing side of entry for the stated direction.
    if sig.direction == "LONG" and not stop_price < entry_price:
        logger.warning(
            "resolve: LONG stop_price %.5f not below entry_price %.5f; WAIT", stop_price, entry_price
        )
        return None
    if sig.direction == "SHORT" and not stop_price > entry_price:
        logger.warning(
            "resolve: SHORT stop_price %.5f not above entry_price %.5f; WAIT", stop_price, entry_price
        )
        return None

    risk_per_unit = abs(entry_price - stop_price)
    min_distance = config.MIN_STOP_DISTANCE_PIPS * config.PIP_SIZE
    if risk_per_unit < min_distance:
        logger.warning(
            "resolve: stop too tight, risk %.5f < min %.5f (field: stop_offset_pips); WAIT",
            risk_per_unit,
            min_distance,
        )
        return None

    tp1_price = entry_price + risk_per_unit * sig.tp1_rr * dir_sign
    tp2_price = entry_price + risk_per_unit * sig.tp2_rr * dir_sign

    warnings: List[str] = []
    # The drift cross-check requires a sane, positive live price. If we
    # cannot verify entry against the market, we do not silently skip the
    # check and pass a signal through — we reject (WAIT). This is stricter
    # than the happy path but consistent with "correctness beats cleverness".
    if not _finite_number(live_price) or live_price <= 0:
        logger.warning(
            "resolve: live_price %r is not a positive finite number; cannot check drift; WAIT",
            live_price,
        )
        return None

    drift_pct = abs(entry_price - live_price) / live_price * 100.0
    if drift_pct > config.MAX_ENTRY_DRIFT_PCT:
        logger.warning(
            "resolve: entry drift %.4f%% exceeds %.4f%% (field: entry_anchor/entry_offset_pips); "
            "resolving WITH ENTRY_DRIFT warning",
            drift_pct,
            config.MAX_ENTRY_DRIFT_PCT,
        )
        warnings.append("ENTRY_DRIFT")

    return ResolvedSignal(
        entry_price=entry_price,
        stop_price=stop_price,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        risk_per_unit=risk_per_unit,
        warnings=warnings,
    )
