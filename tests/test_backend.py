"""
Tests for the process supervisor.

Every test injects a fake registry. No real agent is ever started here, so no
test touches the network, the market data feeds, or the Telegram API — the
only thing under test is the supervision logic itself.
"""
import logging
import signal
import threading
from collections import deque

import pytest

import backend


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_supervisor(agents, **kwargs):
    """A supervisor with Telegram heartbeats off (0 hours) unless asked."""
    kwargs.setdefault("poll_seconds", 0)
    kwargs.setdefault("heartbeat_telegram_hours", 0)
    return backend.Supervisor(agents=agents, **kwargs)


def settle(supervisor, timeout=5.0):
    """Wait for every spawned thread to reach its terminal state."""
    supervisor.join_all(timeout=timeout)


# ---------------------------------------------------------------------------
# the registry itself
# ---------------------------------------------------------------------------


def test_registry_lists_sixteen_agents_in_brief_order():
    # Task 16 appended kernel-watchdog and doctrine. They sit last rather than
    # first because the kernel is CONSTRUCTED in main() before any agent thread
    # starts — Ring 0 is live regardless of its watchdog's position here.
    assert [a.name for a in backend.AGENTS] == [
        "data",
        "analysis-scheduler",
        "paper-engine",
        "telegram",
        "fred",
        "positioning",
        "calendar",
        "news",
        "state-vector",
        "regime",
        "learning",
        "kernel-watchdog",
        "doctrine",
        "basis",
        "pod-agent",
        "position-engine",
    ]


def test_registry_kinds_are_valid_and_subscribers_are_the_bus_agents():
    kinds = {a.name: a.kind for a in backend.AGENTS}
    assert set(kinds.values()) <= {backend.KIND_LOOP, backend.KIND_SUBSCRIBER}
    subscribers = {n for n, k in kinds.items() if k == backend.KIND_SUBSCRIBER}
    # Every agent that returns after subscribing to the BUS. "regime" is here
    # despite the brief listing only four — see the registry comment; it was
    # observed being restarted to death in the first live run.
    assert subscribers == {
        "analysis-scheduler",
        "paper-engine",
        "telegram",
        "state-vector",
        "regime",
        "pod-agent",
        "position-engine",
    }


def test_every_subscriber_in_the_registry_really_subscribes_and_returns():
    """
    Guards the classification against drift: a subscriber must be a function
    whose body ends by returning after BUS.subscribe. Read the source rather
    than run it, so no agent actually starts here.
    """
    import inspect

    # Task 21 injects the router into two subscribers, so their registry entry
    # is a thunk rather than the function itself. The property still holds one
    # level down; this maps each such agent to the implementation that really
    # subscribes, so the check keeps its teeth instead of being skipped.
    injected = {
        "pod-agent": backend.exec_.pod_agent.run_pod_agent,
        "position-engine": backend.exec_.position_engine.run_position_engine,
    }

    for agent in backend.AGENTS:
        if agent.kind != backend.KIND_SUBSCRIBER:
            continue
        target = injected.get(agent.name, agent.target)
        source = inspect.getsource(target)
        assert "BUS.subscribe(" in source, (
            f"{agent.name} is registered as a subscriber but never calls BUS.subscribe"
        )


def test_supervisor_rejects_an_unknown_kind():
    bad = [backend.Agent("nonsense", lambda: None, "daemon")]
    with pytest.raises(ValueError, match="expected one of"):
        backend.Supervisor(agents=bad)


def test_threads_are_named_nexus_prefixed_and_are_daemons():
    seen = {}

    def record():
        current = threading.current_thread()
        seen["name"] = current.name
        seen["daemon"] = current.daemon

    sup = make_supervisor([backend.Agent("probe", record, backend.KIND_SUBSCRIBER)])
    sup.start_all()
    settle(sup)

    assert seen["name"] == "nexus-probe"
    assert seen["daemon"] is True


# ---------------------------------------------------------------------------
# loop agents: restart, then give up
# ---------------------------------------------------------------------------


def test_raising_loop_agent_is_restarted_then_abandoned(caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="backend")
    calls = []
    alerts = []

    def boom():
        calls.append(1)
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(backend, "send_alert", lambda text: alerts.append(text) or True)

    sup = make_supervisor(
        [backend.Agent("boomer", boom, backend.KIND_LOOP)], max_restarts_per_hour=4
    )
    sup.start_all()
    settle(sup)

    for _ in range(5):
        sup.poll_once()
        settle(sup)

    # 1 initial start + exactly 4 restarts, then the supervisor stops trying.
    assert len(calls) == 5
    assert "boomer" in sup.given_up

    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "giving up on an agent must log CRITICAL"
    assert "boomer" in critical[0].getMessage()

    assert len(alerts) == 1
    assert "boomer" in alerts[0]

    # Abandoned means abandoned: further polls never restart it.
    sup.poll_once()
    settle(sup)
    assert len(calls) == 5


def test_loop_agent_that_returns_cleanly_is_also_abnormal(caplog):
    """A loop agent returning is as wrong as one raising — both get restarted."""
    caplog.set_level(logging.INFO, logger="backend")
    calls = []

    sup = make_supervisor(
        [backend.Agent("quitter", lambda: calls.append(1), backend.KIND_LOOP)],
        max_restarts_per_hour=2,
    )
    sup.start_all()
    settle(sup)
    sup.poll_once()
    settle(sup)

    assert len(calls) == 2
    assert "must not return" in caplog.text


def test_restart_budget_is_per_agent(monkeypatch):
    """One agent burning its budget must not disturb a healthy neighbour."""
    monkeypatch.setattr(backend, "send_alert", lambda text: True)
    healthy_starts = []
    running = threading.Event()

    def boom():
        raise RuntimeError("nope")

    def healthy():
        healthy_starts.append(1)
        running.wait(30)

    agents = [
        backend.Agent("boomer", boom, backend.KIND_LOOP),
        backend.Agent("healthy", healthy, backend.KIND_LOOP),
    ]
    sup = make_supervisor(agents, max_restarts_per_hour=2)
    sup.start_all()
    sup.threads()["boomer"].join(timeout=5)

    for _ in range(3):
        sup.poll_once()
        sup.threads()["boomer"].join(timeout=5)

    running.set()

    assert "boomer" in sup.given_up
    assert "healthy" not in sup.given_up
    assert len(healthy_starts) == 1, "a living agent is never respawned"
    assert sup._restarts["healthy"] == deque()


def test_send_alert_failure_does_not_break_the_supervisor(monkeypatch):
    """INVARIANT 6: a dead alert channel is logged, never fatal."""

    def exploding_alert(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(backend, "send_alert", exploding_alert)

    def boom():
        raise RuntimeError("nope")

    sup = make_supervisor(
        [backend.Agent("boomer", boom, backend.KIND_LOOP)], max_restarts_per_hour=1
    )
    sup.start_all()
    settle(sup)
    for _ in range(2):
        sup.poll_once()
        settle(sup)

    assert "boomer" in sup.given_up  # gave up despite the alert blowing up


# ---------------------------------------------------------------------------
# subscriber agents: returning is success
# ---------------------------------------------------------------------------


def test_subscriber_returning_fast_is_registered_never_restarted(caplog):
    caplog.set_level(logging.INFO, logger="backend")
    calls = []

    sup = make_supervisor(
        [backend.Agent("subber", lambda: calls.append(1), backend.KIND_SUBSCRIBER)]
    )
    sup.start_all()
    settle(sup)

    for _ in range(4):
        counts = sup.poll_once()
        settle(sup)

    assert len(calls) == 1, "a subscriber must be started exactly once"
    assert "subber" not in sup.given_up
    assert "registered" in caplog.text
    assert counts == {"alive": 0, "registered": 1, "dead": 0}


def test_subscriber_that_raises_is_not_restarted(caplog):
    """
    A subscriber that dies mid-subscription is logged with its traceback but
    not restarted: the supervisor cannot tell how far it got, and re-running a
    partially-applied subscription would double-register the callbacks.
    """
    caplog.set_level(logging.INFO, logger="backend")
    calls = []

    def half_subscribe():
        calls.append(1)
        raise RuntimeError("subscribe failed")

    sup = make_supervisor([backend.Agent("subber", half_subscribe, backend.KIND_SUBSCRIBER)])
    sup.start_all()
    settle(sup)
    for _ in range(3):
        sup.poll_once()
        settle(sup)

    assert len(calls) == 1
    assert "agent subber raised" in caplog.text


def test_counts_distinguish_alive_registered_and_dead(monkeypatch):
    monkeypatch.setattr(backend, "send_alert", lambda text: True)
    running = threading.Event()

    def forever():
        running.wait(30)

    def boom():
        raise RuntimeError("nope")

    agents = [
        backend.Agent("worker", forever, backend.KIND_LOOP),
        backend.Agent("subber", lambda: None, backend.KIND_SUBSCRIBER),
        backend.Agent("boomer", boom, backend.KIND_LOOP),
    ]
    sup = make_supervisor(agents, max_restarts_per_hour=0)
    sup.start_all()
    for name in ("subber", "boomer"):
        sup.threads()[name].join(timeout=5)

    counts = sup.poll_once()
    running.set()

    assert counts == {"alive": 1, "registered": 1, "dead": 1}


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_logged_every_poll(caplog):
    caplog.set_level(logging.INFO, logger="backend")
    sup = make_supervisor([backend.Agent("subber", lambda: None, backend.KIND_SUBSCRIBER)])
    sup.start_all()
    settle(sup)
    sup.poll_once()
    sup.poll_once()

    beats = [r for r in caplog.records if r.getMessage().startswith("heartbeat:")]
    assert len(beats) == 2
    assert "alive=0 registered=1 dead=0" in beats[-1].getMessage()


def test_telegram_heartbeat_is_disabled_at_zero_hours(monkeypatch):
    sent = []
    monkeypatch.setattr(backend, "send_alert", lambda text: sent.append(text) or True)
    sup = make_supervisor([], heartbeat_telegram_hours=0)
    for _ in range(3):
        sup.poll_once()
    assert sent == []


def test_telegram_heartbeat_fires_once_per_interval(monkeypatch):
    sent = []
    monkeypatch.setattr(backend, "send_alert", lambda text: sent.append(text) or True)
    monkeypatch.setattr(backend, "build_status_text", lambda: "NEXUS status\nstage: PAPER")

    sup = make_supervisor([], heartbeat_telegram_hours=1)
    sup.poll_once()
    assert sent == [], "boot must not count as a heartbeat"

    # Pretend an interval has elapsed.
    sup._last_telegram -= 3601
    sup.poll_once()
    assert len(sent) == 1
    assert "stage: PAPER" in sent[0]
    assert "0 alive" in sent[0]

    sup.poll_once()
    assert len(sent) == 1, "second poll inside the interval must not re-send"


def test_heartbeat_survives_a_failing_status_query(monkeypatch):
    sent = []
    monkeypatch.setattr(backend, "send_alert", lambda text: sent.append(text) or True)

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(backend, "build_status_text", boom)

    sup = make_supervisor([], heartbeat_telegram_hours=1)
    sup._last_telegram -= 3601
    sup.poll_once()

    assert len(sent) == 1
    assert "unavailable" in sent[0]


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_sigint_handler_sets_the_stop_flag(caplog):
    caplog.set_level(logging.INFO, logger="backend")
    sup = make_supervisor([])
    assert not sup.stop_event.is_set()

    sup.request_stop(signal.SIGINT, None)

    assert sup.stop_event.is_set()
    assert "SIGINT received" in caplog.text
    assert "shutting down" in caplog.text


def test_sigterm_handler_sets_the_stop_flag(caplog):
    caplog.set_level(logging.INFO, logger="backend")
    sup = make_supervisor([])
    sup.request_stop(signal.SIGTERM, None)
    assert sup.stop_event.is_set()
    assert "SIGTERM received" in caplog.text


def test_run_returns_once_stop_is_requested(caplog):
    caplog.set_level(logging.INFO, logger="backend")
    sup = make_supervisor([], poll_seconds=0.01)
    stopper = threading.Timer(0.1, sup.request_stop)
    stopper.start()

    sup.run()  # must return, not hang

    stopper.cancel()
    assert sup.stop_event.is_set()
    assert "supervisor loop stopped" in caplog.text


def test_run_exits_immediately_when_already_stopped():
    sup = make_supervisor([], poll_seconds=999)
    sup.request_stop()
    sup.run()  # would block for 999s if the flag were not checked first


# ---------------------------------------------------------------------------
# boot
# ---------------------------------------------------------------------------


def test_boot_exits_1_when_db_init_fails(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="backend")

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(backend.database, "init", boom)

    with pytest.raises(SystemExit) as exc:
        backend.boot_database()

    assert exc.value.code == 1
    assert "database unreachable at boot" in caplog.text


def test_boot_exits_1_when_migrations_fail(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="backend")

    def boom():
        raise RuntimeError("syntax error at or near")

    monkeypatch.setattr(backend.database, "init", lambda: None)
    monkeypatch.setattr(backend.database, "run_migrations", boom)

    with pytest.raises(SystemExit) as exc:
        backend.boot_database()

    assert exc.value.code == 1
    assert "migrations failed at boot" in caplog.text


def test_boot_never_starts_agents_when_the_db_is_down(monkeypatch):
    """The whole point of exiting 1: no agent thread may exist afterwards."""
    started = []
    monkeypatch.setattr(backend.database, "init", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        backend, "AGENTS", [backend.Agent("x", lambda: started.append(1), backend.KIND_LOOP)]
    )
    monkeypatch.setattr(backend.signal, "signal", lambda *a: None)

    with pytest.raises(SystemExit):
        backend.main()

    assert started == []


def test_boot_reports_applied_migrations(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="backend")
    monkeypatch.setattr(backend.database, "init", lambda: None)
    monkeypatch.setattr(backend.database, "run_migrations", lambda: ["009_x.sql"])

    assert backend.boot_database() == ["009_x.sql"]
    assert "migrations applied: 009_x.sql" in caplog.text


def test_stage_is_logged_at_boot(monkeypatch, caplog):
    """INVARIANT 1: the stage is read and logged; there is no setter to test."""
    caplog.set_level(logging.INFO, logger="backend")
    monkeypatch.setattr(backend.database, "init", lambda: None)
    monkeypatch.setattr(backend.database, "run_migrations", lambda: [])
    monkeypatch.setattr(backend, "AGENTS", [])
    monkeypatch.setattr(backend.signal, "signal", lambda *a: None)

    sup_holder = {}
    real_supervisor = backend.Supervisor

    def capture(*args, **kwargs):
        sup = real_supervisor(*args, **kwargs)
        sup.request_stop()  # stop immediately so main() returns
        sup_holder["sup"] = sup
        return sup

    monkeypatch.setattr(backend, "Supervisor", capture)

    assert backend.main() == 0
    assert "NEXUS starting - stage=PAPER" in caplog.text


# INVARIANT 1's other half — that backend.py contains no stage-mutation path —
# is already enforced repo-wide by
# tests/test_stage_immutable.py::test_repo_wide_no_stage_mutation_patterns_outside_this_file,
# whose rglob("*.py") sweep covers this module. Asserting it again here would
# duplicate a stronger guard, and spelling the banned patterns out as literals
# would itself trip that sweep.


# ===========================================================================
# Task O1 — ops heartbeat, model-silence alert, read-only roles file
# ===========================================================================
# Imports for this section only; the supervision tests above need none of them.
import re  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import config  # noqa: E402
from core import database  # noqa: E402
from core.state import STATE  # noqa: E402

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def ops_db():
    """nexus_dev at the current schema (017 included), exactly as boot does."""
    database.run_migrations()


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(backend, "send_alert", lambda text: sent.append(text) or True)
    return sent


def _seed_doctrine(ts, bias="BOTH", source="FABLE"):
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO doctrines (ts, bias, conviction, risk_multiplier, enabled_pods, "
                "swing_signals_allowed, review_horizon_min, source) "
                "VALUES (%s, %s, 5, 1.0, '{}', true, 30, %s) RETURNING id",
                (ts, bias, source),
            )
            return cur.fetchone()[0]


def _heartbeat_row(row_id):
    return database.fetch(
        "SELECT stage, alive, registered, dead, rss_mb, last_market_update_at, model_ok_at, "
        "doctrine_bias, doctrine_source, doctrine_age_s, budget_today_usd, sensors "
        "FROM ops_heartbeats WHERE id = %s",
        (row_id,),
    )[0]


def _max_heartbeat_id():
    return database.fetch("SELECT coalesce(max(id), 0) FROM ops_heartbeats")[0][0]


def test_supervisor_writes_no_heartbeat_unless_enabled():
    sup = make_supervisor([backend.Agent("sub", lambda: None, backend.KIND_SUBSCRIBER)])
    sup.poll_once()
    assert sup.ops_heartbeat is False
    assert sup._ops_thread is None


def test_main_enables_the_ops_heartbeat(monkeypatch):
    monkeypatch.setattr(backend.database, "init", lambda: None)
    monkeypatch.setattr(backend.database, "run_migrations", lambda: [])
    monkeypatch.setattr(backend, "AGENTS", [])
    monkeypatch.setattr(backend.signal, "signal", lambda *a: None)
    seen = {}
    real_supervisor = backend.Supervisor

    def capture(*args, **kwargs):
        seen.update(kwargs)
        sup = real_supervisor(*args, **kwargs)
        sup.request_stop()
        return sup

    monkeypatch.setattr(backend, "Supervisor", capture)
    assert backend.main() == 0
    assert seen.get("ops_heartbeat") is True


@requires_db
def test_one_poll_writes_exactly_one_heartbeat_row(ops_db, alerts, monkeypatch):
    market_epoch = datetime(2026, 9, 30, 11, 55, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(STATE, "last_analysis_ts", market_epoch)
    monkeypatch.setattr(STATE, "last_model_success_at", None, raising=False)
    monkeypatch.setattr(STATE, "budget_spent_today", 0.4321)
    doctrine_id = _seed_doctrine(datetime.now(timezone.utc) - timedelta(seconds=60))

    sup = make_supervisor(
        [
            backend.Agent("sub", lambda: None, backend.KIND_SUBSCRIBER),
            backend.Agent("loop", lambda: threading.Event().wait(2), backend.KIND_LOOP),
        ],
        ops_heartbeat=True,
    )
    sup.start_all()
    watermark = _max_heartbeat_id()
    try:
        counts = sup.poll_once()
        sup.wait_ops_heartbeat()
        new_ids = [r[0] for r in database.fetch(
            "SELECT id FROM ops_heartbeats WHERE id > %s", (watermark,)
        )]
        assert len(new_ids) == 1
        (stage, alive, registered, dead, rss, market_at, model_ok_at, bias, source,
         doctrine_age, budget, sensors) = _heartbeat_row(new_ids[0])
        assert stage == "PAPER"
        assert (alive, registered, dead) == (counts["alive"], counts["registered"], counts["dead"])
        assert rss is not None and float(rss) > 0
        assert market_at == datetime.fromtimestamp(market_epoch, tz=timezone.utc)
        assert model_ok_at is not None  # the seeded FABLE doctrine at least
        assert (bias, source) == ("BOTH", "FABLE")
        assert 55 <= doctrine_age <= 120
        assert float(budget) == pytest.approx(0.4321)
        assert set(sensors) == set(backend.HEARTBEAT_SENSORS)
        assert all(v is None or isinstance(v, int) for v in sensors.values())
    finally:
        database.execute("DELETE FROM ops_heartbeats WHERE id > %s", (watermark,))
        database.execute("DELETE FROM doctrines WHERE id = %s", (doctrine_id,))
        settle(sup)


def test_heartbeat_db_failure_never_breaks_the_poll(alerts, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("db down at heartbeat")

    monkeypatch.setattr(backend.database, "get_conn", boom)
    sup = make_supervisor([backend.Agent("sub", lambda: None, backend.KIND_SUBSCRIBER)], ops_heartbeat=True)
    with caplog.at_level(logging.ERROR, logger="backend"):
        counts = sup.poll_once()  # must not raise
        sup.wait_ops_heartbeat()
    assert counts == sup.counts()
    assert "ops heartbeat: write failed" in caplog.text
    assert alerts == []  # model_ok_at unknown -> no silence verdict, no alert


def test_heartbeat_skips_a_poll_while_the_previous_write_runs(caplog):
    sup = make_supervisor([], ops_heartbeat=True)
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    stuck.start()
    sup._ops_thread = stuck
    try:
        with caplog.at_level(logging.WARNING, logger="backend"):
            sup.poll_once()
        assert sup._ops_thread is stuck  # no second thread piled on
        assert "previous write still running" in caplog.text
    finally:
        release.set()
        stuck.join()


def test_later_picks_the_later_timestamp_either_order():
    early = datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)
    late = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
    assert backend.later(early, late) == late
    assert backend.later(late, early) == late
    assert backend.later(None, early) == early
    assert backend.later(early, None) == early
    assert backend.later(None, None) is None


@requires_db
@pytest.mark.parametrize("newer", ["state", "doctrine"])
def test_model_ok_at_is_the_later_source(ops_db, alerts, monkeypatch, newer):
    now = datetime.now(timezone.utc)
    if newer == "state":
        doctrine_id = _seed_doctrine(now - timedelta(hours=1))
        state_ts = datetime.now(timezone.utc)  # later than every doctrine written so far
        expected = state_ts
    else:
        state_ts = now - timedelta(hours=1)
        doctrine_id = _seed_doctrine(now)  # the newest FABLE row in the table
        expected = now
    monkeypatch.setattr(STATE, "last_model_success_at", state_ts, raising=False)
    sup = make_supervisor([], ops_heartbeat=True)
    row_id = None
    try:
        row_id = sup._ops_heartbeat({"alive": 0, "registered": 0, "dead": 0})
        assert row_id is not None
        assert _heartbeat_row(row_id)[6] == expected
    finally:
        if row_id is not None:
            database.execute("DELETE FROM ops_heartbeats WHERE id = %s", (row_id,))
        database.execute("DELETE FROM doctrines WHERE id = %s", (doctrine_id,))


def test_model_silence_alerts_once_repeats_after_cooldown_and_recovers(alerts):
    sup = make_supervisor([], ops_heartbeat=True)
    last_ok = datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc)
    limit = config.MODEL_SILENCE_ALERT_MINUTES

    sup._check_model_silence(last_ok, last_ok + timedelta(minutes=limit - 1))
    assert alerts == []  # inside the threshold

    sup._check_model_silence(last_ok, last_ok + timedelta(minutes=limit + 1))
    assert alerts == [
        f"NEXUS: no successful AI call for {limit + 1}m - check Anthropic credits/API"
    ]

    sup._check_model_silence(last_ok, last_ok + timedelta(minutes=limit + 30))
    assert len(alerts) == 1  # still silent, inside the cooldown

    sup._check_model_silence(last_ok, last_ok + timedelta(minutes=2 * limit + 1))
    assert len(alerts) == 2  # one repeat per interval

    fresh = last_ok + timedelta(minutes=2 * limit + 5)
    sup._check_model_silence(fresh, fresh + timedelta(minutes=1))
    assert len(alerts) == 3
    assert alerts[2].startswith("NEXUS: AI calls recovered")

    sup._check_model_silence(fresh, fresh + timedelta(minutes=2))
    assert len(alerts) == 3  # exactly one recovery message


def test_model_never_ok_since_boot_alerts_only_past_threshold(alerts):
    sup = make_supervisor([], ops_heartbeat=True)
    boot = sup._boot_utc
    limit = config.MODEL_SILENCE_ALERT_MINUTES

    sup._check_model_silence(None, boot + timedelta(minutes=limit - 1))
    assert alerts == []

    sup._check_model_silence(None, boot + timedelta(minutes=limit + 1))
    assert alerts == [
        f"NEXUS: no successful AI call for {limit + 1}m - check Anthropic credits/API"
    ]


@requires_db
def test_prune_deletes_only_rows_older_than_retention(ops_db):
    # A synthetic clock in 2001, so the cutoff sits far below every real row.
    now = datetime(2001, 1, 15, tzinfo=timezone.utc)
    old = now - timedelta(days=config.HEARTBEAT_RETENTION_DAYS, hours=1)
    kept = now - timedelta(days=config.HEARTBEAT_RETENTION_DAYS - 1)
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO ops_heartbeats (ts) VALUES (%s) RETURNING id", (old,))
            old_id = cur.fetchone()[0]
            cur.execute("INSERT INTO ops_heartbeats (ts) VALUES (%s) RETURNING id", (kept,))
            kept_id = cur.fetchone()[0]
    newest_before = _max_heartbeat_id()
    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                backend.prune_heartbeats(cur, now)
        remaining = {r[0] for r in database.fetch(
            "SELECT id FROM ops_heartbeats WHERE id IN (%s, %s)", (old_id, kept_id)
        )}
        assert remaining == {kept_id}
        assert _max_heartbeat_id() == newest_before  # nothing newer was touched
    finally:
        database.execute("DELETE FROM ops_heartbeats WHERE id IN (%s, %s)", (old_id, kept_id))


def test_readonly_roles_sql_carries_no_password():
    sql = (REPO / "ops" / "deploy" / "readonly_roles.sql").read_text()
    assert "PASSWORD" not in sql
    # No credential literal in any spelling, e.g. `password 'x'` / `PASSWORD = 'x'`.
    assert not re.search(r"password\s*=?\s*'", sql, re.IGNORECASE)
    for role in ("nexus_readonly", "observatory", "architect_agent"):
        assert f"CREATE ROLE {role}" in sql
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_readonly" in sql
    assert not re.search(r"GRANT\s+(INSERT|UPDATE|DELETE|ALL)\b", sql, re.IGNORECASE)


# ===========================================================================
# Task O2b — agent action log (AgentLogHandler)
# ===========================================================================
# Imports for this section only.
import queue as queue_module  # noqa: E402
import sys as sys_module  # noqa: E402
import time as time_module  # noqa: E402

import psycopg2  # noqa: E402

MARK = "TST_O2B"


@pytest.fixture(autouse=True)
def _no_agent_log_writer_unless_asked(request, monkeypatch):
    """
    main() now starts the agent-log writer, which would insert this file's boot
    log lines into nexus_dev on every run. Here start_writer() is a no-op —
    records still queue, nothing is written — unless a test asks for the
    `real_writer` fixture. This only prevents writes; it never permits one.
    """
    if "real_writer" not in request.fixturenames:
        monkeypatch.setattr(backend.AgentLogHandler, "start_writer", lambda self: None)


@pytest.fixture
def real_writer():
    """Opt in to the genuine writer thread (see the autouse fixture above)."""
    return True


@pytest.fixture
def agent_log_rows(ops_db):
    """nexus_dev at the current schema; removes every row this file wrote."""
    yield
    database.execute("DELETE FROM agent_log WHERE message LIKE %s", (f"{MARK}%",))


def _record(name, msg, level=logging.INFO, args=(), exc_info=None):
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def _offline_handler(**kwargs):
    kwargs.setdefault("database_url", "host=127.0.0.1 port=1 dbname=never_opened connect_timeout=1")
    kwargs.setdefault("env", {})
    return backend.AgentLogHandler(**kwargs)


def _drain(handler):
    rows = []
    while True:
        try:
            rows.append(handler.queue.get_nowait())
        except queue_module.Empty:
            return rows


def test_agent_log_handler_enqueues_without_touching_the_database(monkeypatch):
    def no_db(*args, **kwargs):
        raise AssertionError("emit() must never open a connection")

    monkeypatch.setattr(backend.psycopg2, "connect", no_db)
    handler = _offline_handler()
    handler.handle(_record("sensors.news", "fetched %d articles", args=(12,)))
    handler.handle(_record("sensors.news", "debug chatter", level=logging.DEBUG))  # below INFO

    rows = _drain(handler)
    assert len(rows) == 1
    ts, agent, logger_name, level, message, thread = rows[0]
    assert (agent, logger_name, level, message) == ("News", "sensors.news", "INFO", "fetched 12 articles")
    assert ts.tzinfo is not None and thread == threading.current_thread().name


def test_a_full_queue_drops_and_counts_and_never_blocks():
    handler = _offline_handler(queue_max=3)
    durations = []
    for i in range(50):
        started = time_module.perf_counter()
        handler.handle(_record("ai.analysis", f"{MARK} cycle {i}"))
        durations.append(time_module.perf_counter() - started)
    assert handler.queue.qsize() == 3
    assert handler.dropped == 47
    assert max(durations) < 0.01  # emit only redacts and enqueues
    assert sum(durations) < 0.1


def test_a_writer_db_failure_does_not_raise_and_the_next_batch_writes(agent_log_rows, monkeypatch, capsys):
    handler = backend.AgentLogHandler(database_url=config.DATABASE_URL, env={})
    real_connection = handler._connection
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise psycopg2.OperationalError("simulated outage")
        return real_connection()

    monkeypatch.setattr(handler, "_connection", flaky)
    try:
        handler.handle(_record("ai.doctrine", f"{MARK} lost with the failed batch"))
        assert handler.flush_once() == 0  # no raise
        assert handler.failed_batches == 1 and handler.lost_in_failures == 1
        assert "dropped a batch of 1 rows: OperationalError" in capsys.readouterr().err

        handler.handle(_record("ai.doctrine", f"{MARK} written by the next batch"))
        assert handler.flush_once() == 1
    finally:
        handler._drop_connection()

    stored = [r[0] for r in database.fetch("SELECT message FROM agent_log WHERE message LIKE %s", (f"{MARK}%",))]
    assert stored == [f"{MARK} written by the next batch"]


FAKE_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-FAKEanthropic0001",
    "FRED_API_KEY": "fredFAKEkey0002",
    "TELEGRAM_BOT_TOKEN": "7000001:AAFAKEtelegramToken0003",
    "HMAC_SECRET": "hmacFAKEsecret0004",
    "DATABASE_URL": "postgresql://nexus:dbFAKEpass0005@db.example:5432/nexus",
}


def test_secrets_never_land_in_the_table(agent_log_rows):
    handler = backend.AgentLogHandler(database_url=config.DATABASE_URL, env=FAKE_SECRETS)
    lines = [
        f"{MARK} key {FAKE_SECRETS['ANTHROPIC_API_KEY']} in a message",
        f"{MARK} fred {FAKE_SECRETS['FRED_API_KEY']} and hmac {FAKE_SECRETS['HMAC_SECRET']}",
        f"{MARK} bot token {FAKE_SECRETS['TELEGRAM_BOT_TOKEN']}",
        f"{MARK} dsn {FAKE_SECRETS['DATABASE_URL']}",
        f"{MARK} GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=fredOTHERkey9&file_type=json",
        f"{MARK} POST https://api.telegram.org/bot123:ABC/sendMessage",
    ]
    for line in lines:
        handler.handle(_record("sensors.fred", line))
    try:
        assert handler.flush_once() == len(lines)
    finally:
        handler._drop_connection()

    stored = [r[0] for r in database.fetch(
        "SELECT message FROM agent_log WHERE message LIKE %s ORDER BY id", (f"{MARK}%",)
    )]
    assert len(stored) == len(lines)
    forbidden = [v for k, v in FAKE_SECRETS.items() if k != "DATABASE_URL"] + [
        "dbFAKEpass0005", "fredOTHERkey9", "123:ABC",
    ]
    for message in stored:
        for secret in forbidden:
            assert secret not in message, (secret, message)
    # Redacted, not deleted: the shape of each line survives.
    assert "api_key=*&file_type=json" in stored[4]
    assert "https://api.telegram.org/bot*/sendMessage" in stored[5]
    assert "postgresql://nexus:*@db.example:5432/nexus" in stored[3]


def test_exceptions_store_type_and_message_only():
    handler = _offline_handler(env=FAKE_SECRETS)
    try:
        raise ValueError(f"bad value {FAKE_SECRETS['HMAC_SECRET']}")
    except ValueError:
        record = _record("risk.kernel", f"{MARK} kernel check failed", level=logging.ERROR, exc_info=sys_module.exc_info())
    logging.Formatter().format(record)  # another handler rendering the traceback first must not matter
    assert record.exc_text and "Traceback" in record.exc_text

    handler.handle(record)
    (row,) = _drain(handler)
    assert row[1:4] == ("Kernel", "risk.kernel", "ERROR")
    assert row[4] == f"{MARK} kernel check failed | ValueError: bad value *"
    assert "Traceback" not in row[4] and 'File "' not in row[4]


def test_messages_are_capped_after_redaction():
    handler = _offline_handler(env=FAKE_SECRETS)
    secret = FAKE_SECRETS["ANTHROPIC_API_KEY"]
    # The secret straddles the 2,000-character cap: none of it may survive.
    text = "x" * (config.AGENT_LOG_MESSAGE_MAX - 10) + secret + "y" * 5000
    handler.handle(_record("ai.analysis", text))
    (row,) = _drain(handler)
    assert len(row[4]) <= config.AGENT_LOG_MESSAGE_MAX
    assert row[4].startswith("x" * 100)
    assert secret[:6] not in row[4]


def test_a_secret_cut_by_the_precut_cannot_slide_into_the_kept_text():
    # Review finding: earlier redactions shorten the text, which used to pull a
    # fragment of a secret cut at the pre-cut boundary into the stored part.
    handler = _offline_handler(env=FAKE_SECRETS)
    key = FAKE_SECRETS["ANTHROPIC_API_KEY"]
    fred = FAKE_SECRETS["FRED_API_KEY"]
    margin = max(handler.secret_max, backend._REDACTION_MARGIN)
    head = (key + " ") * 40  # 40 redactions shrink the text by ~40 x len(key)
    # Straddle the old pre-cut (message_max + longest secret) and the current one.
    for boundary in (config.AGENT_LOG_MESSAGE_MAX + handler.secret_max, config.AGENT_LOG_MESSAGE_MAX + margin):
        filler = "z" * (boundary - len(head) - 4)
        text = head + filler + fred + "w" * 5000  # only fred's first 4 chars sit before the boundary
        handler.handle(_record("sensors.fred", text))
        (row,) = _drain(handler)
        assert len(row[4]) <= config.AGENT_LOG_MESSAGE_MAX
        for size in range(3, len(fred) + 1):
            assert fred[:size] not in row[4], (boundary, size)
        assert key[:6] not in row[4]


def test_the_queue_is_reentrant_for_signal_handlers():
    # Review finding: request_stop() logs from a signal handler; a non-reentrant
    # queue lock held by the interrupted put() on the same thread would deadlock.
    handler = _offline_handler()
    assert isinstance(handler.queue, queue_module.SimpleQueue)


def test_a_nul_character_does_not_sink_the_batch(agent_log_rows):
    handler = backend.AgentLogHandler(database_url=config.DATABASE_URL, env={})
    handler.handle(_record("sensors.news", f"{MARK} payload with a \x00 inside"))
    handler.handle(_record("sensors.news", f"{MARK} its neighbour in the batch"))
    try:
        assert handler.flush_once() == 2
    finally:
        handler._drop_connection()
    stored = [r[0] for r in database.fetch("SELECT message FROM agent_log WHERE message LIKE %s ORDER BY id", (f"{MARK}%",))]
    assert stored == [f"{MARK} payload with a \\0 inside", f"{MARK} its neighbour in the batch"]


def test_the_writer_connection_detects_a_dead_peer(monkeypatch):
    seen = {}

    def fake_connect(dsn, **kwargs):
        seen.update(kwargs)
        raise psycopg2.OperationalError("no server here")

    monkeypatch.setattr(backend.psycopg2, "connect", fake_connect)
    handler = _offline_handler()
    handler.handle(_record("risk.stage", f"{MARK} stage read"))
    assert handler.flush_once() == 0
    assert seen["connect_timeout"] == config.AGENT_LOG_CONNECT_TIMEOUT_S
    assert f"statement_timeout={config.AGENT_LOG_STATEMENT_TIMEOUT_MS}" in seen["options"]
    assert seen["keepalives"] == 1 and seen["tcp_user_timeout"] > 0


def test_drops_are_reported_on_stderr_once_per_change(capsys):
    handler = _offline_handler(queue_max=1)
    handler._report_drops()
    assert capsys.readouterr().err == ""  # nothing dropped, nothing said
    for i in range(4):
        handler.handle(_record("sensors.basis", f"{MARK} {i}"))
    handler._report_drops()
    handler._report_drops()  # no change since the last report: silent
    err = capsys.readouterr().err
    assert err.count("3 dropped") == 1


@pytest.mark.parametrize(
    "name", ["psycopg2", "psycopg2.pool", "urllib3.connectionpool", "httpx", "httpx2", "httpcore.http11", "asyncio"]
)
def test_the_recursion_guard_drops_plumbing_loggers(name):
    handler = _offline_handler()
    handler.handle(_record(name, f"{MARK} transport chatter", level=logging.WARNING))
    assert handler.queue.qsize() == 0


def test_the_writer_thread_never_logs_itself():
    handler = _offline_handler()
    handler._writer_ident = threading.get_ident()  # as if this thread were the writer
    handler.handle(_record("backend", f"{MARK} from the writer"))
    assert handler.queue.qsize() == 0


@pytest.mark.parametrize(
    "logger_name, agent",
    [
        ("data.gold_agent", "Price feed"),
        ("data.mt5_bridge", "Router"),
        ("sensors.fred", "Macro (FRED)"),
        ("sensors.positioning", "Positioning (COT)"),
        ("sensors.calendar_agent", "Calendar"),
        ("sensors.news", "News"),
        ("sensors.basis", "Basis"),
        ("fusion.state_vector", "State vector"),
        ("fusion.regime", "Regime model"),
        ("fusion.rag", "Memory"),
        ("fusion.learning_loop", "Learning loop"),
        ("ai.analysis", "Analyst"),
        ("ai.price_resolver", "Analyst"),
        ("ai.doctrine", "Doctrine"),
        ("risk.validator", "Validator"),
        ("risk.sizing", "Analyst"),
        ("risk.kernel", "Kernel"),
        ("risk.stage", "Stage"),
        ("exec_.paper_engine", "Paper engine"),
        ("exec_.router", "Router"),
        ("exec_.position_engine", "Position engine"),
        ("exec_.pod_agent", "Pods"),
        ("pods.base", "Pods"),
        ("pods.s1_fixfade", "Pods"),
        ("pods.s4_newsburst", "Pods"),
        ("pods.vwap", "Pods"),
        ("ops.telegram_bot", "Telegram"),
        ("backend", "Supervisor"),
        ("link.channel", "Link"),
        ("link.messages", "Link"),
        ("core.state", "Other"),
        ("ops.promotion", "Other"),
        ("yfinance", "Other"),
        ("backendish", "Other"),  # a prefix matches whole name segments only
        ("podsx", "Other"),
    ],
)
def test_each_real_logger_maps_to_its_agent(logger_name, agent):
    assert backend.agent_for_logger(logger_name) == agent


def test_every_agent_module_in_the_repo_has_a_named_agent():
    from pathlib import Path as _Path

    repo = _Path(backend.__file__).resolve().parent
    deliberately_other = {"core.state", "ops.promotion", "scripts.run_one_cycle"}
    unmapped = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if rel.parts[0] in (".claude", "observatory", "tests") or "node_modules" in rel.parts:
            continue
        if "getLogger(__name__)" not in path.read_text(errors="ignore"):
            continue
        module = ".".join(rel.with_suffix("").parts)
        if backend.agent_for_logger(module) == backend.AGENT_OTHER and module not in deliberately_other:
            unmapped.append(module)
    assert unmapped == []


def test_main_attaches_the_handler_and_a_plain_supervisor_does_not(monkeypatch):
    def handlers():
        return [h for h in logging.getLogger().handlers if isinstance(h, backend.AgentLogHandler)]

    assert handlers() == []
    sup = make_supervisor([backend.Agent("sub", lambda: None, backend.KIND_SUBSCRIBER)])
    sup.poll_once()
    assert handlers() == []  # a Supervisor alone never touches logging

    monkeypatch.setattr(backend.database, "init", lambda: None)
    monkeypatch.setattr(backend.database, "run_migrations", lambda: [])
    monkeypatch.setattr(backend, "AGENTS", [])
    monkeypatch.setattr(backend.signal, "signal", lambda *a: None)
    started = []
    monkeypatch.setattr(backend.AgentLogHandler, "start_writer", lambda self: started.append(self))
    seen_during_run = []
    real_supervisor = backend.Supervisor

    def capture(*args, **kwargs):
        seen_during_run.extend(handlers())
        supervisor = real_supervisor(*args, **kwargs)
        supervisor.request_stop()
        return supervisor

    monkeypatch.setattr(backend, "Supervisor", capture)
    assert backend.main() == 0
    assert len(seen_during_run) == 1 and started == seen_during_run  # attached, writer started after boot
    assert handlers() == []  # and detached on the way out


def test_the_real_writer_thread_lands_rows_and_stops(agent_log_rows, real_writer):
    handler = backend.AgentLogHandler(database_url=config.DATABASE_URL, env={}, flush_seconds=0.05)
    handler.start_writer()
    try:
        handler.handle(_record("fusion.regime", f"{MARK} regime refused to train", level=logging.WARNING))
        deadline = time_module.monotonic() + 5
        rows = []
        while time_module.monotonic() < deadline and not rows:
            rows = database.fetch(
                "SELECT agent, logger, level, thread FROM agent_log WHERE message = %s",
                (f"{MARK} regime refused to train",),
            )
            if not rows:
                time_module.sleep(0.05)
        assert rows == [("Regime model", "fusion.regime", "WARNING", threading.current_thread().name)]
    finally:
        handler.close()
    assert not handler._writer.is_alive()


@requires_db
def test_agent_log_pruning_deletes_only_rows_older_than_retention(ops_db):
    now = datetime(2001, 1, 15, tzinfo=timezone.utc)
    old = now - timedelta(days=config.AGENT_LOG_RETENTION_DAYS, hours=1)
    kept = now - timedelta(days=config.AGENT_LOG_RETENTION_DAYS - 1)
    ids = {}
    with database.get_conn() as conn:
        with conn.cursor() as cur:
            for label, ts in (("old", old), ("kept", kept)):
                cur.execute(
                    "INSERT INTO agent_log (ts, agent, logger, level, message) VALUES (%s, 'Other', 'test', 'INFO', %s) RETURNING id",
                    (ts, f"{MARK} prune {label}"),
                )
                ids[label] = cur.fetchone()[0]
    try:
        with database.get_conn() as conn:
            with conn.cursor() as cur:
                backend.prune_agent_log(cur, now)
        remaining = {r[0] for r in database.fetch("SELECT id FROM agent_log WHERE id IN (%s, %s)", (ids["old"], ids["kept"]))}
        assert remaining == {ids["kept"]}
    finally:
        database.execute("DELETE FROM agent_log WHERE id IN (%s, %s)", (ids["old"], ids["kept"]))


@requires_db
def test_agent_log_is_pruned_once_a_day_beside_the_heartbeats(ops_db, alerts, monkeypatch):
    calls = []
    monkeypatch.setattr(backend, "prune_agent_log", lambda cur, now: calls.append(now))
    sup = make_supervisor([], ops_heartbeat=True)
    row_ids = []
    try:
        first = datetime.now(timezone.utc)
        row_ids.append(sup._ops_heartbeat({"alive": 0, "registered": 0, "dead": 0}, now=first))
        row_ids.append(sup._ops_heartbeat({"alive": 0, "registered": 0, "dead": 0}, now=first + timedelta(seconds=30)))
        assert calls == [first]  # once per UTC day, not every poll
    finally:
        for row_id in filter(None, row_ids):
            database.execute("DELETE FROM ops_heartbeats WHERE id = %s", (row_id,))


def test_the_routine_supervisor_heartbeat_line_is_not_stored_but_a_warning_is(agent_log_rows):
    # The ruling: the periodic "heartbeat:" INFO line duplicates ops_heartbeats
    # and is dropped; a heartbeat line at WARNING or above, and every other
    # supervisor record, is kept.
    handler = backend.AgentLogHandler(database_url=config.DATABASE_URL, env={})
    routine = _record("backend", "heartbeat: alive=%d registered=%d dead=%d (of %d agents) %s", args=(10, 6, 0, 16, MARK))
    warning = _record("backend", f"heartbeat: build_status_text failed {MARK}", level=logging.WARNING)
    other = _record("backend", f"agent sub registered {MARK}")
    try:
        for record in (routine, warning, other):
            handler.handle(record)
        assert handler.routine_skipped == 1
        assert handler.flush_once() == 2
    finally:
        handler._drop_connection()
    try:
        stored = database.fetch(
            "SELECT agent, level, message FROM agent_log WHERE message LIKE %s ORDER BY id", (f"%{MARK}%",)
        )
        assert stored == [
            ("Supervisor", "WARNING", f"heartbeat: build_status_text failed {MARK}"),
            ("Supervisor", "INFO", f"agent sub registered {MARK}"),
        ]
    finally:
        database.execute("DELETE FROM agent_log WHERE message LIKE %s", (f"%{MARK}%",))
