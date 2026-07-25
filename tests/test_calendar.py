"""
Acceptance tests for Task 9 (calendar half): sensors/calendar_agent.py plus
the end-to-end wiring into validator RULE 1.

NO network: requests.get is always monkeypatched. DB-backed tests seed
FAR-FUTURE (year 2099) rows so they sort ahead of and never collide with any
real historical data; the autouse fixture purges them (and any TST_CAL
validator_log rows the integration test writes) afterward.
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

import config
from ai.price_resolver import ResolvedSignal, SignalAnchors
from core import database
from core.state import STATE
from risk.validator import validate
from sensors import calendar_agent as cal

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE = datetime(2099, 6, 1, 8, 0, tzinfo=timezone.utc)  # non-fix hour, far future


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if not config.DATABASE_URL:
        return
    database.execute("DELETE FROM econ_events WHERE event_ts >= %s", (datetime(2099, 1, 1, tzinfo=timezone.utc),))
    database.execute("DELETE FROM validator_log WHERE symbol LIKE 'TST_CAL%'")


@pytest.fixture(autouse=True)
def _reset_state():
    saved = STATE.market_data.get("upcoming_events")
    yield
    if saved is not None:
        STATE.update_market_data("upcoming_events", saved)
    else:
        STATE.market_data.pop("upcoming_events", None)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _ff_entry(**overrides):
    entry = {
        "title": "Non-Farm Employment Change",
        "country": "USD",
        "date": "2099-06-01T13:30:00-05:00",  # -> 18:30 UTC
        "impact": "High",
        "forecast": "180K",
        "previous": "175K",
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------
# fetch_calendar: parsing, impact normalization, shape drift
# --------------------------------------------------------------------------


def test_fetch_calendar_parses_and_normalizes_impact(monkeypatch):
    payload = [_ff_entry(), _ff_entry(title="ISM Services PMI", impact="medium", date="2099-06-02T10:00:00-04:00")]
    monkeypatch.setattr(cal.requests, "get", lambda url, timeout=None: _FakeResp(payload))

    result = cal.fetch_calendar()

    assert result[0]["event_ts"] == datetime(2099, 6, 1, 18, 30, tzinfo=timezone.utc)
    assert result[0]["name"] == "Non-Farm Employment Change"
    assert result[0]["currency"] == "USD"
    assert result[0]["impact"] == "HIGH"        # normalized to upper
    assert result[1]["impact"] == "MEDIUM"      # lowercase input normalized
    assert result[1]["event_ts"] == datetime(2099, 6, 2, 14, 0, tzinfo=timezone.utc)


def test_fetch_calendar_drifted_shape_returns_none_and_logs_keys(monkeypatch, caplog):
    drifted = {"headline": "x", "ccy": "USD", "when": "2099-06-01T13:30:00-05:00"}  # no expected keys
    monkeypatch.setattr(cal.requests, "get", lambda url, timeout=None: _FakeResp([drifted]))

    with caplog.at_level(logging.ERROR):
        result = cal.fetch_calendar()

    assert result is None
    assert "impact" in caplog.text and "title" in caplog.text  # missing keys named
    assert "headline" in caplog.text                            # actual keys named


def test_fetch_calendar_request_failure_returns_none(monkeypatch, caplog):
    def boom(url, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(cal.requests, "get", boom)

    with caplog.at_level(logging.ERROR):
        result = cal.fetch_calendar()

    assert result is None
    assert "request failed" in caplog.text


# --------------------------------------------------------------------------
# persist: USD filter + actual-fills-in on conflict
# --------------------------------------------------------------------------


@requires_db
def test_persist_events_usd_filter():
    events = [
        {"event_ts": _FUTURE, "name": "US CPI", "currency": "USD", "impact": "HIGH",
         "forecast": None, "previous": None, "actual": None},
        {"event_ts": _FUTURE, "name": "EU CPI", "currency": "EUR", "impact": "HIGH",
         "forecast": None, "previous": None, "actual": None},
    ]
    with database.get_conn() as conn:
        written = cal.persist_events(conn, events)

    assert written == 1  # only the USD event
    rows = database.fetch("SELECT name, currency FROM econ_events WHERE event_ts = %s ORDER BY name", (_FUTURE,))
    assert rows == [("US CPI", "USD")]


@requires_db
def test_persist_events_actual_fills_in_on_conflict():
    event = {"event_ts": _FUTURE, "name": "US CPI", "currency": "USD", "impact": "HIGH",
             "forecast": "3.2%", "previous": "3.1%", "actual": None}
    with database.get_conn() as conn:
        cal.persist_events(conn, [event])
        event_with_actual = dict(event, actual="3.4%")  # release lands
        cal.persist_events(conn, [event_with_actual])

    rows = database.fetch("SELECT actual FROM econ_events WHERE event_ts = %s AND name = %s", (_FUTURE, "US CPI"))
    assert rows == [("3.4%",)]  # DO UPDATE SET actual filled it in, no duplicate row


# --------------------------------------------------------------------------
# upcoming_events window math -- the validator feed shape
# --------------------------------------------------------------------------


@requires_db
def test_upcoming_events_window_math_hand_computed():
    now = _FUTURE
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            # 15 min out (in window, HIGH), 90 min out (in window), 200 min out (past 120 lookahead)
            cur.execute("INSERT INTO econ_events (event_ts, name, currency, impact) VALUES (%s,%s,'USD','HIGH')",
                        (now + timedelta(minutes=15), "Soon"))
            cur.execute("INSERT INTO econ_events (event_ts, name, currency, impact) VALUES (%s,%s,'USD','LOW')",
                        (now + timedelta(minutes=90), "Later"))
            cur.execute("INSERT INTO econ_events (event_ts, name, currency, impact) VALUES (%s,%s,'USD','HIGH')",
                        (now + timedelta(minutes=200), "TooFar"))
        upcoming = cal.upcoming_events(conn, now)

    names = {e["name"]: e for e in upcoming}
    assert set(names) == {"Soon", "Later"}          # TooFar excluded (> 120 min)
    assert names["Soon"]["minutes_until"] == 15.0
    assert names["Soon"]["impact"] == "HIGH"
    assert names["Later"]["minutes_until"] == 90.0


# --------------------------------------------------------------------------
# integration: calendar-produced shape -> validator RULE 1 WAIT
# --------------------------------------------------------------------------


@requires_db
def test_calendar_feed_triggers_validator_rule1_wait():
    now = _FUTURE  # 08:00 UTC -- deliberately NOT within a London fix window
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO econ_events (event_ts, name, currency, impact) VALUES (%s,%s,'USD','HIGH')",
                (now + timedelta(minutes=10), "FOMC Statement"),
            )
        upcoming = cal.upcoming_events(conn, now)

    sig = SignalAnchors(direction="LONG", entry_anchor="H1_EMA20", entry_offset_pips=0.0,
                        stop_anchor="H1_EMA50", stop_offset_pips=0.0, tp1_rr=1.5, tp2_rr=3.0)
    resolved = ResolvedSignal(entry_price=4100.0, stop_price=4090.0, tp1_price=4115.0,
                              tp2_price=4130.0, risk_per_unit=10.0, warnings=[])
    ctx = {
        "utc_now": now,
        "upcoming_events": upcoming,  # <-- exactly what the calendar produced
        "rsi": {"h1": 50.0, "h4": 50.0},
        "regime": {"h4": "RANGE", "d1": "RANGE"},
        "symbol": "TST_CAL",
    }

    verdict = validate(sig, resolved, "A", 80, ctx)

    assert verdict.action == "WAIT"
    assert "RULE1_EVENT_BLOCK" in verdict.rules_fired
    assert any("high-impact event" in r for r in verdict.reasons)
