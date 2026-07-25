"""
Acceptance tests for Task 9 (news half): sensors/news.py.

NO network: requests.get is always monkeypatched (feedparser then runs on the
mocked bytes, which is pure/offline). DB-backed tests use url-prefixed test
rows (http://tst-news/...) with FAR-FUTURE fetched_at so net_news_heat's
window can't pull in ambient real rows; the autouse fixture purges them
afterward.
"""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
from core import database
from core.state import STATE
from sensors import news

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_HEAT_NOW = datetime(2099, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if config.DATABASE_URL:
        database.execute("DELETE FROM news_articles WHERE url LIKE 'http://tst-news/%'")


@pytest.fixture(autouse=True)
def _reset_state():
    saved = STATE.market_data.get("news_heat")
    yield
    if saved is not None:
        STATE.update_market_data("news_heat", saved)
    else:
        STATE.market_data.pop("news_heat", None)


# --------------------------------------------------------------------------
# Stage-1 prefilter
# --------------------------------------------------------------------------


def test_prefilter_direct_single_hit_passes():
    assert news.passes_prefilter("Gold hits a record high today") is True  # 1 direct


def test_prefilter_macro_needs_two_hits():
    # one macro term alone -> fail
    assert news.passes_prefilter("The unemployment rate ticked up") is False
    # two distinct macro terms, no direct/geo -> pass
    assert news.passes_prefilter("Fed signals a rate cut amid cooling inflation") is True


def test_prefilter_geo_needs_two_hits():
    assert news.passes_prefilter("A localized conflict flared up") is False       # 1 geo
    assert news.passes_prefilter("War and fresh sanctions rattle markets") is True  # 2 geo


def test_prefilter_unrelated_text_fails():
    assert news.passes_prefilter("New smartphone launch breaks sales records") is False
    assert news.passes_prefilter("") is False


# --------------------------------------------------------------------------
# url_hash
# --------------------------------------------------------------------------


def test_url_hash_is_stable_16_hex_chars():
    h = news.url_hash("http://example.com/story")
    assert len(h) == 16
    assert h == news.url_hash("http://example.com/story")           # deterministic
    assert h != news.url_hash("http://example.com/other")           # url-sensitive
    assert all(c in "0123456789abcdef" for c in h)


# --------------------------------------------------------------------------
# fetch_feed: requests fetch -> feedparser.parse(bytes)
# --------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        pass


def test_fetch_feed_request_failure_returns_empty_and_logs(monkeypatch, caplog):
    def boom(url, timeout=None, headers=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(news.requests, "get", boom)

    with caplog.at_level(logging.ERROR):
        entries = news.fetch_feed("http://feed")

    assert entries == []
    assert "request failed" in caplog.text


def test_fetch_feed_sends_user_agent_and_parses(monkeypatch):
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><title>Gold rallies to record</title>"
        b"<link>http://tst-news/a</link>"
        b"<description>gold up on Fed</description></item>"
        b"</channel></rss>"
    )
    captured = {}

    def fake_get(url, timeout=None, headers=None):
        captured["headers"] = headers
        return _FakeResp(content=rss)

    monkeypatch.setattr(news.requests, "get", fake_get)

    entries = news.fetch_feed("http://feed")  # real feedparser runs on the mocked bytes

    assert captured["headers"]["User-Agent"] == config.NEWS_USER_AGENT  # UA sent on every GET
    assert len(entries) == 1
    assert entries[0].title == "Gold rallies to record"
    assert entries[0].link == "http://tst-news/a"


def test_fetch_feed_logs_bozo_feed_with_zero_entries(monkeypatch, caplog):
    # The feed downloads but is unparseable -> feedparser returns bozo=True
    # with 0 entries. That must be LOGGED (INVARIANT 6), not silently treated
    # as "no news".
    monkeypatch.setattr(
        news.requests, "get", lambda url, timeout=None, headers=None: _FakeResp(content=b"garbage")
    )
    monkeypatch.setattr(
        news.feedparser, "parse",
        lambda content: SimpleNamespace(entries=[], bozo=True, bozo_exception=OSError("bad xml")),
    )

    with caplog.at_level(logging.ERROR):
        entries = news.fetch_feed("http://feed")

    assert entries == []
    assert "unreadable" in caplog.text
    assert "OSError" in caplog.text


# --------------------------------------------------------------------------
# hash dedup on persist
# --------------------------------------------------------------------------


@requires_db
def test_persist_article_hash_dedup():
    entry = SimpleNamespace(
        link="http://tst-news/story-1", title="Gold rallies", summary="gold up on Fed",
        published_parsed=(2099, 6, 1, 12, 0, 0, 0, 0, 0),
    )
    with database.get_conn() as conn:
        first = news.persist_article(conn, "TestSrc", entry)
        second = news.persist_article(conn, "TestSrc", entry)  # same url -> ON CONFLICT DO NOTHING

    assert first is True
    assert second is False
    rows = database.fetch("SELECT count(*) FROM news_articles WHERE url = %s", ("http://tst-news/story-1",))
    assert rows[0][0] == 1


# --------------------------------------------------------------------------
# net_news_heat math
# --------------------------------------------------------------------------


@requires_db
def test_net_news_heat_tanh_math():
    import math

    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(5):
                url = f"http://tst-news/heat-{i}"
                cur.execute(
                    "INSERT INTO news_articles (url_hash, url, source, title, fetched_at) "
                    "VALUES (%s, %s, 'TestSrc', 'Gold', %s)",
                    (news.url_hash(url), url, _HEAT_NOW),
                )
        heat = news.net_news_heat(conn, _HEAT_NOW)

    assert abs(heat - math.tanh(5 / 10.0)) < 1e-9  # 5 articles in-window -> tanh(0.5)


@requires_db
def test_net_news_heat_counts_same_cycle_rows():
    # The real-cycle case the old `fetched_at <= now_utc` upper bound broke:
    # now_utc is captured FIRST, then rows are inserted with DB NOW() (which
    # lands strictly AFTER now_utc). With the lower-bound-only window they must
    # still be counted -> heat > 0. (Exact count isn't asserted here because
    # ambient recent rows may also fall in the live 6h window.)
    now_utc = datetime.now(timezone.utc)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(3):
                url = f"http://tst-news/cycle-{i}"
                cur.execute(
                    "INSERT INTO news_articles (url_hash, url, source, title) "
                    "VALUES (%s, %s, 'TestSrc', 'Gold')",  # fetched_at -> DB NOW(), after now_utc
                    (news.url_hash(url), url),
                )
        heat = news.net_news_heat(conn, now_utc)
    assert heat > 0.0


@requires_db
def test_net_news_heat_empty_window_is_zero():
    with database.get_conn() as conn:
        # far-future window with nothing at/after it -> tanh(0) == 0.0
        heat = news.net_news_heat(conn, datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert heat == 0.0
