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
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
from psycopg2.extras import execute_values

# CLI-only: `python3 -m sensors.fred --once` is a standalone entry point, so
# .env must be loaded here, BEFORE `import config` below reads the
# environment. This must run before that import, not merely before main()'s
# body -- config.py reads env vars at module-import time. A library import
# of this module (or the agent loop started by a future backend) does NOT
# hit this branch and simply inherits the parent process's environment,
# exactly like scripts/run_one_cycle.py does for its own entry point.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE
from ops.telegram_bot import send_alert

logger = logging.getLogger(__name__)

_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
_FETCH_TIMEOUT_SECONDS = 15
_URL_PATTERN = re.compile(r"https?://\S+")


def _redact_url(text: str) -> str:
    """
    Strip any http(s) URL out of `text`. The FRED request URL carries
    api_key as a query parameter, and requests exceptions routinely quote
    the full URL they failed on in their message -- that URL must never
    reach a log line.
    """
    return _URL_PATTERN.sub("**URL_REDACTED**", text)

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
        # Never log the request URL -- it carries api_key as a query param.
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "fetch_series: request failed for series=%s status_code=%s error=%s: %s",
            series_id,
            status_code,
            type(exc).__name__,
            _redact_url(str(exc)),
        )
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
    any_success = False
    for series_id in config.FRED_SERIES:
        pairs = fetch_series(series_id)
        if pairs is None:
            logger.warning("run_fred_cycle: fetch failed for series=%s; continuing", series_id)
            continue
        any_success = True  # FRED was successfully reached and parsed for this series
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
    # fetched_at: this pass COMPLETED (display/liveness only — see check_staleness).
    # last_success_at: at least one series was actually reached and parsed this
    # pass. If this pass had zero successes, carry the PRIOR last_success_at
    # forward rather than clobbering it with None -- staleness is judged on
    # data success, not on whether the scheduler is merely still looping.
    derived["fetched_at"] = now_utc.isoformat()
    if any_success:
        derived["last_success_at"] = now_utc.isoformat()
    else:
        prior = STATE.get_market_data("macro")
        derived["last_success_at"] = prior.get("last_success_at") if isinstance(prior, dict) else None
    STATE.update_market_data("macro", derived)
    BUS.publish("macro_update", derived)
    return derived


def _age_hours(now_utc: datetime, iso_str) -> Optional[float]:
    """Hours between now_utc and an ISO timestamp string, or None if absent/unparseable."""
    if not isinstance(iso_str, str):
        return None
    try:
        ts = datetime.fromisoformat(iso_str)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now_utc - ts).total_seconds() / 3600.0


def check_staleness(now_utc: datetime) -> Optional[str]:
    """
    A dead-man's-switch on DATA SUCCESS, not scheduler liveness: a loop that
    keeps running while every fetch fails must still be called stale.

    Compares now against last_success_at (the last pass with >=1 successful
    series fetch). If we have never once succeeded, fall back to fetched_at
    (the last pass completion) as the failing-duration signal -- so a chain
    of all-failing passes still eventually trips the alert. If neither is
    set, the agent has never run a pass at all -> nothing to compare -> None.
    """
    macro = STATE.get_market_data("macro")
    if not isinstance(macro, dict):
        return None

    age = _age_hours(now_utc, macro.get("last_success_at"))
    if age is None:
        age = _age_hours(now_utc, macro.get("fetched_at"))
        if age is None:
            return None  # agent never ran a pass; nothing to compare

    if age > config.STALE_DATA_ALERT_HOURS:
        return (
            f"NEXUS macro sensor stale: no successful fetch in {age:.1f}h "
            f"(threshold {config.STALE_DATA_ALERT_HOURS}h)"
        )
    return None


# Module-local de-dup state for run_fred_agent's alerting: the ONLY writer is
# run_fred_agent's own loop, which runs single-file. Once a stale warning is
# sent, it is not re-sent every poll pass -- at most once per
# STALE_DATA_ALERT_HOURS, so a prolonged outage pages once, not every pass.
_last_alert_at: Optional[datetime] = None


def run_fred_agent() -> None:
    """
    Poll every config.FRED_POLL_MINUTES. Each iteration: check staleness of
    the PREVIOUS pass's data-success timestamp first, alert (de-duped) if
    stale, then run one fetch cycle. Any exception is logged and swallowed
    so the loop survives (INVARIANT 6); ops.telegram_bot.send_alert's no-op
    contract makes the alert call safe even when Telegram is unconfigured.
    """
    global _last_alert_at
    while True:
        try:
            now = datetime.now(timezone.utc)
            warning = check_staleness(now)
            if warning is not None:
                dedup_window = timedelta(hours=config.STALE_DATA_ALERT_HOURS)
                if _last_alert_at is None or (now - _last_alert_at) >= dedup_window:
                    send_alert(warning)
                    _last_alert_at = now
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
