"""
NEXUS Telegram bot — outbound alerts + a read-only /status command.

Transport: the raw Telegram HTTP API via `requests` (chosen over
python-telegram-bot to keep the dependency surface small and the tests
trivially mockable). The bot token and the chat-id allowlist are SECRETS and
come from the environment ONLY (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS).

Safety posture: this bot can only READ. There is deliberately NO /killswitch
and NO /flatten here — those are Task 22, wired through the kernel. A
/status-only bot cannot be turned into a weapon. Every outbound message goes
ONLY to allowlisted chat ids; a message from any other chat id is logged and
ignored, never answered.

If either secret is missing the bot disables itself: it logs one warning and
every send becomes a no-op returning False. NEXUS never crashes because
Telegram is unconfigured (INVARIANT 6).
"""
import logging
import time
from typing import List, Optional

import requests

import config
from config import get_stage
from core import database
from core.state import BUS, STATE

logger = logging.getLogger(__name__)

_warned_disabled = False


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
# /status
# --------------------------------------------------------------------------


def build_status_text() -> str:
    open_count = 0
    closed_today = 0
    net_r = 0.0
    try:
        open_count = database.fetch("SELECT count(*) FROM signals WHERE status='OPEN'")[0][0]
        row = database.fetch(
            "SELECT count(*), COALESCE(SUM(outcome_r), 0) FROM signals "
            "WHERE status IN ('STOPPED', 'TP2', 'BE') "
            "AND (outcome_ts AT TIME ZONE 'UTC')::date = (now() AT TIME ZONE 'UTC')::date"
        )[0]
        closed_today, net_r = int(row[0]), float(row[1])
    except Exception:
        logger.exception("telegram: /status DB query failed; reporting partial")
    return (
        "NEXUS status\n"
        f"stage: {get_stage().value}\n"
        f"open positions: {open_count}\n"
        f"closed today: {closed_today} (net {net_r:+.2f}R)\n"
        f"budget spent today: ${STATE.budget_spent_today:.2f}"
    )


def handle_update(update: dict) -> bool:
    """
    Process one getUpdates result. Replies to /status ONLY when it comes from
    an allowlisted chat id; any other chat id is logged and ignored (never
    answered). Returns True iff a reply was sent.
    """
    message = (update or {}).get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id"))
    text = (message.get("text") or "").strip()

    if chat_id not in _allowlist():
        logger.warning("telegram: ignoring update from non-allowlisted chat_id=%s", chat_id)
        return False

    if text == "/status":
        if not _is_configured():
            _warn_disabled_once()
            return False
        # Reply to the requesting (allowlisted) chat only.
        return _api_post("sendMessage", {"chat_id": chat_id, "text": build_status_text()}) is not None

    return False


def run_telegram_bot() -> None:
    """
    Wire BUS alerts and (if configured) long-poll getUpdates for /status. The
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
    logger.info("telegram: polling getUpdates every %ss", config.TELEGRAM_POLL_SECONDS)
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
