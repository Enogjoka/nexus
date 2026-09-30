"""
The projector: turns NEXUS's audit tables into one normalized event feed.

Every 2 s it reads each audit table past its id watermark (at most 500 rows per
table per poll) and normalizes each row to

    {id, ts, ring, agent, event_type, severity, correlation_id, trade_id,
     summary, payload}

into a ring buffer of the last 2,000 events. `id` is the projector's own
sequence number (it restarts with the process; `epoch` in health tells a client
to resync). The source table and row id are in `payload`.

Rings: sensors 3 · doctrine/analysis/validator 2 · paper engine/router/
positions/fills 1 · kernel/stage/commands 0.

Signals are updated in place and have no per-change log, so their status
changes are found by re-reading the recently active signals and diffing the
status against memory.

A database error pauses the loop and retries with exponential backoff. The
projector never raises into the API.
"""
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional

import psycopg2
import psycopg2.errors

from .db import ReadOnlyDB

logger = logging.getLogger("observatory.events")

POLL_SECONDS = 2.0
BATCH_LIMIT = 500
BUFFER_SIZE = 2000
BACKFILL_PER_TABLE = 50
BACKOFF_MAX_S = 30.0
ACTIVE_SIGNAL_LIMIT = 500

SEVERITIES = ("debug", "info", "warn", "error", "critical")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}

# (table, SELECT columns). Order is the order tables are read in each poll.
TABLES = [
    ("validator_log", "id, ts, symbol, rule_name, rule_result, details"),
    (
        "doctrines",
        "id, ts, bias, conviction, risk_multiplier, enabled_pods, swing_signals_allowed, "
        "source, no_trade_reason, review_horizon_min, "
        "(raw_response IS NULL OR raw_response = '') AS raw_missing",
    ),
    ("signals", "id, ts, symbol, direction, grade, confidence, status, prices, filled_at, outcome_r"),
    ("kernel_events", "id, ts, breaker, action, reason, context"),
    (
        "positions",
        "id, created_at AS ts, client_order_id, source, pod, direction, lots, entry_px, state, "
        "close_reason",
    ),
    (
        "fills",
        "id, ts, client_order_id, signal_id, direction, lots, requested_px, fill_px, slippage, "
        "fill_mode, status, kernel_reason",
    ),
    ("stage_events", "id, ts, event, env_stage, effective_stage, reason"),
    ("command_audit", "id, ts, command, args, stage, outcome, detail"),
    (
        "ops_heartbeats",
        "id, ts, stage, alive, registered, dead, rss_mb, model_ok_at, doctrine_bias, "
        "doctrine_source, sensors",
    ),
]
TABLE_NAMES = [name for name, _ in TABLES]

# Heartbeats arrive every 30 s. Only a change in these fields is an event;
# otherwise 2,880 identical rows a day would flush everything else out of the
# 2,000-event buffer.
_HEARTBEAT_CHANGE_FIELDS = ("stage", "alive", "registered", "dead", "doctrine_bias", "doctrine_source")

_TERMINAL = ("STOPPED", "BE", "TP2", "EXPIRED")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _rule_label(rule_name: str) -> str:
    """RULE0_ENTRY_DRIFT -> 'RULE0 ENTRY_DRIFT'."""
    head, _, tail = (rule_name or "").partition("_")
    return f"{head} {tail}" if tail else head


def _describe_details(details: Any) -> str:
    """One short clause from a validator_log details object — only what is there."""
    if not isinstance(details, dict):
        return ""
    if details.get("reason"):
        return str(details["reason"])
    warnings = details.get("warnings")
    if warnings:
        return ", ".join(str(w) for w in warnings)
    if details.get("fix_blocked"):
        return f"London fix {details.get('fix_time')} blackout"
    if details.get("event_blocked") and details.get("blocking_event"):
        return f"event blackout: {details['blocking_event']}"
    if details.get("conflict_value"):
        return f"{details.get('direction')} against {details['conflict_value']}"
    parts = [f"{k}={v}" for k, v in details.items() if not isinstance(v, (dict, list))]
    return ", ".join(parts)[:120]


class Projector:
    def __init__(
        self,
        db: ReadOnlyDB,
        poll_seconds: float = POLL_SECONDS,
        batch_limit: int = BATCH_LIMIT,
        buffer_size: int = BUFFER_SIZE,
        backfill_per_table: int = BACKFILL_PER_TABLE,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.db = db
        self.poll_seconds = poll_seconds
        self.batch_limit = batch_limit
        self.backfill_per_table = backfill_per_table
        self.clock = clock

        self._lock = threading.Lock()
        self._buffer: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self._seq = 0
        self.epoch = clock().isoformat()

        self.watermarks: Dict[str, int] = {}
        self.absent_tables: set = set()
        self.active_signals: Dict[int, str] = {}
        self._last_heartbeat_key: Optional[tuple] = None
        self.heartbeats_absorbed = 0

        self.primed = False
        self.state = "starting"
        self.backoff_s = 0.0
        self.last_ok_monotonic: Optional[float] = None
        self.last_error: Optional[str] = None

    # -- reading ------------------------------------------------------------

    def _read_after(self, table: str, columns: str, after_id: int, limit: int) -> List[Dict[str, Any]]:
        return self.db.rows(
            f"SELECT {columns} FROM {table} WHERE id > %s ORDER BY id LIMIT %s",
            (after_id, limit),
        )

    def _read_tail(self, table: str, columns: str, limit: int) -> List[Dict[str, Any]]:
        rows = self.db.rows(f"SELECT {columns} FROM {table} ORDER BY id DESC LIMIT %s", (limit,))
        return list(reversed(rows))

    # -- priming ----------------------------------------------------------------

    def prime(self) -> None:
        """
        Set every watermark to the table's current tail, loading the last
        `backfill_per_table` rows of each as history (sorted by time, so the
        first page of the feed reads chronologically), and remember which
        signals are still active.
        """
        backfill: List[Dict[str, Any]] = []
        for table, columns in TABLES:
            try:
                tail = self._read_tail(table, columns, max(self.backfill_per_table, 1))
            except psycopg2.errors.UndefinedTable:
                self.absent_tables.add(table)
                self.watermarks[table] = 0
                continue
            self.watermarks[table] = tail[-1]["id"] if tail else 0
            if self.backfill_per_table > 0:
                for row in tail:
                    event = self._normalize(table, row)
                    if event is not None:
                        backfill.append(event)

        active = self.db.rows(
            "SELECT id, status FROM signals WHERE status IN ('PENDING', 'OPEN') "
            "ORDER BY id DESC LIMIT %s",
            (ACTIVE_SIGNAL_LIMIT,),
        )
        self.active_signals = {row["id"]: row["status"] for row in active}

        backfill.sort(key=lambda e: e["ts"] or "")
        self._publish(backfill)
        self.primed = True

    # -- polling ----------------------------------------------------------------

    def poll_once(self) -> int:
        """
        One pass over every table past its watermark, then the signal status
        diff. Returns how many events were published. A table that does not
        exist is skipped (and re-checked each poll); any other database error
        propagates so the run loop can back off.
        """
        if not self.primed:
            self.prime()
        published = 0
        for table, columns in TABLES:
            try:
                rows = self._read_after(table, columns, self.watermarks.get(table, 0), self.batch_limit)
            except psycopg2.errors.UndefinedTable:
                self.absent_tables.add(table)
                continue
            self.absent_tables.discard(table)
            events = []
            for row in rows:
                if table == "signals" and row.get("status") in ("PENDING", "OPEN"):
                    self.active_signals[row["id"]] = row["status"]
                event = self._normalize(table, row)
                if event is not None:
                    events.append(event)
            published += self._publish(events)
            if rows:
                # Advance only after the table's rows are published: a failure
                # later in this poll re-reads nothing and loses nothing.
                self.watermarks[table] = rows[-1]["id"]
        published += self._publish(self._signal_transitions())
        self.last_ok_monotonic = time.monotonic()
        return published

    def _signal_transitions(self) -> List[Dict[str, Any]]:
        if not self.active_signals:
            return []
        ids = sorted(self.active_signals)[-ACTIVE_SIGNAL_LIMIT:]
        rows = self.db.rows(
            "SELECT id, ts, symbol, direction, status, prices, filled_at, outcome_ts, outcome_r "
            "FROM signals WHERE id = ANY(%s) ORDER BY id LIMIT %s",
            (ids, ACTIVE_SIGNAL_LIMIT),
        )
        seen = set()
        events = []
        for row in rows:
            seen.add(row["id"])
            before = self.active_signals.get(row["id"])
            after = row["status"]
            if after != before:
                events.append(self._transition_event(row, before))
            if after in ("PENDING", "OPEN"):
                self.active_signals[row["id"]] = after
            else:
                self.active_signals.pop(row["id"], None)
        for gone in set(ids) - seen:
            self.active_signals.pop(gone, None)  # no longer readable: stop tracking
        return events

    def _transition_event(self, row: Dict[str, Any], before: Optional[str]) -> Dict[str, Any]:
        after = row["status"]
        sid = row["id"]
        prices = row.get("prices") or {}
        if after == "OPEN":
            event_type, severity = "signal_filled", "info"
            summary = (
                f"Signal #{sid} {row['direction']} filled on the {row.get('filled_at')} H1 bar "
                f"(entry {_fmt(prices.get('entry'))})"
            )
        elif after == "EXPIRED":
            event_type, severity = "signal_expired", "info"
            summary = f"Signal #{sid} {row['direction']} expired unfilled"
        elif after in _TERMINAL:
            event_type = "signal_closed"
            severity = "warn" if after == "STOPPED" else "info"
            summary = f"Signal #{sid} {row['direction']} closed {after}: {_fmt(row.get('outcome_r'))}R"
        else:
            event_type, severity = "signal_status", "info"
            summary = f"Signal #{sid} status {before} -> {after}"
        return {
            "ts": self.clock().isoformat(),
            "ring": 1,
            "agent": "paper_engine",
            "event_type": event_type,
            "severity": severity,
            "correlation_id": f"cycle:{row['ts']}",
            "trade_id": f"signal:{sid}",
            "summary": summary,
            "payload": {
                "table": "signals",
                "row_id": sid,
                "from": before,
                "to": after,
                "bar_time": row.get("filled_at"),
                "close_bar_time": row.get("outcome_ts"),
                "outcome_r": row.get("outcome_r"),
                "ts_basis": "time the projector saw the change; bar times are in payload (plan F-39)",
            },
        }

    # -- normalizing ------------------------------------------------------------

    def _normalize(self, table: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        handler = getattr(self, f"_n_{table}")
        body = handler(row)
        if body is None:
            return None
        payload = body.pop("payload", {})
        return {
            "ts": row.get("ts"),
            **body,
            "payload": {"table": table, "row_id": row["id"], **payload},
        }

    def _n_validator_log(self, row):
        result = row.get("rule_result") or ""
        rule = row.get("rule_name") or ""
        clause = _describe_details(row.get("details")) if result != "PASS" else ""
        gate = rule == "DOCTRINE_GATE"
        return {
            "ring": 2,
            "agent": "analysis" if gate else "validator",
            "event_type": f"validator_{result.lower()}" if result else "validator",
            "severity": {"WAIT": "warn", "WARN": "info", "PASS": "debug", "SKIPPED_NO_DATA": "debug"}.get(result, "info"),
            "correlation_id": f"cycle:{row.get('ts')}",
            "trade_id": None,
            "summary": f"{_rule_label(rule)}: {result}" + (f" — {clause}" if clause else ""),
            "payload": {"symbol": row.get("symbol"), "details": row.get("details")},
        }

    def _n_doctrines(self, row):
        source = row.get("source")
        api_failed = source == "PARSE_FALLBACK" and bool(row.get("raw_missing"))
        if source == "FABLE":
            severity = "info"
        elif api_failed:
            severity = "error"
        else:
            severity = "warn"
        summary = (
            f"Doctrine {row.get('bias')} (conviction {row.get('conviction')}/10, "
            f"risk x{_fmt(row.get('risk_multiplier'))}, swing "
            f"{'on' if row.get('swing_signals_allowed') else 'off'}, "
            f"{row.get('review_horizon_min')} min) from {source}"
        )
        if api_failed:
            summary += " — model call failed"
        elif row.get("no_trade_reason"):
            summary += f" — {row['no_trade_reason']}"
        return {
            "ring": 2,
            "agent": "doctrine",
            "event_type": "doctrine_issued" if source == "FABLE" else "doctrine_fallback",
            "severity": severity,
            "correlation_id": None,
            "trade_id": None,
            "summary": summary,
            "payload": {k: row.get(k) for k in (
                "bias", "conviction", "risk_multiplier", "enabled_pods", "swing_signals_allowed",
                "source", "no_trade_reason", "review_horizon_min",
            )} | {"api_failed": api_failed},
        }

    def _n_signals(self, row):
        prices = row.get("prices") or {}
        return {
            "ring": 2,
            "agent": "analysis",
            "event_type": "signal_persisted",
            "severity": "info",
            "correlation_id": f"cycle:{row.get('ts')}",
            "trade_id": f"signal:{row['id']}",
            "summary": (
                f"Signal #{row['id']} {row.get('direction')} grade {row.get('grade')} persisted: "
                f"entry {_fmt(prices.get('entry'))}, stop {_fmt(prices.get('stop'))} ({row.get('status')})"
            ),
            "payload": {
                "direction": row.get("direction"),
                "grade": row.get("grade"),
                "confidence": row.get("confidence"),
                "status": row.get("status"),
                "entry": prices.get("entry"),
                "stop": prices.get("stop"),
                "tp1": prices.get("tp1"),
                "tp2": prices.get("tp2"),
            },
        }

    def _n_kernel_events(self, row):
        action = row.get("action") or ""
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        coid = context.get("client_order_id")
        severity = {
            "HALT": "critical", "FLATTEN": "critical", "DEMOTE": "critical",
            "DENY": "error", "CLAMP": "warn", "ALLOW": "debug", "PASS": "debug",
        }.get(action, "info")
        return {
            "ring": 0,
            "agent": "kernel",
            "event_type": f"kernel_{action.lower()}" if action else "kernel",
            "severity": severity,
            "correlation_id": coid,
            "trade_id": coid,
            "summary": f"Kernel {action} ({row.get('breaker')}): {row.get('reason')}",
            "payload": {"breaker": row.get("breaker"), "action": action, "reason": row.get("reason"), "context": context},
        }

    def _n_positions(self, row):
        coid = row.get("client_order_id")
        origin = row.get("source") or "?"
        if row.get("pod"):
            origin += f"/{row['pod']}"
        return {
            "ring": 1,
            "agent": "router",
            "event_type": f"position_{(row.get('state') or 'recorded').lower()}",
            "severity": "info",
            "correlation_id": coid,
            "trade_id": coid,
            "summary": (
                f"Position {coid} {row.get('direction')} {_fmt(row.get('lots'))} lots ({origin}) "
                f"state {row.get('state')}"
            ),
            "payload": {k: row.get(k) for k in (
                "client_order_id", "source", "pod", "direction", "lots", "entry_px", "state", "close_reason",
            )},
        }

    def _n_fills(self, row):
        coid = row.get("client_order_id")
        status = row.get("status") or ""
        summary = (
            f"Fill {coid} {row.get('direction')} {_fmt(row.get('lots'))} lots @ {_fmt(row.get('fill_px'))} "
            f"({row.get('fill_mode')}, {status})"
        )
        if row.get("kernel_reason"):
            summary += f" — {row['kernel_reason']}"
        return {
            "ring": 1,
            "agent": "router",
            "event_type": f"fill_{status.lower()}" if status else "fill",
            "severity": "info" if status == "FILLED" else "warn",
            "correlation_id": coid,
            "trade_id": coid,
            "summary": summary,
            "payload": {k: row.get(k) for k in (
                "client_order_id", "signal_id", "direction", "lots", "requested_px", "fill_px",
                "slippage", "fill_mode", "status", "kernel_reason",
            )},
        }

    def _n_stage_events(self, row):
        event = row.get("event") or ""
        severity = {"DEMOTION_WRITTEN": "critical", "BOOT": "info"}.get(event, "warn")
        summary = f"{event}: env stage {row.get('env_stage')} -> effective {row.get('effective_stage')}"
        if row.get("reason"):
            summary += f" — {row['reason']}"
        return {
            "ring": 0,
            "agent": "stage",
            "event_type": f"stage_{event.lower()}" if event else "stage",
            "severity": severity,
            "correlation_id": None,
            "trade_id": None,
            "summary": summary,
            "payload": {k: row.get(k) for k in ("event", "env_stage", "effective_stage", "reason")},
        }

    def _n_command_audit(self, row):
        command = row.get("command") or ""
        outcome = row.get("outcome") or ""
        if command in ("/killswitch", "/flatten") and outcome == "CONFIRMED":
            severity = "critical"
        elif command in ("/killswitch", "/flatten", "/clearkill"):
            severity = "warn"
        elif outcome == "UNKNOWN":
            severity = "debug"
        else:
            severity = "info"
        args = f" {row['args']}" if row.get("args") else ""
        return {
            "ring": 0,
            "agent": "commands",
            "event_type": "command",
            "severity": severity,
            "correlation_id": None,
            "trade_id": None,
            "summary": f"Operator command {command}{args} -> {outcome}",
            # chat_id is deliberately not read: the feed never carries who sent it.
            "payload": {k: row.get(k) for k in ("command", "args", "stage", "outcome", "detail")},
        }

    def _n_ops_heartbeats(self, row):
        key = tuple(row.get(field) for field in _HEARTBEAT_CHANGE_FIELDS)
        if key == self._last_heartbeat_key:
            self.heartbeats_absorbed += 1
            return None
        self._last_heartbeat_key = key
        dead = row.get("dead") or 0
        return {
            "ring": 3,
            "agent": "supervisor",
            "event_type": "heartbeat",
            "severity": "error" if dead else "debug",
            "correlation_id": None,
            "trade_id": None,
            "summary": (
                f"Heartbeat: {row.get('alive')} alive / {row.get('registered')} registered / "
                f"{dead} dead, stage {row.get('stage')}, doctrine {row.get('doctrine_bias')} "
                f"({row.get('doctrine_source')})"
            ),
            "payload": {k: row.get(k) for k in (
                "stage", "alive", "registered", "dead", "rss_mb", "model_ok_at",
                "doctrine_bias", "doctrine_source", "sensors",
            )},
        }

    # -- buffer -----------------------------------------------------------------

    def _publish(self, events: List[Dict[str, Any]]) -> int:
        if not events:
            return 0
        with self._lock:
            for event in events:
                self._seq += 1
                self._buffer.append({"id": self._seq, **event})
        return len(events)

    def events(
        self,
        since_id: int = 0,
        ring: Optional[int] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Buffered events with id > since_id, oldest first. `severity` is a floor:
        "warn" returns warn, error and critical.
        """
        floor = SEVERITY_RANK.get(severity, 0) if severity else 0
        with self._lock:
            snapshot = list(self._buffer)
        out = [
            e for e in snapshot
            if e["id"] > since_id
            and (ring is None or e["ring"] == ring)
            and SEVERITY_RANK.get(e["severity"], 0) >= floor
        ]
        return out[:limit]

    def last_event_id(self) -> int:
        with self._lock:
            return self._seq

    # -- run loop -----------------------------------------------------------------

    def step(self) -> float:
        """
        One guarded iteration: poll, or record the failure and back off.
        Returns how long to wait before the next step. Never raises.
        """
        try:
            self.poll_once()
        except Exception as exc:
            self.backoff_s = min(BACKOFF_MAX_S, max(self.poll_seconds, self.backoff_s * 2 or self.poll_seconds))
            self.state = "backoff"
            self.last_error = f"{type(exc).__name__} at {self.clock().isoformat()}"
            logger.error("projector: poll failed (%s); retrying in %.1fs", type(exc).__name__, self.backoff_s)
            return self.backoff_s
        self.backoff_s = 0.0
        self.state = "running"
        return self.poll_seconds

    def run(self, stop_event: threading.Event) -> None:
        logger.info("projector: starting (poll every %.1fs)", self.poll_seconds)
        while not stop_event.is_set():
            wait = self.step()
            if stop_event.wait(wait):
                break
        self.state = "stopped"
        logger.info("projector: stopped")

    def health(self) -> Dict[str, Any]:
        lag = None if self.last_ok_monotonic is None else round(time.monotonic() - self.last_ok_monotonic, 1)
        return {
            "state": self.state,
            "lag_s": lag,
            "epoch": self.epoch,
            "last_event_id": self.last_event_id(),
            "buffered": len(self._buffer),
            "absent_tables": sorted(self.absent_tables) or None,
            "last_error": self.last_error,
            "heartbeats_absorbed": self.heartbeats_absorbed,
        }


def dumps(event: Dict[str, Any]) -> str:
    return json.dumps(event, separators=(",", ":"))
