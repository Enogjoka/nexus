"""
NEXUS news sensor — the smallest honest version of the news pipeline: pull a
handful of RSS feeds, keep only articles that pass a cheap keyword prefilter,
persist them, and expose a crude "news heat" scalar.

DEFERRED (documented, not silently skipped):
  * Stage-2 AI relevance/direction scoring belongs to the fusion task -- no
    Gemini/AI wiring here; the news_articles.relevance/direction columns stay
    NULL. `net_news_heat` is a crude count-based proxy that the scored
    version replaces later.
  * rapidfuzz near-duplicate title dedup is deferred; this task dedups on the
    url_hash primary-dedup key only (ON CONFLICT DO NOTHING). A one-time TODO
    is logged so the gap is visible, not hidden.

INVARIANT 6: each feed is fetched with requests (timeout 15s, try/except ->
log + []), and only the downloaded bytes are handed to feedparser.parse().
requests brings certifi's CA bundle, so TLS verification actually succeeds
where feedparser's bare-urllib fetch did not. Any feed failure is logged
(no URL in the message, Task 7 pattern) and yields an empty entry list --
one dead feed never kills the cycle or the loop.
"""
import argparse
import hashlib
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import feedparser
import requests

# CLI-only: `python3 -m sensors.news --once` is a standalone entry point, so
# .env must be loaded here, BEFORE `import config` below reads the environment
# (config.py reads env vars at module-import time). A library import of this
# module does NOT hit this branch -- mirrors the other sensors.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_dedup_todo_logged = False


# ==========================================================================
# Stage-1 keyword prefilter
# ==========================================================================


def _count_hits(text_lower: str, keywords: List[str]) -> int:
    """Number of DISTINCT keywords from the list present as substrings."""
    return sum(1 for kw in keywords if kw in text_lower)


def passes_prefilter(text: str) -> bool:
    """
    Stage-1 keyword gate: passes if >=1 GOLD_DIRECT hit, OR >=2 distinct
    GOLD_MACRO hits, OR >=2 distinct GOLD_GEOPOLITICAL hits. Case-insensitive
    substring matching.
    """
    if not text:
        return False
    lowered = text.lower()
    direct = _count_hits(lowered, config.GOLD_DIRECT)
    macro = _count_hits(lowered, config.GOLD_MACRO)
    geo = _count_hits(lowered, config.GOLD_GEOPOLITICAL)
    return direct >= 1 or macro >= 2 or geo >= 2


# ==========================================================================
# fetch + parse
# ==========================================================================


def url_hash(url: str) -> str:
    """Stable 16-hex-char dedup key: first 16 chars of sha256(url)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def fetch_feed(url: str) -> list:
    """
    Fetch one RSS feed with requests (so certifi's CA bundle is used for TLS)
    and hand the raw bytes to feedparser.parse(). Any request failure is
    logged and returns an empty list (INVARIANT 6); the URL is never logged
    (Task 7 pattern). A feed that downloads but is unparseable comes back
    from feedparser as `bozo` with zero entries -- that too is logged rather
    than silently returned as "no news".
    """
    try:
        resp = requests.get(
            url,
            timeout=config.NEWS_FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": config.NEWS_USER_AGENT},
        )
        resp.raise_for_status()
        content = resp.content
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        logger.error("fetch_feed: request failed status_code=%s error=%s", status_code, type(exc).__name__)
        return []

    parsed = feedparser.parse(content)
    entries = list(getattr(parsed, "entries", []) or [])
    if not entries and getattr(parsed, "bozo", False):
        bozo_exc = getattr(parsed, "bozo_exception", None)
        logger.error(
            "fetch_feed: feed unreadable (0 entries) error=%s",
            type(bozo_exc).__name__ if bozo_exc is not None else "unknown",
        )
    return entries


def _published_at(entry) -> Optional[datetime]:
    parsed_time = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed_time is None:
        return None
    try:
        return datetime(*parsed_time[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def persist_article(conn, source: str, entry) -> bool:
    """
    Persist one passing article to the existing news_articles table, deduped
    on url_hash (ON CONFLICT DO NOTHING). relevance/direction stay NULL --
    Stage-2 scoring is the fusion task's job. Returns True if a row was
    inserted (False if it was a duplicate or had no URL).
    """
    url = getattr(entry, "link", None)
    if not url:
        return False
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO news_articles (url_hash, url, source, title, summary, published_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url_hash) DO NOTHING",
            (
                url_hash(url),
                url,
                source,
                getattr(entry, "title", None),
                getattr(entry, "summary", None),
                _published_at(entry),
            ),
        )
        return cur.rowcount > 0


# ==========================================================================
# crude news heat (replaced by the scored version in fusion)
# ==========================================================================


def net_news_heat(conn, now_utc: datetime) -> float:
    """
    tanh(count / 10) where count is the number of prefilter-passing articles
    fetched in the last config.NEWS_HEAT_LOOKBACK_HOURS (only passing articles
    are ever persisted, so a straight row count in the window IS that count).
    Crude by design; the scored version replaces it in fusion.

    Lower bound only (no `fetched_at <= now_utc`): a cycle captures now_utc at
    its start but the rows it inserts get a DB NOW() a few seconds LATER, so an
    upper bound of now_utc would wrongly exclude the very articles this cycle
    just fetched. "Last 6h" needs only the floor.
    """
    window_start = now_utc - timedelta(hours=config.NEWS_HEAT_LOOKBACK_HOURS)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM news_articles WHERE fetched_at >= %s",
            (window_start,),
        )
        count = cur.fetchone()[0]
    return math.tanh(count / 10.0)


# ==========================================================================
# cycle + loop
# ==========================================================================


def run_news_cycle(now_utc: datetime) -> dict:
    """
    One pass across all config.NEWS_FEEDS: fetch, prefilter, persist passing
    articles, then compute news heat. One dead feed is logged and skipped.
    Network happens OUTSIDE the DB checkout.
    """
    global _dedup_todo_logged
    if not _dedup_todo_logged:
        logger.info("run_news_cycle: TODO rapidfuzz near-duplicate title dedup deferred; url_hash dedup only")
        _dedup_todo_logged = True

    to_persist = []  # (source, entry) for articles that passed the prefilter
    for source, url in config.NEWS_FEEDS.items():
        for entry in fetch_feed(url):
            text = f"{getattr(entry, 'title', '') or ''} {getattr(entry, 'summary', '') or ''}"
            if passes_prefilter(text):
                to_persist.append((source, entry))

    inserted = 0
    try:
        with database.get_conn() as conn:
            for source, entry in to_persist:
                if persist_article(conn, source, entry):
                    inserted += 1
            heat = net_news_heat(conn, now_utc)
    except Exception:
        logger.exception("run_news_cycle: DB error while persisting/deriving")
        heat = None

    summary = {
        "passed_prefilter": len(to_persist),
        "inserted": inserted,
        "news_heat": heat,
        "fetched_at": now_utc.isoformat(),
    }
    STATE.update_market_data("news_heat", heat)
    BUS.publish("news_update", summary)
    return summary


def run_news_agent() -> None:
    """Poll every config.NEWS_POLL_MINUTES. The loop survives any cycle
    exception (INVARIANT 6)."""
    while True:
        try:
            run_news_cycle(datetime.now(timezone.utc))
        except Exception:
            logger.exception("run_news_agent: cycle failed; continuing")
        time.sleep(config.NEWS_POLL_MINUTES * 60)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS news sensor")
    parser.add_argument(
        "--once", action="store_true", help="Run a single fetch cycle, print a summary, and exit"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        summary = run_news_cycle(datetime.now(timezone.utc))
        print(summary)
        return

    run_news_agent()


if __name__ == "__main__":
    main()
