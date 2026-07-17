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
    yield


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


def test_non_status_text_is_ignored(monkeypatch):
    _configure(monkeypatch, chats="111")
    calls = _capture_post(monkeypatch)
    replied = telegram_bot.handle_update({"message": {"chat": {"id": 111}, "text": "hello"}})
    assert replied is False
    assert calls == []


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
