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
    # Task 23: nightly_job now writes pod_stats too, so the pre-existing tests
    # that run it through a COMMITTING connection leak year-2099 rows unless
    # they are purged here alongside dim_rankings.
    database.execute("DELETE FROM pod_stats WHERE computed_at >= %s", (_CUTOFF,))
    database.execute("DELETE FROM positions WHERE client_order_id LIKE 'TST_LL-%'")
    database.execute("DELETE FROM fills WHERE client_order_id LIKE 'TST_LL-%'")
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


# ==========================================================================
# TASK 23 — pod stats, the doctrine provider, and the weekly narrative
# ==========================================================================


def seed_pod_trades(conn, pod, pnls, costs=None, base=None):
    """
    Closed POD positions, optionally with matching FILLED fills rows.

    `costs` is a list parallel to `pnls`; a None entry means NO fills row for
    that trade, which is how the "absent fills -> NULL costs" path is driven.
    """
    base = base or _FUTURE
    with conn.cursor() as cur:
        for i, pnl in enumerate(pnls):
            cid = f"TST_LL-{pod}-{base.isoformat()}-{i}"
            cur.execute(
                "INSERT INTO positions (client_order_id, source, pod, direction, lots, "
                "entry_px, stop_px, state, opened_at, closed_at, realized_pnl_usd) "
                "VALUES (%s,'POD',%s,'LONG',0.01,4000,3990,'CLOSED',%s,%s,%s)",
                (cid, pod, base, base + timedelta(hours=i), pnl),
            )
            if costs is None:
                continue
            spread = costs[i]
            if spread is None:
                continue
            cur.execute(
                "INSERT INTO fills (ts, client_order_id, direction, lots, spread_at_send, "
                "slippage, fill_mode, status) "
                "VALUES (%s,%s,'LONG',0.01,%s,0,'MODELED','FILLED')",
                (base + timedelta(hours=i), cid, spread),
            )


@contextmanager
def pod_conn():
    """Owns positions/fills/pod_stats for one test; never committed."""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pod_stats")
            cur.execute("DELETE FROM fills")
            cur.execute("DELETE FROM positions")
        yield conn
    finally:
        conn.rollback()
        conn.close()


@requires_db
def test_pod_stats_math_is_hand_computed_with_the_cost_join():
    """
    S1: pnls +10, -4, +6, -2  -> n=4, wins=2, gross=+10.00
        expectancy = 10/4 = 2.50
        costs: spread 0.35, slippage 0, lots 0.01, commission 7.0/lot
               = 0.35 * 100 * 0.01 + 7.0 * 0.01 = 0.35 + 0.07 = 0.42 each
               total = 4 * 0.42 = 1.68
        cost drag = 1.68 / 10.00 * 100 = 16.8%
        worst losing streak = 1
    """
    with pod_conn() as conn:
        seed_pod_trades(conn, "S1_FIXFADE", [10.0, -4.0, 6.0, -2.0], costs=[0.35] * 4)
        summary = loop._step_pod_stats(conn, _FUTURE)
        row = fetch_on(
            conn,
            "SELECT n_trades, wins, expectancy_usd, gross_pnl_usd, total_costs_usd, "
            "cost_drag_pct, max_consecutive_losses FROM pod_stats WHERE pod='S1_FIXFADE'",
        )[0]

    assert summary["status"] == "ok"
    n, wins, expectancy, gross, costs, drag, worst = row
    assert (n, wins) == (4, 2)
    assert float(expectancy) == pytest.approx(2.50)
    assert float(gross) == pytest.approx(10.00)
    assert float(costs) == pytest.approx(1.68)
    assert float(drag) == pytest.approx(16.8, abs=0.05)
    assert worst == 1


@requires_db
def test_a_pod_with_no_trades_still_gets_a_row():
    """"This pod did nothing for two weeks" is a finding, not an absence."""
    with pod_conn() as conn:
        loop._step_pod_stats(conn, _FUTURE)
        rows = fetch_on(
            conn, "SELECT pod, n_trades, expectancy_usd FROM pod_stats ORDER BY pod"
        )

    assert {r[0] for r in rows} == set(config.POD_NAMES)
    for _pod, n, expectancy in rows:
        assert n == 0
        assert expectancy is None, "a pod that never traded has no expectancy to state"


@requires_db
def test_costs_are_null_when_any_fills_row_is_missing():
    """Never invent a cost — a partial join understates drag, which flatters."""
    with pod_conn() as conn:
        seed_pod_trades(conn, "S2_VWAPSNAP", [5.0, -3.0], costs=[0.35, None])
        loop._step_pod_stats(conn, _FUTURE)
        row = fetch_on(
            conn,
            "SELECT n_trades, gross_pnl_usd, total_costs_usd, cost_drag_pct "
            "FROM pod_stats WHERE pod='S2_VWAPSNAP'",
        )[0]

    n, gross, costs, drag = row
    assert n == 2
    assert float(gross) == pytest.approx(2.0)
    assert costs is None, "one missing fills row means costs are unknown, not partial"
    assert drag is None


@requires_db
def test_trades_outside_the_window_are_excluded():
    with pod_conn() as conn:
        old = _FUTURE - timedelta(days=config.POD_STATS_WINDOW_DAYS + 2)
        seed_pod_trades(conn, "S3_BASIS", [99.0], base=old)
        loop._step_pod_stats(conn, _FUTURE)
        row = fetch_on(conn, "SELECT n_trades FROM pod_stats WHERE pod='S3_BASIS'")[0]
    assert row[0] == 0


@requires_db
def test_pod_stats_runs_as_a_nightly_step():
    with pod_conn() as conn:
        seed_pod_trades(conn, "S1_FIXFADE", [1.0], costs=[0.35])
        summary = loop.nightly_job(conn, _FUTURE)
    assert summary["steps"]["pod_stats"]["status"] == "ok"


# --- the snapshot the doctrine consumes ------------------------------------


@requires_db
def test_snapshot_is_empty_before_anything_is_computed():
    with pod_conn() as conn:
        assert loop.pod_stats_snapshot(conn) is None


@requires_db
def test_snapshot_shape_matches_what_the_doctrine_prompt_renders():
    """
    INTEGRATION. The contract between the learning loop and the doctrine is a
    dict shape, and nothing type-checks it — so render a REAL snapshot through
    the REAL prompt builder and assert the numbers actually appear.
    """
    from ai.doctrine import build_doctrine_prompt

    with pod_conn() as conn:
        seed_pod_trades(conn, "S1_FIXFADE", [10.0, -4.0, 6.0, -2.0], costs=[0.35] * 4)
        loop._step_pod_stats(conn, _FUTURE)
        snapshot = loop.pod_stats_snapshot(conn)

    assert snapshot is not None
    assert snapshot["S1_FIXFADE"]["trades"] == 4
    assert snapshot["S1_FIXFADE"]["win_rate"] == pytest.approx(0.5)

    prompt = build_doctrine_prompt({"rsi_h1": 55}, snapshot)

    assert "no pod history" not in prompt
    assert "S1_FIXFADE" in prompt
    assert "'trades': 4" in prompt, "the numbers must survive into the prompt"
    assert "'win_rate': 0.5" in prompt


@requires_db
def test_snapshot_takes_the_newest_row_per_pod():
    with pod_conn() as conn:
        seed_pod_trades(conn, "S1_FIXFADE", [1.0], costs=[0.35])
        loop._step_pod_stats(conn, _FUTURE)
        seed_pod_trades(conn, "S1_FIXFADE", [1.0, 2.0], costs=[0.35, 0.35],
                        base=_FUTURE + timedelta(hours=5))
        loop._step_pod_stats(conn, _FUTURE + timedelta(days=1))
        snapshot = loop.pod_stats_snapshot(conn)

    assert snapshot["S1_FIXFADE"]["trades"] == 3, "the newer computation wins"


def test_the_pool_provider_never_raises(monkeypatch):
    def unavailable(*a, **k):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(database, "get_conn", unavailable)
    assert loop.pod_stats_snapshot_from_pool() is None


# --- the weekly narrative ---------------------------------------------------


class _FakeResp:
    def __init__(self, text):
        self.content = [type("B", (), {"text": text})()]
        self.usage = type("U", (), {"input_tokens": 500, "output_tokens": 200})()


class _FakeClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return _FakeResp(self.text)


@requires_db
def test_weekly_prose_is_appended_verbatim(monkeypatch, _reports_to_tmp):
    prose = "The desk did nothing this week, and that was correct.\n\nThe single biggest concern is the doctrine's silence."
    client = _FakeClient(text=prose)
    monkeypatch.setattr(loop, "_get_client", lambda: client)

    with pod_conn() as conn:
        path = loop.weekly_report(conn, _FUTURE)

    text = open(path, encoding="utf-8").read()
    assert "## Desk notes (Fable)" in text
    assert prose in text, "the narrative must be appended verbatim, not paraphrased"
    assert client.calls[0]["max_tokens"] == config.WEEKLY_PROSE_MAX_TOKENS


@requires_db
def test_the_report_still_writes_when_the_essayist_is_out(monkeypatch, _reports_to_tmp):
    """The essayist is a commentator, not a source. The numbers do not need it."""
    monkeypatch.setattr(loop, "_get_client", lambda: _FakeClient(error=RuntimeError("API down")))

    with pod_conn() as conn:
        seed_pod_trades(conn, "S1_FIXFADE", [1.0], costs=[0.35])
        loop._step_pod_stats(conn, _FUTURE)
        path = loop.weekly_report(conn, _FUTURE)

    text = open(path, encoding="utf-8").read()
    assert "## Desk notes: unavailable (API)" in text
    assert "## Desk notes (Fable)" not in text
    assert "# NEXUS weekly self-report" in text
    assert "## Pods" in text, "every number must survive the essayist's absence"
    assert "S1_FIXFADE" in text


@requires_db
def test_prose_is_skipped_when_the_budget_is_spent(monkeypatch, _reports_to_tmp):
    monkeypatch.setattr(loop.STATE, "budget_spent_today", config.MAX_DAILY_COST + 1)

    def forbidden():
        raise AssertionError("the budget cap must be checked BEFORE the client is built")

    monkeypatch.setattr(loop, "_get_client", forbidden)
    assert loop.write_prose("numbers") is None


def test_the_prose_prompt_forbids_inventing_numbers():
    prompt = loop.build_prose_prompt("issued: 0")
    assert "GROUND TRUTH" in prompt
    assert "Do NOT invent" in prompt
    assert "biggest concern" in prompt


@requires_db
def test_prose_cost_is_accumulated_to_the_budget(monkeypatch):
    monkeypatch.setattr(loop, "_get_client", lambda: _FakeClient(text="ok"))
    monkeypatch.setattr(loop.STATE, "budget_spent_today", 0.0)
    loop.write_prose("numbers")
    assert loop.STATE.budget_spent_today > 0.0


# --- doctrine accounting ----------------------------------------------------


@requires_db
def test_doctrine_source_accounting_math(_reports_to_tmp):
    """
    3 FABLE (conviction 4/6/8 -> avg 6.0, horizon 30 each = 90 min)
    2 PARSE_FALLBACK (horizon 15 each = 30 min)
    fallback share = 30 / 120 = 25.0%
    """
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM doctrines")
            for conviction in (4, 6, 8):
                cur.execute(
                    "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, "
                    "source, created_at) VALUES (%s,'BOTH',%s,30,'FABLE',%s)",
                    (_FUTURE, conviction, _FUTURE),
                )
            for _ in range(2):
                cur.execute(
                    "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, "
                    "source, created_at) VALUES (%s,'FLAT',0,15,'PARSE_FALLBACK',%s)",
                    (_FUTURE, _FUTURE),
                )
        data = loop._collect_weekly(conn, _FUTURE + timedelta(hours=1))
    finally:
        conn.rollback()
        conn.close()

    assert data["doctrine_sources"] == {"FABLE": 3, "PARSE_FALLBACK": 2}
    assert data["doctrine_avg_conviction"] == pytest.approx(6.0)
    assert data["doctrine_fallback_pct"] == pytest.approx(25.0)
