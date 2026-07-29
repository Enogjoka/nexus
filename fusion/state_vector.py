"""
NEXUS state vector — the fusion core: one wide row, ~26 typed dims, carrying
everything the sensors know at a point in time, stored hourly.

This is the substrate for what comes next: RAG embeds it (Task 11), the
learning loop runs per-column correlations over it (Task 12), and the analyst
prompt reads from the same assembled dict.

Sources, all read out of AppState.market_data (each sensor's own agent owns
writing its slice there):

    "1h"/"4h"/"1d"      gold_agent   price, atr, rsi, bb_position, regime
    "session_hilo"      gold_agent   session high/low
    "dxy"               gold_agent   dxy close + 5-bar trend
    "macro"             fred         real yield, breakevens, 2s10s
    "positioning"       positioning  COT managed-money net, comex coverage
    "news_heat"         news         crude news-heat scalar
    "upcoming_events"   calendar     -> minutes_to_next_high_event

A sensor that is down, absent, or reporting a non-finite number yields NULL
for its column. Nothing here fabricates, substitutes, or carries a stale
value forward under a fresh timestamp -- a missing dim is missing.

NO interpretation lives here: plain numbers and the sensors' own labels.
"""
import argparse
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# CLI-only: `python3 -m fusion.state_vector --once` is a standalone entry
# point, so .env must be loaded here, BEFORE `import config` below reads the
# environment (config.py reads env vars at module-import time). A library
# import of this module does NOT hit this branch and inherits the parent's
# environment -- mirrors the sensors.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE
from data.gold_agent import detect_session

logger = logging.getLogger(__name__)

# Column order for the INSERT. Kept as one tuple so the column list and the
# value tuple can never drift apart. `id` and `assembled_at` are DB-generated.
_COLUMNS = (
    "ts",
    # L0 price / microstructure
    "price", "atr_h1", "atr_h4", "rsi_h1", "rsi_h4", "rsi_d1", "bb_position_h1",
    "regime_h1", "regime_h4", "regime_d1", "session", "session_high", "session_low",
    # L1 macro
    "real_yield", "real_yield_5d_delta", "curve_2s10s", "breakeven_10y", "dxy", "dxy_trend",
    # L2 positioning
    "cot_mm_net", "cot_mm_net_pctile", "comex_coverage",
    # L4 news / L5 time
    "news_heat", "minutes_to_next_high_event", "fix_window",
)


def _num(value) -> Optional[float]:
    """A real finite float, or None. Rejects bools, strings, NaN and inf --
    a garbage reading is a missing reading, never a persisted number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value) -> Optional[int]:
    num = _num(value)
    return None if num is None else int(num)


def _str(value) -> Optional[str]:
    return value if isinstance(value, str) else None


def _dict(state: dict, key: str) -> dict:
    value = state.get(key)
    return value if isinstance(value, dict) else {}


def _ind(state: dict, timeframe: str, key: str):
    indicators = _dict(state, timeframe).get("indicators")
    return indicators.get(key) if isinstance(indicators, dict) else None


def in_fix_window(now_utc: datetime) -> bool:
    """
    True when now_utc falls within config.FIX_BLOCK_MINUTES BEFORE either
    London fix time.

    NOTE: risk/validator.py owns the authoritative copy of this check -- it is
    the gate that actually blocks trades. This is a deliberate local
    reimplementation (fusion must not import from risk/); it only records what
    the clock looked like, and must never be mistaken for the gate itself.
    """
    now = now_utc.astimezone(timezone.utc) if now_utc.tzinfo is not None else now_utc
    now_minutes = now.hour * 60.0 + now.minute + now.second / 60.0
    for fix in config.LONDON_FIX_UTC:
        fix_hour, fix_minute = (int(part) for part in fix.split(":"))
        delta = (fix_hour * 60.0 + fix_minute) - now_minutes
        if 0.0 <= delta <= config.FIX_BLOCK_MINUTES:
            return True
    return False


def minutes_to_next_high_event(events) -> Optional[float]:
    """
    Minutes until the soonest HIGH-impact event in the calendar's window, or
    None when the calendar is absent or holds no HIGH events. Non-HIGH events
    are ignored entirely -- this dim answers "how close is the next landmine".
    """
    if not isinstance(events, list):
        return None
    candidates = [
        _num(event.get("minutes_until"))
        for event in events
        if isinstance(event, dict) and event.get("impact") == "HIGH"
    ]
    finite = [minutes for minutes in candidates if minutes is not None]
    return min(finite) if finite else None


def assemble(state: dict, conn, now_utc: datetime) -> Dict[str, Any]:
    """
    Build one state vector from AppState.market_data. Returns a dict keyed by
    _COLUMNS; every dim is a plain number/string/bool or None.

    `conn` is accepted for interface stability (persist() and future readers
    take one) but is deliberately unused: every dim comes from `state`, so
    assembly stays a pure, offline, testable function.
    """
    session_hilo = _dict(state, "session_hilo")
    dxy = _dict(state, "dxy")
    macro = _dict(state, "macro")
    positioning = _dict(state, "positioning")

    return {
        "ts": now_utc,
        # ---- L0 price / microstructure
        "price": _num(_dict(state, "1h").get("price")),
        "atr_h1": _num(_ind(state, "1h", "atr14")),
        "atr_h4": _num(_ind(state, "4h", "atr14")),
        "rsi_h1": _num(_ind(state, "1h", "rsi14")),
        "rsi_h4": _num(_ind(state, "4h", "rsi14")),
        "rsi_d1": _num(_ind(state, "1d", "rsi14")),
        "bb_position_h1": _num(_ind(state, "1h", "bb_position_pct")),
        "regime_h1": _str(_dict(state, "1h").get("regime")),
        "regime_h4": _str(_dict(state, "4h").get("regime")),
        "regime_d1": _str(_dict(state, "1d").get("regime")),
        "session": detect_session(now_utc),
        "session_high": _num(session_hilo.get("high")),
        "session_low": _num(session_hilo.get("low")),
        # ---- L1 macro
        "real_yield": _num(macro.get("real_yield")),
        "real_yield_5d_delta": _num(macro.get("real_yield_5d_delta")),
        "curve_2s10s": _num(macro.get("curve_2s10s")),
        "breakeven_10y": _num(macro.get("breakeven_10y")),
        "dxy": _num(dxy.get("close")),
        "dxy_trend": _str(dxy.get("trend")),
        # ---- L2 positioning
        "cot_mm_net": _int(positioning.get("cot_mm_net")),
        "cot_mm_net_pctile": _num(positioning.get("cot_mm_net_pctile")),
        "comex_coverage": _num(positioning.get("comex_coverage")),
        # ---- L4 news / L5 time
        "news_heat": _num(state.get("news_heat")),
        "minutes_to_next_high_event": minutes_to_next_high_event(state.get("upcoming_events")),
        "fix_window": in_fix_window(now_utc),
    }


def hour_bucket(ts: datetime) -> datetime:
    """Truncate to the hour -- the table holds one row per hour."""
    return ts.replace(minute=0, second=0, microsecond=0)


def persist(vector: Dict[str, Any], conn) -> bool:
    """
    Insert one state vector, ts truncated to the hour. ON CONFLICT (ts) DO
    NOTHING keeps repeated assembles within the same hour idempotent.
    Returns True if a row was actually written.
    """
    row = dict(vector)
    row["ts"] = hour_bucket(row["ts"])
    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO state_vectors ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
            "ON CONFLICT (ts) DO NOTHING",
            tuple(row[column] for column in _COLUMNS),
        )
        return cur.rowcount > 0


# Hour bucket of the last successful persist; the assembler callback is the
# only writer and the bus calls it single-file.
_last_persisted_hour: Optional[datetime] = None


def run_assembler() -> None:
    """
    Subscribe to BUS "market_update". On each event assemble a fresh vector
    and publish it to STATE.market_data["state_vector"] for downstream
    readers, but persist only once the hour boundary has been crossed (the
    table is hourly). Any exception is logged and swallowed so the assembler
    -- and the data agent publishing to it -- survive (INVARIANT 6).
    """
    def _on_market_update(_payload) -> None:
        global _last_persisted_hour
        try:
            now = datetime.now(timezone.utc)
            vector = assemble(STATE.market_data, None, now)
            # Always refresh in-memory, even if the DB is unreachable below.
            STATE.update_market_data("state_vector", vector)

            bucket = hour_bucket(now)
            if _last_persisted_hour != bucket:
                with database.get_conn() as conn:
                    persist(vector, conn)
                _last_persisted_hour = bucket
                logger.info("state_vector: persisted hour bucket %s", bucket.isoformat())
        except Exception:
            logger.exception("run_assembler: market_update handler raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    logger.info("state vector assembler subscribed to market_update")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS state vector assembler")
    parser.add_argument(
        "--once", action="store_true", help="Assemble one vector, persist it, print it, and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        now = datetime.now(timezone.utc)
        vector = assemble(STATE.market_data, None, now)
        try:
            with database.get_conn() as conn:
                persist(vector, conn)
        except Exception:
            logger.exception("main: persist failed; printing the assembled vector anyway")
        print(vector)
        return

    raise SystemExit("run_assembler() is event-driven; use --once for a one-shot assemble")


if __name__ == "__main__":
    main()
