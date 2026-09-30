"""
Tests for the Observatory (Task O2): a separate, read-only API process.

Two database identities are used, deliberately:
  * the OWNER (DATABASE_URL = nexus_dev) seeds rows and removes them by id;
  * the read-only `observatory` role is the only identity the observatory code
    ever connects as. Tests needing it skip with a pointer to
    ops/dev/local_readonly_roles.sql when the role does not exist.
No network: the API is exercised through FastAPI's TestClient.
"""
import ast
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from observatory import queries
from observatory.api import build_app
from observatory.db import DatabaseUnavailable, ReadOnlyDB, jsonable
from observatory.events import Projector
from observatory.settings import MIN_TOKEN_LENGTH, Settings, SettingsError, load_settings

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "observatory"
PACKAGE_FILES = sorted(PACKAGE.glob("*.py"))

TOKEN = "test-token-" + "x" * 32  # a test fixture, not a secret
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SETTINGS = Settings(database_url="dbname=unused", token=TOKEN)

OWNER_URL = os.environ.get("DATABASE_URL", "")


def _observatory_dsn() -> str:
    explicit = os.environ.get("OBSERVATORY_TEST_DATABASE_URL")
    if explicit:
        return explicit
    params = psycopg2.extensions.parse_dsn(OWNER_URL)
    params.pop("password", None)
    params["user"] = "observatory"
    return psycopg2.extensions.make_dsn(**params)


@pytest.fixture(scope="module")
def owner():
    if not OWNER_URL:
        pytest.skip("DATABASE_URL not set")
    conn = psycopg2.connect(OWNER_URL)
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def obs_dsn(owner):
    with owner.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'observatory'")
        if cur.fetchone() is None:
            pytest.skip("observatory role missing: run ops/dev/local_readonly_roles.sql")
    return _observatory_dsn()


@pytest.fixture
def obs_db(obs_dsn):
    db = ReadOnlyDB(obs_dsn)
    yield db
    db.close()


class Seeder:
    """Owner-side inserts, each remembered and removed by id afterwards."""

    def __init__(self, conn):
        self.conn = conn
        self.created = []  # (table, id)

    def add(self, table, **values):
        columns = ", ".join(values)
        marks = ", ".join(["%s"] * len(values))
        params = [json.dumps(v) if isinstance(v, (dict, list)) and table != "doctrines" else v for v in values.values()]
        with self.conn.cursor() as cur:
            cur.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks}) RETURNING id", params)
            row_id = cur.fetchone()[0]
        self.created.append((table, row_id))
        return row_id

    def set(self, table, row_id, **values):
        assignments = ", ".join(f"{k} = %s" for k in values)
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET {assignments} WHERE id = %s", [*values.values(), row_id])

    def cleanup(self):
        with self.conn.cursor() as cur:
            for table, row_id in reversed(self.created):
                cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))


@pytest.fixture
def seed(owner):
    seeder = Seeder(owner)
    yield seeder
    seeder.cleanup()


def _doctrine(seed, ts, bias="BOTH", source="FABLE", raw="{}", horizon=30, swing=True):
    return seed.add(
        "doctrines", ts=ts, bias=bias, conviction=5, risk_multiplier=1.0, enabled_pods=[],
        swing_signals_allowed=swing, review_horizon_min=horizon, source=source, raw_response=raw,
    )


def _signal(seed, ts, direction="LONG", status="PENDING", symbol="TST_OBS", **extra):
    return seed.add(
        "signals", ts=ts, symbol=symbol, direction=direction, status=status, grade="A",
        confidence=80, prices={"entry": 4100.0, "stop": 4090.0, "tp1": 4115.0, "tp2": 4130.0},
        thesis="observatory test", **extra,
    )


RULES = [
    "RULE0_ENTRY_DRIFT", "RULE1_EVENT_BLOCK", "RULE2_RSI_EXTREME", "RULE3_REGIME_CONFLICT",
    "RULE4_VOLATILE_CAP", "RULE5_MACRO_DIVERGENCE", "RULE6_RR_FLOOR", "RULE7_CONFIDENCE_FLOOR",
]


# ==========================================================================
# isolation: no repo imports, no write SQL, read-only role
# ==========================================================================


def _repo_top_level_modules():
    names = set()
    for path in REPO.iterdir():
        if path.name.startswith(".") or path.name == "observatory":
            continue
        if path.is_dir() and any(path.glob("*.py")):
            names.add(path.name)
        elif path.suffix == ".py":
            names.add(path.stem)
    return names


def test_observatory_imports_nothing_from_the_repo():
    forbidden = _repo_top_level_modules()
    # The guard is only meaningful if it knows the trading packages.
    assert {"config", "core", "ai", "risk", "exec_", "sensors", "fusion", "data", "ops", "link"} <= forbidden
    assert PACKAGE_FILES, "observatory package not found"

    offenders = []
    for path in PACKAGE_FILES:
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            offenders += [f"{path.name}:{node.lineno} imports {r}" for r in roots if r in forbidden]
        assert "__import__" not in source and "import_module" not in source, path.name
    assert offenders == []


def test_importing_the_observatory_loads_no_trading_module():
    # A fresh interpreter with the repo on sys.path: any transitive import of a
    # trading module would succeed here, so its absence proves there is none.
    forbidden = sorted(_repo_top_level_modules())
    code = (
        "import sys, observatory.api, observatory.events, observatory.queries;"
        f"bad = sorted({{m.split('.')[0] for m in sys.modules}} & set({forbidden!r}));"
        "print(','.join(bad))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_no_write_sql_anywhere_in_the_package():
    pattern = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE)\b", re.IGNORECASE)
    hits = [
        f"{path.name}:{lineno}: {line.strip()}"
        for path in PACKAGE_FILES
        for lineno, line in enumerate(path.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert hits == []


def test_a_real_write_through_the_observatory_role_fails(owner, obs_dsn, obs_db):
    with owner.cursor() as cur:
        cur.execute("SELECT count(1) FROM kernel_events")
        before = cur.fetchone()[0]

    # 1. Through the observatory's own connection: the session is read-only.
    with pytest.raises(psycopg2.Error):
        obs_db.rows("INSERT INTO kernel_events (ts, breaker, action, reason) VALUES (now(), 'T', 'T', 'T') RETURNING id")

    # 2. The role itself, with the read-only session default switched OFF:
    #    only the missing privilege stands in the way — and it does.
    raw = psycopg2.connect(obs_dsn, options="-c default_transaction_read_only=off")
    try:
        with raw.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            assert cur.fetchone()[0] == "off"
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO kernel_events (ts, breaker, action, reason) VALUES (now(), 'T', 'T', 'T')")
    finally:
        raw.rollback()
        raw.close()

    with owner.cursor() as cur:
        cur.execute("SELECT count(1) FROM kernel_events")
        assert cur.fetchone()[0] == before


def test_every_session_is_read_only_with_timeouts(obs_db):
    row = obs_db.one(
        "SELECT current_user AS who, current_setting('default_transaction_read_only') AS ro, "
        "current_setting('statement_timeout') AS st, current_setting('TimeZone') AS tz LIMIT 1"
    )
    assert row == {"who": "observatory", "ro": "on", "st": "5s", "tz": "UTC"}


class RecordingDB:
    """Wraps a ReadOnlyDB and records every statement the observatory runs."""

    def __init__(self, inner):
        self.inner = inner
        self.statements = []

    def rows(self, sql, params=()):
        self.statements.append(sql)
        return self.inner.rows(sql, params)

    def one(self, sql, params=()):
        found = self.rows(sql, params)
        return found[0] if found else None

    def table_exists(self, table):
        return ReadOnlyDB.table_exists(self, table)


def test_every_statement_is_a_bounded_select(obs_db):
    db = RecordingDB(obs_db)
    now = datetime.now(timezone.utc)
    queries.state(db, now)
    queries.freshness(db, now)
    queries.trades(db, now, 7)
    queries.trace_signal(db, 1)
    queries.candles(db, now, "1h", 3)
    for window in queries.WINDOWS:
        queries.perf(db, now, window)
    queries.heartbeats(db, now, 60)
    projector = Projector(db, backfill_per_table=2)
    projector.poll_once()
    assert db.statements
    for sql in db.statements:
        assert sql.lstrip().upper().startswith("SELECT"), sql
        assert re.search(r"\bLIMIT\b", sql), sql


# ==========================================================================
# settings
# ==========================================================================


@pytest.mark.parametrize(
    "env, missing",
    [
        ({}, "OBSERVATORY_DATABASE_URL, OBSERVATORY_TOKEN"),
        ({"OBSERVATORY_TOKEN": TOKEN}, "OBSERVATORY_DATABASE_URL"),
        ({"OBSERVATORY_DATABASE_URL": "dbname=x"}, "OBSERVATORY_TOKEN"),
        ({"OBSERVATORY_DATABASE_URL": "  ", "OBSERVATORY_TOKEN": TOKEN}, "OBSERVATORY_DATABASE_URL"),
    ],
)
def test_settings_refuse_to_start_without_db_url_or_token(env, missing):
    with pytest.raises(SettingsError) as info:
        load_settings(env)
    assert f"missing {missing}" in str(info.value)
    assert "no observatory" in str(info.value)


def test_settings_reject_a_short_token_and_a_bad_port():
    with pytest.raises(SettingsError, match="shorter than"):
        load_settings({"OBSERVATORY_DATABASE_URL": "dbname=x", "OBSERVATORY_TOKEN": "a" * (MIN_TOKEN_LENGTH - 1)})
    with pytest.raises(SettingsError, match="not an integer"):
        load_settings({"OBSERVATORY_DATABASE_URL": "dbname=x", "OBSERVATORY_TOKEN": TOKEN, "OBSERVATORY_PORT": "http"})


def test_settings_defaults_and_secrets_stay_out_of_repr():
    settings = load_settings({"OBSERVATORY_DATABASE_URL": "postgresql://observatory:pw@/nexus", "OBSERVATORY_TOKEN": TOKEN})
    assert (settings.bind, settings.port) == ("127.0.0.1", 8787)
    assert TOKEN not in repr(settings) and "pw" not in repr(settings)


def test_build_app_refuses_without_settings(monkeypatch):
    monkeypatch.delenv("OBSERVATORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("OBSERVATORY_TOKEN", raising=False)
    with pytest.raises(SettingsError):
        build_app()


def test_main_exits_2_with_a_clear_message(monkeypatch, capsys):
    from observatory import api

    monkeypatch.delenv("OBSERVATORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("OBSERVATORY_TOKEN", raising=False)
    assert api.main() == 2
    assert "refusing to start" in capsys.readouterr().err


# ==========================================================================
# auth and methods (no database needed: rejected before any query)
# ==========================================================================


def _offline_app():
    offline = ReadOnlyDB("dbname=never_opened")
    return build_app(SETTINGS, db=offline, projector=Projector(offline), start_projector=False)


def _api_paths(app):
    # Every route on the token-protected router, plus a check that nothing
    # else under /api/v1 is mounted directly on the app except health.
    paths = sorted(route.path.replace("{signal_id}", "1") for route in app.state.api_router.routes)
    direct = [r.path for r in app.routes if getattr(r, "path", None) and r.path.startswith("/api/")]
    assert direct == ["/api/v1/health"]
    assert paths == [
        "/api/v1/candles", "/api/v1/events", "/api/v1/heartbeats", "/api/v1/perf",
        "/api/v1/state", "/api/v1/trace/signal/1", "/api/v1/trades",
    ]
    return paths


def test_every_api_route_needs_the_token():
    app = _offline_app()
    client = TestClient(app)
    for path in _api_paths(app):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong-token-" + "y" * 30}).status_code == 401, path
        assert client.get(path, headers={"Authorization": TOKEN}).status_code == 401, path  # no scheme
        assert client.get(f"{path}?token={TOKEN}").status_code == 401, path  # query token is WS-only


def test_health_needs_no_token():
    client = TestClient(_offline_app())
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and "projector_lag_s" in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_every_non_get_request_is_405(method):
    app = _offline_app()
    client = TestClient(app)
    for path in _api_paths(app) + ["/api/v1/health", "/api/v1/nope", "/"]:
        for headers in ({}, AUTH):
            response = client.request(method, path, headers=headers)
            assert response.status_code == 405, (method, path)
            assert response.headers["allow"] == "GET, HEAD"


def test_docs_and_openapi_are_not_served():
    client = TestClient(_offline_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=AUTH).status_code == 404


def test_websocket_needs_the_token():
    projector = Projector(ReadOnlyDB("dbname=never_opened"))
    projector._publish([{"ts": "2026-09-30T00:00:00+00:00", "ring": 2, "agent": "validator",
                         "event_type": "validator_wait", "severity": "warn", "correlation_id": None,
                         "trade_id": None, "summary": "RULE0 ENTRY_DRIFT: WAIT", "payload": {}}])
    app = build_app(SETTINGS, db=ReadOnlyDB("dbname=never_opened"), projector=projector, start_projector=False)
    client = TestClient(app)

    for url, headers in (("/ws/events", {}), ("/ws/events?token=wrong-" + "z" * 30, {})):
        with pytest.raises(WebSocketDisconnect) as info:
            with client.websocket_connect(url, headers=headers) as ws:
                ws.receive_json()
        assert info.value.code == 1008

    with client.websocket_connect(f"/ws/events?since_id=0&token={TOKEN}") as ws:
        assert ws.receive_json()["summary"] == "RULE0 ENTRY_DRIFT: WAIT"
    with client.websocket_connect("/ws/events?since_id=0", headers=AUTH) as ws:
        assert ws.receive_json()["id"] == 1


# ==========================================================================
# JSON safety
# ==========================================================================


def test_jsonable_handles_decimal_datetime_and_friends():
    plus_two = timezone(timedelta(hours=2))
    value = {
        "price": Decimal("4409.85"),
        "nan": Decimal("NaN"),
        "inf": float("inf"),
        "aware": datetime(2026, 9, 30, 14, 0, tzinfo=plus_two),
        "naive": datetime(2026, 9, 30, 12, 0),
        "day": date(2026, 9, 30),
        "span": timedelta(minutes=2),
        "nested": [Decimal("1.5"), {"x": Decimal("2")}],
        "pods": {"S2", "S1"},
    }
    out = jsonable(value)
    assert out == {
        "price": 4409.85,
        "nan": None,
        "inf": None,
        "aware": "2026-09-30T12:00:00+00:00",
        "naive": "2026-09-30T12:00:00+00:00",
        "day": "2026-09-30",
        "span": 120.0,
        "nested": [1.5, {"x": 2.0}],
        "pods": ["S1", "S2"],
    }
    json.dumps(out)  # must not raise


def test_database_values_arrive_json_safe(obs_db):
    row = obs_db.one("SELECT 1.25::numeric AS n, timestamptz '2026-09-30 14:00+02' AS t, ARRAY[1.5::numeric] AS a LIMIT 1")
    assert row == {"n": 1.25, "t": "2026-09-30T12:00:00+00:00", "a": [1.5]}
    json.dumps(row)


# ==========================================================================
# trace
# ==========================================================================


def test_trace_signal_joins_validator_rows_and_doctrine_in_force(obs_db, seed):
    t = datetime(2001, 2, 3, 4, 5, 6, 789000, tzinfo=timezone.utc)  # a quiet, unique instant
    in_force = _doctrine(seed, t - timedelta(minutes=5))
    _doctrine(seed, t + timedelta(minutes=1), bias="FLAT", swing=False)  # after the signal: not in force
    signal_id = _signal(seed, t)
    for rule in RULES:
        seed.add("validator_log", ts=t, symbol="TST_OBS", rule_name=rule, rule_result="PASS", details={"warnings": []})
    seed.add("validator_log", ts=t + timedelta(seconds=1), symbol="TST_OBS", rule_name="RULE0_ENTRY_DRIFT",
             rule_result="WAIT", details={"warnings": ["ENTRY_DRIFT"]})  # another cycle
    orphan_id = _signal(seed, t + timedelta(minutes=10))  # F-40: no validator rows at all

    trace = queries.trace_signal(obs_db, signal_id)
    assert trace["signal"]["id"] == signal_id
    assert trace["correlation"] == {"key": "ts", "value": t.isoformat()}
    assert [v["rule_name"] for v in trace["validator"]] == RULES
    assert all(v["ts"] == t.isoformat() for v in trace["validator"])
    assert trace["doctrine_in_force"]["id"] == in_force
    assert trace["doctrine_in_force"]["expired"] is False
    assert trace["kernel_events"] is None and "kernel_events" in trace["missing"]
    assert "validator" not in trace["missing"]

    orphan = queries.trace_signal(obs_db, orphan_id)
    assert orphan["validator"] is None
    assert "F-40" in orphan["missing"]["validator"]
    assert orphan["doctrine_in_force"]["id"] != in_force  # the later FLAT doctrine
    assert orphan["doctrine_in_force"]["bias"] == "FLAT"

    assert queries.trace_signal(obs_db, -1) is None

    app = build_app(SETTINGS, db=obs_db, projector=Projector(obs_db), start_projector=False)
    client = TestClient(app)
    response = client.get(f"/api/v1/trace/signal/{orphan_id}", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["validator"] is None
    assert client.get("/api/v1/trace/signal/-1", headers=AUTH).status_code == 404


# ==========================================================================
# perf
# ==========================================================================


def test_perf_reports_rows_and_decisions_with_wilson_bounds(obs_db, seed):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    bar = lambda hours: (now - timedelta(hours=hours)).replace(minute=0, second=0)  # noqa: E731
    recent, old = now - timedelta(hours=2), now - timedelta(days=3)
    closed = [
        # bar A, LONG x2: one decision, mean -1.0 -> loss
        (bar(10), "LONG", "STOPPED", -1.0, recent),
        (bar(10), "LONG", "STOPPED", -1.0, recent),
        # bar B, SHORT x2: one decision, mean +1.0 -> win
        (bar(8), "SHORT", "TP2", 3.0, recent),
        (bar(8), "SHORT", "STOPPED", -1.0, recent),
        # bar C, SHORT x1: one decision, +0.75 -> win
        (bar(6), "SHORT", "BE", 0.75, recent),
        # bar D, LONG x1, closed three days ago: only in "week"
        (bar(80), "LONG", "TP2", 3.0, old),
    ]
    for filled_at, direction, status, r, closed_at in closed:
        _signal(seed, filled_at - timedelta(minutes=30), direction=direction, status=status,
                filled_at=filled_at, outcome_r=r, outcome_ts=closed_at)
    _signal(seed, now - timedelta(hours=3), status="EXPIRED", outcome_ts=now - timedelta(hours=1))

    day = queries.perf(obs_db, now, "day", symbol="TST_OBS")["swing"]
    # Hand-computed: rows 5, wins 2 (3.0, 0.75), R sum -1-1+3-1+0.75 = 0.75.
    assert day["rows"]["n"] == 5
    assert day["rows"]["wins"] == 2
    assert day["rows"]["r_sum"] == pytest.approx(0.75)
    assert day["rows"]["win_rate"] == pytest.approx(0.4)
    assert day["rows"]["wilson95"] == pytest.approx([0.117618, 0.769280], abs=1e-5)
    # Decisions A(-1.0), B(+1.0), C(+0.75): 3, wins 2, sum 0.75.
    assert day["decisions"]["n"] == 3
    assert day["decisions"]["wins"] == 2
    assert day["decisions"]["r_sum"] == pytest.approx(0.75)
    assert day["decisions"]["wilson95"] == pytest.approx([0.207655, 0.938510], abs=1e-5)
    assert day["statistically_meaningful"] is False
    assert "not statistically meaningful" in day["note"]
    assert day["expired_unfilled"] == 1

    week = queries.perf(obs_db, now, "week", symbol="TST_OBS")["swing"]
    assert (week["rows"]["n"], week["rows"]["wins"]) == (6, 3)
    assert week["rows"]["r_sum"] == pytest.approx(3.75)
    assert week["rows"]["wilson95"] == pytest.approx([0.187613, 0.812387], abs=1e-5)
    assert (week["decisions"]["n"], week["decisions"]["wins"]) == (4, 3)
    assert week["decisions"]["wilson95"] == pytest.approx([0.300636, 0.954414], abs=1e-5)


def test_wilson_edge_cases():
    assert queries.wilson(0, 0) == (None, None)
    low, high = queries.wilson(0, 10)
    assert low == pytest.approx(0.0, abs=1e-12) and 0 < high < 0.35


# ==========================================================================
# events
# ==========================================================================


def _seed_cycle(seed, t):
    v1 = seed.add("validator_log", ts=t, symbol="TST_OBS", rule_name="RULE0_ENTRY_DRIFT",
                  rule_result="WAIT", details={"warnings": ["ENTRY_DRIFT"]})
    v2 = seed.add("validator_log", ts=t, symbol="TST_OBS", rule_name="RULE1_EVENT_BLOCK",
                  rule_result="WAIT", details={"fix_time": "15:00", "fix_blocked": True})
    d = _doctrine(seed, t + timedelta(seconds=1), bias="FLAT", source="PARSE_FALLBACK", raw=None, swing=False,
                  horizon=15)
    s = _signal(seed, t)  # a real cycle stamps its signal with the validator rows' ts
    return v1, v2, d, s


def test_projector_normalizes_new_rows_in_order_and_advances_watermarks(obs_db, seed):
    projector = Projector(obs_db, backfill_per_table=0)
    projector.prime()
    start_id = projector.last_event_id()

    t = datetime(2001, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    v1, v2, d, s = _seed_cycle(seed, t)
    assert projector.poll_once() == 4

    events = projector.events(since_id=start_id)
    assert [(e["payload"]["table"], e["payload"]["row_id"]) for e in events] == [
        ("validator_log", v1), ("validator_log", v2), ("doctrines", d), ("signals", s),
    ]
    assert [e["id"] for e in events] == list(range(start_id + 1, start_id + 5))
    keys = {"id", "ts", "ring", "agent", "event_type", "severity", "correlation_id", "trade_id", "summary", "payload"}
    assert all(set(e) == keys for e in events)

    wait, fix, doctrine, signal = events
    assert wait["summary"] == "RULE0 ENTRY_DRIFT: WAIT — ENTRY_DRIFT"
    assert (wait["ring"], wait["agent"], wait["severity"]) == (2, "validator", "warn")
    assert wait["correlation_id"] == f"cycle:{t.isoformat()}"
    assert fix["summary"] == "RULE1 EVENT_BLOCK: WAIT — London fix 15:00 blackout"
    assert (doctrine["event_type"], doctrine["severity"]) == ("doctrine_fallback", "error")
    assert doctrine["payload"]["api_failed"] is True  # F-37
    assert (signal["event_type"], signal["trade_id"], signal["ring"]) == ("signal_persisted", f"signal:{s}", 2)
    assert signal["correlation_id"] == wait["correlation_id"]  # same cycle

    assert projector.watermarks["validator_log"] == v2
    assert projector.watermarks["doctrines"] == d
    assert projector.watermarks["signals"] == s
    assert projector.poll_once() == 0  # nothing new, nothing repeated

    # Signals change in place: the projector diffs their status.
    seed.set("signals", s, status="OPEN", filled_at=t.replace(minute=0, second=0))
    assert projector.poll_once() == 1
    filled = projector.events(since_id=start_id + 4)[0]
    assert (filled["event_type"], filled["ring"], filled["agent"]) == ("signal_filled", 1, "paper_engine")
    seed.set("signals", s, status="STOPPED", outcome_r=-1.0, outcome_ts=t + timedelta(hours=2))
    assert projector.poll_once() == 1
    stopped = projector.events(since_id=start_id + 5)[0]
    assert (stopped["event_type"], stopped["severity"]) == ("signal_closed", "warn")
    assert stopped["summary"] == f"Signal #{s} LONG closed STOPPED: -1.00R"
    assert s not in projector.active_signals


def test_event_filters_ring_severity_and_limit():
    projector = Projector(ReadOnlyDB("dbname=never_opened"))
    base = {"ts": None, "agent": "x", "event_type": "x", "correlation_id": None, "trade_id": None,
            "summary": "x", "payload": {}}
    projector._publish([
        {**base, "ring": 2, "severity": "debug"},
        {**base, "ring": 2, "severity": "warn"},
        {**base, "ring": 0, "severity": "critical"},
        {**base, "ring": 1, "severity": "info"},
    ])
    assert [e["id"] for e in projector.events(severity="warn")] == [2, 3]
    assert [e["id"] for e in projector.events(ring=2)] == [1, 2]
    assert [e["id"] for e in projector.events(since_id=2)] == [3, 4]
    assert [e["id"] for e in projector.events(limit=1)] == [1]


class FlakyDB:
    """A real read-only DB that fails on demand for statements naming a table."""

    def __init__(self, inner):
        self.inner = inner
        self.fail_on = None

    def rows(self, sql, params=()):
        if self.fail_on and f"FROM {self.fail_on}" in sql:
            raise DatabaseUnavailable("simulated outage")
        return self.inner.rows(sql, params)

    def one(self, sql, params=()):
        found = self.rows(sql, params)
        return found[0] if found else None

    def table_exists(self, table):
        return self.inner.table_exists(table)


def test_a_db_failure_mid_poll_backs_off_and_loses_nothing(obs_db, seed):
    flaky = FlakyDB(obs_db)
    projector = Projector(flaky, backfill_per_table=0)
    projector.prime()
    start_id = projector.last_event_id()

    v1, v2, d, s = _seed_cycle(seed, datetime(2001, 4, 5, 6, 7, 8, tzinfo=timezone.utc))
    flaky.fail_on = "doctrines"
    wait = projector.step()  # must not raise
    assert projector.state == "backoff" and wait >= projector.poll_seconds
    assert "DatabaseUnavailable" in projector.last_error
    # validator_log was read before the failure; its watermark moved, nothing after it did.
    assert [e["payload"]["row_id"] for e in projector.events(since_id=start_id)] == [v1, v2]
    assert projector.watermarks["doctrines"] < d

    second_wait = projector.step()
    assert second_wait > wait  # exponential backoff

    flaky.fail_on = None
    assert projector.step() == projector.poll_seconds
    assert projector.state == "running"
    assert [e["payload"]["row_id"] for e in projector.events(since_id=start_id)] == [v1, v2, d, s]


def test_run_loop_survives_a_dead_database():
    projector = Projector(ReadOnlyDB("host=127.0.0.1 port=1 dbname=nowhere connect_timeout=1"), poll_seconds=0.05)
    stop = threading.Event()
    thread = threading.Thread(target=projector.run, args=(stop,), daemon=True)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()  # the loop is backing off, not dead
    assert projector.state == "backoff"
    stop.set()
    thread.join(timeout=35)
    assert not thread.is_alive()
    assert projector.state == "stopped"


def test_heartbeat_rows_become_events_only_when_they_change():
    projector = Projector(ReadOnlyDB("dbname=never_opened"))
    row = {"id": 1, "ts": "2026-09-30T12:00:00+00:00", "stage": "PAPER", "alive": 10, "registered": 6,
           "dead": 0, "doctrine_bias": "FLAT", "doctrine_source": "FABLE", "sensors": {}}
    assert projector._normalize("ops_heartbeats", row)["event_type"] == "heartbeat"
    assert projector._normalize("ops_heartbeats", {**row, "id": 2}) is None
    changed = projector._normalize("ops_heartbeats", {**row, "id": 3, "dead": 1})
    assert changed["severity"] == "error"
    assert projector.heartbeats_absorbed == 1


# ==========================================================================
# endpoints against the real read-only role
# ==========================================================================


def test_endpoints_answer_through_the_read_only_role(obs_db):
    app = build_app(SETTINGS, db=obs_db, projector=Projector(obs_db, backfill_per_table=5), start_projector=False)
    client = TestClient(app)

    state = client.get("/api/v1/state", headers=AUTH).json()
    assert state["kill_switch"]["state"] == "unknown"
    assert "flag file" in state["kill_switch"]["note"]
    assert {"stage", "doctrine", "heartbeat", "freshness", "missing"} <= set(state)

    trades = client.get("/api/v1/trades?days=7", headers=AUTH).json()
    assert set(trades) >= {"swing", "router", "notes"}
    assert "F-39" in trades["notes"]["bar_time"]

    perf = client.get("/api/v1/perf?window=all", headers=AUTH).json()
    assert {"rows", "decisions", "statistically_meaningful"} <= set(perf["swing"])

    assert client.get("/api/v1/candles?tf=1h&days=2", headers=AUTH).status_code == 200
    assert client.get("/api/v1/candles?tf=5m", headers=AUTH).status_code == 422
    assert client.get("/api/v1/perf?window=month", headers=AUTH).status_code == 422
    assert client.get("/api/v1/heartbeats?minutes=30", headers=AUTH).status_code == 200

    app.state.projector.poll_once()
    events = client.get("/api/v1/events?limit=5", headers=AUTH).json()
    assert events["count"] <= 5 and events["epoch"]


def test_missing_heartbeat_table_is_reported_not_raised():
    class NoHeartbeats:
        def table_exists(self, table):
            return False

    out = queries.heartbeats(NoHeartbeats(), datetime.now(timezone.utc), 60)
    assert out["rows"] is None and "migration 017" in out["missing"]
