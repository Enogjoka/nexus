"""
Acceptance tests for Task 7: the FRED macro sensor (sensors/fred.py).

NO network: requests.get is always monkeypatched. Derived-math tests seed
synthetic rows with FAR-FUTURE dates (year 2099) so they always sort ahead
of any real historical FRED data and never collide with it — this keeps the
tests deterministic regardless of whether a real fetch (acceptance step C)
has ever populated macro_observations in this database.
"""
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

import config
from core import database
from core.state import BUS, STATE
from sensors import fred

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE_START = date(2099, 1, 5)  # never collides with real (past/present) FRED dates


@pytest.fixture(autouse=True)
def _cleanup_future_rows():
    yield
    if config.DATABASE_URL:
        database.execute("DELETE FROM macro_observations WHERE ts >= %s", (date(2099, 1, 1),))


@pytest.fixture(autouse=True)
def _reset_macro_state():
    saved = STATE.market_data.get("macro")
    yield
    if saved is not None:
        STATE.update_market_data("macro", saved)
    else:
        STATE.market_data.pop("macro", None)


@pytest.fixture(autouse=True)
def _reset_alert_dedup():
    """fred._last_alert_at is module-level mutable state (run_fred_agent's
    alert de-dup); reset it so one test's alert cannot suppress another's."""
    fred._last_alert_at = None
    yield
    fred._last_alert_at = None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _canned_observations(pairs):
    """pairs: list of (date, value_str_or_'.')"""
    return {"observations": [{"date": d.isoformat(), "value": v} for d, v in pairs]}


# --------------------------------------------------------------------------
# fetch_series: parsing + "." skip + request params
# --------------------------------------------------------------------------


def test_fetch_series_parses_and_skips_null_marker(monkeypatch):
    payload = _canned_observations([
        (date(2026, 7, 1), "1.85"),
        (date(2026, 7, 2), "."),      # FRED's null marker -> skipped
        (date(2026, 7, 3), "1.90"),
    ])
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResp(payload)

    monkeypatch.setattr(fred.requests, "get", fake_get)
    monkeypatch.setattr(config, "FRED_API_KEY", "testkey")

    result = fred.fetch_series("DFII10")

    assert result == [(date(2026, 7, 1), 1.85), (date(2026, 7, 3), 1.90)]
    assert len(calls) == 1
    assert calls[0]["params"]["series_id"] == "DFII10"
    assert calls[0]["params"]["api_key"] == "testkey"
    assert calls[0]["params"]["file_type"] == "json"
    assert "observation_start" in calls[0]["params"]
    assert calls[0]["timeout"] == 15


def test_fetch_series_request_failure_returns_none(monkeypatch, caplog):
    def boom(url, params=None, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fred.requests, "get", boom)

    with caplog.at_level(logging.ERROR):
        result = fred.fetch_series("DFII10")

    assert result is None
    assert "request failed" in caplog.text


# --------------------------------------------------------------------------
# run_fred_cycle survives one dead series
# --------------------------------------------------------------------------


@requires_db
def test_run_fred_cycle_survives_one_dead_series(monkeypatch):
    live_pairs = [(date(2099, 6, 1), 4.10)]

    def fake_fetch(series_id):
        if series_id == "DGS2":
            return None  # simulated dead series
        return live_pairs if series_id == "DGS10" else []

    monkeypatch.setattr(fred, "fetch_series", fake_fetch)

    events = []
    BUS.subscribe("macro_update", lambda payload: events.append(payload))

    derived = fred.run_fred_cycle(datetime(2099, 6, 1, 12, tzinfo=timezone.utc))

    assert isinstance(derived, dict)
    assert "fetched_at" in derived
    assert len(events) == 1
    rows = database.fetch(
        "SELECT value FROM macro_observations WHERE series='DGS10' AND ts=%s", (date(2099, 6, 1),)
    )
    assert rows and float(rows[0][0]) == 4.10
    assert STATE.get_market_data("macro")["fetched_at"] == derived["fetched_at"]
    assert derived["last_success_at"] == derived["fetched_at"]  # DGS10 succeeded this pass

    database.execute("DELETE FROM macro_observations WHERE series='DGS10' AND ts=%s", (date(2099, 6, 1),))


# --------------------------------------------------------------------------
# compute_derived: exact hand-computed math on seeded rows
# --------------------------------------------------------------------------


@requires_db
def test_compute_derived_exact_math_on_seeded_rows():
    dfii10_values = [1.60, 1.65, 1.70, 1.75, 1.80, 1.85]  # oldest -> newest
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for i, value in enumerate(dfii10_values):
                cur.execute(
                    "INSERT INTO macro_observations (series, ts, value) VALUES (%s, %s, %s)",
                    ("DFII10", _FUTURE_START + timedelta(days=i), value),
                )
            cur.execute(
                "INSERT INTO macro_observations (series, ts, value) VALUES (%s, %s, %s)",
                ("DGS10", _FUTURE_START, 4.20),
            )
            cur.execute(
                "INSERT INTO macro_observations (series, ts, value) VALUES (%s, %s, %s)",
                ("DGS2", _FUTURE_START, 3.95),
            )
        derived = fred.compute_derived(conn)

    assert derived["real_yield"] == 1.85
    assert round(derived["real_yield_5d_delta"], 10) == round(1.85 - 1.60, 10)  # == 0.25
    assert round(derived["curve_2s10s"], 10) == round(4.20 - 3.95, 10)          # == 0.25


@requires_db
def test_compute_derived_missing_series_returns_none_no_fabrication():
    """
    Guarantee a series has ZERO rows for this assertion by deleting inside an
    open transaction, reading compute_derived from that SAME (uncommitted)
    transaction state, then forcing a rollback -- so no real historical data
    is ever permanently destroyed, regardless of what a live fetch may have
    already populated.
    """
    class _Rollback(Exception):
        pass

    captured = {}
    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM macro_observations WHERE series = 'T10YIE'")
            captured["derived"] = fred.compute_derived(conn)
            raise _Rollback()
    except _Rollback:
        pass

    assert captured["derived"]["breakeven_10y"] is None


# --------------------------------------------------------------------------
# staleness + Telegram alert wiring
# --------------------------------------------------------------------------


def test_check_staleness_27h_old_returns_warning():
    STATE.update_market_data(
        "macro", {"fetched_at": (datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)).isoformat()}
    )
    now = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)  # 27h later
    warning = fred.check_staleness(now)
    assert isinstance(warning, str)
    assert "stale" in warning.lower()


def test_check_staleness_1h_old_returns_none():
    STATE.update_market_data(
        "macro", {"fetched_at": (datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)).isoformat()}
    )
    now = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)  # 1h later
    assert fred.check_staleness(now) is None


def test_check_staleness_no_prior_fetch_returns_none():
    STATE.market_data.pop("macro", None)
    assert fred.check_staleness(datetime(2026, 7, 18, tzinfo=timezone.utc)) is None


def test_check_staleness_prefers_last_success_at_when_present():
    # last_success_at is fresh (1h old); fetched_at is stale (17 days old) but
    # must be IGNORED whenever last_success_at is set -- data success, not
    # scheduler liveness, is what staleness tracks.
    STATE.update_market_data(
        "macro",
        {
            "last_success_at": datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc).isoformat(),
            "fetched_at": datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc).isoformat(),
        },
    )
    now = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    assert fred.check_staleness(now) is None


@requires_db
def test_all_series_failing_over_time_eventually_stale(monkeypatch):
    # Every series fails on every pass -- last_success_at never gets set.
    monkeypatch.setattr(fred, "fetch_series", lambda series_id: None)

    t0 = datetime(2099, 3, 1, 0, 0, tzinfo=timezone.utc)
    for hours in (0, 10, 20):
        fred.run_fred_cycle(t0 + timedelta(hours=hours))

    macro = STATE.get_market_data("macro")
    assert macro["last_success_at"] is None  # never once succeeded

    # No new pass has run since t0+20h; the clock advances past the threshold.
    later = t0 + timedelta(hours=20 + config.STALE_DATA_ALERT_HOURS + 1)
    warning = fred.check_staleness(later)
    assert warning is not None
    assert "stale" in warning.lower()


@requires_db
def test_partial_success_sets_fresh_last_success_no_warning(monkeypatch):
    def fake_fetch(series_id):
        return [(date(2099, 7, 1), 1.5)] if series_id == "DFII10" else None

    monkeypatch.setattr(fred, "fetch_series", fake_fetch)

    now = datetime(2099, 7, 1, 12, tzinfo=timezone.utc)
    derived = fred.run_fred_cycle(now)

    assert derived["last_success_at"] == now.isoformat()
    assert fred.check_staleness(now) is None


def test_run_fred_agent_alerts_once_per_stale_iteration(monkeypatch):
    STATE.update_market_data(
        "macro", {"fetched_at": datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat()}
    )  # far more than STALE_DATA_ALERT_HOURS old

    calls = {"send_alert": 0, "cycle": 0}
    monkeypatch.setattr(fred, "send_alert", lambda text: calls.__setitem__("send_alert", calls["send_alert"] + 1))
    monkeypatch.setattr(fred, "run_fred_cycle", lambda now: calls.__setitem__("cycle", calls["cycle"] + 1))

    class _StopLoop(Exception):
        pass

    def stop_sleep(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(fred.time, "sleep", stop_sleep)

    with pytest.raises(_StopLoop):
        fred.run_fred_agent()

    assert calls["send_alert"] == 1
    assert calls["cycle"] == 1


def test_alert_dedup_two_consecutive_stale_iterations_sends_once(monkeypatch):
    # Stale from the start, and stays stale: run_fred_cycle is mocked as a
    # no-op that never advances last_success_at/fetched_at, so BOTH loop
    # iterations see the same stale condition.
    STATE.update_market_data(
        "macro",
        {"fetched_at": datetime(2026, 7, 1, tzinfo=timezone.utc).isoformat(), "last_success_at": None},
    )

    calls = {"send_alert": 0, "cycle": 0}
    monkeypatch.setattr(fred, "send_alert", lambda text: calls.__setitem__("send_alert", calls["send_alert"] + 1))
    monkeypatch.setattr(fred, "run_fred_cycle", lambda now: calls.__setitem__("cycle", calls["cycle"] + 1))

    class _StopLoop(Exception):
        pass

    iterations = {"n": 0}

    def stop_after_two(_seconds):
        iterations["n"] += 1
        if iterations["n"] >= 2:
            raise _StopLoop()

    monkeypatch.setattr(fred.time, "sleep", stop_after_two)

    with pytest.raises(_StopLoop):
        fred.run_fred_agent()

    assert calls["cycle"] == 2       # the loop ran twice
    assert calls["send_alert"] == 1  # but the alert was de-duped to once
