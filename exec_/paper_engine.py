"""
NEXUS paper engine — models fills and outcomes for issued signals against
live H1 data. No real orders anywhere; this tracks what a paper position
WOULD have done.

Fill model (coarse BY DESIGN — H1 bars, no tick data):

  * ENTRY (PENDING -> OPEN) is decided on the H1 CLOSE: a LONG fills when the
    close trades down to entry (close <= entry); a SHORT when close >= entry.
    A PENDING signal older than config.PAPER_TTL_HOURS expires unfilled.

  * EXITS (while OPEN) are decided on the H1 bar's HIGH/LOW (intrabar reach),
    checked in a fixed, conservative order every bar — STOP FIRST, then TP2,
    then TP1 — so a single wide bar that spans both the stop and a target
    books the STOP, never the target. Order matters; do not reorder.

      stop hit                 -> STOPPED, outcome_r = -1.0
      tp2 hit                  -> TP2,     outcome_r = +tp2_rr
      tp1 hit (first time)     -> partial: flag it, move the tracked stop to
                                  entry (break-even). Stays OPEN.
      after partial, stop(=entry) hit -> BE, outcome_r = tp1_rr * 0.5
                                  (half the position came off at TP1, the
                                  other half exits at break-even).

  outcome_pips = outcome_r * risk_per_unit / config.PIP_SIZE.

MAE/MFE are tracked in R against each bar's high/low while OPEN and persisted
on every update (so they are always current, not only at close).

INVARIANT 6: the event handler and the per-signal processing are each
try/except-guarded — one bad signal or a DB hiccup never kills the loop.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import config
from core import database
from core.state import BUS

logger = logging.getLogger(__name__)

_ACTIVE = ("PENDING", "OPEN")


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# persistence helpers (each its own short transaction)
# --------------------------------------------------------------------------


def _update_open(sig_id: int, market_snapshot: dict, filled_at: datetime) -> None:
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals SET status='OPEN', filled_at=%s, market_snapshot=%s::jsonb "
                "WHERE id=%s",
                (filled_at, json.dumps(market_snapshot, default=str), sig_id),
            )


def _update_tracking(sig_id: int, market_snapshot: dict, mae_r: float, mfe_r: float) -> None:
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals SET market_snapshot=%s::jsonb, mae_r=%s, mfe_r=%s WHERE id=%s",
                (json.dumps(market_snapshot, default=str), mae_r, mfe_r, sig_id),
            )


def _update_terminal(
    sig_id: int,
    status: str,
    outcome_r: Optional[float],
    outcome_pips: Optional[float],
    mae_r: Optional[float],
    mfe_r: Optional[float],
    market_snapshot: dict,
    closed_at: datetime,
) -> None:
    outcome_hit = None if outcome_r is None else outcome_r > 0
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE signals SET status=%s, outcome_r=%s, outcome_pips=%s, mae_r=%s, "
                "mfe_r=%s, outcome_hit=%s, outcome_ts=%s, market_snapshot=%s::jsonb WHERE id=%s",
                (
                    status,
                    outcome_r,
                    outcome_pips,
                    mae_r,
                    mfe_r,
                    outcome_hit,
                    closed_at,
                    json.dumps(market_snapshot, default=str),
                    sig_id,
                ),
            )


# --------------------------------------------------------------------------
# per-signal state machine
# --------------------------------------------------------------------------


def _process_pending(
    sig_id, direction, prices, market_snapshot, issued_ts, grade, thesis, lots, bar, event_ts
) -> Optional[Tuple[str, dict]]:
    entry = _num(prices.get("entry"))
    stop = _num(prices.get("stop"))
    if entry is None or stop is None:
        logger.warning("paper_engine: signal %s has no entry/stop; skipping", sig_id)
        return None

    age_hours = (event_ts - issued_ts).total_seconds() / 3600.0
    if age_hours > config.PAPER_TTL_HOURS:
        _update_terminal(sig_id, "EXPIRED", None, None, None, None, market_snapshot, event_ts)
        logger.info("paper_engine: signal %s EXPIRED (age %.1fh > %sh)", sig_id, age_hours, config.PAPER_TTL_HOURS)
        return ("signal_closed", {"id": sig_id, "status": "EXPIRED", "outcome_r": None})

    close = _num(bar.get("close"))
    if close is None:
        return None
    crossed = close <= entry if direction == "LONG" else close >= entry
    if not crossed:
        return None

    market_snapshot["paper_engine"] = {
        "partial_filled": False,
        "tracked_stop": stop,
        "mae_r": 0.0,
        "mfe_r": 0.0,
        "opened_at": event_ts.isoformat(),
    }
    _update_open(sig_id, market_snapshot, event_ts)
    logger.info("paper_engine: signal %s FILLED at %.5f -> OPEN", sig_id, close)
    return (
        "signal_opened",
        {
            "id": sig_id,
            "direction": direction,
            "grade": grade,
            "entry": entry,
            "stop": stop,
            "tp1": _num(prices.get("tp1")),
            "tp2": _num(prices.get("tp2")),
            "lots": _num(lots) if lots is not None else _num(prices.get("lots")),
            "thesis": thesis or "",
        },
    )


def _process_open(sig_id, direction, prices, market_snapshot, bar, event_ts) -> Optional[Tuple[str, dict]]:
    entry = _num(prices.get("entry"))
    stop = _num(prices.get("stop"))
    tp1 = _num(prices.get("tp1"))
    tp2 = _num(prices.get("tp2"))
    risk = _num(prices.get("risk_per_unit"))
    tp1_rr = _num(prices.get("tp1_rr"))
    tp2_rr = _num(prices.get("tp2_rr"))
    high = _num(bar.get("high"))
    low = _num(bar.get("low"))
    if None in (entry, stop, tp1, tp2, risk, tp1_rr, tp2_rr, high, low) or risk <= 0:
        logger.warning("paper_engine: signal %s missing/invalid fields; skipping bar", sig_id)
        return None

    pe = _as_dict(market_snapshot.get("paper_engine"))
    partial = bool(pe.get("partial_filled", False))
    tracked_stop = _num(pe.get("tracked_stop"))
    if tracked_stop is None:
        tracked_stop = stop
    mae_r = _num(pe.get("mae_r")) or 0.0
    mfe_r = _num(pe.get("mfe_r")) or 0.0

    # Excursions in R (favorable is positive for either direction).
    dir_sign = 1.0 if direction == "LONG" else -1.0
    r_high = dir_sign * (high - entry) / risk
    r_low = dir_sign * (low - entry) / risk
    mfe_r = max(mfe_r, r_high, r_low)
    mae_r = min(mae_r, r_high, r_low)

    if direction == "LONG":
        stop_hit = low <= tracked_stop
        tp2_hit = high >= tp2
        tp1_hit = high >= tp1
    else:
        stop_hit = high >= tracked_stop
        tp2_hit = low <= tp2
        tp1_hit = low <= tp1

    pe.update({"partial_filled": partial, "tracked_stop": tracked_stop, "mae_r": mae_r, "mfe_r": mfe_r})
    market_snapshot["paper_engine"] = pe

    # STOP FIRST (conservative), then TP2, then TP1 partial. Do not reorder.
    if stop_hit:
        if partial:
            status, outcome_r = "BE", tp1_rr * 0.5
        else:
            status, outcome_r = "STOPPED", -1.0
        outcome_pips = outcome_r * risk / config.PIP_SIZE
        _update_terminal(sig_id, status, outcome_r, outcome_pips, mae_r, mfe_r, market_snapshot, event_ts)
        logger.info("paper_engine: signal %s -> %s outcome_r=%.3f", sig_id, status, outcome_r)
        return ("signal_closed", {"id": sig_id, "status": status, "outcome_r": outcome_r})

    if tp2_hit:
        outcome_r = tp2_rr
        outcome_pips = outcome_r * risk / config.PIP_SIZE
        _update_terminal(sig_id, "TP2", outcome_r, outcome_pips, mae_r, mfe_r, market_snapshot, event_ts)
        logger.info("paper_engine: signal %s -> TP2 outcome_r=%.3f", sig_id, outcome_r)
        return ("signal_closed", {"id": sig_id, "status": "TP2", "outcome_r": outcome_r})

    if tp1_hit and not partial:
        pe["partial_filled"] = True
        pe["tracked_stop"] = entry  # move stop to break-even
        pe["tp1_partial_at"] = event_ts.isoformat()
        market_snapshot["paper_engine"] = pe
        _update_tracking(sig_id, market_snapshot, mae_r, mfe_r)
        logger.info("paper_engine: signal %s TP1 partial; stop -> break-even", sig_id)
        return None

    # No transition — just persist the running excursions / tracked state.
    _update_tracking(sig_id, market_snapshot, mae_r, mfe_r)
    return None


def _process_signal(row, bar, event_ts) -> Optional[Tuple[str, dict]]:
    sig_id, direction, prices, market_snapshot, status, issued_ts, grade, thesis, lots = row
    prices = _as_dict(prices)
    market_snapshot = _as_dict(market_snapshot)
    if status == "PENDING":
        return _process_pending(
            sig_id, direction, prices, market_snapshot, issued_ts, grade, thesis, lots, bar, event_ts
        )
    if status == "OPEN":
        return _process_open(sig_id, direction, prices, market_snapshot, bar, event_ts)
    return None


def evaluate_signals(bar: dict, event_ts: datetime, symbol: Optional[str] = None) -> list:
    """
    Evaluate every active (PENDING/OPEN) signal for `symbol` against one H1
    `bar` (dict with close/high/low). Returns the list of BUS events emitted
    (signal_opened / signal_closed). A single failing signal is logged and
    skipped; the rest are still processed (INVARIANT 6).
    """
    symbol = symbol or config.YF_SYMBOL
    try:
        rows = database.fetch(
            "SELECT id, direction, prices, market_snapshot, status, ts, grade, thesis, lots "
            "FROM signals WHERE symbol=%s AND status IN %s ORDER BY id",
            (symbol, _ACTIVE),
        )
    except Exception:
        logger.exception("paper_engine: failed to load active signals; skipping bar")
        return []

    events = []
    for row in rows:
        try:
            event = _process_signal(row, bar, event_ts)
            if event is not None:
                events.append(event)
        except Exception:
            logger.exception("paper_engine: error processing signal id=%s; continuing", row[0])

    for name, payload in events:
        BUS.publish(name, payload)
    return events


def _latest_h1_bar(symbol: str) -> Optional[dict]:
    """Fetch the most recent H1 candle (has the high/low the fill model needs)."""
    try:
        rows = database.fetch(
            "SELECT ts, high, low, close FROM candles WHERE symbol=%s AND timeframe='1h' "
            "ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
    except Exception:
        logger.exception("paper_engine: failed to fetch latest H1 bar")
        return None
    if not rows:
        return None
    ts, high, low, close = rows[0]
    return {"ts": ts, "high": float(high), "low": float(low), "close": float(close)}


def run_paper_engine() -> None:
    """
    Subscribe to the BUS "market_update" event and, on each, re-evaluate all
    active signals against the latest H1 bar. Any exception is logged and
    swallowed so the engine (and the publisher) survive (INVARIANT 6).
    """
    def _on_market_update(_payload) -> None:
        try:
            bar = _latest_h1_bar(config.YF_SYMBOL)
            if bar is None:
                logger.warning("paper_engine: no H1 bar available; nothing to evaluate")
                return
            event_ts = bar["ts"] if isinstance(bar["ts"], datetime) else datetime.now(timezone.utc)
            evaluate_signals(bar, event_ts, config.YF_SYMBOL)
        except Exception:
            logger.exception("paper_engine: market_update handler raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    logger.info("paper engine subscribed to market_update")
