"""
NEXUS analyst — the signal spine.

    market state -> prompt -> Claude -> anchors -> resolver -> validator
                 -> sizer -> signals table

This module ORCHESTRATES three UNTOUCHABLE modules and never modifies or
wraps-with-overrides any of them:

  * ai.price_resolver  (build_anchor_map, resolve, parse_ai_output, contracts)
  * risk.validator     (validate)
  * risk.sizing        (position_size)

It imports and calls them; it does not re-implement, soften, or bypass a
single gate. INVARIANT 2 is upheld end to end: the AI only ever emits anchor
names + bounded offsets (parsed by the resolver's strict pydantic contract),
never raw prices. INVARIANT 6 is upheld: the one external call (Claude) is
timeout-wrapped, try/except-guarded, and logs every failure before returning
None. A None anywhere downstream means WAIT — no trade.

Clock discipline: `datetime.now()` appears ONLY in run_scheduler(). Every
other function that needs the time takes `utc_now` as an argument, so the
whole spine is deterministic under test.
"""
import json
import logging
import math
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

import config
from ai.price_resolver import (
    ResolvedSignal,
    SignalAnchors,
    build_anchor_map,
    parse_ai_output,
    resolve,
)
from core import database
from core.state import BUS, STATE
from risk.sizing import position_size
from risk.validator import Verdict, validate

logger = logging.getLogger(__name__)

_VALID_GRADES = ("A+", "A", "B")

# Scheduler cadence bookkeeping (module-local; the scheduler is the only
# writer and it runs single-file on the bus thread).
_scheduler_lock = threading.Lock()
_last_cycle_at: Optional[datetime] = None

# UTC day the budget counter belongs to. The daily reset is driven off the
# cycle's `utc_now` argument (never datetime.now()), since call_claude may not
# hold a clock.
_budget_day: Optional[date] = None


# ==========================================================================
# a. prompt construction
# ==========================================================================


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _fmt(value, digits: int = 2) -> str:
    return f"{value:.{digits}f}" if _num(value) else "n/a"


def build_prompt(state: dict) -> str:
    """
    Render the analysis prompt from the AppState.market_data-shaped dict.
    Offers exactly config.OFFERED_ANCHORS (the 16 anchors the resolver can
    currently fill) and never references SESSION_HIGH/SESSION_LOW.
    """
    def tf(name: str) -> dict:
        d = state.get(name)
        return d if isinstance(d, dict) else {}

    def ind(name: str, key: str):
        indicators = tf(name).get("indicators")
        return indicators.get(key) if isinstance(indicators, dict) else None

    h1, h4, d1 = tf("1h"), tf("4h"), tf("1d")
    dxy = state.get("dxy") if isinstance(state.get("dxy"), dict) else {}
    macro = tf("macro")
    positioning = tf("positioning")
    session = state.get("session_label")  # "session" holds the resolver's {high, low}
    session_label = session if isinstance(session, str) else "UNKNOWN"

    lines = [
        "You are the analysis engine for NEXUS, a disciplined XAUUSD (gold) swing trader.",
        "Judge the setup from the live data below and reply per the OUTPUT CONTRACT.",
        "",
        "## LIVE GOLD DATA",
        f"Spot (H1 close): {_fmt(h1.get('price'))}",
        f"Session: {session_label}",
    ]
    for label, d in (("H1", h1), ("H4", h4), ("D1", d1)):
        i = d.get("indicators") if isinstance(d.get("indicators"), dict) else {}
        stale = " [STALE]" if d.get("stale") else ""
        lines.append(
            f"{label}{stale}: price={_fmt(d.get('price'))} RSI14={_fmt(i.get('rsi14'))} "
            f"EMA20={_fmt(i.get('ema20'))} EMA50={_fmt(i.get('ema50'))} ATR14={_fmt(i.get('atr14'))} "
            f"BB_upper={_fmt(i.get('bb_upper'))} BB_mid={_fmt(i.get('bb_mid'))} "
            f"BB_lower={_fmt(i.get('bb_lower'))}"
        )

    lines += [
        "",
        "## KEY LEVELS",
        f"H4 swing high / low: {_fmt(ind('4h', 'swing_high'))} / {_fmt(ind('4h', 'swing_low'))}",
        f"D1 swing high / low: {_fmt(ind('1d', 'swing_high'))} / {_fmt(ind('1d', 'swing_low'))}",
        "",
        "## GOLD DRIVERS",
        f"DXY: {_fmt(dxy.get('close'), 3)} trend={dxy.get('trend', 'n/a')} "
        "(DXY is inversely correlated with gold)",
        "",
        "## MACRO & POSITIONING",
        f"Real yield (10y TIPS): {_fmt(macro.get('real_yield'), 4)}  "
        f"5d change: {_fmt(macro.get('real_yield_5d_delta'), 4)}",
        f"2s10s curve: {_fmt(macro.get('curve_2s10s'), 4)}",
        f"DXY: {_fmt(dxy.get('close'), 3)} trend={dxy.get('trend', 'n/a')}",
        f"COT managed-money net percentile (3y): {_fmt(positioning.get('cot_mm_net_pctile'))}",
        f"COMEX registered coverage: {_fmt(positioning.get('comex_coverage'), 4)}",
        f"News heat (0-1): {_fmt(state.get('news_heat'), 4)}",
        "",
        "## REGIME",
        f"H1={h1.get('regime', 'n/a')}  H4={h4.get('regime', 'n/a')}  D1={d1.get('regime', 'n/a')}",
        "",
        "## OUTPUT CONTRACT",
        "Reply with ONLY a single JSON object (no prose, no markdown fences) with EXACTLY these keys:",
        '  "direction": "LONG" or "SHORT"',
        '  "entry_anchor": one of the ALLOWED ANCHORS below',
        '  "entry_offset_pips": number in [-50, 50]',
        '  "stop_anchor": one of the ALLOWED ANCHORS below',
        '  "stop_offset_pips": number in [-100, 100]',
        '  "tp1_rr": number in [1.0, 5.0]',
        '  "tp2_rr": number in [1.5, 10.0], strictly greater than tp1_rr',
        '  "grade": "A+", "A", or "B"',
        '  "confidence": integer 0-100',
        '  "thesis": string, at most 500 characters',
        "",
        "ALLOWED ANCHORS (use these names ONLY): " + ", ".join(config.OFFERED_ANCHORS),
        "",
        "1 pip = $0.10. You NEVER output raw prices — NEXUS computes prices from the anchor",
        "you name plus your pip offset. If no valid setup exists, reply with the single word:",
        "WAIT",
        "Any other format — extra keys, raw price numbers, unknown anchors, prose — is DISCARDED.",
        "",
        "Example valid reply:",
        '{"direction":"LONG","entry_anchor":"H1_EMA20","entry_offset_pips":-5,'
        '"stop_anchor":"H4_SWING_LOW","stop_offset_pips":-10,"tp1_rr":1.5,"tp2_rr":3.0,'
        '"grade":"A","confidence":72,"thesis":"H4 uptrend, pullback to H1 EMA20 with DXY rolling over."}',
        "",
        "Example no-setup reply:",
        "WAIT",
    ]
    return "\n".join(lines)


# ==========================================================================
# b. Claude call (the one external call — guarded + budgeted)
# ==========================================================================


def _get_client():
    """
    Construct the Anthropic client. Imported lazily so the module loads with
    no SDK/key present, and so tests can monkeypatch this seam without any
    network. API key comes from the environment only (config reads it from
    ANTHROPIC_API_KEY); it is never a literal and never persisted.
    """
    import anthropic  # lazy: keeps import-time clean and test-friendly

    return anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY,
        timeout=config.ANALYSIS_TIMEOUT_SECONDS,
    )


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_OUTPUT
    )


def _response_text(resp) -> Optional[str]:
    """Concatenate the text blocks of an Anthropic messages response."""
    content = getattr(resp, "content", None)
    if not content:
        return None
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) if parts else None


def call_claude(prompt: str) -> Optional[str]:
    """
    Send `prompt` to Claude and return the raw text reply, or None (WAIT).

    Guards, in order:
      * Daily budget cap: if STATE.budget_spent_today already exceeds
        config.MAX_DAILY_COST, return None WITHOUT calling the API.
      * Any API failure (timeout, APIError, connection error, unexpected
        shape) is logged at error level and returns None. No retries — the
        scheduler cadence is the retry (INVARIANT 6).
    On success, the response's token usage is turned into an estimated cost,
    logged, and accumulated into STATE.budget_spent_today.
    """
    if STATE.budget_spent_today > config.MAX_DAILY_COST:
        logger.error(
            "call_claude: daily budget cap reached (spent=$%.4f > cap=$%.4f); returning None",
            STATE.budget_spent_today,
            config.MAX_DAILY_COST,
        )
        return None

    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=config.ANALYSIS_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        logger.error("call_claude: API call failed (%s); returning None", exc, exc_info=True)
        return None

    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = _estimate_cost(input_tokens, output_tokens)
    STATE.budget_spent_today += cost
    logger.info(
        "call_claude: usage in=%d out=%d est_cost=$%.4f budget_spent=$%.4f",
        input_tokens,
        output_tokens,
        cost,
        STATE.budget_spent_today,
    )

    return _response_text(resp)


# ==========================================================================
# c. reply -> (SignalAnchors | None, metadata)
# ==========================================================================


def _defence(text: str) -> str:
    """Strip a single leading ```/```json fence and matching trailing fence."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    body = t.split("\n")[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _extract_meta(data: dict) -> dict:
    """Pull grade/confidence/thesis, defaulting to B/0/'' on missing or malformed."""
    grade = data.get("grade")
    if grade not in _VALID_GRADES:
        grade = "B"
    confidence = data.get("confidence")
    if not _num(confidence):
        confidence = 0
    thesis = data.get("thesis")
    if not isinstance(thesis, str):
        thesis = ""
    return {"grade": grade, "confidence": confidence, "thesis": thesis}


def extract_signal(raw: str) -> Tuple[Optional[SignalAnchors], dict]:
    """
    Turn a raw Claude reply into (SignalAnchors | None, metadata dict).

      * "WAIT" (case-insensitive, optionally fenced) -> (None, {"wait": True}).
      * Otherwise: parse JSON, strip the grade/confidence/thesis METADATA keys
        (they are not part of the SignalAnchors contract — extra="forbid"
        would reject them), and pass the REMAINDER, unchanged, through the
        resolver's parse_ai_output(). No repair beyond the metadata strip.
    Metadata missing/malformed -> grade "B", confidence 0, thesis "" (the
    validator's confidence floor will WAIT it; its GRADE_SANITY path logs).
    """
    default_meta = {"grade": "B", "confidence": 0, "thesis": ""}

    if not isinstance(raw, str):
        logger.warning("extract_signal: reply is not a string (%s); malformed", type(raw).__name__)
        return None, default_meta

    defenced = _defence(raw)
    if defenced.upper() == "WAIT":
        logger.info("extract_signal: model replied WAIT")
        return None, {"wait": True}

    try:
        data = json.loads(defenced)
    except (json.JSONDecodeError, ValueError):
        # Not WAIT and not JSON. Hand the ORIGINAL raw text to the resolver's
        # parser, which logs the reason and rejects — no repair here.
        return parse_ai_output(raw), default_meta

    if not isinstance(data, dict):
        return parse_ai_output(raw), default_meta

    meta = _extract_meta(data)
    remainder = {k: v for k, v in data.items() if k not in ("grade", "confidence", "thesis")}
    sig = parse_ai_output(json.dumps(remainder))
    return sig, meta


# ==========================================================================
# d. the full cycle
# ==========================================================================


def _build_validator_ctx(state: dict, utc_now: datetime) -> dict:
    """rsi h1/h4 + regime h4/d1 + the clock + upcoming_events (wired in Task 9
    from the calendar sensor; the validator SKIPs it when absent). macro_regime
    remains absent until the fusion task wires it in."""
    def _ind(tf: str, key: str):
        d = state.get(tf)
        if isinstance(d, dict) and isinstance(d.get("indicators"), dict):
            return d["indicators"].get(key)
        return None

    def _regime(tf: str):
        d = state.get(tf)
        return d.get("regime") if isinstance(d, dict) else None

    return {
        "utc_now": utc_now,
        "rsi": {"h1": _ind("1h", "rsi14"), "h4": _ind("4h", "rsi14")},
        "regime": {"h4": _regime("4h"), "d1": _regime("1d")},
        "upcoming_events": state.get("upcoming_events"),
        # Same label the signals row is written under (config.YF_SYMBOL), so one
        # cycle's validator_log rows and its signal share a symbol and can be
        # joined — never the validator's internal "XAUUSD" default.
        "symbol": config.YF_SYMBOL,
    }


def _reset_budget_if_new_day(utc_now: datetime) -> None:
    """Reset the daily budget counter on a UTC day rollover, driven off the
    passed clock (never datetime.now())."""
    global _budget_day
    day = utc_now.date()
    if _budget_day != day:
        if _budget_day is not None:
            logger.info("analysis: UTC day rollover -> %s; resetting budget_spent_today", day)
        STATE.budget_spent_today = 0.0
        _budget_day = day


def _persist_signal(
    state: dict,
    sig: SignalAnchors,
    resolved: ResolvedSignal,
    verdict: Verdict,
    meta: dict,
    lots: float,
    utc_now: datetime,
) -> int:
    """
    Write the issued signal to the signals table and return its id.

    The signals schema (UNTOUCHABLE — migrations/ is FORBIDDEN) has no columns
    for bias, the validator flags, or an execution-mode marker, so those map
    onto existing columns per the architect's decision: bias -> direction; the
    resolved prices + tp R:Rs -> the prices JSONB; the execution-mode marker,
    validator_passed and validator_warnings -> the market_snapshot JSONB
    alongside the raw state.
    """
    anchors_json = {
        "entry_anchor": sig.entry_anchor.value,
        "entry_offset_pips": sig.entry_offset_pips,
        "stop_anchor": sig.stop_anchor.value,
        "stop_offset_pips": sig.stop_offset_pips,
        "tp1_rr": sig.tp1_rr,
        "tp2_rr": sig.tp2_rr,
    }
    prices_json = {
        "entry": resolved.entry_price,
        "stop": resolved.stop_price,
        "tp1": resolved.tp1_price,
        "tp2": resolved.tp2_price,
        "risk_per_unit": resolved.risk_per_unit,
        "tp1_rr": sig.tp1_rr,
        "tp2_rr": sig.tp2_rr,
        "lots": lots,
    }
    market_snapshot = dict(state)  # the raw state dict...
    market_snapshot.update(  # ...plus the fields the schema has no column for
        {
            # Hardcoded PAPER this task (the stage governor arrives in Task 13).
            # Named execution_mode because a standing repo invariant
            # (test_stage_immutable) bans the legacy stage-flag token repo-wide;
            # see the architect decision recorded for this task.
            "execution_mode": "PAPER",
            "validator_passed": True,
            "validator_warnings": list(verdict.reasons),
            "validator_rules_fired": list(verdict.rules_fired),
            "grade_pre_validator": meta["grade"],
            "confidence_pre_validator": meta["confidence"],
            "sized_lots": lots,
        }
    )

    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals
                    (ts, symbol, direction, anchors, prices, grade, confidence,
                     thesis, market_snapshot, status)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
                RETURNING id
                """,
                (
                    utc_now,
                    config.YF_SYMBOL,
                    sig.direction,
                    json.dumps(anchors_json),
                    json.dumps(prices_json),
                    verdict.grade,        # POST-validator grade
                    verdict.confidence,   # POST-validator confidence
                    meta["thesis"],
                    json.dumps(market_snapshot, default=str),
                    "PENDING",
                ),
            )
            signal_id = cur.fetchone()[0]
    return signal_id


def run_analysis_cycle(state: dict, utc_now: datetime) -> dict:
    """
    The full spine for one analysis attempt. Returns a dict whose "outcome"
    names exactly where the attempt ended. Every step is logged. Nothing in
    here calls datetime.now(); the clock arrives as `utc_now`.
    """
    _reset_budget_if_new_day(utc_now)
    logger.info("run_analysis_cycle: start utc_now=%s", utc_now.isoformat())

    # 1. anchors + live price
    anchor_map = build_anchor_map(state)
    h1 = state.get("1h") if isinstance(state.get("1h"), dict) else {}
    live_price = h1.get("price")
    logger.info("run_analysis_cycle: %d anchors resolvable, live_price=%s", len(anchor_map), live_price)

    # 2. prompt -> Claude
    prompt = build_prompt(state)
    raw = call_claude(prompt)
    if raw is None:
        logger.info("run_analysis_cycle: outcome=API_UNAVAILABLE")
        return {"outcome": "API_UNAVAILABLE"}

    # 3. reply -> signal + metadata
    sig, meta = extract_signal(raw)
    if sig is None:
        if meta.get("wait"):
            logger.info("run_analysis_cycle: outcome=WAIT_NO_SETUP")
            return {"outcome": "WAIT_NO_SETUP"}
        logger.warning("run_analysis_cycle: outcome=REJECTED_MALFORMED")
        return {"outcome": "REJECTED_MALFORMED"}

    # 4. resolve anchors -> concrete prices
    resolved = resolve(sig, anchor_map, live_price)
    if resolved is None:
        logger.warning("run_analysis_cycle: outcome=REJECTED_UNRESOLVABLE")
        return {"outcome": "REJECTED_UNRESOLVABLE"}

    # 5. pre-flight validator
    ctx = _build_validator_ctx(state, utc_now)
    verdict = validate(sig, resolved, meta["grade"], meta["confidence"], ctx)
    if verdict.action == "WAIT":
        logger.info("run_analysis_cycle: outcome=VALIDATOR_WAIT reasons=%s", verdict.reasons)
        return {"outcome": "VALIDATOR_WAIT", "verdict": verdict}

    # 6. position size
    lots = position_size(config.ACCOUNT_SIZE, config.MAX_RISK_PCT, resolved.risk_per_unit)
    if lots == 0.0:
        logger.info("run_analysis_cycle: outcome=SIZED_ZERO")
        return {"outcome": "SIZED_ZERO"}

    # 7. persist
    try:
        signal_id = _persist_signal(state, sig, resolved, verdict, meta, lots, utc_now)
    except Exception:
        logger.error("run_analysis_cycle: persist failed; outcome=PERSIST_FAILED", exc_info=True)
        return {"outcome": "PERSIST_FAILED"}

    logger.info("run_analysis_cycle: outcome=SIGNAL_PERSISTED id=%s lots=%s", signal_id, lots)
    return {
        "outcome": "SIGNAL_PERSISTED",
        "signal_id": signal_id,
        "lots": lots,
        "verdict": verdict,
    }


# ==========================================================================
# e. scheduler (the ONLY place a real clock enters)
# ==========================================================================


def run_scheduler() -> None:
    """
    Subscribe to the BUS "market_update" event. On each event, run an analysis
    cycle if at least config.ANALYSIS_MIN_INTERVAL_MINUTES have elapsed since
    the last one. Any cycle exception is logged and swallowed so the scheduler
    (and the data agent publishing to it) survives (INVARIANT 6).
    """
    def _on_market_update(_payload) -> None:
        global _last_cycle_at
        try:
            now = datetime.now(timezone.utc)  # the ONE real clock in this module
            with _scheduler_lock:
                interval = timedelta(minutes=config.ANALYSIS_MIN_INTERVAL_MINUTES)
                if _last_cycle_at is not None and (now - _last_cycle_at) < interval:
                    return
                _last_cycle_at = now
            result = run_analysis_cycle(STATE.market_data, now)
            logger.info("scheduler: cycle outcome=%s", result.get("outcome"))
        except Exception:
            logger.exception("scheduler: analysis cycle raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    logger.info(
        "analysis scheduler subscribed to market_update (min interval=%d min)",
        config.ANALYSIS_MIN_INTERVAL_MINUTES,
    )
