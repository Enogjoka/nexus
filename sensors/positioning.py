"""
NEXUS positioning sensor — who is actually long gold, from three free,
public sources: CFTC's weekly Commitments of Traders (COT) report,
SPDR's GLD ETF (as a flow proxy, see below), and CME's daily COMEX
warehouse stocks report. NO interpretation lives here: no "bullish"/
"bearish" labels, no directional bias — plain numbers. Turning a number
into a trading opinion belongs to the analyst prompt (a later task).

HONEST SCOPE REDUCTION — GLD tonnage: there is no reliable GLD tonnage
source available to this task. SPDR's own public CSV and World Gold
Council/goldhub HTML are fragile to scrape (explicitly out of scope —
see CLAUDE.md/FORBIDDEN); yfinance's `.info`/`fast_info` "total assets"
fields are unreliable proxies for physical tonnage, and computing tonnage
from shares-outstanding x NAV is overkill for what this task needs.
`fetch_gld_tonnes()` therefore always returns None, and `etf_holdings.
gld_tonnes` stays NULL until a reliable source lands in a later task.
In the meantime, `fetch_gld_flow_proxy()` persists GLD's daily closing
price and volume (columns added to etf_holdings alongside gld_tonnes —
an architect-approved extension of the originally-specified single-column
table) as a rough flow proxy.

INVARIANT 6: every external call (CFTC's Socrata API, yfinance, CME's
workbook download) is timeout-wrapped, try/except-guarded, and a failure
is logged and returns None — never raises. A single dead source never
kills a fetch cycle or the polling loop.
"""
import argparse
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from psycopg2.extras import execute_values

# CLI-only: `python3 -m sensors.positioning --once` is a standalone entry
# point, so .env must be loaded here, BEFORE `import config` below reads the
# environment (config.py reads env vars at module-import time). A library
# import of this module (or the agent loop started by a future backend) does
# NOT hit this branch and simply inherits the parent process's environment —
# mirrors sensors/fred.py and scripts/run_one_cycle.py.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_COT_TIMEOUT_SECONDS = 20
_COT_LIMIT = 200
_COMEX_URL = "https://www.cmegroup.com/delivery_reports/Gold_Stocks.xls"
_COMEX_TIMEOUT_SECONDS = 20
_MIN_PCTILE_WEEKS = 26  # never compute a percentile from thinner history than this

_DERIVED_KEYS = ("cot_mm_net", "cot_mm_net_pctile", "etf_flow_5d", "comex_coverage")

_COT_EXPECTED_KEYS = {
    "report_date_as_yyyy_mm_dd",
    "m_money_positions_long_all",
    "m_money_positions_short_all",
    "open_interest_all",
    "market_and_exchange_names",
}


# ==========================================================================
# COT (CFTC Commitments of Traders)
# ==========================================================================


def _probe_market_values() -> None:
    """
    The market_and_exchange_names filter returned zero rows with a valid 200
    -- rather than guess at a different filter value, probe unfiltered and
    log the DISTINCT market_and_exchange_names values actually present in a
    small sample, so a human can pick the correct one. No fuzzy
    substitution: this only logs, it never changes what fetch_cot() does.
    """
    try:
        resp = requests.get(config.COT_SOCRATA_URL, params={"$limit": 5}, timeout=_COT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        probe_rows = resp.json()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "fetch_cot: market_and_exchange_names='%s' returned zero rows; unfiltered probe "
            "request failed status_code=%s error=%s",
            config.COT_MARKET_NAME,
            status_code,
            type(exc).__name__,
        )
        return

    if not isinstance(probe_rows, list) or not probe_rows:
        logger.error(
            "fetch_cot: market_and_exchange_names='%s' returned zero rows; unfiltered probe "
            "was also empty/malformed (%s)",
            config.COT_MARKET_NAME,
            type(probe_rows).__name__,
        )
        return

    distinct_values = sorted({str(row.get("market_and_exchange_names")) for row in probe_rows})
    logger.error(
        "fetch_cot: market_and_exchange_names='%s' returned zero rows; distinct "
        "market_and_exchange_names values from an unfiltered $limit=5 probe: %s",
        config.COT_MARKET_NAME,
        distinct_values,
    )


def fetch_cot() -> Optional[List[dict]]:
    """
    Fetch the most recent COT rows for the single pinned gold contract
    (config.COT_MARKET_NAME -- the standard 100oz COMEX gold future) from
    the CFTC's public Socrata API (no API key needed). Returns parsed rows
    newest first, or None on any failure (INVARIANT 6).

    Filtering on market_and_exchange_names alone (not commodity_name) is
    deliberate: commodity_name='GOLD' matches MULTIPLE distinct contracts on
    this dataset (e.g. standard COMEX gold and e-micro gold), each reporting
    its own open_interest under the same report_date -- mixing them would
    make cot_reports a blend of unrelated contracts, not one coherent series.
    market_and_exchange_names alone is sufficient and tighter.

    Two independent layers make sure only the pinned contract is ever
    persisted: the server-side $where clause, AND a client-side re-check on
    every row (never trust that the server returned exactly what was asked).

    This feed's column names have drifted historically — if the expected
    keys are absent from the first row, the actual keys present are logged
    and this returns None; there is no fuzzy/best-effort substitution.
    Likewise, if the market filter itself is wrong for this dataset (zero
    rows on a valid 200), an unfiltered probe logs the actual values rather
    than guessing.
    """
    params = {
        "$where": f"market_and_exchange_names='{config.COT_MARKET_NAME}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": _COT_LIMIT,
    }
    try:
        resp = requests.get(config.COT_SOCRATA_URL, params=params, timeout=_COT_TIMEOUT_SECONDS)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:
        # No secret in this URL, but keep the Task 7 pattern anyway: log
        # status + exception class only, never the request itself.
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "fetch_cot: request failed status_code=%s error=%s", status_code, type(exc).__name__
        )
        return None

    if not isinstance(rows, list):
        logger.error("fetch_cot: unexpected response shape (%s)", type(rows).__name__)
        return None

    if not rows:
        _probe_market_values()
        return None

    actual_keys = set(rows[0].keys())
    missing = _COT_EXPECTED_KEYS - actual_keys
    if missing:
        logger.error(
            "fetch_cot: expected keys missing %s; actual keys present: %s",
            sorted(missing),
            sorted(actual_keys),
        )
        return None

    parsed: List[dict] = []
    for row in rows:
        # Client-side re-check: never trust the server-side filter alone.
        if row.get("market_and_exchange_names") != config.COT_MARKET_NAME:
            continue
        try:
            report_date = datetime.strptime(row["report_date_as_yyyy_mm_dd"][:10], "%Y-%m-%d").date()
            mm_long = int(float(row["m_money_positions_long_all"]))
            mm_short = int(float(row["m_money_positions_short_all"]))
            open_interest = int(float(row["open_interest_all"]))
        except (KeyError, TypeError, ValueError):
            continue  # skip a malformed row -- never fabricate a value
        parsed.append(
            {
                "report_date": report_date,
                "mm_long": mm_long,
                "mm_short": mm_short,
                "mm_net": mm_long - mm_short,
                "open_interest": open_interest,
            }
        )
    return parsed


def persist_cot(conn, rows: List[dict]) -> int:
    """Upsert COT rows: ON CONFLICT (report_date) DO NOTHING."""
    if not rows:
        return 0
    values = [(r["report_date"], r["mm_long"], r["mm_short"], r["mm_net"], r["open_interest"]) for r in rows]
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
            "VALUES %s ON CONFLICT (report_date) DO NOTHING",
            values,
        )
    return len(values)


# ==========================================================================
# GLD (ETF flow proxy — see HONEST SCOPE REDUCTION above)
# ==========================================================================


def fetch_gld_tonnes() -> Optional[float]:
    """Always None this task — see the module docstring's HONEST SCOPE REDUCTION."""
    logger.info("fetch_gld_tonnes: no reliable tonnage source yet; returning None (see module docstring)")
    return None


def fetch_gld_flow_proxy() -> Optional[Tuple[date, float, int]]:
    """
    GLD's latest daily close + volume via yfinance, as a flow proxy for
    tonnage until a reliable tonnage source exists. Any failure is logged
    and returns None (INVARIANT 6).
    """
    try:
        history = yf.Ticker("GLD").history(period="5d", interval="1d")
    except Exception as exc:
        logger.error("fetch_gld_flow_proxy: yfinance request failed error=%s", type(exc).__name__)
        return None

    if history is None or history.empty:
        logger.error("fetch_gld_flow_proxy: yfinance returned no data")
        return None

    try:
        last = history.iloc[-1]
        ts = history.index[-1].date()
        close = float(last["Close"])
        volume = int(last["Volume"])
    except (KeyError, TypeError, ValueError, IndexError):
        logger.error(
            "fetch_gld_flow_proxy: unexpected response shape, columns=%s", list(history.columns)
        )
        return None

    # float(nan) does NOT raise, so a NaN close would otherwise slide past the
    # except above and get persisted as a literal 'NaN' -- Postgres NUMERIC
    # accepts that value, silently polluting real data. Reject it explicitly.
    if not math.isfinite(close):
        logger.error("fetch_gld_flow_proxy: non-finite close price %r for ts=%s", close, ts)
        return None

    return (ts, close, volume)


def persist_etf_holding(
    conn, ts: date, gld_tonnes: Optional[float], gld_close: Optional[float], gld_volume: Optional[int]
) -> None:
    """Upsert one day's ETF row: ON CONFLICT (ts) DO NOTHING."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO etf_holdings (ts, gld_tonnes, gld_close, gld_volume) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (ts) DO NOTHING",
            (ts, gld_tonnes, gld_close, gld_volume),
        )


# ==========================================================================
# COMEX warehouse stocks
# ==========================================================================


def fetch_comex_stocks() -> Optional[Tuple[float, float]]:
    """
    Download CME's daily Gold_Stocks.xls via a timeout-controlled requests
    GET first — pd.read_excel(url) directly has no timeout control — then
    parse the TOTAL row's registered + eligible troy oz. Any download
    failure is logged and returns None (INVARIANT 6); parsing is delegated
    to parse_comex_workbook() (kept separate so it's directly testable on
    canned bytes with no network).
    """
    try:
        resp = requests.get(_COMEX_URL, timeout=_COMEX_TIMEOUT_SECONDS)
        resp.raise_for_status()
        content = resp.content
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "fetch_comex_stocks: download failed status_code=%s error=%s",
            status_code,
            type(exc).__name__,
        )
        return None

    return parse_comex_workbook(content)


def parse_comex_workbook(content: bytes) -> Optional[Tuple[float, float]]:
    """
    Parse a CME Gold_Stocks workbook's TOTAL row for registered + eligible
    troy oz. The header row is located by searching for cells containing
    "registered" and "eligible" (case-insensitive substrings); the TOTAL row
    is the first row below it with a cell that case-insensitively equals
    "total". Any drift in that layout — a missing header, a missing TOTAL
    row, non-numeric totals — is logged with the sheet's actual shape and
    returns None. There is no fuzzy/best-effort guess.
    """
    try:
        raw = pd.read_excel(BytesIO(content), header=None)
    except Exception:
        logger.error("parse_comex_workbook: failed to open workbook", exc_info=True)
        return None

    header_row = registered_col = eligible_col = None
    for row_idx in range(len(raw)):
        cells = [str(v).strip().lower() if pd.notna(v) else "" for v in raw.iloc[row_idx]]
        reg_matches = [i for i, c in enumerate(cells) if "registered" in c]
        elig_matches = [i for i, c in enumerate(cells) if "eligible" in c]
        if reg_matches and elig_matches:
            header_row, registered_col, eligible_col = row_idx, reg_matches[0], elig_matches[0]
            break

    if header_row is None:
        logger.error(
            "parse_comex_workbook: could not locate REGISTERED/ELIGIBLE header; shape=%s", raw.shape
        )
        return None

    total_row = None
    for row_idx in range(header_row + 1, len(raw)):
        cells = [str(v).strip().lower() if pd.notna(v) else "" for v in raw.iloc[row_idx]]
        if "total" in cells:
            total_row = row_idx
            break

    if total_row is None:
        logger.error("parse_comex_workbook: could not locate TOTAL row; shape=%s", raw.shape)
        return None

    try:
        registered = float(raw.iat[total_row, registered_col])
        eligible = float(raw.iat[total_row, eligible_col])
    except (TypeError, ValueError):
        logger.error(
            "parse_comex_workbook: TOTAL row values not numeric; shape=%s row=%s",
            raw.shape,
            list(raw.iloc[total_row]),
        )
        return None

    return (registered, eligible)


def persist_comex_stocks(conn, ts: date, registered_oz: float, eligible_oz: float) -> None:
    """Upsert one day's COMEX row: ON CONFLICT (ts) DO NOTHING."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO comex_stocks (ts, registered_oz, eligible_oz) VALUES (%s, %s, %s) "
            "ON CONFLICT (ts) DO NOTHING",
            (ts, registered_oz, eligible_oz),
        )


# ==========================================================================
# derived summary
# ==========================================================================


def _latest_cot_rows(conn, limit: int) -> list:
    """Up to `limit` most recent (report_date, mm_net, open_interest) rows, newest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT report_date, mm_net, open_interest FROM cot_reports ORDER BY report_date DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def _latest_comex_registered(conn) -> Optional[float]:
    with conn.cursor() as cur:
        cur.execute("SELECT registered_oz FROM comex_stocks ORDER BY ts DESC LIMIT 1")
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _percentile_rank(values: List[float], target: float) -> float:
    """
    Percentile rank (0-100) of `target` within `values`: the percentage of
    historical observations at or below `target` (ties counted; the
    observation being ranked is itself part of `values`).
    """
    count_le = sum(1 for v in values if v <= target)
    return count_le / len(values) * 100.0


def compute_derived(conn) -> Dict[str, Optional[float]]:
    """
    Derive the small numeric positioning summary from whatever is currently
    persisted (not just this cycle's fetch — a source that succeeded on a
    prior pass still has real historical rows to derive from).

      cot_mm_net          = latest mm_net
      cot_mm_net_pctile    = percentile rank (0-100) of latest mm_net within
                             config.COT_PCTILE_LOOKBACK_WEEKS weeks of
                             history -- None if fewer than 26 weeks of rows
                             exist (never a percentile from thin history)
      etf_flow_5d          = None -- no tonnage source yet (see module docstring)
      comex_coverage       = latest registered_oz / (latest open_interest * 100)
                             (100 oz/contract); None if either is absent
    """
    cot_rows = _latest_cot_rows(conn, config.COT_PCTILE_LOOKBACK_WEEKS)
    cot_mm_net = float(cot_rows[0][1]) if cot_rows and cot_rows[0][1] is not None else None

    cot_mm_net_pctile = None
    if cot_mm_net is not None:
        values = [float(r[1]) for r in cot_rows if r[1] is not None]
        if len(values) >= _MIN_PCTILE_WEEKS:
            cot_mm_net_pctile = _percentile_rank(values, cot_mm_net)

    etf_flow_5d = None  # NULL-tolerant: no tonnage source yet (honest scope reduction)

    latest_open_interest = cot_rows[0][2] if cot_rows and cot_rows[0][2] is not None else None
    registered = _latest_comex_registered(conn)
    comex_coverage = None
    if registered is not None and latest_open_interest:
        comex_coverage = registered / (float(latest_open_interest) * 100.0)

    return {
        "cot_mm_net": cot_mm_net,
        "cot_mm_net_pctile": cot_mm_net_pctile,
        "etf_flow_5d": etf_flow_5d,
        "comex_coverage": comex_coverage,
    }


# ==========================================================================
# cycle + loop
# ==========================================================================


def run_positioning_cycle(now_utc: datetime) -> dict:
    """
    One fetch-persist-derive-publish pass across all three sources. A single
    dead source is logged and skipped — never aborts the cycle. Network
    calls happen OUTSIDE the DB connection checkout, so a slow/failed HTTP
    call never ties up a pooled connection.
    """
    cot_rows = fetch_cot()
    if cot_rows is None:
        logger.warning("run_positioning_cycle: COT fetch failed; continuing")

    gld_flow = fetch_gld_flow_proxy()
    if gld_flow is None:
        logger.warning("run_positioning_cycle: GLD flow-proxy fetch failed; continuing")
    gld_tonnes = fetch_gld_tonnes()  # always None this task; see module docstring

    comex = fetch_comex_stocks()
    if comex is None:
        logger.warning("run_positioning_cycle: COMEX fetch failed; continuing")

    try:
        with database.get_conn() as conn:
            if cot_rows:
                persist_cot(conn, cot_rows)
            if gld_flow is not None:
                ts, close, volume = gld_flow
                persist_etf_holding(conn, ts, gld_tonnes, close, volume)
            if comex is not None:
                registered, eligible = comex
                persist_comex_stocks(conn, now_utc.date(), registered, eligible)
            derived = compute_derived(conn)
    except Exception:
        logger.exception("run_positioning_cycle: DB error while persisting/deriving")
        derived = {k: None for k in _DERIVED_KEYS}

    derived = dict(derived)
    derived["fetched_at"] = now_utc.isoformat()
    STATE.update_market_data("positioning", derived)
    BUS.publish("positioning_update", derived)
    return derived


def run_positioning_agent() -> None:
    """
    Poll every config.POSITIONING_POLL_HOURS. Any exception is logged and
    swallowed so the loop survives (INVARIANT 6).
    """
    while True:
        try:
            run_positioning_cycle(datetime.now(timezone.utc))
        except Exception:
            logger.exception("run_positioning_agent: cycle failed; continuing")
        time.sleep(config.POSITIONING_POLL_HOURS * 3600)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS positioning sensor")
    parser.add_argument(
        "--once", action="store_true", help="Run a single fetch cycle, print the derived dict, and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        derived = run_positioning_cycle(datetime.now(timezone.utc))
        print(derived)
        return

    run_positioning_agent()


if __name__ == "__main__":
    main()
