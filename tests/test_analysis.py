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
from ai import doctrine as doctrine_mod
from ai.doctrine import Doctrine, DoctrineHolder
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

# Task A1: the doctrine gates every cycle. This doctrine is valid from its ts
# until ts + 120 min and current() does not reject a ts after now_utc, so one
# doctrine at 08:30 covers both NON_FIX_UTC (08:00) and FIX_UTC (10:15).
DOCTRINE_TS = datetime(2026, 7, 17, 8, 30, tzinfo=timezone.utc)


def make_holder(**fields):
    """An injected, non-persisting DoctrineHolder holding one doctrine."""
    doctrine = dict(
        ts=DOCTRINE_TS, bias="BOTH", conviction=5, risk_multiplier=1.0,
        swing_signals_allowed=True, review_horizon_min=120,
    )
    doctrine.update(fields)
    holder = DoctrineHolder(persist=False)
    holder.set(Doctrine(**doctrine))
    return holder


@pytest.fixture
def permissive_holder():
    """BOTH / swing allowed / risk 1.0. Opt-in per test: never autouse, so a
    test that forgets it is blocked by the FLAT-by-default global HOLDER."""
    return make_holder()


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


def test_build_prompt_offers_session_anchors():
    # Task 10 lit these up: data/gold_agent.py now tracks session high/low into
    # AppState, so the resolver can fill them and the prompt may offer them.
    # (Through Tasks 5-9 this test asserted the opposite, for the same reason.)
    prompt = build_prompt(make_state())
    assert "SESSION_HIGH" in prompt
    assert "SESSION_LOW" in prompt


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
def test_cycle_persists_signal(monkeypatch, permissive_holder):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), NON_FIX_UTC, holder=permissive_holder)

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
def test_cycle_unifies_symbol_across_validator_log_and_signal(monkeypatch, permissive_holder):
    # One cycle's validator_log rows and its signals row must carry the SAME
    # symbol label (config.YF_SYMBOL = 'GC=F'), never the validator's internal
    # 'XAUUSD' default.
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), NON_FIX_UTC, holder=permissive_holder)
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


def test_cycle_unresolvable_anchor_no_persist(monkeypatch, permissive_holder):
    # Anchor D1_SWING_HIGH is valid, but we strip the 1d timeframe so it is
    # absent from the anchor map -> resolve() returns None.
    monkeypatch.setattr(
        analysis, "call_claude",
        lambda prompt: canned_reply(stop_anchor="D1_SWING_LOW"),
    )
    state = make_state()
    state.pop("1d")
    result = run_analysis_cycle(state, NON_FIX_UTC, holder=permissive_holder)
    assert result["outcome"] == "REJECTED_UNRESOLVABLE"
    assert "signal_id" not in result


def test_cycle_fix_window_validator_wait_no_persist(monkeypatch, permissive_holder):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    result = run_analysis_cycle(make_state(), FIX_UTC, holder=permissive_holder)  # 10:15 UTC -> fix blackout
    assert result["outcome"] == "VALIDATOR_WAIT"
    assert "signal_id" not in result
    assert result["verdict"].action == "WAIT"


def test_cycle_api_unavailable(monkeypatch, permissive_holder):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: None)
    result = run_analysis_cycle(make_state(), NON_FIX_UTC, holder=permissive_holder)
    assert result["outcome"] == "API_UNAVAILABLE"
    assert "signal_id" not in result


def test_cycle_wait_no_setup(monkeypatch, permissive_holder):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: "WAIT")
    result = run_analysis_cycle(make_state(), NON_FIX_UTC, holder=permissive_holder)
    assert result["outcome"] == "WAIT_NO_SETUP"


def test_cycle_persist_failed_no_exception(monkeypatch, permissive_holder):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())

    def boom(*args, **kwargs):
        raise RuntimeError("db down at persist")

    monkeypatch.setattr(database, "get_conn", boom)
    # Must not raise; the signal is simply not considered issued.
    result = run_analysis_cycle(make_state(), NON_FIX_UTC, holder=permissive_holder)
    assert result["outcome"] == "PERSIST_FAILED"
    assert "signal_id" not in result


# --------------------------------------------------------------------------
# Task A1 — the doctrine gate (F-01, F-19) and API-failure alerts (F-36)
# --------------------------------------------------------------------------

# A dedicated, microsecond-unique cycle time (a non-fix hour) so the rows these
# tests create in nexus_dev can be removed by exact ts and nothing else.
A1_UTC = datetime(2026, 7, 17, 8, 7, 11, 424242, tzinfo=timezone.utc)

FLAT_FIELDS = dict(
    bias="FLAT", conviction=0, risk_multiplier=0.0,
    swing_signals_allowed=False, no_trade_reason="test: stand down",
)


@pytest.fixture
def a1_rows():
    """Remove every validator_log / signals row written at A1_UTC."""
    yield
    if not config.DATABASE_URL:
        return
    database.execute("DELETE FROM validator_log WHERE ts = %s", (A1_UTC,))
    database.execute("DELETE FROM signals WHERE ts = %s", (A1_UTC,))


@pytest.fixture
def claude_calls(monkeypatch):
    """Replace call_claude with a counter that returns a valid LONG reply."""
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return canned_reply()

    monkeypatch.setattr(analysis, "call_claude", fake)
    return calls


def _gate_rows(ts):
    return database.fetch(
        "SELECT ts, symbol, rule_name, rule_result, details FROM validator_log "
        "WHERE rule_name = 'DOCTRINE_GATE' AND ts = %s",
        (ts,),
    )


def test_gate_flat_doctrine_blocks_before_claude(claude_calls, a1_rows):
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(**FLAT_FIELDS))
    assert result == {"outcome": "DOCTRINE_BLOCKED", "reason": "bias FLAT"}
    assert len(claude_calls) == 0


def test_gate_empty_holder_blocks_before_claude(claude_calls, a1_rows):
    # Never issued / expired: DoctrineHolder.current() degrades to FLAT.
    result = run_analysis_cycle(make_state(), A1_UTC, holder=DoctrineHolder(persist=False))
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    assert len(claude_calls) == 0


def test_gate_expired_doctrine_blocks(claude_calls, a1_rows):
    holder = make_holder(ts=datetime(2026, 7, 17, 4, 0, tzinfo=timezone.utc), review_horizon_min=15)
    result = run_analysis_cycle(make_state(), A1_UTC, holder=holder)
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    assert len(claude_calls) == 0


def test_gate_swing_not_allowed_blocks(claude_calls, a1_rows):
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(swing_signals_allowed=False))
    assert result == {"outcome": "DOCTRINE_BLOCKED", "reason": "swing_signals_allowed False"}
    assert len(claude_calls) == 0


def test_gate_zero_risk_multiplier_blocks(claude_calls, a1_rows):
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(risk_multiplier=0.0))
    assert result == {"outcome": "DOCTRINE_BLOCKED", "reason": "risk_multiplier <= 0"}
    assert len(claude_calls) == 0


def test_gate_unreadable_doctrine_blocks(claude_calls, a1_rows):
    class _BrokenHolder:
        def current(self, now_utc):
            raise RuntimeError("holder exploded")

    result = run_analysis_cycle(make_state(), A1_UTC, holder=_BrokenHolder())
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    assert len(claude_calls) == 0


def test_gate_default_holder_is_closed_in_tests(claude_calls, monkeypatch, a1_rows):
    # No holder argument -> ai.doctrine.HOLDER, which nothing sets under
    # pytest: a test that forgets to inject a doctrine is blocked.
    assert doctrine_mod.HOLDER.get_held() is None
    # Keep the global holder's first-call EXPIRY_FALLBACK out of nexus_dev.
    monkeypatch.setattr(doctrine_mod, "persist_doctrine", lambda *a, **k: None)
    result = run_analysis_cycle(make_state(), A1_UTC)
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    assert len(claude_calls) == 0


def test_gate_db_failure_still_blocks(claude_calls, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db down at audit")

    monkeypatch.setattr(database, "get_conn", boom)
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(**FLAT_FIELDS))
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    assert len(claude_calls) == 0


@requires_db
def test_gate_long_only_blocks_short_reply_no_persist(monkeypatch, a1_rows):
    monkeypatch.setattr(
        analysis, "call_claude",
        lambda prompt: canned_reply(direction="SHORT", stop_anchor="H4_SWING_HIGH"),
    )
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(bias="LONG_ONLY"))
    assert result == {"outcome": "DOCTRINE_BLOCKED", "reason": "direction SHORT against bias LONG_ONLY"}
    assert database.fetch("SELECT id FROM signals WHERE ts = %s", (A1_UTC,)) == []
    assert len(_gate_rows(A1_UTC)) == 1


@requires_db
def test_gate_risk_multiplier_scales_position_size(monkeypatch, a1_rows):
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply())
    seen = {}

    def fake_size(account_size, risk_pct, risk_per_unit):
        seen["risk_pct"] = risk_pct
        return 0.0  # stop at SIZED_ZERO: nothing is persisted

    monkeypatch.setattr(analysis, "position_size", fake_size)
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(risk_multiplier=0.25))
    assert result["outcome"] == "SIZED_ZERO"
    assert seen["risk_pct"] == pytest.approx(config.MAX_RISK_PCT * 0.25)
    assert seen["risk_pct"] == pytest.approx(0.25)


@requires_db
def test_gate_doctrine_fields_persist_in_snapshot(monkeypatch, a1_rows):
    # Stop at H1_EMA50 (10 below entry): at 0.5% risk the sizer still clears
    # MIN_LOT, where the default H4 swing-low stop would floor to zero.
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: canned_reply(stop_anchor="H1_EMA50"))
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(risk_multiplier=0.5))
    assert result["outcome"] == "SIGNAL_PERSISTED"
    row = database.fetch(
        "SELECT market_snapshot->>'doctrine_ts', market_snapshot->>'doctrine_bias', "
        "market_snapshot->>'risk_multiplier' FROM signals WHERE id = %s",
        (result["signal_id"],),
    )[0]
    assert row == (DOCTRINE_TS.isoformat(), "BOTH", "0.5")


@requires_db
def test_gate_row_lands_with_cycle_ts(claude_calls, a1_rows):
    result = run_analysis_cycle(make_state(), A1_UTC, holder=make_holder(swing_signals_allowed=False))
    assert result["outcome"] == "DOCTRINE_BLOCKED"
    rows = _gate_rows(A1_UTC)
    assert len(rows) == 1
    ts, symbol, rule_name, rule_result, details = rows[0]
    assert ts == A1_UTC  # same ts as the cycle: the correlation key holds
    assert symbol == config.YF_SYMBOL
    assert (rule_name, rule_result) == ("DOCTRINE_GATE", "WAIT")
    assert details == {
        "bias": "BOTH",
        "swing_signals_allowed": False,
        "risk_multiplier": 1.0,
        "doctrine_ts": DOCTRINE_TS.isoformat(),
        "reason": "swing_signals_allowed False",
    }


class _StatusError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    "exc, category",
    [
        (_StatusError("Error code: 400 - Your credit balance is too low", 400), "credit"),
        (_StatusError("invalid x-api-key", 401), "auth"),
        (_StatusError("permission denied", 403), "auth"),
        (_StatusError("rate limited", 429), "rate"),
        (_StatusError("overloaded", 529), "other"),
        (TimeoutError("request timed out"), "other"),
    ],
)
def test_classify_api_error(exc, category):
    assert analysis._classify_api_error(exc) == category


def test_credit_failures_alert_once_inside_cooldown(monkeypatch, caplog):
    monkeypatch.setattr(analysis, "_last_alert_at", {})
    monkeypatch.setattr(STATE, "last_model_success_at", None, raising=False)
    alerts = []
    monkeypatch.setattr(analysis, "send_alert", lambda text: alerts.append(text) or True)
    monkeypatch.setattr(
        analysis, "_get_client",
        lambda: _FakeClient(exc=_StatusError(
            "Error code: 400 - Your credit balance is too low to access the Anthropic API", 400,
        )),
    )
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            assert call_claude("prompt") is None
    assert len(alerts) == 1
    assert "[credit]" in alerts[0]
    assert "category=credit" in caplog.text
    assert STATE.last_model_success_at is None  # failures never stamp success

    fixed_now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(analysis, "_utcnow", lambda: fixed_now)
    monkeypatch.setattr(analysis, "_get_client", lambda: _FakeClient(_FakeResponse("WAIT")))
    assert call_claude("prompt") == "WAIT"
    assert STATE.last_model_success_at == fixed_now


def test_other_failures_never_alert(monkeypatch):
    monkeypatch.setattr(analysis, "_last_alert_at", {})
    alerts = []
    monkeypatch.setattr(analysis, "send_alert", lambda text: alerts.append(text) or True)
    monkeypatch.setattr(analysis, "_get_client", lambda: _FakeClient(exc=TimeoutError("timed out")))
    assert call_claude("prompt") is None
    assert alerts == []


def test_alert_failure_never_raises(monkeypatch):
    monkeypatch.setattr(analysis, "_last_alert_at", {})

    def boom(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(analysis, "send_alert", boom)
    monkeypatch.setattr(analysis, "_get_client", lambda: _FakeClient(exc=_StatusError("slow down", 429)))
    assert call_claude("prompt") is None
