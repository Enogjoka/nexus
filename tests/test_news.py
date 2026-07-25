"""
Acceptance tests for Task 9 (news half): sensors/news.py.

NO network: feedparser.parse is always monkeypatched. DB-backed tests use
url-prefixed test rows (http://tst-news/...) with FAR-FUTURE fetched_at so
net_news_heat's window can't pull in ambient real rows; the autouse fixture
purges them afterward.
"""
import logging
import socket
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
# fetch_feed: socket timeout set during parse, ALWAYS restored
# --------------------------------------------------------------------------


def test_fetch_feed_sets_and_restores_socket_timeout(monkeypatch):
    socket.setdefaulttimeout(None)  # known baseline
    observed = {}

    def fake_parse(url):
        observed["during"] = socket.getdefaulttimeout()
        return SimpleNamespace(entries=[SimpleNamespace(title="Gold up", link="http://x/1")])

    monkeypatch.setattr(news.feedparser, "parse", fake_parse)

    entries = news.fetch_feed("http://feed")

    assert len(entries) == 1
    assert observed["during"] == config.NEWS_SOCKET_TIMEOUT_SECONDS  # set during parse
    assert socket.getdefaulttimeout() is None                        # restored after


def test_fetch_feed_restores_socket_timeout_even_on_exception(monkeypatch):
    socket.setdefaulttimeout(None)

    def boom(url):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(news.feedparser, "parse", boom)

    entries = news.fetch_feed("http://feed")

    assert entries == []
    assert socket.getdefaulttimeout() is None  # restored despite the exception


def test_fetch_feed_logs_bozo_feed_with_zero_entries(monkeypatch, caplog):
    # feedparser never raises -- a TLS/network failure comes back as bozo=True
    # with 0 entries. That external failure must be LOGGED (INVARIANT 6), not
    # silently treated as "no news".
    socket.setdefaulttimeout(None)
    monkeypatch.setattr(
        news.feedparser, "parse",
        lambda url: SimpleNamespace(entries=[], bozo=True, bozo_exception=OSError("ssl verify failed")),
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
def test_net_news_heat_empty_window_is_zero():
    with database.get_conn() as conn:
        # far-future window with nothing in it -> tanh(0) == 0.0
        heat = news.net_news_heat(conn, datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert heat == 0.0
