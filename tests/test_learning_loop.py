"""
Acceptance tests for Task 12: the learning loop (fusion/learning_loop.py) plus
the two deferred wirings it closes in ai/analysis.py.

NO network. Reports are written to a tmp_path, never the repo's reports/.
DB-backed tests use FAR-FUTURE (year 2099) timestamps so they sort ahead of
and never collide with real rows; the autouse fixture purges them.

ISOLATION (task 16.1). A future timestamp separates our rows from real ones,
but it does not separate our POPULATION from theirs: _step_rank_dims and
_load_outcome_pairs aggregate over the whole signals x state_vectors join with
no time filter, which is correct production behaviour and is not changing.
Once nexus_dev accumulated real rows from the Task 10-16 acceptance runs, the
seeded arithmetic stopped being the only arithmetic — 21 ambient signals
qualified, past LEARN_MIN_SAMPLES, so a "constant" dim was no longer constant
and a deliberately-too-small sample was no longer too small.

Tests that pin the loop's maths therefore run inside scoped_conn(), which owns
the whole table for the duration of one test by deleting the ambient rows in a
transaction that is never committed. The seeded values and every expected
number below are unchanged; only the population they are computed over is.
"""
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import config
from ai import analysis
from ai.price_resolver import ResolvedSignal, SignalAnchors
from core import database
from core.state import STATE
from fusion import learning_loop as loop
from risk.validator import validate

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")

_FUTURE = datetime(2099, 6, 1, 2, 0, tzinfo=timezone.utc)
_CUTOFF = datetime(2099, 1, 1, tzinfo=timezone.utc)

# Every row this file seeds carries this symbol prefix; everything else in the
# database is ambient and must be invisible to a population-sensitive test.
_MARKER = "TST_LL"


@contextmanager
def scoped_conn():
    """
    A connection on which the learning loop sees ONLY this test's seeded rows,
    and whose work is ALWAYS rolled back.

    Ambient signals are deleted rather than merely unlinked: _step_link
    back-links every signal whose state_vector_id IS NULL, so detaching them
    would simply re-attach them one step later. Nothing is committed, so the
    real rows are untouched the moment the block exits — including on failure.

    Reads that need to see the loop's writes (dim_rankings) must go through
    fetch_on() on this same connection; a second connection would sit outside
    the transaction and see nothing.
    """
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM signals WHERE symbol IS NULL OR symbol NOT LIKE %s", (f"{_MARKER}%",))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def fetch_on(conn, sql, params=None):
    """Read inside scoped_conn's uncommitted transaction."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    if not config.DATABASE_URL:
        return
    database.execute("DELETE FROM dim_rankings WHERE computed_at >= %s", (_CUTOFF,))
    database.execute("DELETE FROM signals WHERE symbol LIKE 'TST_LL%'")
    database.execute("DELETE FROM state_vectors WHERE ts >= %s", (_CUTOFF,))
    database.execute("DELETE FROM validator_log WHERE symbol LIKE 'TST_LL%'")


@pytest.fixture(autouse=True)
def _reports_to_tmp(tmp_path, monkeypatch):
    """Never write reports into the real repo directory during tests."""
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _isolate_state():
    saved = STATE.market_data.get("macro_regime")
    yield
    if saved is None:
        STATE.market_data.pop("macro_regime", None)
    else:
        STATE.update_market_data("macro_regime", saved)


def seed_correlated(conn, n=12):
    """
    n terminal signals whose linked state vectors have rsi_h1 rising in lockstep
    with outcome_r — a perfect monotonic relationship, so Spearman must be +1.
    Vectors are left UNEMBEDDED so the nightly embed step has real work to do.
    """
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO state_vectors (ts, rsi_h1, rsi_h4, news_heat, regime_h4, session) "
                "VALUES (%s, %s, 50, 0.5, 'TREND_UP', 'LONDON') RETURNING id",
                (_FUTURE + timedelta(hours=i), 10.0 + i * 5.0),
            )
            sv_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO signals (ts, symbol, direction, grade, status, outcome_r, state_vector_id) "
                "VALUES (%s, 'TST_LL', 'LONG', %s, %s, %s, %s)",
                (
                    _FUTURE + timedelta(hours=i, minutes=30),
                    "A" if i % 2 == 0 else "B",
                    "TP2" if i >= n // 2 else "STOPPED",
                    float(i),          # outcome_r rises with rsi_h1
                    sv_id,
                ),
            )


# --------------------------------------------------------------------------
# nightly: dim ranking on seeded, engineered data
# --------------------------------------------------------------------------


@requires_db
def test_nightly_ranks_engineered_dim_at_plus_one():
    with scoped_conn() as conn:
        seed_correlated(conn, n=12)
        summary = loop.nightly_job(conn, _FUTURE)
        rows = fetch_on(
            conn,
            "SELECT dim, spearman, n_samples FROM dim_rankings "
            "WHERE computed_at = %s AND dim = 'rsi_h1'",
            (_FUTURE,),
        )

    assert summary["steps"]["embed"]["embedded"] >= 12   # embed step did real work
    assert summary["steps"]["rank_dims"]["status"] == "ok"
    assert summary["steps"]["rank_dims"]["samples"] >= 12

    assert len(rows) == 1
    dim, spearman, n_samples = rows[0]
    assert float(spearman) == pytest.approx(1.0, abs=1e-4)  # perfect monotonic relationship
    assert n_samples == 12


@requires_db
def test_nightly_skips_ranking_below_min_samples(caplog):
    with scoped_conn() as conn:
        seed_correlated(conn, n=3)  # below LEARN_MIN_SAMPLES (10)
        with caplog.at_level(logging.INFO):
            summary = loop.nightly_job(conn, _FUTURE)
        persisted = fetch_on(
            conn, "SELECT count(*) FROM dim_rankings WHERE computed_at = %s", (_FUTURE,)
        )[0][0]

    assert summary["steps"]["rank_dims"]["status"] == "skipped_min_samples"
    assert summary["steps"]["rank_dims"]["ranked"] == 0
    assert "skipping" in caplog.text
    assert persisted == 0


@requires_db
def test_nightly_does_not_persist_constant_dims():
    # rsi_h4 is seeded at a constant 50 for every row: no rank order exists, so
    # scipy would return NaN. It must be skipped, never persisted. This only
    # holds if the seeded rows ARE the population — an ambient row with a
    # different rsi_h4 makes the dim vary and the test meaningless.
    with scoped_conn() as conn:
        seed_correlated(conn, n=12)
        summary = loop.nightly_job(conn, _FUTURE)
        persisted = fetch_on(
            conn,
            "SELECT count(*) FROM dim_rankings WHERE computed_at = %s AND dim = 'rsi_h4'",
            (_FUTURE,),
        )[0][0]

    assert "rsi_h4" in summary["steps"]["rank_dims"]["skipped_dims"]
    assert persisted == 0


# --------------------------------------------------------------------------
# step isolation
# --------------------------------------------------------------------------


@requires_db
def test_failing_step_does_not_stop_the_following_steps(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("link exploded")

    monkeypatch.setattr(loop, "_step_link", boom)

    with database.get_conn() as conn:
        seed_correlated(conn, n=12)
        with caplog.at_level(logging.ERROR):
            summary = loop.nightly_job(conn, _FUTURE)

    assert summary["steps"]["link"]["status"] == "failed"
    assert summary["steps"]["link"]["error"] == "RuntimeError"
    # ...and every later step still ran.
    assert summary["steps"]["embed"]["status"] == "ok"
    assert summary["steps"]["rank_dims"]["status"] == "ok"
    assert "retrain_regime" in summary["steps"]


# --------------------------------------------------------------------------
# report files + atomic write
# --------------------------------------------------------------------------


@requires_db
def test_nightly_writes_json_report_atomically(_reports_to_tmp):
    with database.get_conn() as conn:
        seed_correlated(conn, n=12)
        summary = loop.nightly_job(conn, _FUTURE)

    path = summary["report_path"]
    assert os.path.basename(path) == "nightly-2099-06-01.json"
    payload = json.loads(open(path, encoding="utf-8").read())
    assert payload["steps"]["rank_dims"]["status"] == "ok"
    # No temp/partial file survives an atomic write.
    assert [f for f in os.listdir(_reports_to_tmp) if ".tmp-report-" in f or f.endswith(".partial")] == []


@requires_db
def test_weekly_report_contents_and_hand_computed_win_rate(_reports_to_tmp):
    with database.get_conn() as conn:
        seed_correlated(conn, n=12)
        loop.nightly_job(conn, _FUTURE)
        path = loop.weekly_report(conn, _FUTURE + timedelta(hours=13))

    text = open(path, encoding="utf-8").read()

    assert os.path.basename(path).startswith("weekly-")
    assert "# NEXUS weekly self-report" in text
    assert "rsi_h1" in text  # the engineered top dim surfaces in the table

    # Hand-computed: outcome_r = 0..11, so wins (>0) are 1..11 -> 11 of 12.
    assert "| Signals resolved | 12 |" in text
    assert f"| Win rate | {11 / 12 * 100:.1f}% |" in text
    # Wilson LB must sit below the naive rate, and the small-sample caveat is absent at n=12.
    assert "Wilson 95% lower bound" in text
    assert "| A |" in text and "| B |" in text  # per-grade breakdown
    assert [f for f in os.listdir(_reports_to_tmp) if ".partial" in f] == []


@requires_db
def test_weekly_report_is_honest_when_there_is_nothing_to_report(_reports_to_tmp):
    with database.get_conn() as conn:
        path = loop.weekly_report(conn, _FUTURE)
    text = open(path, encoding="utf-8").read()

    assert "n/a" in text  # never fabricates a rate it cannot compute
    assert "_no resolved signals_" in text or "| Signals resolved | 0 |" in text


# --------------------------------------------------------------------------
# wiring 3a: macro_regime reaches the validator
# --------------------------------------------------------------------------


def test_ctx_gains_macro_regime_from_state():
    state = {"macro_regime": "RISK_OFF", "1h": {"price": 4100.0, "indicators": {"rsi14": 50.0}}}
    ctx = analysis._build_validator_ctx(state, _FUTURE)
    assert ctx["macro_regime"] == "RISK_OFF"


def test_ctx_macro_regime_is_none_when_absent():
    ctx = analysis._build_validator_ctx({}, _FUTURE)
    assert ctx["macro_regime"] is None  # validator SKIPs RULE 5, as built


@requires_db
def test_rule5_fires_end_to_end_through_the_new_ctx_wiring():
    """Integration: the regime brain publishes RISK_OFF -> the analyst's ctx
    carries it -> validator RULE 5 penalizes shorting the safe haven."""
    STATE.update_market_data("macro_regime", "RISK_OFF")

    sig = SignalAnchors(
        direction="SHORT", entry_anchor="H1_EMA20", entry_offset_pips=0.0,
        stop_anchor="H1_EMA50", stop_offset_pips=0.0, tp1_rr=1.5, tp2_rr=3.0,
    )
    resolved = ResolvedSignal(
        entry_price=4100.0, stop_price=4110.0, tp1_price=4085.0,
        tp2_price=4070.0, risk_per_unit=10.0, warnings=[],
    )
    state = {
        "macro_regime": STATE.get_market_data("macro_regime"),
        "1h": {"price": 4100.0, "regime": "RANGE", "indicators": {"rsi14": 50.0}},
        "4h": {"regime": "RANGE", "indicators": {"rsi14": 50.0}},
        "1d": {"regime": "RANGE"},
    }
    ctx = analysis._build_validator_ctx(state, datetime(2099, 6, 1, 8, 0, tzinfo=timezone.utc))
    ctx["symbol"] = "TST_LL_RULE5"

    verdict = validate(sig, resolved, "A+", 80, ctx)

    assert "RULE5_MACRO_DIVERGENCE" in verdict.rules_fired
    assert verdict.grade == "A"                                   # A+ capped to A
    assert verdict.confidence == 80 - config.MACRO_DIVERGENCE_PENALTY
    assert any("safe-haven" in reason for reason in verdict.reasons)


# --------------------------------------------------------------------------
# wiring 3b: persist -> link
# --------------------------------------------------------------------------


@requires_db
def test_cycle_links_the_persisted_signal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "fusion.rag.link_latest", lambda conn, signal_id: calls.append(signal_id) or 1
    )
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: json.dumps({
        "direction": "LONG", "entry_anchor": "H1_EMA20", "entry_offset_pips": 0.0,
        "stop_anchor": "H4_SWING_LOW", "stop_offset_pips": 0.0,
        "tp1_rr": 1.5, "tp2_rr": 3.0, "grade": "A", "confidence": 80, "thesis": "t",
    }))

    state = {
        "1h": {"price": 4100.0, "stale": False, "regime": "TREND_UP",
               "indicators": {"ema20": 4100.0, "ema50": 4090.0, "rsi14": 55.0, "atr14": 5.0}},
        "4h": {"price": 4100.0, "stale": False, "regime": "TREND_UP",
               "indicators": {"ema20": 4095.0, "rsi14": 52.0, "swing_low": 4050.0}},
        "1d": {"price": 4100.0, "stale": False, "regime": "RANGE", "indicators": {"rsi14": 50.0}},
    }
    result = analysis.run_analysis_cycle(state, datetime(2099, 6, 1, 8, 0, tzinfo=timezone.utc))

    assert result["outcome"] == "SIGNAL_PERSISTED"
    assert calls == [result["signal_id"]]  # linked with the freshly-persisted id
    database.execute("DELETE FROM signals WHERE id = %s", (result["signal_id"],))


@requires_db
def test_link_failure_never_unmakes_a_persisted_signal(monkeypatch, caplog):
    def boom(conn, signal_id):
        raise RuntimeError("link exploded")

    monkeypatch.setattr("fusion.rag.link_latest", boom)
    monkeypatch.setattr(analysis, "call_claude", lambda prompt: json.dumps({
        "direction": "LONG", "entry_anchor": "H1_EMA20", "entry_offset_pips": 0.0,
        "stop_anchor": "H4_SWING_LOW", "stop_offset_pips": 0.0,
        "tp1_rr": 1.5, "tp2_rr": 3.0, "grade": "A", "confidence": 80, "thesis": "t",
    }))

    state = {
        "1h": {"price": 4100.0, "stale": False, "regime": "TREND_UP",
               "indicators": {"ema20": 4100.0, "ema50": 4090.0, "rsi14": 55.0, "atr14": 5.0}},
        "4h": {"price": 4100.0, "stale": False, "regime": "TREND_UP",
               "indicators": {"ema20": 4095.0, "rsi14": 52.0, "swing_low": 4050.0}},
        "1d": {"price": 4100.0, "stale": False, "regime": "RANGE", "indicators": {"rsi14": 50.0}},
    }
    with caplog.at_level(logging.WARNING):
        result = analysis.run_analysis_cycle(state, datetime(2099, 6, 1, 8, 0, tzinfo=timezone.utc))

    assert result["outcome"] == "SIGNAL_PERSISTED"  # the signal stands
    assert "link_latest failed" in caplog.text
    database.execute("DELETE FROM signals WHERE id = %s", (result["signal_id"],))
