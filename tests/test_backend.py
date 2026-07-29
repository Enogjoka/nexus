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


def test_registry_lists_eleven_agents_in_brief_order():
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
    }


def test_every_subscriber_in_the_registry_really_subscribes_and_returns():
    """
    Guards the classification against drift: a subscriber must be a function
    whose body ends by returning after BUS.subscribe. Read the source rather
    than run it, so no agent actually starts here.
    """
    import inspect

    for agent in backend.AGENTS:
        if agent.kind != backend.KIND_SUBSCRIBER:
            continue
        source = inspect.getsource(agent.target)
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
