"""
Acceptance tests for Task 6 part 2: the Telegram bot (ops/telegram_bot.py).

NO network: requests.post/get are always monkeypatched and their payloads
inspected. Covers the disabled (unconfigured) no-op path, send formatting,
the allowlist gate on /status, and send-failure handling.
"""
import logging

import pytest

import config
from ops import telegram_bot


@pytest.fixture(autouse=True)
def _reset_bot(monkeypatch):
    # Each test starts from a known, unconfigured baseline.
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", None)
    monkeypatch.setattr(telegram_bot, "_warned_disabled", False)
    telegram_bot._PENDING.clear()
    # FIX 22.1. audit() writes to command_audit, and the pre-Task-22 tests in
    # this file predate it — they were silently filling the real audit trail
    # with chat ids 111/999. Muted by default here; the tests that care about
    # auditing re-patch it with _capture_audit and inspect the calls instead.
    monkeypatch.setattr(telegram_bot, "audit", lambda *a, **k: True)
    yield
    telegram_bot._PENDING.clear()


def _mute_audit(monkeypatch):
    """Silence the audit writer; tests that care about it capture instead."""
    monkeypatch.setattr(telegram_bot, "audit", lambda *a, **k: True)


def _capture_audit(monkeypatch):
    rows = []

    def fake_audit(chat_id, command, args, outcome, detail=""):
        rows.append(
            {"chat_id": chat_id, "command": command, "args": args,
             "outcome": outcome, "detail": detail}
        )
        return True

    monkeypatch.setattr(telegram_bot, "audit", fake_audit)
    return rows


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {"ok": True, "result": []}

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture_post(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResp()

    monkeypatch.setattr(telegram_bot.requests, "post", fake_post)
    return calls


def _configure(monkeypatch, token="TESTTOKEN", chats="111,222"):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_IDS", chats)


# --------------------------------------------------------------------------
# disabled when unconfigured
# --------------------------------------------------------------------------


def test_unconfigured_send_is_noop(monkeypatch, caplog):
    calls = _capture_post(monkeypatch)  # must never be called
    with caplog.at_level(logging.WARNING):
        result = telegram_bot.send_alert("hello")
    assert result is False
    assert calls == []
    assert "disabled" in caplog.text


def test_unconfigured_only_warns_once(monkeypatch):
    _capture_post(monkeypatch)
    assert telegram_bot.send_alert("a") is False
    assert telegram_bot.send_alert("b") is False
    assert telegram_bot._warned_disabled is True


# --------------------------------------------------------------------------
# configured send
# --------------------------------------------------------------------------


def test_send_alert_posts_to_each_allowlisted_chat(monkeypatch):
    _configure(monkeypatch)
    calls = _capture_post(monkeypatch)

    result = telegram_bot.send_alert("hello world")

    assert result is True
    assert len(calls) == 2
    assert {c["json"]["chat_id"] for c in calls} == {"111", "222"}
    for c in calls:
        assert c["json"]["text"] == "hello world"
        assert "sendMessage" in c["url"]
        assert "TESTTOKEN" in c["url"]
        assert c["timeout"] == config.TELEGRAM_TIMEOUT_SECONDS


def test_on_signal_opened_formats_payload(monkeypatch):
    _configure(monkeypatch, chats="111")
    calls = _capture_post(monkeypatch)

    telegram_bot.on_signal_opened({
        "id": 42, "direction": "LONG", "grade": "A+",
        "entry": 4100.0, "stop": 4090.0, "tp1": 4130.0, "tp2": 4160.0,
        "lots": 0.1, "thesis": "H4 uptrend pullback " * 20,  # > 200 chars
    })

    assert len(calls) == 1
    text = calls[0]["json"]["text"]
    assert "LONG" in text and "A+" in text
    assert "4100.0" in text and "4090.0" in text
    assert "0.1" in text
    # thesis truncated to 200 chars in the message
    assert "H4 uptrend pullback " * 20 not in text


def test_on_signal_closed_formats_payload(monkeypatch):
    _configure(monkeypatch, chats="111")
    calls = _capture_post(monkeypatch)

    telegram_bot.on_signal_closed({"id": 7, "status": "TP2", "outcome_r": 4.0})

    text = calls[0]["json"]["text"]
    assert "#7" in text and "TP2" in text and "+4.00R" in text


# --------------------------------------------------------------------------
# /status allowlist gate
# --------------------------------------------------------------------------


def test_status_from_non_allowlisted_chat_is_ignored(monkeypatch, caplog):
    _configure(monkeypatch, chats="111")
    calls = _capture_post(monkeypatch)

    with caplog.at_level(logging.WARNING):
        replied = telegram_bot.handle_update({"message": {"chat": {"id": 999}, "text": "/status"}})

    assert replied is False
    assert calls == []  # never answered
    assert "non-allowlisted" in caplog.text


def test_status_from_allowlisted_chat_replies(monkeypatch):
    _configure(monkeypatch, chats="111")
    calls = _capture_post(monkeypatch)

    replied = telegram_bot.handle_update({"message": {"chat": {"id": 111}, "text": "/status"}})

    assert replied is True
    assert len(calls) == 1
    assert calls[0]["json"]["chat_id"] == "111"       # reply to requester only
    assert "NEXUS status" in calls[0]["json"]["text"]
    assert "stage:" in calls[0]["json"]["text"]


def test_unknown_text_gets_the_help_reply(monkeypatch):
    """
    Task 22: an unrecognised command answers with help rather than silence.
    Silence from an ALLOWLISTED chat is indistinguishable from a dead bot, and
    the operator needs to know the deck is alive. Non-allowlisted chats are
    still ignored entirely — see the rejection test.
    """
    _configure(monkeypatch, chats="111")
    _mute_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    replied = telegram_bot.handle_update({"message": {"chat": {"id": 111}, "text": "hello"}})

    assert replied is True
    assert len(calls) == 1
    assert "command deck" in calls[0]["json"]["text"]


# --------------------------------------------------------------------------
# send failure -> logged + False
# --------------------------------------------------------------------------


def test_send_failure_logs_and_returns_false(monkeypatch, caplog):
    _configure(monkeypatch, chats="111")

    def boom(url, json=None, timeout=None):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(telegram_bot.requests, "post", boom)

    with caplog.at_level(logging.ERROR):
        result = telegram_bot.send_alert("x")

    assert result is False
    assert "sendMessage failed" in caplog.text


# --------------------------------------------------------------------------
# the token must NEVER reach a log record
# --------------------------------------------------------------------------

_SECRET_TOKEN = "123456789:AAHsecretTOKENvalueXYZ"


def _no_token_in_records(caplog):
    """The raw token must appear in no log record (message OR formatted output)."""
    for record in caplog.records:
        assert _SECRET_TOKEN not in record.getMessage()
    assert _SECRET_TOKEN not in caplog.text
    assert "**TOKEN**" in caplog.text


def test_api_post_error_redacts_token(monkeypatch, caplog):
    _configure(monkeypatch, token=_SECRET_TOKEN, chats="111")
    leaky_url = f"{config.TELEGRAM_API_BASE}/bot{_SECRET_TOKEN}/sendMessage"

    def boom(url, json=None, timeout=None):
        raise RuntimeError(f"Max retries exceeded with url: {leaky_url}")

    monkeypatch.setattr(telegram_bot.requests, "post", boom)

    with caplog.at_level(logging.ERROR):
        assert telegram_bot._api_post("sendMessage", {"chat_id": "111", "text": "x"}) is None

    _no_token_in_records(caplog)


def test_api_get_error_redacts_token(monkeypatch, caplog):
    _configure(monkeypatch, token=_SECRET_TOKEN, chats="111")
    leaky_url = f"{config.TELEGRAM_API_BASE}/bot{_SECRET_TOKEN}/getUpdates"

    def boom(url, params=None, timeout=None):
        raise RuntimeError(f"Max retries exceeded with url: {leaky_url}")

    monkeypatch.setattr(telegram_bot.requests, "get", boom)

    with caplog.at_level(logging.ERROR):
        assert telegram_bot._api_get("getUpdates", {"timeout": 0}) is None

    _no_token_in_records(caplog)


def test_poll_loop_error_redacts_token(monkeypatch, caplog):
    _configure(monkeypatch, token=_SECRET_TOKEN, chats="111")
    leaky_url = f"{config.TELEGRAM_API_BASE}/bot{_SECRET_TOKEN}/getUpdates"

    def raising_get(method, params):
        raise RuntimeError(f"HTTPSConnectionPool failure for {leaky_url}")

    class _StopLoop(Exception):
        pass

    def stop_sleep(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(telegram_bot, "_api_get", raising_get)
    monkeypatch.setattr(telegram_bot.time, "sleep", stop_sleep)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(_StopLoop):
            telegram_bot.run_telegram_bot()

    _no_token_in_records(caplog)


# ==========================================================================
# TASK 22 — the command deck
#
# The governing property: no destructive command acts on a single message,
# and every message is audited whether or not it was allowed to do anything.
# ==========================================================================


def _msg(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def _sandbox_kill(monkeypatch, tmp_path):
    """Never let a test touch the repo's real KILL file."""
    monkeypatch.setattr(config, "KILL_FILE_PATH", str(tmp_path / "KILL"))
    return tmp_path / "KILL"


# --- reads ------------------------------------------------------------------


@pytest.mark.parametrize("command", ["/status", "/pods", "/doctrine", "/help"])
def test_each_read_command_replies_and_audits_ok(monkeypatch, command):
    _configure(monkeypatch, chats="111")
    rows = _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    assert telegram_bot.handle_update(_msg(111, command)) is True
    assert len(calls) == 1, "a command replies to exactly one chat"
    assert calls[0]["json"]["chat_id"] == "111"
    assert [r["outcome"] for r in rows] == ["OK"]
    assert rows[0]["command"] == command


def test_status_reports_stage_and_the_budget_caveat(monkeypatch):
    _configure(monkeypatch, chats="111")
    text = telegram_bot.build_status_text()
    assert "stage:" in text
    assert "this process only" in text, "the budget caveat must travel with the number"
    assert "uptime:" in text


def test_status_announces_a_present_kill_file(monkeypatch, tmp_path):
    kill = _sandbox_kill(monkeypatch, tmp_path)
    assert "KILL FILE PRESENT" not in telegram_bot.build_status_text()
    kill.write_text("stop", encoding="utf-8")
    assert "KILL FILE PRESENT" in telegram_bot.build_status_text()


def test_pods_lists_every_configured_pod(monkeypatch):
    text = telegram_bot.build_pods_text()
    for pod in config.POD_NAMES:
        assert pod in text


def test_readers_survive_a_dead_database(monkeypatch):
    """A status command that can crash is useless in the moment you need it."""
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(telegram_bot.database, "fetch", boom)
    assert "unavailable" in telegram_bot.build_status_text()
    telegram_bot.build_pods_text()   # must not raise


# --- two-step: nothing happens on one message -------------------------------


@pytest.mark.parametrize("command", ["/killswitch", "/clearkill", "/flatten"])
def test_a_dangerous_command_only_asks_first(monkeypatch, tmp_path, command):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    kill.write_text("pre-existing", encoding="utf-8")
    rows = _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    assert telegram_bot.handle_update(_msg(111, command)) is True

    assert "CONFIRM" in calls[0]["json"]["text"]
    assert rows[0]["outcome"] == "CONFIRM_SENT"
    assert kill.exists(), "no destructive action may happen on the first message"
    assert kill.read_text(encoding="utf-8") == "pre-existing"


def test_killswitch_writes_the_kill_file_only_after_confirm(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    rows = _capture_audit(monkeypatch)
    _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))
    assert not kill.exists()

    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert kill.exists(), "CONFIRM must actually write the file the kernel checks"
    assert "/killswitch" in kill.read_text(encoding="utf-8")
    assert [r["outcome"] for r in rows] == ["CONFIRM_SENT", "CONFIRMED"]
    # no temp file survives the atomic write
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".KILL.")] == []


def test_clearkill_removes_the_file_after_confirm(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    kill.write_text("stop", encoding="utf-8")
    _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/clearkill"))
    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert not kill.exists()
    assert "process restart" in calls[-1]["json"]["text"], (
        "the reply must say a halted kernel does not resume on its own"
    )


def test_flatten_calls_the_router_after_confirm(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    _sandbox_kill(monkeypatch, tmp_path)
    _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    flattened = []

    class FakeRouter:
        def flatten_all(self, reason):
            flattened.append(reason)
            return {"outcome": "CLOSED", "success": True, "closed": 3}

    monkeypatch.setattr(telegram_bot, "_resolve_router", lambda: FakeRouter())

    telegram_bot.handle_update(_msg(111, "/flatten"))
    assert flattened == [], "no flatten on the first message"

    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert flattened == ["telegram /flatten"]
    assert "3 position(s) closed" in calls[-1]["json"]["text"]


def test_flatten_without_a_router_reports_honestly(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)
    monkeypatch.setattr(telegram_bot, "_resolve_router", lambda: None)

    telegram_bot.handle_update(_msg(111, "/flatten"))
    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert "nothing was flattened" in calls[-1]["json"]["text"]


# --- two-step: the confirmation is scoped and time-boxed --------------------


def test_a_confirm_from_a_different_chat_is_refused(monkeypatch, tmp_path):
    """The lever belongs to whoever pulled it, not to whoever is watching."""
    _configure(monkeypatch, chats="111,222")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    rows = _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))
    telegram_bot.handle_update(_msg(222, "CONFIRM"))

    assert not kill.exists(), "chat 222 must not be able to complete chat 111's command"
    assert "Nothing is awaiting confirmation" in calls[-1]["json"]["text"]
    assert rows[-1]["outcome"] == "REFUSED"
    assert rows[-1]["chat_id"] == "222"


def test_a_confirm_after_the_window_expires(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    rows = _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))

    # 61 seconds later.
    real_monotonic = telegram_bot.time.monotonic
    monkeypatch.setattr(
        telegram_bot.time, "monotonic",
        lambda: real_monotonic() + config.COMMAND_CONFIRM_SECONDS + 1,
    )
    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert not kill.exists(), "an expired confirmation must not fire"
    assert "Nothing is awaiting confirmation" in calls[-1]["json"]["text"]
    outcomes = [r["outcome"] for r in rows]
    assert "EXPIRED" in outcomes, "the lapse itself must be audited"
    assert outcomes[-1] == "REFUSED"


def test_a_confirm_just_inside_the_window_still_fires(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    _capture_audit(monkeypatch)
    _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))
    real_monotonic = telegram_bot.time.monotonic
    monkeypatch.setattr(
        telegram_bot.time, "monotonic",
        lambda: real_monotonic() + config.COMMAND_CONFIRM_SECONDS - 1,
    )
    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert kill.exists()


def test_a_bare_confirm_with_nothing_pending_is_refused(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    _sandbox_kill(monkeypatch, tmp_path)
    rows = _capture_audit(monkeypatch)
    _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert rows[-1]["outcome"] == "REFUSED"


def test_a_second_dangerous_command_replaces_the_first(monkeypatch, tmp_path):
    """
    One pending confirmation per chat: CONFIRM must never apply to a command
    the operator has since moved on from.
    """
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    kill.write_text("stop", encoding="utf-8")
    _capture_audit(monkeypatch)
    _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))
    telegram_bot.handle_update(_msg(111, "/clearkill"))
    telegram_bot.handle_update(_msg(111, "CONFIRM"))

    assert not kill.exists(), "CONFIRM applied to /clearkill, the most recent command"


# --- the allowlist ----------------------------------------------------------


def test_a_non_allowlisted_killswitch_is_ignored_and_audited(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    rows = _capture_audit(monkeypatch)
    calls = _capture_post(monkeypatch)

    assert telegram_bot.handle_update(_msg(999, "/killswitch")) is False

    assert calls == [], "never answer a stranger — it confirms the bot exists"
    assert not kill.exists()
    assert rows[-1]["outcome"] == "REJECTED"
    assert rows[-1]["chat_id"] == "999"
    assert rows[-1]["command"] == "/killswitch"


def test_a_stranger_cannot_confirm_an_operators_command(monkeypatch, tmp_path):
    _configure(monkeypatch, chats="111")
    kill = _sandbox_kill(monkeypatch, tmp_path)
    _capture_audit(monkeypatch)
    _capture_post(monkeypatch)

    telegram_bot.handle_update(_msg(111, "/killswitch"))
    telegram_bot.handle_update(_msg(999, "CONFIRM"))

    assert not kill.exists()


# --- unconfigured regression ------------------------------------------------


def test_an_unconfigured_bot_does_nothing_at_all(monkeypatch, tmp_path):
    """Regression: the no-op contract must survive the command rewrite."""
    kill = _sandbox_kill(monkeypatch, tmp_path)
    calls = _capture_post(monkeypatch)
    _capture_audit(monkeypatch)

    for text in ("/status", "/pods", "/killswitch", "CONFIRM", "/flatten"):
        assert telegram_bot.handle_update(_msg(111, text)) is False

    assert calls == []
    assert not kill.exists()
    assert telegram_bot.send_alert("hi") is False


# --- the deck pulls existing levers only ------------------------------------


def test_the_bot_never_touches_kernel_internals():
    """
    /killswitch writes a FILE the kernel already checks. If the bot ever
    imported the kernel to set a halt directly, there would be two safety
    systems, and they would disagree at the worst possible moment.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(telegram_bot))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert "risk.kernel" not in imported

    # CALL nodes, not raw text: the module docstring names these functions
    # precisely to explain that it does not call them.
    banned = {"emergency_flatten", "permit", "_halt"}
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name:
            called.add(name)

    assert not (called & banned), f"the deck must not call {called & banned}"
    # ...and the lever it DOES pull is the file the kernel already checks.
    assert "KILL_FILE_PATH" in inspect.getsource(telegram_bot.write_kill_file)
