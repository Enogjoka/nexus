"""
NEXUS economic calendar sensor — this is what ARMS validator RULE 1.

It fetches the week's US economic events from ForexFactory's long-stable
public weekly JSON feed, persists USD events, and hands the validator a
list of upcoming events in exactly the shape RULE 1 reads:

    {"minutes_until": float, "impact": "HIGH"|"MEDIUM"|"LOW", "name": str}

The validator applies its OWN blackout window (config.EVENT_BLOCK_MINUTES,
30 min) against that list; this sensor supplies the wider candidate window
(config.EVENT_LOOKAHEAD_MINUTES, 120 min). NO interpretation lives here.

INVARIANT 6: the one external call (the FF JSON feed) is timeout-wrapped,
try/except-guarded, and a failure is logged and returns None — never
raises. The request URL is never logged (Task 7 pattern). A drifted feed
shape (missing expected keys) logs the actual keys and returns None rather
than silently substituting.
"""
import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

# CLI-only: `python3 -m sensors.calendar_agent --once` is a standalone entry
# point, so .env must be loaded here, BEFORE `import config` below reads the
# environment (config.py reads env vars at module-import time). A library
# import of this module does NOT hit this branch and inherits the parent's
# environment -- mirrors sensors/fred.py and sensors/positioning.py.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_CAL_TIMEOUT_SECONDS = 15
_CAL_EXPECTED_KEYS = {"title", "country", "date", "impact"}


def fetch_calendar() -> Optional[List[dict]]:
    """
    Fetch and parse the week's events from the ForexFactory JSON feed.
    Returns a list of event dicts (all currencies; persist filters to USD),
    or None on any failure. Each event dict:

        {"event_ts": datetime (UTC, tz-aware), "name": str, "currency": str,
         "impact": str (UPPER), "forecast": str|None, "previous": str|None,
         "actual": str|None}

    A row whose date can't be parsed is skipped, never fabricated.
    """
    try:
        resp = requests.get(config.CALENDAR_URL, timeout=_CAL_TIMEOUT_SECONDS)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        # Keep the Task 7 pattern: log status + exception class only, never
        # the request URL itself.
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error(
            "fetch_calendar: request failed status_code=%s error=%s", status_code, type(exc).__name__
        )
        return None

    if not isinstance(raw, list) or not raw:
        logger.error("fetch_calendar: unexpected/empty response shape (%s)", type(raw).__name__)
        return None

    actual_keys = set(raw[0].keys())
    missing = _CAL_EXPECTED_KEYS - actual_keys
    if missing:
        logger.error(
            "fetch_calendar: expected keys missing %s; actual keys present: %s",
            sorted(missing),
            sorted(actual_keys),
        )
        return None

    events: List[dict] = []
    for entry in raw:
        raw_date = entry.get("date")
        name = entry.get("title")
        currency = entry.get("country")
        impact = entry.get("impact")
        if not raw_date or not name or not currency or impact is None:
            continue
        try:
            event_ts = datetime.fromisoformat(raw_date).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue  # unparseable timestamp -- skip, never fabricate
        events.append(
            {
                "event_ts": event_ts,
                "name": name,
                "currency": currency,
                "impact": str(impact).upper(),
                "forecast": entry.get("forecast") or None,
                "previous": entry.get("previous") or None,
                "actual": entry.get("actual") or None,
            }
        )
    return events


def persist_events(conn, events: List[dict]) -> int:
    """
    Upsert USD events (config.CALENDAR_CURRENCIES). ON CONFLICT (event_ts,
    name) DO UPDATE SET actual = EXCLUDED.actual so a later pass fills in the
    `actual` value after a release without duplicating the row. Returns the
    number of rows written.
    """
    filtered = [e for e in events if e["currency"] in config.CALENDAR_CURRENCIES]
    if not filtered:
        return 0
    with conn.cursor() as cur:
        for e in filtered:
            cur.execute(
                "INSERT INTO econ_events (event_ts, name, currency, impact, actual, forecast, previous) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (event_ts, name) DO UPDATE SET actual = EXCLUDED.actual",
                (e["event_ts"], e["name"], e["currency"], e["impact"], e["actual"], e["forecast"], e["previous"]),
            )
    return len(filtered)


def upcoming_events(conn, now_utc: datetime) -> List[dict]:
    """
    THE VALIDATOR FEED. Events in the next config.EVENT_LOOKAHEAD_MINUTES,
    shaped EXACTLY as risk/validator.py RULE 1 reads them:

        {"minutes_until": float, "impact": str, "name": str}

    Only future events within the window are returned (minutes_until >= 0).
    The validator decides which impact levels and how-close-is-too-close
    matter; this just supplies the candidates.
    """
    lookahead = now_utc + timedelta(minutes=config.EVENT_LOOKAHEAD_MINUTES)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_ts, name, impact FROM econ_events "
            "WHERE event_ts >= %s AND event_ts <= %s ORDER BY event_ts",
            (now_utc, lookahead),
        )
        rows = cur.fetchall()

    result: List[dict] = []
    for event_ts, name, impact in rows:
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)
        minutes_until = (event_ts - now_utc).total_seconds() / 60.0
        result.append({"minutes_until": minutes_until, "impact": impact, "name": name})
    return result


def recent_high_events(conn, now_utc: datetime, lookback_min: int = 20) -> List[dict]:
    """
    THE S4 FEED. HIGH-impact USD events ALREADY RELEASED within the last
    `lookback_min` minutes, shaped as:

        {"minutes_since": float, "name": str}

    The mirror image of upcoming_events: that one looks forward and reports
    minutes_until, this one looks back and reports minutes_since. Only events
    strictly in the past are returned (minutes_since >= 0), so an event that
    has not printed yet can never arm a pod.

    Impact is filtered HERE rather than by the caller, unlike upcoming_events
    which hands the validator every candidate. The difference is deliberate:
    the validator decides for itself what is close enough to matter across all
    impact levels, whereas "a HIGH-impact release just happened" is the entire
    definition of the event S4 waits for — a MEDIUM print is not a weaker
    version of it, it is a different thing.
    """
    since = now_utc - timedelta(minutes=lookback_min)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_ts, name FROM econ_events "
            "WHERE event_ts >= %s AND event_ts <= %s AND upper(impact) = 'HIGH' "
            "ORDER BY event_ts DESC",
            (since, now_utc),
        )
        rows = cur.fetchall()

    result: List[dict] = []
    for event_ts, name in rows:
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)
        minutes_since = (now_utc - event_ts).total_seconds() / 60.0
        result.append({"minutes_since": minutes_since, "name": name})
    return result


def run_calendar_cycle(now_utc: datetime) -> dict:
    """
    One fetch-persist-publish pass. Network happens OUTSIDE the DB checkout
    (a slow feed never ties up a pooled connection). upcoming_events is
    computed from the table (so it works even if this fetch failed but prior
    passes populated rows), written to STATE.market_data["upcoming_events"]
    for the analyst, and published on BUS "calendar_update".
    """
    events = fetch_calendar()
    if events is None:
        logger.warning("run_calendar_cycle: calendar fetch failed; continuing from stored events")

    persisted = 0
    try:
        with database.get_conn() as conn:
            if events:
                persisted = persist_events(conn, events)
            upcoming = upcoming_events(conn, now_utc)
    except Exception:
        logger.exception("run_calendar_cycle: DB error while persisting/deriving")
        upcoming = []

    STATE.update_market_data("upcoming_events", upcoming)
    BUS.publish("calendar_update", upcoming)
    return {
        "fetched": len(events) if events else 0,
        "persisted": persisted,
        "upcoming": upcoming,
        "fetched_at": now_utc.isoformat(),
    }


def run_calendar_agent() -> None:
    """Poll every config.CALENDAR_POLL_HOURS. The loop survives any cycle
    exception (INVARIANT 6)."""
    while True:
        try:
            run_calendar_cycle(datetime.now(timezone.utc))
        except Exception:
            logger.exception("run_calendar_agent: cycle failed; continuing")
        time.sleep(config.CALENDAR_POLL_HOURS * 3600)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS economic calendar sensor")
    parser.add_argument(
        "--once", action="store_true", help="Run a single fetch cycle, print a summary, and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        summary = run_calendar_cycle(datetime.now(timezone.utc))
        print(summary)
        return

    run_calendar_agent()


if __name__ == "__main__":
    main()
