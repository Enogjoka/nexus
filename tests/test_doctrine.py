"""
Acceptance tests for Task 15: Ring 2's output cage.

The governing property is one sentence: NO INPUT PRODUCES ANYTHING BUT A VALID
DOCTRINE. Malformed JSON, out-of-range numbers, unknown pods, extra keys, a
dead API, an expired posture — every one of them must come back FLAT, and none
of them may raise. The fuzz sweep below states that as a property rather than
a list of examples.

No network: the model seam (_get_client) is monkeypatched in every test that
would otherwise reach for it.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import config
from ai import doctrine as doc
from ai.doctrine import Doctrine, DoctrineHolder, PodEnum
from core import database
from core.state import STATE

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def valid_payload(**overrides):
    payload = {
        "regime": "YIELDS_FALLING",
        "bias": "LONG_ONLY",
        "conviction": 7,
        "risk_multiplier": 0.6,
        "enabled_pods": ["S1_FIXFADE"],
        "swing_signals_allowed": True,
        "no_trade_reason": None,
        "review_horizon_min": 30,
    }
    payload.update(overrides)
    return payload


def make_doctrine(**overrides):
    fields = dict(
        ts=NOW,
        regime="NEUTRAL",
        bias="BOTH",
        conviction=5,
        risk_multiplier=0.5,
        enabled_pods=set(),
        swing_signals_allowed=True,
        no_trade_reason=None,
        review_horizon_min=30,
    )
    fields.update(overrides)
    return Doctrine(**fields)


class FakeResponse:
    def __init__(self, text, input_tokens=100, output_tokens=50):
        self.content = [type("Block", (), {"text": text})()]
        self.usage = type("Usage", (), {"input_tokens": input_tokens, "output_tokens": output_tokens})()


class FakeClient:
    """Records calls; returns canned text or raises."""

    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return FakeResponse(self.text)


@pytest.fixture(autouse=True)
def _reset_budget():
    STATE.budget_spent_today = 0.0
    yield
    STATE.budget_spent_today = 0.0


@pytest.fixture
def no_network(monkeypatch):
    """Guarantee no test reaches a real client unless it installs its own."""

    def forbidden():
        raise AssertionError("test attempted to build a real Anthropic client")

    monkeypatch.setattr(doc, "_get_client", forbidden)


# ===========================================================================
# the contract
# ===========================================================================


def test_valid_doctrine_round_trips():
    d = doc.parse_doctrine(json.dumps(valid_payload()), NOW)
    assert d.bias == "LONG_ONLY"
    assert d.conviction == 7
    assert d.risk_multiplier == 0.6
    assert d.enabled_pods == {PodEnum.S1_FIXFADE}
    assert d.ts == NOW


def test_doctrine_is_frozen_and_forbids_extras():
    d = make_doctrine()
    with pytest.raises(ValidationError):
        d.bias = "FLAT"
    with pytest.raises(ValidationError):
        make_doctrine(entry_price=4000.0)


def test_ai_supplied_timestamp_is_overwritten():
    """The AI does not get to set its own clock."""
    payload = valid_payload()
    payload["ts"] = "1999-01-01T00:00:00+00:00"
    d = doc.parse_doctrine(json.dumps(payload), NOW)
    # A model-supplied ts is an extra key on the way in; either it is stripped
    # and we get the real doctrine, or it is rejected and we get FLAT. Both are
    # acceptable; serving a 1999 timestamp is not.
    assert d.ts == NOW


def test_pod_enum_matches_config():
    assert [p.value for p in PodEnum] == config.POD_NAMES


# ---------------------------------------------------------------------------
# the fuzz sweep — the whole point of the cage
# ---------------------------------------------------------------------------


FUZZ_CASES = [
    ("conviction above range", json.dumps(valid_payload(conviction=11))),
    ("conviction negative", json.dumps(valid_payload(conviction=-1))),
    ("conviction as string", json.dumps(valid_payload(conviction="7"))),
    ("risk_multiplier 1.5", json.dumps(valid_payload(risk_multiplier=1.5))),
    ("risk_multiplier negative", json.dumps(valid_payload(risk_multiplier=-0.1))),
    ("unknown pod", json.dumps(valid_payload(enabled_pods=["S9_MOONSHOT"]))),
    ("pods not a list", json.dumps(valid_payload(enabled_pods="S1_FIXFADE"))),
    ("unknown bias", json.dumps(valid_payload(bias="MAYBE"))),
    ("missing bias", json.dumps({k: v for k, v in valid_payload().items() if k != "bias"})),
    ("extra key", json.dumps(valid_payload(entry_price=4000.0))),
    ("order fields smuggled", json.dumps(valid_payload(lots=0.5, stop_price=3990.0))),
    ("review horizon too short", json.dumps(valid_payload(review_horizon_min=5))),
    ("review horizon too long", json.dumps(valid_payload(review_horizon_min=999))),
    ("FLAT with pods enabled", json.dumps(valid_payload(bias="FLAT", enabled_pods=["S1_FIXFADE"], no_trade_reason="x", swing_signals_allowed=False))),
    ("FLAT allowing swing", json.dumps(valid_payload(bias="FLAT", enabled_pods=[], no_trade_reason="x", swing_signals_allowed=True))),
    ("FLAT without reason", json.dumps(valid_payload(bias="FLAT", enabled_pods=[], no_trade_reason=None, swing_signals_allowed=False))),
    ("not json", "I think we should go long, honestly"),
    ("truncated json", '{"bias": "LONG_ONLY", "conviction":'),
    ("json array", "[1, 2, 3]"),
    ("json scalar", "42"),
    ("empty string", ""),
    ("whitespace only", "   \n  "),
    ("null", "null"),
    ("prose around json", 'Sure! Here you go:\n{"bias":"LONG_ONLY"}\nHope that helps.'),
    ("swing not boolean", json.dumps(valid_payload(swing_signals_allowed="yes"))),
]


@pytest.mark.parametrize("label,raw", FUZZ_CASES, ids=[c[0] for c in FUZZ_CASES])
def test_every_malformed_response_degrades_to_flat(label, raw):
    d = doc.parse_doctrine(raw, NOW)

    assert isinstance(d, Doctrine)
    assert d.bias == "FLAT", f"{label} should have degraded to FLAT"
    assert d.conviction == 0
    assert d.risk_multiplier == 0.0
    assert d.enabled_pods == set()
    assert d.swing_signals_allowed is False
    assert d.no_trade_reason
    assert d.ts == NOW


def test_parse_doctrine_never_raises_on_hostile_input():
    for hostile in [None, 123, b"bytes", {"already": "parsed"}, [], "\x00\x01"]:
        result = doc.parse_doctrine(hostile, NOW)
        assert result.bias == "FLAT"


def test_fenced_json_is_accepted():
    fenced = "```json\n" + json.dumps(valid_payload()) + "\n```"
    assert doc.parse_doctrine(fenced, NOW).bias == "LONG_ONLY"


# ---------------------------------------------------------------------------
# FLAT consistency, both directions
# ---------------------------------------------------------------------------


def test_flat_with_everything_disarmed_is_valid():
    d = make_doctrine(
        bias="FLAT",
        conviction=0,
        risk_multiplier=0.0,
        enabled_pods=set(),
        swing_signals_allowed=False,
        no_trade_reason="macro stress",
    )
    assert d.bias == "FLAT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled_pods": {PodEnum.S1_FIXFADE}},
        {"swing_signals_allowed": True},
        {"no_trade_reason": None},
        {"no_trade_reason": ""},
    ],
)
def test_flat_that_leaves_something_enabled_is_rejected(overrides):
    fields = dict(
        bias="FLAT",
        conviction=0,
        risk_multiplier=0.0,
        enabled_pods=set(),
        swing_signals_allowed=False,
        no_trade_reason="reason",
    )
    fields.update(overrides)
    with pytest.raises(ValidationError):
        make_doctrine(**fields)


def test_non_flat_bias_needs_no_reason():
    assert make_doctrine(bias="BOTH", no_trade_reason=None).bias == "BOTH"


def test_flat_fallback_is_fully_disarmed():
    d = doc.FLAT_FALLBACK("because", doc.SOURCE_EXPIRY_FALLBACK, NOW)
    assert (d.bias, d.conviction, d.risk_multiplier) == ("FLAT", 0, 0.0)
    assert d.enabled_pods == set()
    assert d.swing_signals_allowed is False
    assert d.no_trade_reason == "because"
    assert d.review_horizon_min == 15


# ===========================================================================
# prompt
# ===========================================================================


def test_prompt_includes_pod_stats_when_given():
    stats = {"S1_FIXFADE": {"trades": 12, "win_rate": 0.58}}
    prompt = doc.build_doctrine_prompt({"rsi_h1": 55}, stats)
    assert "S1_FIXFADE" in prompt
    assert "0.58" in prompt
    assert "no pod history" not in prompt.split("## AVAILABLE PODS")[0].split("## POD PERFORMANCE")[1]


def test_prompt_says_no_pod_history_when_absent():
    prompt = doc.build_doctrine_prompt({"rsi_h1": 55}, None)
    assert "no pod history" in prompt


def test_prompt_never_carries_raw_prices():
    """INVARIANT 2: the head of desk reasons about posture, not levels."""
    state = {
        "price": 4123.45,
        "session_high": 4150.75,
        "session_low": 4099.25,
        "rsi_h1": 55.0,
        "real_yield": 2.44,
    }
    prompt = doc.build_doctrine_prompt(state, None)

    for banned_value in ("4123.45", "4150.75", "4099.25"):
        assert banned_value not in prompt
    for banned_dim in ("price", "session_high", "session_low"):
        assert f"{banned_dim}:" not in prompt
    # the non-price dims still made it through
    assert "rsi_h1" in prompt and "real_yield" in prompt


def test_prompt_never_offers_order_fields():
    prompt = doc.build_doctrine_prompt({"rsi_h1": 55}, None).lower()
    for banned in ("entry_price", "stop_price", "take_profit", "tp1", "tp2", "lots"):
        assert banned not in prompt


def test_prompt_states_the_risk_multiplier_can_only_reduce():
    prompt = doc.build_doctrine_prompt({}, None)
    assert "only REDUCE risk" in prompt
    assert "ceiling" in prompt


def test_prompt_carries_both_examples_and_the_discard_rule():
    prompt = doc.build_doctrine_prompt({}, None)
    assert '"bias": "LONG_ONLY"' in prompt
    assert '"bias": "FLAT"' in prompt
    assert "discarded" in prompt


def test_prompt_survives_empty_state():
    assert "no sensor data available" in doc.build_doctrine_prompt({}, None)


# ===========================================================================
# issuing
# ===========================================================================


def test_issue_doctrine_persists_a_valid_doctrine(monkeypatch):
    client = FakeClient(text=json.dumps(valid_payload()))
    monkeypatch.setattr(doc, "_get_client", lambda: client)

    d = doc.issue_doctrine({"rsi_h1": 55}, NOW)

    assert d.bias == "LONG_ONLY"
    rows = database.fetch(
        "SELECT bias, source, enabled_pods, risk_multiplier FROM doctrines ORDER BY id DESC LIMIT 1"
    )
    bias, source, pods, risk = rows[0]
    assert bias == "LONG_ONLY"
    assert source == doc.SOURCE_FABLE
    assert pods == ["S1_FIXFADE"], "enabled_pods must round-trip through TEXT[]"
    assert float(risk) == 0.6


def test_enabled_pods_round_trip_multiple_values(monkeypatch):
    payload = valid_payload(enabled_pods=["S3_BASIS", "S1_FIXFADE"])
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=json.dumps(payload)))

    doc.issue_doctrine({}, NOW)

    pods = database.fetch("SELECT enabled_pods FROM doctrines ORDER BY id DESC LIMIT 1")[0][0]
    assert sorted(pods) == ["S1_FIXFADE", "S3_BASIS"]


def test_issue_doctrine_api_failure_persists_flat_parse_fallback(monkeypatch):
    monkeypatch.setattr(
        doc, "_get_client", lambda: FakeClient(error=RuntimeError("API is down"))
    )

    d = doc.issue_doctrine({"rsi_h1": 55}, NOW)

    assert d.bias == "FLAT"
    assert d.risk_multiplier == 0.0
    bias, source = database.fetch(
        "SELECT bias, source FROM doctrines ORDER BY id DESC LIMIT 1"
    )[0]
    assert (bias, source) == ("FLAT", doc.SOURCE_PARSE_FALLBACK)


def test_issue_doctrine_malformed_response_persists_raw_text(monkeypatch):
    garbage = "I reckon we go long, mate"
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=garbage))

    d = doc.issue_doctrine({}, NOW)

    assert d.bias == "FLAT"
    bias, source, raw = database.fetch(
        "SELECT bias, source, raw_response FROM doctrines ORDER BY id DESC LIMIT 1"
    )[0]
    assert (bias, source) == ("FLAT", doc.SOURCE_PARSE_FALLBACK)
    assert raw == garbage, "the raw text is the only evidence of why we fell back"


def test_deliberate_flat_from_the_model_is_recorded_as_fable(monkeypatch):
    """A model that stands the desk down is not a parse failure."""
    payload = valid_payload(
        bias="FLAT",
        conviction=0,
        risk_multiplier=0.0,
        enabled_pods=[],
        swing_signals_allowed=False,
        no_trade_reason="macro stress, no edge",
    )
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=json.dumps(payload)))

    doc.issue_doctrine({}, NOW)

    bias, source = database.fetch(
        "SELECT bias, source FROM doctrines ORDER BY id DESC LIMIT 1"
    )[0]
    assert (bias, source) == ("FLAT", doc.SOURCE_FABLE)


def test_issue_doctrine_accumulates_cost(monkeypatch):
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=json.dumps(valid_payload())))
    STATE.budget_spent_today = 0.0

    doc.issue_doctrine({}, NOW)

    assert STATE.budget_spent_today > 0.0


def test_budget_cap_blocks_the_call_and_yields_flat(monkeypatch, no_network):
    STATE.budget_spent_today = config.MAX_DAILY_COST + 1.0

    d = doc.issue_doctrine({}, NOW)

    assert d.bias == "FLAT"  # no_network fixture proves no client was built


def test_issue_doctrine_links_a_state_vector(monkeypatch):
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=json.dumps(valid_payload())))
    database.execute("INSERT INTO state_vectors (ts) VALUES (%s) ON CONFLICT DO NOTHING", (NOW,))

    doc.issue_doctrine({}, NOW)

    linked = database.fetch(
        "SELECT state_vector_id FROM doctrines ORDER BY id DESC LIMIT 1"
    )[0][0]
    expected = database.fetch("SELECT id FROM state_vectors WHERE ts = %s", (NOW,))
    if expected:
        assert linked == expected[0][0]


def test_persist_survives_a_dead_database(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="ai.doctrine")

    def unavailable(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(database, "get_conn", unavailable)
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=json.dumps(valid_payload())))

    d = doc.issue_doctrine({}, NOW)

    assert d.bias == "LONG_ONLY", "a dead database must not change the posture"
    assert any("persist failed" in r.getMessage() for r in caplog.records)


# ===========================================================================
# holder / expiry
# ===========================================================================


def test_fresh_doctrine_passes_through():
    holder = DoctrineHolder(persist=False)
    held = make_doctrine(review_horizon_min=30)
    holder.set(held)

    assert holder.current(NOW + timedelta(minutes=29)) is held


def test_doctrine_at_exactly_its_horizon_is_still_valid():
    holder = DoctrineHolder(persist=False)
    holder.set(make_doctrine(review_horizon_min=30))
    assert holder.current(NOW + timedelta(minutes=30)).bias == "BOTH"


def test_expired_doctrine_degrades_to_flat(caplog):
    caplog.set_level(logging.INFO, logger="ai.doctrine")
    holder = DoctrineHolder(persist=False)
    holder.set(make_doctrine(review_horizon_min=30))

    result = holder.current(NOW + timedelta(minutes=31))

    assert result.bias == "FLAT"
    assert result.risk_multiplier == 0.0
    assert "expired" in result.no_trade_reason
    assert any("DOCTRINE_EXPIRED" in r.getMessage() for r in caplog.records)


def test_expiry_is_logged_and_persisted_exactly_once(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="ai.doctrine")
    persisted = []
    monkeypatch.setattr(
        doc, "persist_doctrine", lambda d, s, raw_response=None: persisted.append(s)
    )

    holder = DoctrineHolder()
    holder.set(make_doctrine(review_horizon_min=30))

    for _ in range(5):
        assert holder.current(NOW + timedelta(minutes=45)).bias == "FLAT"

    assert persisted == [doc.SOURCE_EXPIRY_FALLBACK], "must persist once, not per call"
    expired_logs = [r for r in caplog.records if "DOCTRINE_EXPIRED" in r.getMessage()]
    assert len(expired_logs) == 1


def test_setting_a_new_doctrine_rearms_the_expiry_announcement(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        doc, "persist_doctrine", lambda d, s, raw_response=None: persisted.append(s)
    )
    holder = DoctrineHolder()

    holder.set(make_doctrine(review_horizon_min=30))
    holder.current(NOW + timedelta(minutes=45))
    holder.set(make_doctrine(ts=NOW + timedelta(minutes=50), review_horizon_min=30))
    holder.current(NOW + timedelta(minutes=200))

    assert len(persisted) == 2


def test_holder_with_nothing_ever_issued_is_flat(monkeypatch):
    monkeypatch.setattr(doc, "persist_doctrine", lambda *a, **k: None)
    result = DoctrineHolder().current(NOW)
    assert result.bias == "FLAT"
    assert "no doctrine" in result.no_trade_reason


# ===========================================================================
# triage
# ===========================================================================


@pytest.mark.parametrize(
    "reply,expected",
    [("0.2", 0.2), ("0.85", 0.85), ("1", 1.0), ("0", 0.0), ("  0.5  ", 0.5), ("0.7\n", 0.7)],
)
def test_triage_parses_a_number(monkeypatch, reply, expected):
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=reply))
    assert doc.triage({"rsi_h1": 55}, NOW) == expected


@pytest.mark.parametrize(
    "reply", ["not a number", "", "high", "1.5", "-0.2", "NaN", "maybe 0.3"]
)
def test_triage_fails_toward_waking_the_desk(monkeypatch, reply):
    """
    An unreadable or out-of-range triage must escalate, never silence. Note
    "1.5" and "-0.2": a score outside 0-1 is not clamped into range, because a
    model that ignored the bounds may have ignored the question too.
    """
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text=reply))
    assert doc.triage({"rsi_h1": 55}, NOW) == 1.0


def test_triage_takes_the_leading_number_of_a_chatty_reply(monkeypatch):
    """
    Deliberate, and the one repair triage performs: a leading in-range number
    followed by prose is the model answering and then editorialising. Unlike a
    doctrine, a triage score only decides whether to spend money, so reading it
    is safe where guessing at a malformed doctrine would not be.
    """
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text="0.5 but maybe more"))
    assert doc.triage({}, NOW) == 0.5


def test_triage_api_failure_escalates(monkeypatch):
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(error=RuntimeError("down")))
    assert doc.triage({}, NOW) == 1.0


def test_triage_accumulates_cost(monkeypatch):
    monkeypatch.setattr(doc, "_get_client", lambda: FakeClient(text="0.3"))
    STATE.budget_spent_today = 0.0
    doc.triage({"rsi_h1": 55}, NOW)
    assert STATE.budget_spent_today > 0.0


def test_triage_uses_the_cheap_model(monkeypatch):
    client = FakeClient(text="0.1")
    monkeypatch.setattr(doc, "_get_client", lambda: client)
    doc.triage({}, NOW)
    assert client.calls[0]["model"] == config.TRIAGE_MODEL
    assert client.calls[0]["max_tokens"] == config.TRIAGE_MAX_TOKENS


def test_triage_prompt_carries_no_prices():
    prompt = doc.build_triage_prompt({"price": 4123.45, "rsi_h1": 55})
    assert "4123.45" not in prompt
    assert "rsi_h1" in prompt


# ===========================================================================
# cadence + delta
# ===========================================================================


@pytest.mark.parametrize("session", ["LONDON", "OVERLAP", "NY"])
def test_active_sessions_use_the_fast_cadence(session):
    assert doc.cadence_minutes({"session_label": session}) == config.DOCTRINE_CADENCE_ACTIVE_MIN


@pytest.mark.parametrize("session", ["ASIA", "OFF", None, "", "nonsense"])
def test_quiet_sessions_use_the_slow_cadence(session):
    assert doc.cadence_minutes({"session_label": session}) == config.DOCTRINE_CADENCE_QUIET_MIN


def test_state_delta_reports_only_changes():
    previous = {"rsi_h1": 50, "real_yield": 2.4}
    current = {"rsi_h1": 70, "real_yield": 2.4}
    assert doc.state_delta(current, previous) == {"rsi_h1": 70}


def test_state_delta_without_history_is_everything():
    assert doc.state_delta({"a": 1}, None) == {"a": 1}


# ===========================================================================
# isolation (the forbidden imports)
# ===========================================================================


def test_doctrine_imports_nothing_from_analysis_or_risk():
    import ast
    from pathlib import Path

    source = Path(doc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.append(node.module)

    for module in imported:
        assert module != "ai.analysis", "must not import ai.analysis"
        assert not module.startswith("risk"), f"must not import {module}"
