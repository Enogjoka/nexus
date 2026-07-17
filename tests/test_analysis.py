"""
Acceptance tests for Task 5: the analyst signal spine (ai/analysis.py).

Hard rule: NO network, NO real API key. The single external call (Claude) is
always mocked — either call_claude() is monkeypatched wholesale, or the
_get_client() seam is replaced with a fake client. The DB-backed cycle tests
talk to nexus_dev and clean up the rows they create (by id).
"""
import json
import logging
from datetime import datetime, timezone

import pytest

import config
from ai import analysis
from ai.analysis import (
    build_prompt,
    call_claude,
    extract_signal,
    run_analysis_cycle,
)
from ai.price_resolver import SignalAnchors
from core import database
from core.state import STATE

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

# The 16 anchors that must be offered (exactly config.OFFERED_ANCHORS).
EXPECTED_ANCHORS = [
    "CURRENT_BID", "CURRENT_ASK", "CURRENT_MID",
    "H1_EMA20", "H1_EMA50", "H1_BB_UPPER", "H1_BB_LOWER", "H1_BB_MID",
    "H4_EMA20", "H4_EMA50", "H4_SWING_HIGH", "H4_SWING_LOW",
    "D1_EMA20", "D1_EMA50", "D1_SWING_HIGH", "D1_SWING_LOW",
]


@pytest.fixture(autouse=True)
def _reset_budget():
    """Isolate the shared STATE.budget_spent_today across tests."""
    saved = STATE.budget_spent_today
    STATE.budget_spent_today = 0.0
    yield
    STATE.budget_spent_today = saved


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def make_state():
    """A clean, non-stale market_data snapshot that resolves + validates."""
    return {
        "1h": {
            "price": 4100.0, "stale": False, "regime": "TREND_UP",
            "indicators": {
                "ema20": 4100.0, "ema50": 4090.0, "rsi14": 55.0, "atr14": 5.0,
                "bb_upper": 4120.0, "bb_lower": 4080.0, "bb_mid": 4100.0,
                "swing_high": 4130.0, "swing_low": 4070.0,
            },
        },
        "4h": {
            "price": 4100.0, "stale": False, "regime": "TREND_UP",
            "indicators": {
                "ema20": 4095.0, "ema50": 4085.0, "rsi14": 52.0, "atr14": 8.0,
                "bb_upper": 4160.0, "bb_lower": 4040.0, "bb_mid": 4100.0,
                "swing_high": 4150.0, "swing_low": 4050.0,
            },
        },
        "1d": {
            "price": 4100.0, "stale": False, "regime": "RANGE",
            "indicators": {
                "ema20": 4090.0, "ema50": 4080.0, "rsi14": 50.0, "atr14": 15.0,
                "bb_upper": 4300.0, "bb_lower": 3900.0, "bb_mid": 4100.0,
                "swing_high": 4200.0, "swing_low": 4000.0,
            },
        },
        "dxy": {"close": 103.5, "trend": "DOWN"},
        "session": "LONDON",
    }


def canned_reply(**overrides):
    """A valid JSON reply (LONG anchored at H1_EMA20 / H4_SWING_LOW) + metadata."""
    payload = {
        "direction": "LONG",
        "entry_anchor": "H1_EMA20",
        "entry_offset_pips": 0.0,
        "stop_anchor": "H4_SWING_LOW",
        "stop_offset_pips": 0.0,
        "tp1_rr": 1.5,
        "tp2_rr": 3.0,
        "grade": "A",
        "confidence": 80,
        "thesis": "test thesis",
    }
    payload.update(overrides)
    return json.dumps(payload)


class _FakeUsage:
    def __init__(self, i, o):
        self.input_tokens = i
        self.output_tokens = o


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text, i=100, o=50):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(i, o)


class _FakeMessages:
    def __init__(self, response=None, exc=None, recorder=None):
        self._response = response
        self._exc = exc
        self._recorder = recorder

    def create(self, **kwargs):
        if self._recorder is not None:
            self._recorder["called"] = True
            self._recorder["kwargs"] = kwargs
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeClient:
    def __init__(self, response=None, exc=None, recorder=None):
        self.messages = _FakeMessages(response, exc, recorder)


NON_FIX_UTC = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
FIX_UTC = datetime(2026, 7, 17, 10, 15, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# build_prompt
# --------------------------------------------------------------------------


def test_build_prompt_has_all_sections():
    prompt = build_prompt(make_state())
    for section in ("LIVE GOLD DATA", "KEY LEVELS", "GOLD DRIVERS", "REGIME", "OUTPUT CONTRACT"):
        assert section in prompt


def test_build_prompt_offers_exactly_the_16_anchors():
    prompt = build_prompt(make_state())
    for anchor in EXPECTED_ANCHORS:
        assert anchor in prompt, f"missing anchor {anchor}"
    assert len(EXPECTED_ANCHORS) == 16


def test_build_prompt_never_mentions_session_anchors():
    prompt = build_prompt(make_state())
    assert "SESSION_HIGH" not in prompt
    assert "SESSION_LOW" not in prompt


def test_build_prompt_survives_empty_state():
    # Must not raise on missing timeframes/indicators.
    prompt = build_prompt({})
    assert "OUTPUT CONTRACT" in prompt


# --------------------------------------------------------------------------
# extract_signal
# --------------------------------------------------------------------------


def test_extract_signal_bare_wait():
    sig, meta = extract_signal("WAIT")
    assert sig is None
    assert meta == {"wait": True}


def test_extract_signal_lowercase_wait():
    sig, meta = extract_signal("wait")
    assert sig is None
    assert meta.get("wait") is True


def test_extract_signal_fenced_wait():
    sig, meta = extract_signal("```\nWAIT\n```")
    assert sig is None
    assert meta.get("wait") is True


def test_extract_signal_valid_json_with_metadata():
    sig, meta = extract_signal(canned_reply())
    assert isinstance(sig, SignalAnchors)
    assert sig.direction == "LONG"
    assert sig.entry_anchor.value == "H1_EMA20"
    assert meta == {"grade": "A", "confidence": 80, "thesis": "test thesis"}


def test_extract_signal_strips_metadata_before_parse():
    # A raw reply carrying grade/confidence/thesis would fail the resolver's
    # extra="forbid" contract if NOT stripped first. It parses -> stripping works.
    sig, meta = extract_signal(canned_reply(grade="A+", confidence=90))
    assert isinstance(sig, SignalAnchors)
    assert meta["grade"] == "A+"


def test_extract_signal_malformed_metadata_defaults():
    sig, meta = extract_signal(canned_reply(grade="ZZZ", confidence="high", thesis=123))
    assert isinstance(sig, SignalAnchors)
    assert meta == {"grade": "B", "confidence": 0, "thesis": ""}


def test_extract_signal_malformed_json_is_rejected():
    sig, meta = extract_signal("this is not json and not wait")
    assert sig is None
    assert meta == {"grade": "B", "confidence": 0, "thesis": ""}


def test_extract_signal_raw_price_injection_rejected():
    # entry_price is not a contract key; extra="forbid" must reject it.
    reply = json.dumps({
        "direction": "LONG", "entry_anchor": "H1_EMA20", "entry_offset_pips": 0.0,
        "stop_anchor": "H4_SWING_LOW", "stop_offset_pips": 0.0,
        "tp1_rr": 1.5, "tp2_rr": 3.0, "entry_price": 4100.0,
    })
    sig, _ = extract_signal(reply)
    assert sig is None


# --------------------------------------------------------------------------
# call_claude (mocked client — never real network)
# --------------------------------------------------------------------------


def test_call_claude_success_accumulates_budget(monkeypatch):
    monkeypatch.setattr(analysis, "_get_client", lambda: _FakeClient(_FakeResponse("WAIT", 1000, 500)))
    assert STATE.budget_spent_today == 0.0
    out = call_claude("prompt")
    assert out == "WAIT"
    assert STATE.budget_spent_today > 0.0  # cost accumulated from usage


def test_call_claude_timeout_returns_none_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        analysis, "_get_client",
        lambda: _FakeClient(exc=TimeoutError("request timed out")),
    )
    with caplog.at_level(logging.ERROR):
        out = call_claude("prompt")
    assert out is None
    assert "API call failed" in caplog.text


def test_call_claude_budget_cap_skips_client(monkeypatch, caplog):
    recorder = {"called": False}
    monkeypatch.setattr(
        analysis, "_get_client",
        lambda: _FakeClient(_FakeResponse("WAIT"), recorder=recorder),
    )
    STATE.budget_spent_today = config.MAX_DAILY_COST + 1.0
    with caplog.at_level(logging.ERROR):
        out = call_claude("prompt")
    assert out is None
    assert recorder["called"] is False  # client NOT called past the cap
    assert "budget cap" in caplog.text.lower()


# --------------------------------------------------------------------------
# run_analysis_cycle — end to end
# --------------------------------------------------------------------------


@requires_db
def test_cycle_persists_signal(monkeypatch):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), NON_FIX_UTC)

    assert result["outcome"] == "SIGNAL_PERSISTED"
    signal_id = result["signal_id"]
    try:
        rows = database.fetch(
            "SELECT direction, grade, confidence, status, "
            "market_snapshot->>'execution_mode', market_snapshot->>'validator_passed' "
            "FROM signals WHERE id = %s",
            (signal_id,),
        )
        assert len(rows) == 1
        direction, grade, confidence, status, execution_mode, validator_passed = rows[0]
        assert direction == "LONG"
        assert grade == "A"                # POST-validator grade persisted
        assert float(confidence) == 80.0
        assert status == "PENDING"
        assert execution_mode == "PAPER"   # hardcoded PAPER (in JSONB)
        assert validator_passed == "true"
    finally:
        database.execute("DELETE FROM signals WHERE id = %s", (signal_id,))


@requires_db
def test_cycle_unifies_symbol_across_validator_log_and_signal(monkeypatch):
    # One cycle's validator_log rows and its signals row must carry the SAME
    # symbol label (config.YF_SYMBOL = 'GC=F'), never the validator's internal
    # 'XAUUSD' default.
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), NON_FIX_UTC)
    assert result["outcome"] == "SIGNAL_PERSISTED"
    signal_id = result["signal_id"]
    try:
        assert config.YF_SYMBOL == "GC=F"

        sig_symbol = database.fetch("SELECT symbol FROM signals WHERE id = %s", (signal_id,))[0][0]
        assert sig_symbol == "GC=F"

        # The 8 newest validator_log rows are this cycle's (rule 0 + rules 1-7).
        vlog = database.fetch("SELECT symbol FROM validator_log ORDER BY id DESC LIMIT 8")
        assert len(vlog) == 8
        assert all(row[0] == "GC=F" for row in vlog)
        assert all(row[0] != "XAUUSD" for row in vlog)
    finally:
        database.execute("DELETE FROM signals WHERE id = %s", (signal_id,))


def test_cycle_unresolvable_anchor_no_persist(monkeypatch):
    # Anchor D1_SWING_HIGH is valid, but we strip the 1d timeframe so it is
    # absent from the anchor map -> resolve() returns None.
    monkeypatch.setattr(
        analysis, "call_claude",
        lambda prompt: canned_reply(stop_anchor="D1_SWING_LOW"),
    )
    state = make_state()
    state.pop("1d")
    result = run_analysis_cycle(state, NON_FIX_UTC)
    assert result["outcome"] == "REJECTED_UNRESOLVABLE"
    assert "signal_id" not in result


def test_cycle_fix_window_validator_wait_no_persist(monkeypatch):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), FIX_UTC)  # 10:15 UTC -> fix blackout
    assert result["outcome"] == "VALIDATOR_WAIT"
    assert "signal_id" not in result
    assert result["verdict"].action == "WAIT"


def test_cycle_api_unavailable(monkeypatch):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: None)
    result = run_analysis_cycle(make_state(), NON_FIX_UTC)
    assert result["outcome"] == "API_UNAVAILABLE"
    assert "signal_id" not in result


def test_cycle_wait_no_setup(monkeypatch):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: "WAIT")
    result = run_analysis_cycle(make_state(), NON_FIX_UTC)
    assert result["outcome"] == "WAIT_NO_SETUP"


def test_cycle_persist_failed_no_exception(monkeypatch):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())

    def boom(*args, **kwargs):
        raise RuntimeError("db down at persist")

    monkeypatch.setattr(database, "get_conn", boom)
    # Must not raise; the signal is simply not considered issued.
    result = run_analysis_cycle(make_state(), NON_FIX_UTC)
    assert result["outcome"] == "PERSIST_FAILED"
    assert "signal_id" not in result
