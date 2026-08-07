"""
NEXUS process supervisor — the spine.

One process starts every agent as a daemon thread, watches them, and shuts
down cleanly. Nothing here knows what any agent *does*; agents run AS-IS and
this module never reaches into them.

Two kinds of agent, and the distinction is the whole reason the registry
carries a `kind`:

  "loop"       — runs forever (poll, sleep, repeat). If the thread ends, for
                 ANY reason, something is wrong. Restart it.
  "subscriber" — subscribes callbacks to the BUS and returns immediately. The
                 thread ending is the SUCCESS path: the subscription outlives
                 the thread that registered it. Restarting would re-subscribe
                 the same callback over and over.

Without that distinction a supervisor would hammer the four subscription
agents forever, so the registry declares intent rather than guessing from
behaviour.

INVARIANT 1: STAGE is read, logged, and never written. This module has no
mutation path for it.
"""
# load_dotenv() runs FIRST, before any application import. config.py reads the
# environment at module-import time, so anything imported before this line
# would capture an environment with no .env in it. load_dotenv() does not
# override variables that are already set, so an explicit
# `DATABASE_URL=... python3 backend.py` (and pytest's env) still wins.
from dotenv import load_dotenv

load_dotenv()

import logging
import signal
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, Iterable, List, NamedTuple, Optional

import config
from config import get_stage
from core import database

# Agent entry points. Importing these pulls in every agent module; an import
# failure here is fatal by design — a half-started trading system is worse
# than one that refuses to boot.
import ai.analysis
import ai.doctrine
import data.gold_agent
import data.mt5_bridge
import exec_.paper_engine
import exec_.pod_agent
import exec_.position_engine
import exec_.router
import fusion.learning_loop
import fusion.regime
import fusion.state_vector
import ops.telegram_bot
import sensors.basis
import sensors.calendar_agent
import sensors.fred
import sensors.news
import sensors.positioning
from ops.telegram_bot import build_status_text, send_alert

logger = logging.getLogger("backend")

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"

KIND_LOOP = "loop"
KIND_SUBSCRIBER = "subscriber"
_KINDS = (KIND_LOOP, KIND_SUBSCRIBER)


class Agent(NamedTuple):
    name: str
    target: Callable[[], None]
    kind: str


# Start order is significant and matches the task brief exactly: data first so
# the BUS has market state to publish, subscribers next so they are listening
# before the sensors start firing, fusion last because it reads what the
# others write.
AGENTS: List[Agent] = [
    Agent("data", data.gold_agent.run_data_agent, KIND_LOOP),
    Agent("analysis-scheduler", ai.analysis.run_scheduler, KIND_SUBSCRIBER),
    Agent("paper-engine", exec_.paper_engine.run_paper_engine, KIND_SUBSCRIBER),
    # Telegram is a hybrid: it subscribes, then long-polls only when a token is
    # configured. Declaring it "subscriber" is the safe classification — an
    # unconfigured bot returns straight away and must NOT be restarted, while a
    # configured one simply never ends and is never a restart candidate anyway.
    Agent("telegram", ops.telegram_bot.run_telegram_bot, KIND_SUBSCRIBER),
    Agent("fred", sensors.fred.run_fred_agent, KIND_LOOP),
    Agent("positioning", sensors.positioning.run_positioning_agent, KIND_LOOP),
    Agent("calendar", sensors.calendar_agent.run_calendar_agent, KIND_LOOP),
    Agent("news", sensors.news.run_news_agent, KIND_LOOP),
    Agent("state-vector", fusion.state_vector.run_assembler, KIND_SUBSCRIBER),
    # REPORTED DEVIATION: the task brief listed only four subscription-style
    # agents, implying regime was a loop. It is not — fusion/regime.py:310 ends
    # with BUS.subscribe("market_update", ...) and returns, exactly like the
    # other four. Classified "loop" it boots fine, is restarted 4 times, and is
    # abandoned with a CRITICAL ~2min in, permanently disabling regime
    # classification. The registry must describe the agent that exists.
    Agent("regime", fusion.regime.run_regime_agent, KIND_SUBSCRIBER),
    Agent("learning", fusion.learning_loop.run_learning_loop, KIND_LOOP),
    # Appended rather than placed first, even though Ring 0 "boots first":
    # the kernel is CONSTRUCTED in main() before any agent thread starts, so
    # it is already live regardless of where its watchdog sits in this list.
    # Reordering the existing entries would change a start order that has
    # been verified in a live run, to no benefit.
    Agent("kernel-watchdog", lambda: get_kernel().run_kernel_watchdog(), KIND_LOOP),
    Agent("doctrine", ai.doctrine.run_doctrine_agent, KIND_LOOP),
    # Task 21: pods go live. The basis sensor feeds S3; the pod agent sweeps
    # the pods and submits their intents; the position engine owns every
    # router-opened position from fill to close.
    Agent("basis", sensors.basis.run_basis_agent, KIND_LOOP),
    Agent("pod-agent", lambda: exec_.pod_agent.run_pod_agent(get_router()), KIND_SUBSCRIBER),
    Agent("position-engine", lambda: exec_.position_engine.run_position_engine(get_router()), KIND_SUBSCRIBER),
]

_RESTART_WINDOW_SECONDS = 3600.0


def configure_logging() -> None:
    """INFO to stderr, with the thread name — the only way to read this log."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


# --------------------------------------------------------------------------
# Execution stack. Built ONCE at boot, before any agent thread exists, so that
# Ring 0 is live before anything can ask it for permission. The accessors are
# read-only views; there is no setter and nothing rebuilds these at runtime.
# --------------------------------------------------------------------------

_BRIDGE = None
_KERNEL = None
_ROUTER = None


def build_execution_stack():
    """Construct bridge -> kernel -> router, in that order of dependency."""
    global _BRIDGE, _KERNEL, _ROUTER
    _BRIDGE = data.mt5_bridge.make_bridge()
    _KERNEL = exec_.router.build_kernel(_BRIDGE)
    _ROUTER = exec_.router.Router(_BRIDGE, _KERNEL)
    logger.info(
        "execution stack ready: bridge=%s kernel=%s router=%s",
        type(_BRIDGE).__name__, type(_KERNEL).__name__, type(_ROUTER).__name__,
    )

    # Task 23: hand the doctrine a way to read pod performance. The dependency
    # is inverted deliberately — ai/ never imports fusion/, so the doctrine
    # keeps working if the learning loop is broken or absent. A provider that
    # raises degrades the prompt to "no pod history" and nothing more.
    ai.doctrine.set_pod_stats_provider(fusion.learning_loop.pod_stats_snapshot_from_pool)
    logger.info("doctrine: pod stats provider wired to the learning loop")

    return _ROUTER


def get_bridge():
    return _BRIDGE


def get_kernel():
    """The process's single Kernel. None until build_execution_stack() runs."""
    if _KERNEL is None:
        raise RuntimeError("execution stack not built; call build_execution_stack() first")
    return _KERNEL


def get_router():
    """Accessor for Task 21's position engine. Nothing calls submit() yet."""
    return _ROUTER


def boot_database() -> List[str]:
    """
    Verify the database is reachable and bring the schema up to date, before
    a single agent starts.

    A brain with no memory must not pretend to run: if either step fails the
    process exits 1 rather than starting agents whose every write would fail.
    """
    try:
        database.init()
    except Exception as exc:
        logger.critical("database unreachable at boot: %s", exc)
        logger.critical("refusing to start without a database — exiting")
        raise SystemExit(1)

    try:
        applied = database.run_migrations()
    except Exception as exc:
        logger.critical("migrations failed at boot: %s", exc)
        logger.critical("refusing to start on an unknown schema — exiting")
        raise SystemExit(1)

    if applied:
        logger.info("migrations applied: %s", ", ".join(applied))
    else:
        logger.info("migrations: schema already up to date")
    return applied


class Supervisor:
    """
    Owns the agent threads. Deliberately synchronous and poll-based: one
    `poll_once()` pass is a complete, testable unit of supervision, and the
    run loop is nothing more than that pass on a timer.
    """

    def __init__(
        self,
        agents: Optional[Iterable[Agent]] = None,
        poll_seconds: Optional[float] = None,
        max_restarts_per_hour: Optional[int] = None,
        heartbeat_telegram_hours: Optional[float] = None,
    ) -> None:
        self.agents: List[Agent] = list(AGENTS if agents is None else agents)
        for agent in self.agents:
            if agent.kind not in _KINDS:
                raise ValueError(
                    f"agent {agent.name!r} has kind {agent.kind!r}; expected one of {_KINDS}"
                )

        self.poll_seconds = (
            config.SUPERVISOR_POLL_SECONDS if poll_seconds is None else poll_seconds
        )
        self.max_restarts_per_hour = (
            config.MAX_RESTARTS_PER_HOUR
            if max_restarts_per_hour is None
            else max_restarts_per_hour
        )
        hours = (
            config.HEARTBEAT_TELEGRAM_HOURS
            if heartbeat_telegram_hours is None
            else heartbeat_telegram_hours
        )
        self.heartbeat_telegram_seconds = float(hours) * 3600.0

        self.stop_event = threading.Event()
        self.given_up: set = set()
        self._threads: Dict[str, threading.Thread] = {}
        self._restarts: Dict[str, Deque[float]] = {a.name: deque() for a in self.agents}
        # Boot does not count as a heartbeat send: the first Telegram summary
        # is due one full interval after start, so restarting the process is
        # not a way to spam the operator.
        self._last_telegram = time.monotonic()

    # -- thread plumbing ---------------------------------------------------

    def _run_agent(self, agent: Agent) -> None:
        try:
            agent.target()
        except Exception:
            # The traceback belongs here, at the point of death. The poll pass
            # only sees "not alive" and cannot say why.
            logger.exception("agent %s raised", agent.name)
            return

        if agent.kind == KIND_SUBSCRIBER:
            logger.info(
                "agent %s registered (subscriptions active; thread exiting normally)",
                agent.name,
            )
        else:
            logger.error("agent %s returned unexpectedly — loop agents must not return", agent.name)

    def _spawn(self, agent: Agent) -> threading.Thread:
        thread = threading.Thread(
            target=self._run_agent,
            args=(agent,),
            name=f"nexus-{agent.name}",
            daemon=True,
        )
        self._threads[agent.name] = thread
        thread.start()
        return thread

    def start_all(self) -> None:
        logger.info("starting %d agents", len(self.agents))
        for agent in self.agents:
            self._spawn(agent)
            logger.info("agent %s started (kind=%s)", agent.name, agent.kind)

    def threads(self) -> Dict[str, threading.Thread]:
        return dict(self._threads)

    def join_all(self, timeout: float = 5.0) -> None:
        """Wait for currently-running threads to finish. For shutdown and tests."""
        for thread in list(self._threads.values()):
            thread.join(timeout=timeout)

    # -- restart budget ----------------------------------------------------

    def _budget_allows(self, name: str) -> bool:
        """True if `name` may be restarted now; consumes one slot when it is."""
        now = time.monotonic()
        history = self._restarts[name]
        while history and now - history[0] > _RESTART_WINDOW_SECONDS:
            history.popleft()
        if len(history) >= self.max_restarts_per_hour:
            return False
        history.append(now)
        return True

    def _give_up(self, agent: Agent) -> None:
        self.given_up.add(agent.name)
        logger.critical(
            "agent %s exceeded %d restarts in an hour — giving up; it will NOT run again "
            "until the process is restarted",
            agent.name,
            self.max_restarts_per_hour,
        )
        self._notify(
            f"🚨 NEXUS: agent '{agent.name}' failed {self.max_restarts_per_hour} times "
            f"in an hour and has been abandoned. Manual restart required."
        )

    def _notify(self, text: str) -> None:
        """Best-effort Telegram. A dead alert channel must never kill the supervisor."""
        try:
            send_alert(text)
        except Exception:
            logger.warning("send_alert failed", exc_info=True)

    # -- supervision -------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        alive = registered = dead = 0
        for agent in self.agents:
            thread = self._threads.get(agent.name)
            if thread is not None and thread.is_alive():
                alive += 1
            elif agent.kind == KIND_SUBSCRIBER:
                registered += 1
            else:
                dead += 1
        return {"alive": alive, "registered": registered, "dead": dead}

    def poll_once(self) -> Dict[str, int]:
        """
        One supervision pass: restart what should be running, then report.
        Counts are taken after the restart pass so the heartbeat describes the
        state the supervisor just left things in, not the state it found.
        """
        for agent in self.agents:
            if agent.name in self.given_up:
                continue
            thread = self._threads.get(agent.name)
            if thread is not None and thread.is_alive():
                continue
            if agent.kind == KIND_SUBSCRIBER:
                # Terminal and correct. Nothing to do, ever.
                continue

            if not self._budget_allows(agent.name):
                self._give_up(agent)
                continue

            logger.error(
                "agent %s is not alive — restarting (%d/%d within the hour)",
                agent.name,
                len(self._restarts[agent.name]),
                self.max_restarts_per_hour,
            )
            self._spawn(agent)

        counts = self.counts()
        logger.info(
            "heartbeat: alive=%d registered=%d dead=%d (of %d agents)",
            counts["alive"],
            counts["registered"],
            counts["dead"],
            len(self.agents),
        )
        self._maybe_telegram_heartbeat(counts)
        return counts

    def _maybe_telegram_heartbeat(self, counts: Dict[str, int]) -> None:
        if self.heartbeat_telegram_seconds <= 0:
            return
        now = time.monotonic()
        if now - self._last_telegram < self.heartbeat_telegram_seconds:
            return
        self._last_telegram = now
        try:
            summary = build_status_text()
        except Exception:
            logger.warning("heartbeat: build_status_text failed", exc_info=True)
            summary = "NEXUS status unavailable"
        self._notify(
            f"{summary}\n"
            f"agents: {counts['alive']} alive / {counts['registered']} registered / "
            f"{counts['dead']} dead"
        )

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self, signum=None, frame=None) -> None:
        """SIGINT/SIGTERM handler. Must stay trivial — it runs in a signal context."""
        name = signal.Signals(signum).name if signum is not None else "request"
        logger.info("%s received — shutting down", name)
        self.stop_event.set()

    def run(self) -> None:
        """Poll until stopped. Daemon threads die with the process."""
        while not self.stop_event.is_set():
            self.poll_once()
            if self.stop_event.wait(self.poll_seconds):
                break
        logger.info("supervisor loop stopped")


def main() -> int:
    configure_logging()
    # INVARIANT 1: read once, log, never write.
    logger.info("NEXUS starting - stage=%s", get_stage().value)

    boot_database()
    # Ring 0 exists before any agent does.
    build_execution_stack()

    supervisor = Supervisor()
    signal.signal(signal.SIGINT, supervisor.request_stop)
    signal.signal(signal.SIGTERM, supervisor.request_stop)

    supervisor.start_all()
    supervisor.run()

    # Agents hold no unflushed state — every DB write is per-cycle and already
    # committed — so there is nothing to drain here.
    logger.info("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
