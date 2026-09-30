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

import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, Iterable, List, NamedTuple, Optional

import config
from config import get_stage
from core import database
from core.state import STATE

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

# Sensor freshness for the ops heartbeat: name -> (table, column). The three
# tables without a `ts` column are measured by fetched_at (plan F-44).
HEARTBEAT_SENSORS = {
    "candles": ("candles", "ts"),
    "state_vectors": ("state_vectors", "ts"),
    "macro_observations": ("macro_observations", "ts"),
    "cot_reports": ("cot_reports", "fetched_at"),
    "econ_events": ("econ_events", "fetched_at"),
    "news_articles": ("news_articles", "fetched_at"),
}


def rss_mb() -> Optional[float]:
    """
    This process's resident memory in MB. Current RSS from /proc/self/status
    (Linux, i.e. production). Elsewhere the stdlib only offers the PEAK
    (resource.ru_maxrss: KB on Linux, bytes on macOS). None if neither works.
    """
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
        return round(peak / divisor, 1)
    except Exception:
        return None


def later(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    """The later of two optional timestamps; None only when both are None."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _last_market_update_at() -> Optional[datetime]:
    """
    When market data was last refreshed. data/gold_agent.py stamps
    STATE.last_analysis_ts (epoch seconds, despite the name) right before it
    publishes market_update. None when it has never run.
    """
    raw = getattr(STATE, "last_analysis_ts", None)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    return None


def _query_one(cur, sql: str, params=()) -> Optional[tuple]:
    """
    One read inside the heartbeat transaction, isolated by a savepoint so a
    missing table or column yields None instead of aborting the transaction.
    """
    cur.execute("SAVEPOINT hb_read")
    try:
        cur.execute(sql, params)
        row = cur.fetchone()
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT hb_read")
        logger.debug("ops heartbeat: read failed: %s", sql, exc_info=True)
        return None
    cur.execute("RELEASE SAVEPOINT hb_read")
    return row


def _age_s(now: datetime, then: Optional[datetime]) -> Optional[int]:
    return int((now - then).total_seconds()) if isinstance(then, datetime) else None


def prune_heartbeats(cur, now: datetime) -> None:
    """Delete ops_heartbeats rows older than config.HEARTBEAT_RETENTION_DAYS."""
    cutoff = now - timedelta(days=config.HEARTBEAT_RETENTION_DAYS)
    cur.execute("DELETE FROM ops_heartbeats WHERE ts < %s", (cutoff,))
    logger.info("ops heartbeat: pruned %d rows older than %s", cur.rowcount, cutoff.isoformat())


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
        ops_heartbeat: bool = False,
    ) -> None:
        """
        `ops_heartbeat` turns on the per-poll ops_heartbeats row and the
        model-silence alert. main() turns it on; constructing a Supervisor
        elsewhere (the supervision tests) writes nothing to the database.
        """
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

        # Ops heartbeat (Task O1). State below is touched only by the single
        # heartbeat thread (at most one runs at a time).
        self.ops_heartbeat = ops_heartbeat
        self._ops_thread: Optional[threading.Thread] = None
        self._boot_utc = datetime.now(timezone.utc)
        self._last_prune_day: Optional[date] = None
        self._model_silent = False
        self._silence_alert_at: Optional[datetime] = None

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
        self._start_ops_heartbeat(counts)
        return counts

    # -- ops heartbeat (Task O1) ---------------------------------------------

    def _start_ops_heartbeat(self, counts: Dict[str, int]) -> None:
        """
        Fire-and-forget: the heartbeat runs on its own daemon thread, so a
        slow or hung database can never delay this loop (INVARIANT 6). If the
        previous heartbeat is still running, this poll's is skipped rather
        than piling up threads.
        """
        if not self.ops_heartbeat:
            return
        try:
            previous = self._ops_thread
            if previous is not None and previous.is_alive():
                logger.warning("ops heartbeat: previous write still running; skipping this poll")
                return
            thread = threading.Thread(
                target=self._ops_heartbeat,
                args=(dict(counts),),
                name="nexus-ops-heartbeat",
                daemon=True,
            )
            self._ops_thread = thread
            thread.start()
        except Exception:
            logger.error("ops heartbeat: could not start the heartbeat thread", exc_info=True)

    def wait_ops_heartbeat(self, timeout: float = 5.0) -> None:
        """Join the in-flight heartbeat thread, if any. For shutdown and tests."""
        thread = self._ops_thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _ops_heartbeat(self, counts: Dict[str, int], now: Optional[datetime] = None) -> Optional[int]:
        """
        Write one ops_heartbeats row, prune once per UTC day, then check for
        model silence. Returns the new row id, or None when the write failed.
        Never raises.
        """
        now = now or datetime.now(timezone.utc)
        model_ok_at = getattr(STATE, "last_model_success_at", None)
        model_ok_known = False  # True once the doctrines side has been read
        row_id = None
        try:
            with database.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SET LOCAL statement_timeout = %s",
                        (int(config.HEARTBEAT_STATEMENT_TIMEOUT_MS),),
                    )
                    fable = _query_one(
                        cur, "SELECT max(ts) FROM doctrines WHERE source = 'FABLE'"
                    )
                    if fable is not None:
                        model_ok_at = later(model_ok_at, fable[0])
                        model_ok_known = True

                    newest = _query_one(
                        cur, "SELECT ts, bias, source FROM doctrines ORDER BY ts DESC LIMIT 1"
                    )
                    doctrine_ts, doctrine_bias, doctrine_source = newest or (None, None, None)

                    sensors: Dict[str, Optional[int]] = {}
                    for name, (table, column) in HEARTBEAT_SENSORS.items():
                        # Identifiers come from the constant map above, never input.
                        freshest = _query_one(cur, f"SELECT max({column}) FROM {table}")
                        sensors[name] = _age_s(now, freshest[0] if freshest else None)

                    cur.execute(
                        """
                        INSERT INTO ops_heartbeats
                            (ts, stage, alive, registered, dead, rss_mb,
                             last_market_update_at, model_ok_at, doctrine_bias,
                             doctrine_source, doctrine_age_s, budget_today_usd, sensors)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        RETURNING id
                        """,
                        (
                            now,
                            get_stage().value,
                            counts.get("alive"),
                            counts.get("registered"),
                            counts.get("dead"),
                            rss_mb(),
                            _last_market_update_at(),
                            model_ok_at,
                            doctrine_bias,
                            doctrine_source,
                            _age_s(now, doctrine_ts),
                            float(getattr(STATE, "budget_spent_today", 0.0) or 0.0),
                            json.dumps(sensors),
                        ),
                    )
                    row_id = cur.fetchone()[0]

                    if self._last_prune_day != now.date():
                        # Claimed before running: a failing prune is retried
                        # tomorrow, not every 30 seconds.
                        self._last_prune_day = now.date()
                        cur.execute("SAVEPOINT hb_prune")
                        try:
                            prune_heartbeats(cur, now)
                        except Exception:
                            cur.execute("ROLLBACK TO SAVEPOINT hb_prune")
                            logger.error("ops heartbeat: prune failed", exc_info=True)
        except Exception:
            row_id = None
            logger.error("ops heartbeat: write failed; supervision continues", exc_info=True)

        if not model_ok_known:
            # Half the evidence is missing: an in-process timestamp alone
            # would raise false "AI dead" alarms while the doctrine gate keeps
            # the analyst quiet. No guess; the next poll tries again.
            logger.warning("ops heartbeat: model_ok_at unknown (doctrines unreadable); silence check skipped")
            return row_id
        try:
            self._check_model_silence(model_ok_at, now)
        except Exception:
            logger.error("ops heartbeat: model-silence check failed", exc_info=True)
        return row_id

    def _check_model_silence(self, model_ok_at: Optional[datetime], now: datetime) -> None:
        """
        Alert once when no AI call has succeeded for MODEL_SILENCE_ALERT_MINUTES
        (measured from boot while there has never been one), repeat at most
        once per that interval, and send one recovery message when it clears.
        """
        threshold = timedelta(minutes=config.MODEL_SILENCE_ALERT_MINUTES)
        reference = model_ok_at if model_ok_at is not None else self._boot_utc
        silence = now - reference
        if silence > threshold:
            if self._silence_alert_at is None or now - self._silence_alert_at >= threshold:
                self._silence_alert_at = now
                self._model_silent = True
                minutes = int(silence.total_seconds() // 60)
                self._notify(
                    f"NEXUS: no successful AI call for {minutes}m - check Anthropic credits/API"
                )
            return
        if self._model_silent:
            self._notify(
                "NEXUS: AI calls recovered - last successful call "
                f"{model_ok_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )
        self._model_silent = False
        self._silence_alert_at = None

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

    supervisor = Supervisor(ops_heartbeat=True)
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
