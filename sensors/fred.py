"""
NEXUS macro sensor — FRED real yields, breakevens, and the 2s10s curve.

Real yields drive gold more than any other single input; this is the first
Phase 2 sensor. It fetches a handful of FRED series, persists every raw
observation, and derives a small numeric summary. NO interpretation lives
here: no "bullish"/"bearish" labels, no directional bias — plain numbers.
Turning a number into a trading opinion belongs to the analyst prompt (a
later task), not this sensor.

INVARIANT 6: the one external call (FRED's HTTP API) is timeout-wrapped,
try/except-guarded, and a failure is logged and returns None — never raises.
A single dead series never kills a fetch cycle or the polling loop.
"""
import argparse
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from psycopg2.extras import execute_values

import config
from core import database
from core.state import BUS, STATE
from ops.telegram_bot import send_alert

logger = logging.getLogger(__name__)

_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_FETCH_TIMEOUT_SECONDS = 15

_DERIVED_KEYS = ("real_yield", "real_yield_5d_delta", "curve_2s10s", "breakeven_10y")


def fetch_series(series_id: str) -> Optional[List[Tuple[date, float]]]:
    """
    Fetch one FRED series as ascending (date, value) pairs over the last
    config.FRED_LOOKBACK_DAYS days. FRED's null marker ("." for a value on a
    day the series has no observation) is skipped, not fabricated as 0 or
    carried forward. Any request/parse failure is logged and returns None —
    this function never raises (INVARIANT 6).
    """
    start = (datetime.now(timezone.utc).date() - timedelta(days=config.FRED_LOOKBACK_DAYS)).isoformat()
    params = {
        "series_id": series_id,
        "api_key": config.FRED_API_KEY,
        "file_type": "json",
        "observation_start": start,
    }
    try:
        resp = requests.get(_OBSERVATIONS_URL, params=params, timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("fetch_series: request failed for series=%s (%s)", series_id, exc)
        return None

    observations = data.get("observations") if isinstance(data, dict) else None
    if not isinstance(observations, list):
        logger.error("fetch_series: unexpected response shape for series=%s", series_id)
        return None

    pairs: List[Tuple[date, float]] = []
    for obs in observations:
        raw_date = obs.get("date")
        raw_value = obs.get("value")
        if raw_value in (".", None) or raw_date is None:
            continue  # FRED's null marker, or a malformed row — skip, don't fabricate
        try:
            ts = datetime.strptime(raw_date, "%Y-%m-%d").date()
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        pairs.append((ts, value))
    return pairs


def persist_observations(conn, series_id: str, pairs: List[Tuple[date, float]]) -> int:
    """Upsert observations: ON CONFLICT (series, ts) DO NOTHING. Returns rows attempted."""
    if not pairs:
        return 0
    rows = [(series_id, ts, value) for ts, value in pairs]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO macro_observations (series, ts, value) VALUES %s "
            "ON CONFLICT (series, ts) DO NOTHING",
            rows,
        )
    return len(rows)


def _latest(conn, series: str, limit: int) -> list:
    """Most recent `limit` (ts, value) rows for `series`, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, value FROM macro_observations WHERE series=%s ORDER BY ts DESC LIMIT %s",
            (series, limit),
        )
        return cur.fetchall()


def compute_derived(conn) -> Dict[str, Optional[float]]:
    """
    Derive the small numeric macro summary from whatever's currently in
    macro_observations (not just this cycle's fetch — a series that fetched
    successfully yesterday but failed today still has real historical rows
    to derive from). A series with no rows at all yields None for its
    key(s) — never a fabricated number.

      real_yield            = latest DFII10
      real_yield_5d_delta   = latest DFII10 minus the value 5 observations back
      curve_2s10s           = latest DGS10 minus latest DGS2
      breakeven_10y         = latest T10YIE
    """
    dfii10 = _latest(conn, "DFII10", 6)
    real_yield = float(dfii10[0][1]) if dfii10 and dfii10[0][1] is not None else None
    real_yield_5d_delta = None
    if len(dfii10) >= 6 and dfii10[0][1] is not None and dfii10[5][1] is not None:
        real_yield_5d_delta = float(dfii10[0][1]) - float(dfii10[5][1])

    dgs10 = _latest(conn, "DGS10", 1)
    dgs2 = _latest(conn, "DGS2", 1)
    curve_2s10s = None
    if dgs10 and dgs2 and dgs10[0][1] is not None and dgs2[0][1] is not None:
        curve_2s10s = float(dgs10[0][1]) - float(dgs2[0][1])

    t10yie = _latest(conn, "T10YIE", 1)
    breakeven_10y = float(t10yie[0][1]) if t10yie and t10yie[0][1] is not None else None

    return {
        "real_yield": real_yield,
        "real_yield_5d_delta": real_yield_5d_delta,
        "curve_2s10s": curve_2s10s,
        "breakeven_10y": breakeven_10y,
    }


def run_fred_cycle(now_utc: datetime) -> dict:
    """
    One fetch-persist-derive-publish pass: fetch every config.FRED_SERIES,
    persist whatever came back, compute the derived summary from the table,
    write it into STATE.market_data["macro"], and publish BUS "macro_update".
    A single dead series is logged and skipped — never aborts the cycle.
    Network calls happen OUTSIDE the DB connection checkout, so a slow/failed
    HTTP call never ties up a pooled connection.
    """
    fetched: Dict[str, List[Tuple[date, float]]] = {}
    for series_id in config.FRED_SERIES:
        pairs = fetch_series(series_id)
        if pairs is None:
            logger.warning("run_fred_cycle: fetch failed for series=%s; continuing", series_id)
            continue
        fetched[series_id] = pairs

    try:
        with database.get_conn() as conn:
            for series_id, pairs in fetched.items():
                persist_observations(conn, series_id, pairs)
            derived = compute_derived(conn)
    except Exception:
        logger.exception("run_fred_cycle: DB error while persisting/deriving")
        derived = {k: None for k in _DERIVED_KEYS}

    derived = dict(derived)
    derived["fetched_at"] = now_utc.isoformat()
    STATE.update_market_data("macro", derived)
    BUS.publish("macro_update", derived)
    return derived


def check_staleness(now_utc: datetime) -> Optional[str]:
    """
    A dead-man's-switch: if the macro sensor's last recorded fetch is older
    than config.STALE_DATA_ALERT_HOURS, return a plain factual warning
    string (no interpretation). If nothing has been fetched yet, or the
    timestamp is unreadable, there is nothing to call stale -> None.
    """
    macro = STATE.get_market_data("macro")
    if not isinstance(macro, dict):
        return None
    fetched_at_raw = macro.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)

    age_hours = (now_utc - fetched_at).total_seconds() / 3600.0
    if age_hours > config.STALE_DATA_ALERT_HOURS:
        return (
            f"NEXUS macro sensor stale: last fetch {age_hours:.1f}h ago "
            f"(threshold {config.STALE_DATA_ALERT_HOURS}h)"
        )
    return None


def run_fred_agent() -> None:
    """
    Poll every config.FRED_POLL_MINUTES. Each iteration: check staleness of
    the PREVIOUS cycle's fetch first (so a run of failing cycles eventually
    surfaces a Telegram alert once per iteration it stays stale), then run
    one fetch cycle. Any exception is logged and swallowed so the loop
    survives (INVARIANT 6); ops.telegram_bot.send_alert's no-op contract
    makes the alert call safe even when Telegram is unconfigured.
    """
    while True:
        try:
            now = datetime.now(timezone.utc)
            warning = check_staleness(now)
            if warning is not None:
                send_alert(warning)
            run_fred_cycle(now)
        except Exception:
            logger.exception("run_fred_agent: cycle failed; continuing")
        time.sleep(config.FRED_POLL_MINUTES * 60)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS FRED macro sensor")
    parser.add_argument("--once", action="store_true", help="Run a single fetch cycle, print the derived dict, and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        derived = run_fred_cycle(datetime.now(timezone.utc))
        print(derived)
        return

    run_fred_agent()


if __name__ == "__main__":
    main()
