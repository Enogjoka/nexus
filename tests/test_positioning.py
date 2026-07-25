"""
Acceptance tests for Task 8: the positioning sensor (sensors/positioning.py).

NO network: requests.get/yfinance are always monkeypatched. DB-backed tests
seed synthetic rows with FAR-FUTURE dates (year 2099) so they always sort
ahead of any real historical data and never collide with it -- this keeps
the tests deterministic regardless of whether a real fetch (acceptance step
C) has ever populated these tables. Percentile tests additionally monkeypatch
config.COT_PCTILE_LOOKBACK_WEEKS down to the exact number of synthetic rows
seeded, so the lookback window can never accidentally pull in ambient real
historical rows and contaminate a hand-computed expectation.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

import pytest
from openpyxl import Workbook

import config
from core import database
from core.state import STATE
from sensors import positioning as pos

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE_TABLES = ("cot_reports", "etf_holdings", "comex_stocks")
_FUTURE_DATE_COL = {"cot_reports": "report_date", "etf_holdings": "ts", "comex_stocks": "ts"}


@pytest.fixture(autouse=True)
def _cleanup_future_rows():
    yield
    if not config.DATABASE_URL:
        return
    for table in _FUTURE_TABLES:
        col = _FUTURE_DATE_COL[table]
        database.execute(f"DELETE FROM {table} WHERE {col} >= %s", (date(2099, 1, 1),))


@pytest.fixture(autouse=True)
def _reset_positioning_state():
    saved = STATE.market_data.get("positioning")
    yield
    if saved is not None:
        STATE.update_market_data("positioning", saved)
    else:
        STATE.market_data.pop("positioning", None)


class _FakeResp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _canned_cot_row(**overrides):
    row = {
        "report_date_as_yyyy_mm_dd": "2026-07-14T00:00:00.000",
        "commodity_name": "GOLD",
        "m_money_positions_long_all": "150000",
        "m_money_positions_short_all": "50000",
        "open_interest_all": "450000",
    }
    row.update(overrides)
    return row


def _build_canned_workbook(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# fetch_cot: canned JSON parsing + key-drift defense
# --------------------------------------------------------------------------


def test_fetch_cot_parses_canned_json(monkeypatch):
    payload = [_canned_cot_row(), _canned_cot_row(
        report_date_as_yyyy_mm_dd="2026-07-07T00:00:00.000",
        m_money_positions_long_all="140000",
        m_money_positions_short_all="60000",
        open_interest_all="440000",
    )]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResp(payload=payload)

    monkeypatch.setattr(pos.requests, "get", fake_get)

    result = pos.fetch_cot()

    assert result == [
        {"report_date": date(2026, 7, 14), "mm_long": 150000, "mm_short": 50000,
         "mm_net": 100000, "open_interest": 450000},
        {"report_date": date(2026, 7, 7), "mm_long": 140000, "mm_short": 60000,
         "mm_net": 80000, "open_interest": 440000},
    ]
    assert len(calls) == 1
    assert calls[0]["params"]["$where"] == "commodity_name='GOLD'"
    assert calls[0]["params"]["$order"] == "report_date_as_yyyy_mm_dd DESC"
    assert calls[0]["params"]["$limit"] == 200
    assert calls[0]["timeout"] == 20


def test_fetch_cot_missing_expected_keys_returns_none_and_logs(monkeypatch, caplog):
    bad_row = _canned_cot_row()
    del bad_row["open_interest_all"]

    monkeypatch.setattr(pos.requests, "get", lambda url, params=None, timeout=None: _FakeResp(payload=[bad_row]))

    with caplog.at_level(logging.ERROR):
        result = pos.fetch_cot()

    assert result is None
    assert "open_interest_all" in caplog.text  # the missing key is named
    assert "m_money_positions_long_all" in caplog.text  # actual keys present are named


def test_fetch_cot_request_failure_returns_none(monkeypatch, caplog):
    def boom(url, params=None, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(pos.requests, "get", boom)

    with caplog.at_level(logging.ERROR):
        result = pos.fetch_cot()

    assert result is None
    assert "request failed" in caplog.text


def test_fetch_cot_zero_rows_triggers_probe_and_logs_distinct_values(monkeypatch, caplog):
    # A valid 200 with an empty list means the commodity filter itself may be
    # wrong for this dataset -- fetch_cot() must probe unfiltered and log the
    # actual field values rather than guess at a different filter.
    probe_rows = [
        {"commodity_name": "GOLD - COMMODITY EXCHANGE INC.",
         "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC."},
        {"commodity_name": "SILVER - COMMODITY EXCHANGE INC.",
         "market_and_exchange_names": "SILVER - COMMODITY EXCHANGE INC."},
    ]
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        if "$where" in params:
            return _FakeResp(payload=[])  # filtered query: zero rows
        return _FakeResp(payload=probe_rows)  # unfiltered probe

    monkeypatch.setattr(pos.requests, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        result = pos.fetch_cot()

    assert result is None
    assert len(calls) == 2
    assert "$where" not in calls[1]
    assert calls[1]["$limit"] == 5
    assert "GOLD - COMMODITY EXCHANGE INC." in caplog.text
    assert "SILVER - COMMODITY EXCHANGE INC." in caplog.text


def test_fetch_cot_zero_rows_probe_itself_fails(monkeypatch, caplog):
    def fake_get(url, params=None, timeout=None):
        if "$where" in params:
            return _FakeResp(payload=[])
        raise RuntimeError("probe connection reset")

    monkeypatch.setattr(pos.requests, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        result = pos.fetch_cot()

    assert result is None
    assert "probe" in caplog.text.lower()


# --------------------------------------------------------------------------
# COMEX workbook parsing (pure function, canned bytes, no network)
# --------------------------------------------------------------------------


def test_parse_comex_workbook_extracts_total_row():
    content = _build_canned_workbook([
        ["DEPOSITORY", "REGISTERED", "ELIGIBLE", "TOTAL"],
        ["Brinks", 100000, 200000, 300000],
        ["HSBC", 50000, 80000, 130000],
        ["TOTAL", 150000, 280000, 430000],
    ])
    result = pos.parse_comex_workbook(content)
    assert result == (150000.0, 280000.0)


def test_parse_comex_workbook_malformed_sheet_returns_none_and_logs_shape(caplog):
    content = _build_canned_workbook([
        ["SOMETHING", "ELSE", "ENTIRELY"],
        ["a", "b", "c"],
    ])
    with caplog.at_level(logging.ERROR):
        result = pos.parse_comex_workbook(content)

    assert result is None
    assert "shape" in caplog.text.lower()


def test_parse_comex_workbook_missing_total_row_returns_none(caplog):
    content = _build_canned_workbook([
        ["DEPOSITORY", "REGISTERED", "ELIGIBLE"],
        ["Brinks", 100000, 200000],
        # no TOTAL row
    ])
    with caplog.at_level(logging.ERROR):
        result = pos.parse_comex_workbook(content)

    assert result is None
    assert "TOTAL row" in caplog.text


# --------------------------------------------------------------------------
# percentile: hand-computed on seeded weekly rows; thin history -> None
# --------------------------------------------------------------------------


@requires_db
def test_compute_derived_percentile_hand_computed_60_weeks(monkeypatch):
    monkeypatch.setattr(config, "COT_PCTILE_LOOKBACK_WEEKS", 60)
    base = date(2099, 2, 1)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(59):
                cur.execute(
                    "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
                    "VALUES (%s, 300, 100, 200, 1000)",
                    (base + timedelta(weeks=i),),
                )
            # the 60th (LATEST, most recent date) row is the odd one out
            cur.execute(
                "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
                "VALUES (%s, 150, 50, 100, 1000)",
                (base + timedelta(weeks=59),),
            )
        derived = pos.compute_derived(conn)

    assert derived["cot_mm_net"] == 100.0
    # Only the latest row itself (100) is <= 100; the other 59 rows are all 200.
    expected = 1 / 60 * 100.0
    assert abs(derived["cot_mm_net_pctile"] - expected) < 1e-9


@requires_db
def test_compute_derived_percentile_none_below_min_weeks(monkeypatch):
    monkeypatch.setattr(config, "COT_PCTILE_LOOKBACK_WEEKS", 10)
    base = date(2099, 5, 1)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for i in range(10):
                cur.execute(
                    "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
                    "VALUES (%s, 300, 100, 200, 1000)",
                    (base + timedelta(weeks=i),),
                )
        derived = pos.compute_derived(conn)

    assert derived["cot_mm_net"] == 200.0     # latest value still reported
    assert derived["cot_mm_net_pctile"] is None  # but thin history -> no percentile


# --------------------------------------------------------------------------
# comex_coverage: hand-computed; missing OI -> None
# --------------------------------------------------------------------------


@requires_db
def test_compute_derived_comex_coverage_hand_computed():
    cot_date = date(2099, 6, 1)
    comex_date = date(2099, 6, 2)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
                "VALUES (%s, 300, 100, 200, 500)",
                (cot_date,),
            )
            cur.execute(
                "INSERT INTO comex_stocks (ts, registered_oz, eligible_oz) VALUES (%s, %s, %s)",
                (comex_date, 1_000_000, 2_000_000),
            )
        derived = pos.compute_derived(conn)

    # 1,000,000 registered_oz / (500 contracts * 100 oz/contract) = 20.0 exactly.
    assert derived["comex_coverage"] == 20.0


@requires_db
def test_compute_derived_comex_coverage_none_when_oi_missing():
    cot_date = date(2099, 7, 1)
    comex_date = date(2099, 7, 2)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO cot_reports (report_date, mm_long, mm_short, mm_net, open_interest) "
                "VALUES (%s, 300, 100, 200, NULL)",
                (cot_date,),
            )
            cur.execute(
                "INSERT INTO comex_stocks (ts, registered_oz, eligible_oz) VALUES (%s, %s, %s)",
                (comex_date, 1_000_000, 2_000_000),
            )
        derived = pos.compute_derived(conn)

    assert derived["comex_coverage"] is None


@requires_db
def test_compute_derived_no_cot_data_yields_none_fields():
    """
    Deterministically prove COT-fed fields are None when cot_reports has zero
    rows, WITHOUT ever destroying real historical data: delete inside an open
    transaction, read compute_derived from that same (uncommitted) state,
    then force a rollback so the delete never actually persists.
    """
    class _Rollback(Exception):
        pass

    captured = {}
    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM cot_reports")
            captured["derived"] = pos.compute_derived(conn)
            raise _Rollback()
    except _Rollback:
        pass

    assert captured["derived"]["cot_mm_net"] is None
    assert captured["derived"]["cot_mm_net_pctile"] is None
    assert captured["derived"]["comex_coverage"] is None  # needs OI too


# --------------------------------------------------------------------------
# fetch_gld_tonnes: honest scope reduction
# --------------------------------------------------------------------------


def test_fetch_gld_tonnes_always_none():
    assert pos.fetch_gld_tonnes() is None


def test_fetch_gld_flow_proxy_rejects_nan_close(monkeypatch):
    # float('nan') does not raise on conversion -- a NaN close must be
    # explicitly rejected, not silently persisted as a literal 'NaN'.
    import pandas as pd

    df = pd.DataFrame({"Close": [float("nan")], "Volume": [1000]}, index=pd.to_datetime(["2099-09-01"]))

    class _FakeTicker:
        def history(self, period=None, interval=None):
            return df

    monkeypatch.setattr(pos.yf, "Ticker", lambda symbol: _FakeTicker())

    assert pos.fetch_gld_flow_proxy() is None


# --------------------------------------------------------------------------
# run_positioning_cycle: one dead source never kills the cycle
# --------------------------------------------------------------------------


@requires_db
def test_cycle_survives_dead_cot_source_others_persist(monkeypatch):
    now = datetime(2099, 8, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(pos, "fetch_cot", lambda: None)  # simulated dead source
    monkeypatch.setattr(
        pos, "fetch_gld_flow_proxy", lambda: (date(2099, 8, 1), 190.5, 5_000_000)
    )
    monkeypatch.setattr(pos, "fetch_comex_stocks", lambda: (1_500_000.0, 2_500_000.0))

    derived = pos.run_positioning_cycle(now)

    assert isinstance(derived, dict)
    assert "fetched_at" in derived

    etf_row = database.fetch(
        "SELECT gld_close, gld_volume, gld_tonnes FROM etf_holdings WHERE ts = %s", (date(2099, 8, 1),)
    )
    assert etf_row and float(etf_row[0][0]) == 190.5 and int(etf_row[0][1]) == 5_000_000
    assert etf_row[0][2] is None  # gld_tonnes stays NULL (honest scope reduction)

    comex_row = database.fetch(
        "SELECT registered_oz, eligible_oz FROM comex_stocks WHERE ts = %s", (now.date(),)
    )
    assert comex_row and float(comex_row[0][0]) == 1_500_000.0
