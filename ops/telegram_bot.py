"""
NEXUS Telegram bot — outbound alerts + the human's command deck.

Transport: the raw Telegram HTTP API via `requests` (chosen over
python-telegram-bot to keep the dependency surface small and the tests
trivially mockable). The bot token and the chat-id allowlist are SECRETS and
come from the environment ONLY (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS).

THE DECK PULLS LEVERS THAT ALREADY EXIST. It never grows new ones.
/killswitch writes config.KILL_FILE_PATH — the same file risk/kernel.py checks
at the top of permit() and on every watchdog tick. /flatten calls the router's
existing flatten_all. Nothing here reaches into kernel internals, sets a halt
flag, or invents a way to stop trading that the kernel does not already
honour. That is deliberate: a control panel with its own private mechanisms is
a second safety system, and two safety systems disagree at exactly the wrong
moment.

Consequently there is also nothing here that STARTS anything. Every command is
either a read or a brake. The deck can stop NEXUS; it cannot make it trade.

EVERY DANGEROUS COMMAND IS TWO-STEP. /killswitch, /clearkill and /flatten
reply asking for CONFIRM and do nothing until it arrives — from the SAME chat
id, within config.COMMAND_CONFIRM_SECONDS. A fat-fingered /flatten is a
message; a fat-fingered flatten is a realised loss.

EVERY COMMAND IS AUDITED, including ones from chat ids that are not allowed to
speak to the bot. A log that records only successful commands cannot tell you
who else was trying.

If either secret is missing the bot disables itself: it logs one warning and
every send becomes a no-op returning False. NEXUS never crashes because
Telegram is unconfigured (INVARIANT 6).
"""
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

import config
from config import get_stage
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_warned_disabled = False

# Process start, used for the uptime line. Module import happens at boot, so
# this is the backend's start time to within a few milliseconds.
_STARTED_AT = time.monotonic()

# chat_id -> (command, args, deadline_monotonic). One pending confirmation per
# chat: a second dangerous command replaces the first rather than queueing, so
# CONFIRM can never apply to a command the operator has forgotten about.
_PENDING: Dict[str, Tuple[str, str, float]] = {}
_PENDING_LOCK = threading.Lock()

CONFIRM_WORD = "CONFIRM"
DANGEROUS = ("/killswitch", "/clearkill", "/flatten")

HELP_TEXT = (
    "NEXUS command deck\n"
    "\n"
    "/status     — stage, positions, pnl, doctrine, budget, uptime\n"
    "/pods       — per-pod enablement and today's activity\n"
    "/doctrine   — the current doctrine in full\n"
    "\n"
    "Dangerous (two-step — reply CONFIRM within "
    f"{config.COMMAND_CONFIRM_SECONDS}s):\n"
    "/killswitch — write the KILL file; the kernel refuses every order\n"
    "/clearkill  — remove the KILL file\n"
    "/flatten    — close every open position now\n"
)


def _allowlist() -> List[str]:
    raw = config.TELEGRAM_CHAT_IDS
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def _is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN) and bool(_allowlist())


def _warn_disabled_once() -> None:
    global _warned_disabled
    if not _warned_disabled:
        logger.warning(
            "telegram: disabled (TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_IDS unset); "
            "all sends are no-ops"
        )
        _warned_disabled = True


def _redact(text: str) -> str:
    """
    Replace the bot token (when set and non-empty) with "**TOKEN**". The full
    API URL embeds the token, and requests exceptions routinely quote the URL
    they failed on — so every exception string that reaches a log line must
    pass through here first. The token must never appear in a log record.
    """
    token = config.TELEGRAM_BOT_TOKEN
    if token and text:
        return text.replace(token, "**TOKEN**")
    return text


def _api_post(method: str, payload: dict) -> Optional[dict]:
    """POST to the Telegram API; return parsed JSON or None on any failure."""
    url = f"{config.TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.post(url, json=payload, timeout=config.TELEGRAM_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        # Never log the URL; redact the token out of the exception text.
        logger.error("telegram: %s failed (%s)", method, _redact(str(exc)))
        return None


def _api_get(method: str, params: dict) -> Optional[dict]:
    url = f"{config.TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        resp = requests.get(url, params=params, timeout=config.TELEGRAM_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        # Never log the URL; redact the token out of the exception text.
        logger.error("telegram: %s failed (%s)", method, _redact(str(exc)))
        return None


def send_alert(text: str) -> bool:
    """
    Send `text` to every allowlisted chat id. Returns True only if all sends
    succeeded, False if the bot is disabled or any send failed.
    """
    if not _is_configured():
        _warn_disabled_once()
        return False
    ok = True
    for chat_id in _allowlist():
        if _api_post("sendMessage", {"chat_id": chat_id, "text": text}) is None:
            ok = False
    return ok


def _reply(chat_id: str, text: str) -> bool:
    """Reply to ONE chat — the requesting one — never broadcast a command reply."""
    if not _is_configured():
        _warn_disabled_once()
        return False
    return _api_post("sendMessage", {"chat_id": chat_id, "text": text}) is not None


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def audit(chat_id: str, command: str, args: str, outcome: str, detail: str = "") -> bool:
    """
    Record one command. Best-effort (INVARIANT 6): losing the audit row must
    never stop a brake from being applied, so this never raises into a handler.
    """
    try:
        database.execute(
            "INSERT INTO command_audit (chat_id, command, args, stage, outcome, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (chat_id, command, args or None, get_stage().value, outcome, detail or None),
        )
        return True
    except Exception:
        logger.error(
            "telegram: could not audit %s/%s from %s", command, outcome, chat_id, exc_info=True
        )
        return False


# --------------------------------------------------------------------------
# alert formatting + BUS wiring
# --------------------------------------------------------------------------


def _format_opened(p: dict) -> str:
    thesis = (p.get("thesis") or "")[:200]
    return (
        f"🟢 OPENED {p.get('direction')} [{p.get('grade')}] #{p.get('id')}\n"
        f"entry {p.get('entry')}  stop {p.get('stop')}\n"
        f"tp1 {p.get('tp1')}  tp2 {p.get('tp2')}\n"
        f"lots {p.get('lots')}\n"
        f"{thesis}"
    )


def _format_closed(p: dict) -> str:
    outcome = p.get("outcome_r")
    outcome_str = "n/a" if outcome is None else f"{outcome:+.2f}R"
    return f"🔴 CLOSED #{p.get('id')} {p.get('status')} outcome={outcome_str}"


def on_signal_opened(payload: dict) -> None:
    send_alert(_format_opened(payload or {}))


def on_signal_closed(payload: dict) -> None:
    send_alert(_format_closed(payload or {}))


# --------------------------------------------------------------------------
# readers — every one of these degrades to a printed "unavailable" rather than
# raising, because a status command that can crash is a status command you
# cannot trust in the exact moment you need it.
# --------------------------------------------------------------------------


def _stage_line() -> str:
    try:
        from risk import stage

        if stage.demotion_active():
            return (
                f"stage: {stage.effective_stage().value} "
                f"(DEMOTED from {stage.env_stage().value})\n"
                f"  reason: {stage.demotion_reason()}"
            )
        return f"stage: {stage.effective_stage().value}"
    except Exception:
        logger.warning("telegram: stage unavailable", exc_info=True)
        return f"stage: {get_stage().value} (demotion state unavailable)"


def _positions_line() -> str:
    try:
        rows = database.fetch(
            "SELECT direction, COALESCE(lots,0), COALESCE(entry_px,0) FROM positions "
            "WHERE state IN ('OPEN','PARTIAL','BE','TRAILING')"
        )
    except Exception:
        logger.warning("telegram: positions query failed", exc_info=True)
        return "open positions: unavailable"

    if not rows:
        return "open positions: 0"
    net = sum(float(r[1]) * (1 if r[0] == "LONG" else -1) for r in rows)
    return f"open positions: {len(rows)} (net {net:+.2f} lots)"


def _closed_today_line() -> str:
    try:
        row = database.fetch(
            "SELECT count(*), COALESCE(SUM(realized_pnl_usd),0) FROM positions "
            "WHERE state='CLOSED' AND (closed_at AT TIME ZONE 'UTC')::date "
            "= (now() AT TIME ZONE 'UTC')::date"
        )[0]
        return f"closed today: {int(row[0])} (net ${float(row[1]):+.2f})"
    except Exception:
        logger.warning("telegram: closed-today query failed", exc_info=True)
        return "closed today: unavailable"


def _held_doctrine():
    """
    The doctrine as HELD, without triggering the expiry side effects.

    HOLDER.current() persists a fallback row when the posture has expired;
    reading status must not write to the doctrines table, so this uses the
    side-effect-free accessor and reports the age itself.
    """
    try:
        from ai.doctrine import HOLDER

        return HOLDER.get_held()
    except Exception:
        logger.warning("telegram: doctrine unavailable", exc_info=True)
        return None


def _doctrine_line() -> str:
    doctrine = _held_doctrine()
    if doctrine is None:
        return "doctrine: none issued"

    age_min = (datetime.now(timezone.utc) - doctrine.ts).total_seconds() / 60.0
    stale = " EXPIRED" if age_min > doctrine.review_horizon_min else ""
    pods = ", ".join(sorted(p.value for p in doctrine.enabled_pods)) or "none"
    return (
        f"doctrine: {doctrine.bias} conviction {doctrine.conviction}/10{stale}\n"
        f"  pods: {pods}\n"
        f"  age: {age_min:.0f}m of {doctrine.review_horizon_min}m horizon"
    )


def _uptime_line() -> str:
    seconds = time.monotonic() - _STARTED_AT
    hours, remainder = divmod(int(seconds), 3600)
    return f"uptime: {hours}h {remainder // 60}m"


def _kill_line() -> str:
    return "KILL FILE PRESENT — the kernel is refusing every order\n" if _kill_present() else ""


def build_status_text() -> str:
    return (
        "NEXUS status\n"
        f"{_kill_line()}"
        f"{_stage_line()}\n"
        f"{_positions_line()}\n"
        f"{_closed_today_line()}\n"
        f"{_doctrine_line()}\n"
        # STATE is process-local: this counts what THIS process spent, and a
        # restart zeroes it. It is a guard rail, not an accounting record.
        f"budget spent today: ${STATE.budget_spent_today:.2f} (this process only)\n"
        f"{_uptime_line()}"
    )


def build_pods_text() -> str:
    """
    Per-pod enablement and today's activity.

    HONEST LIMIT: live breaker state (consecutive losses, daily caps) lives in
    the PodSupervisor's memory inside the pod agent, and this task may not
    modify exec_/ to expose it. What is shown is what is durable: whether the
    current doctrine enables the pod, and what it actually did today. A pod
    that is enabled but silent may be breakered — check the backend log.
    """
    doctrine = _held_doctrine()
    enabled = set()
    if doctrine is not None:
        enabled = {p.value for p in doctrine.enabled_pods}

    try:
        rows = database.fetch(
            "SELECT pod, count(*), COALESCE(SUM(realized_pnl_usd),0) FROM positions "
            "WHERE pod IS NOT NULL AND (created_at AT TIME ZONE 'UTC')::date "
            "= (now() AT TIME ZONE 'UTC')::date GROUP BY pod"
        )
        today = {r[0]: (int(r[1]), float(r[2])) for r in rows}
    except Exception:
        logger.warning("telegram: pods query failed", exc_info=True)
        today = {}

    lines = ["NEXUS pods"]
    for pod in config.POD_NAMES:
        trades, pnl = today.get(pod, (0, 0.0))
        mark = "ON " if pod in enabled else "off"
        lines.append(f"  [{mark}] {pod}: {trades} trades today, ${pnl:+.2f}")

    lines.append("")
    lines.append("enablement is the current doctrine's; breaker state is in-process only.")
    if "S4_NEWSBURST" in config.POD_NAMES:
        lines.append("S4_NEWSBURST is not registered live (tick-native).")
    return "\n".join(lines)


def build_doctrine_text() -> str:
    doctrine = _held_doctrine()
    if doctrine is None:
        return "No doctrine has been issued in this process."

    pods = ", ".join(sorted(p.value for p in doctrine.enabled_pods)) or "none"
    age_min = (datetime.now(timezone.utc) - doctrine.ts).total_seconds() / 60.0
    return (
        "NEXUS doctrine\n"
        f"issued: {doctrine.ts.isoformat()} ({age_min:.0f}m ago)\n"
        f"regime: {doctrine.regime or 'unknown'}\n"
        f"bias: {doctrine.bias}\n"
        f"conviction: {doctrine.conviction}/10\n"
        f"risk multiplier: {doctrine.risk_multiplier}\n"
        f"pods enabled: {pods}\n"
        f"swing signals allowed: {doctrine.swing_signals_allowed}\n"
        f"review horizon: {doctrine.review_horizon_min}m\n"
        f"no-trade reason: {doctrine.no_trade_reason or '—'}"
    )


# --------------------------------------------------------------------------
# the levers
# --------------------------------------------------------------------------


def _kill_present() -> bool:
    try:
        return os.path.exists(config.KILL_FILE_PATH)
    except Exception:
        return False


def write_kill_file(reason: str) -> bool:
    """
    Create the KILL file that risk/kernel.py already checks.

    Atomic (temp + os.replace) so a crash mid-write cannot leave a partial
    file — though note the kernel only tests for EXISTENCE, so even a truncated
    file would stop trading. The atomicity is for the humans reading it.

    This is the ONLY thing /killswitch does. It does not call into the kernel,
    set a halt flag, or touch a position. The kernel picks the file up on its
    next permit() or watchdog tick, exactly as it would if a human had
    created it with `touch`.
    """
    path = config.KILL_FILE_PATH
    directory = os.path.dirname(os.path.abspath(path)) or "."
    payload = f"{datetime.now(timezone.utc).isoformat()} {reason}\n"
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".KILL.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        logger.critical("telegram: KILL FILE WRITTEN at %s (%s)", path, reason)
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        logger.error("telegram: could not write the KILL file", exc_info=True)
        return False


def remove_kill_file() -> bool:
    try:
        os.remove(config.KILL_FILE_PATH)
        logger.warning("telegram: KILL file removed")
        return True
    except FileNotFoundError:
        return False
    except Exception:
        logger.error("telegram: could not remove the KILL file", exc_info=True)
        return False


def _resolve_router():
    """
    The process's single router.

    backend.py runs as __main__, so a plain `import backend` builds a SECOND
    module object whose execution stack was never constructed and whose
    get_router() returns None — the exact bug the Task 21 live run surfaced.
    __main__ is checked first for that reason; the import path remains for a
    future launcher that imports backend rather than running it.
    """
    for module in (sys.modules.get("__main__"), sys.modules.get("backend")):
        getter = getattr(module, "get_router", None)
        if getter is not None:
            router = getter()
            if router is not None:
                return router
    try:
        import backend

        return backend.get_router()
    except Exception:
        logger.error("telegram: could not resolve the router", exc_info=True)
        return None


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------


def _do_killswitch(chat_id: str) -> str:
    if write_kill_file(f"telegram /killswitch from chat {chat_id}"):
        return (
            "🛑 KILL FILE WRITTEN.\n"
            f"path: {config.KILL_FILE_PATH}\n"
            "The kernel denies every order from its next check, and the watchdog "
            "flattens on its next tick.\n"
            "Use /clearkill to remove the file — note the kernel's halt itself "
            "clears only on process restart."
        )
    return "⚠️ Could not write the KILL file. Check the backend log — NEXUS may still be trading."


def _do_clearkill(chat_id: str) -> str:
    if remove_kill_file():
        return (
            "KILL file removed.\n"
            "IMPORTANT: this does not resume trading on its own. Once a breaker has "
            "halted the kernel, that halt clears ONLY on process restart — the same "
            "one-way discipline as a stage demotion. Restart the backend."
        )
    return "No KILL file was present; nothing to remove."


def _do_flatten(chat_id: str) -> str:
    router = _resolve_router()
    if router is None:
        return "⚠️ The router is not available in this process; nothing was flattened."
    try:
        result = router.flatten_all("telegram /flatten")
    except Exception:
        logger.exception("telegram: flatten_all raised")
        return "⚠️ flatten_all raised — check the backend log; positions may still be open."
    closed = (result or {}).get("closed", "?")
    ok = (result or {}).get("success")
    return f"Flatten requested: {closed} position(s) closed, bridge reported success={ok}."


_CONFIRM_ACTIONS = {
    "/killswitch": _do_killswitch,
    "/clearkill": _do_clearkill,
    "/flatten": _do_flatten,
}

_CONFIRM_PROMPT = {
    "/killswitch": (
        "⚠️ /killswitch will write the KILL file and stop ALL trading.\n"
        f"Reply {CONFIRM_WORD} within {config.COMMAND_CONFIRM_SECONDS}s to proceed."
    ),
    "/clearkill": (
        "⚠️ /clearkill will remove the KILL file.\n"
        f"Reply {CONFIRM_WORD} within {config.COMMAND_CONFIRM_SECONDS}s to proceed."
    ),
    "/flatten": (
        "⚠️ /flatten will CLOSE EVERY OPEN POSITION at market, realising any loss.\n"
        f"Reply {CONFIRM_WORD} within {config.COMMAND_CONFIRM_SECONDS}s to proceed."
    ),
}


def _expire_stale(now: float) -> Optional[Tuple[str, str, str]]:
    """Drop any lapsed pending confirmation. Returns it so it can be audited."""
    with _PENDING_LOCK:
        for chat_id, (command, args, deadline) in list(_PENDING.items()):
            if now > deadline:
                del _PENDING[chat_id]
                return chat_id, command, args
    return None


def handle_command(chat_id: str, text: str) -> bool:
    """
    Dispatch ONE command from an already-allowlisted chat. Returns True iff a
    reply was sent. Never raises.
    """
    parts = text.split()
    command = parts[0].lower() if parts else ""
    args = " ".join(parts[1:])
    now = time.monotonic()

    # Lazily expire anything stale before deciding anything else, so an old
    # pending confirmation can never be completed by a later CONFIRM.
    lapsed = _expire_stale(now)
    if lapsed is not None:
        audit(lapsed[0], lapsed[1], lapsed[2], "EXPIRED",
              f"no {CONFIRM_WORD} within {config.COMMAND_CONFIRM_SECONDS}s")
        logger.warning("telegram: %s from %s expired unconfirmed", lapsed[1], lapsed[0])

    # --- the confirmation word -------------------------------------------
    if text.strip().upper() == CONFIRM_WORD:
        with _PENDING_LOCK:
            pending = _PENDING.pop(chat_id, None)
        if pending is None:
            audit(chat_id, CONFIRM_WORD, "", "REFUSED", "no pending command for this chat")
            return _reply(chat_id, "Nothing is awaiting confirmation.")

        pending_command, pending_args, _deadline = pending
        action = _CONFIRM_ACTIONS.get(pending_command)
        if action is None:
            audit(chat_id, pending_command, pending_args, "REFUSED", "unknown pending action")
            return _reply(chat_id, "That command is no longer available.")

        try:
            detail = action(chat_id)
        except Exception as exc:
            logger.exception("telegram: %s raised", pending_command)
            audit(chat_id, pending_command, pending_args, "ERROR", str(exc)[:400])
            return _reply(chat_id, f"⚠️ {pending_command} failed — check the backend log.")

        audit(chat_id, pending_command, pending_args, "CONFIRMED", detail[:400])
        return _reply(chat_id, detail)

    # --- dangerous commands arm a confirmation ---------------------------
    if command in DANGEROUS:
        with _PENDING_LOCK:
            _PENDING[chat_id] = (command, args, now + config.COMMAND_CONFIRM_SECONDS)
        audit(chat_id, command, args, "CONFIRM_SENT")
        logger.warning("telegram: %s armed by chat %s, awaiting %s", command, chat_id, CONFIRM_WORD)
        return _reply(chat_id, _CONFIRM_PROMPT[command])

    # --- reads ------------------------------------------------------------
    readers = {
        "/status": build_status_text,
        "/pods": build_pods_text,
        "/doctrine": build_doctrine_text,
        "/help": lambda: HELP_TEXT,
        "/start": lambda: HELP_TEXT,
    }
    reader = readers.get(command)
    if reader is not None:
        try:
            body = reader()
        except Exception as exc:
            logger.exception("telegram: %s raised", command)
            audit(chat_id, command, args, "ERROR", str(exc)[:400])
            return _reply(chat_id, f"⚠️ {command} failed — check the backend log.")
        audit(chat_id, command, args, "OK")
        return _reply(chat_id, body)

    audit(chat_id, command or "(empty)", args, "UNKNOWN")
    return _reply(chat_id, HELP_TEXT)


def handle_update(update: dict) -> bool:
    """
    Process one getUpdates result.

    A message from a chat id that is not allowlisted is logged, AUDITED and
    ignored — never answered. Answering would confirm the bot exists to
    whoever is probing it.
    """
    message = (update or {}).get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    text = (message.get("text") or "").strip()

    if chat_id not in _allowlist():
        logger.warning("telegram: ignoring update from non-allowlisted chat_id=%s", chat_id)
        audit(chat_id, (text.split() or ["(empty)"])[0][:64], "", "REJECTED",
              "chat id not on the allowlist")
        return False

    if not _is_configured():
        _warn_disabled_once()
        return False

    try:
        return handle_command(chat_id, text)
    except Exception:
        logger.exception("telegram: command dispatch raised; continuing")
        return False


def run_telegram_bot() -> None:
    """
    Wire BUS alerts and (if configured) long-poll getUpdates for commands. The
    poll loop survives any error (INVARIANT 6). If unconfigured, the BUS
    subscriptions still register (their sends are harmless no-ops) but no
    network loop starts.
    """
    BUS.subscribe("signal_opened", on_signal_opened)
    BUS.subscribe("signal_closed", on_signal_closed)

    if not _is_configured():
        _warn_disabled_once()
        logger.info("telegram: not polling (disabled)")
        return

    offset: Optional[int] = None
    logger.info(
        "telegram: command deck polling every %ss (two-step confirm %ss)",
        config.TELEGRAM_POLL_SECONDS, config.COMMAND_CONFIRM_SECONDS,
    )
    while True:
        try:
            data = _api_get("getUpdates", {"timeout": 0, "offset": offset})
            for update in (data or {}).get("result", []):
                offset = update["update_id"] + 1
                handle_update(update)
        except Exception as exc:
            # exc_info=False so no traceback (which could quote the URL) is
            # emitted; the redacted exception text is the whole log payload.
            logger.error("telegram: poll loop error: %s", _redact(str(exc)), exc_info=False)
        time.sleep(config.TELEGRAM_POLL_SECONDS)
