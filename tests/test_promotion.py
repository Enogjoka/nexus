"""
Acceptance tests for Task 24: the promotion pack.

The property that matters most is FAIL CLOSED: a criterion that cannot be
computed must FAIL, not skip and not pass with a caveat. Several tests below
exist only to prove that absent evidence never reads as good news.

Every scenario is seeded inside a transaction that is never committed, so the
suite cannot leave rows that a later promotion report would count as real
history — the lesson from 22.1, applied to a module whose entire job is
reading history.
"""
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

import config
from ops import promotion

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

requires_db = pytest.mark.skipif(not config.DATABASE_URL, reason="DATABASE_URL not set")


@contextmanager
def clean_conn():
    """Owns every table the pack reads, for one test, never committed."""
    conn = psycopg2.connect(config.DATABASE_URL)
    try:
        with conn.cursor() as cur:
            for table in ("stage_events", "positions", "fills", "signals",
                          "pod_stats", "doctrines", "kernel_events", "command_audit"):
                cur.execute(f"DELETE FROM {table}")
        yield conn
    finally:
        conn.rollback()
        conn.close()


def seed_all_pass(conn, now=NOW, *, stage="PAPER"):
    """Everything SHADOW needs, comfortably."""
    with conn.cursor() as cur:
        # 20 days at PAPER
        cur.execute(
            "INSERT INTO stage_events (ts, event, env_stage, effective_stage) "
            "VALUES (%s,'BOOT',%s,%s)",
            (now - timedelta(days=20), stage, stage),
        )
        cur.execute(
            "INSERT INTO stage_events (ts, event, env_stage, effective_stage) "
            "VALUES (%s,'BOOT',%s,%s)",
            (now - timedelta(hours=1), stage, stage),
        )
        # 120 closed pod trades
        for i in range(120):
            cur.execute(
                "INSERT INTO positions (client_order_id, source, pod, direction, lots, "
                "entry_px, state, opened_at, closed_at, realized_pnl_usd) "
                "VALUES (%s,'POD','S1_FIXFADE','LONG',0.01,4000,'CLOSED',%s,%s,%s)",
                (f"TSTP-{i}", now - timedelta(days=2), now - timedelta(days=1), 1.0),
            )
        # 25 swing signals
        for i in range(25):
            cur.execute(
                "INSERT INTO signals (ts, symbol, direction, status) "
                "VALUES (%s,'TSTP','LONG','PENDING')",
                (now - timedelta(days=3),),
            )
        # doctrines: 90% FABLE by horizon -> fallback 10% (<= 25)
        for _ in range(9):
            cur.execute(
                "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, source, created_at) "
                "VALUES (%s,'BOTH',6,30,'FABLE',%s)", (now, now)
            )
        cur.execute(
            "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, source, created_at) "
            "VALUES (%s,'FLAT',0,30,'PARSE_FALLBACK',%s)", (now, now)
        )
        # a kill drill 3 days ago
        cur.execute(
            "INSERT INTO command_audit (ts, chat_id, command, stage, outcome) "
            "VALUES (%s,'1','/killswitch','PAPER','CONFIRMED')",
            (now - timedelta(days=3),),
        )


def seed_pod_stats(conn, rows, now=NOW):
    with conn.cursor() as cur:
        for pod, n, wins, expectancy, wilson, drag in rows:
            cur.execute(
                "INSERT INTO pod_stats (computed_at, pod, window_days, n_trades, wins, "
                "expectancy_usd, wilson_lb, cost_drag_pct, max_consecutive_losses) "
                "VALUES (%s,%s,14,%s,%s,%s,%s,%s,0)",
                (now, pod, n, wins, expectancy, wilson, drag),
            )


# ===========================================================================
# the happy path
# ===========================================================================


@requires_db
def test_a_clean_sweep_is_eligible_and_prints_the_epilogue():
    with clean_conn() as conn:
        seed_all_pass(conn)
        result = promotion.evaluate(conn, "SHADOW", NOW)

    assert result["eligible"] is True, result["failures"]
    assert result["failures"] == []

    report = promotion.render(result)
    assert "## Verdict: ELIGIBLE" in report
    assert "Eligibility is not promotion" in report
    assert f"wait {config.COOLING_OFF_HOURS} hours" in report
    assert "The wait is the control." in report


@requires_db
def test_even_an_eligible_verdict_says_it_changes_nothing():
    with clean_conn() as conn:
        seed_all_pass(conn)
        report = promotion.render(promotion.evaluate(conn, "SHADOW", NOW))
    assert "This report changes nothing" in report
    assert "human config edit plus a restart" in report


# ===========================================================================
# each criterion, individually failing
# ===========================================================================


def _break(cur, criterion, now=NOW):
    """Undo exactly one seeded criterion."""
    if criterion == "min_pod_trades":
        cur.execute("DELETE FROM positions")
    elif criterion == "min_swing_signals":
        cur.execute("DELETE FROM signals")
    elif criterion == "min_soak_days":
        cur.execute("DELETE FROM stage_events")
        cur.execute(
            "INSERT INTO stage_events (ts, event, env_stage, effective_stage) "
            "VALUES (%s,'BOOT','PAPER','PAPER')", (now - timedelta(days=2),)
        )
    elif criterion == "max_fallback_pct":
        cur.execute("DELETE FROM doctrines")
        for _ in range(9):
            cur.execute(
                "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, source, created_at) "
                "VALUES (%s,'FLAT',0,30,'PARSE_FALLBACK',%s)", (now, now)
            )
        cur.execute(
            "INSERT INTO doctrines (ts, bias, conviction, review_horizon_min, source, created_at) "
            "VALUES (%s,'BOTH',6,30,'FABLE',%s)", (now, now)
        )
    elif criterion == "kill_drill_within_days":
        cur.execute("DELETE FROM command_audit")


@requires_db
@pytest.mark.parametrize(
    "criterion",
    ["min_pod_trades", "min_swing_signals", "min_soak_days",
     "max_fallback_pct", "kill_drill_within_days"],
)
def test_each_criterion_alone_blocks_promotion(criterion):
    with clean_conn() as conn:
        seed_all_pass(conn)
        with conn.cursor() as cur:
            _break(cur, criterion)
        result = promotion.evaluate(conn, "SHADOW", NOW)

    assert result["eligible"] is False
    failed = {r["criterion"] for r in result["failures"]}
    assert criterion in failed, f"{criterion} should have blocked; failures were {failed}"

    report = promotion.render(result)
    assert "## Verdict: NOT ELIGIBLE" in report
    assert "Eligibility is not promotion" not in report, "no epilogue on a failed verdict"
    assert criterion in report


# ===========================================================================
# fail closed
# ===========================================================================


@requires_db
def test_an_empty_database_fails_every_criterion_it_cannot_compute():
    """You cannot promote on evidence you do not have."""
    with clean_conn() as conn:
        result = promotion.evaluate(conn, "SHADOW", NOW)

    assert result["eligible"] is False
    statuses = {r["criterion"]: r["status"] for r in result["rows"]}
    assert promotion.PASS not in statuses.values(), statuses
    # and the ones that are genuinely unknowable say so
    unknown = [r for r in result["rows"] if r["actual"] == promotion.INSUFFICIENT]
    assert unknown, "absent evidence must be labelled, not silently zeroed"


@requires_db
def test_a_never_drilled_kill_switch_fails_outright():
    with clean_conn() as conn:
        seed_all_pass(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM command_audit")
        result = promotion.evaluate(conn, "SHADOW", NOW)

    row = next(r for r in result["rows"] if r["criterion"] == "kill_drill_within_days")
    assert row["status"] == promotion.FAIL
    assert row["actual"] == "never drilled"
    assert "belief, not a control" in row["note"]


@requires_db
def test_a_stale_kill_drill_fails_at_thirty_one_days():
    """30 days passes, 31 does not — the boundary is the whole point."""
    for age_days, expected in ((30.0, promotion.PASS), (31.0, promotion.FAIL)):
        with clean_conn() as conn:
            seed_all_pass(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM command_audit")
                cur.execute(
                    "INSERT INTO command_audit (ts, chat_id, command, stage, outcome) "
                    "VALUES (%s,'1','/killswitch','PAPER','CONFIRMED')",
                    (NOW - timedelta(days=age_days),),
                )
            result = promotion.evaluate(conn, "SHADOW", NOW)
        row = next(r for r in result["rows"] if r["criterion"] == "kill_drill_within_days")
        assert row["status"] == expected, f"{age_days}d should be {expected}"


@requires_db
def test_an_armed_but_unconfirmed_drill_does_not_count():
    """Arming proves nothing about whether the kernel actually reacts."""
    with clean_conn() as conn:
        seed_all_pass(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM command_audit")
            cur.execute(
                "INSERT INTO command_audit (ts, chat_id, command, stage, outcome) "
                "VALUES (%s,'1','/killswitch','PAPER','CONFIRM_SENT')", (NOW,)
            )
        result = promotion.evaluate(conn, "SHADOW", NOW)

    row = next(r for r in result["rows"] if r["criterion"] == "kill_drill_within_days")
    assert row["status"] == promotion.FAIL


# ===========================================================================
# earliest-date arithmetic
# ===========================================================================


@requires_db
def test_earliest_date_for_soak_days_is_hand_computed():
    """
    2 days soaked of 14 required -> 12 days short -> earliest 2026-08-19.
    """
    with clean_conn() as conn:
        seed_all_pass(conn)
        with conn.cursor() as cur:
            _break(cur, "min_soak_days")
        result = promotion.evaluate(conn, "SHADOW", NOW)

    row = next(r for r in result["rows"] if r["criterion"] == "min_soak_days")
    assert row["status"] == promotion.FAIL
    assert row["earliest"] == "2026-08-19"
    assert "2026-08-19" in promotion.render(result)


@requires_db
def test_the_report_calls_dates_earliest_not_forecast():
    with clean_conn() as conn:
        seed_all_pass(conn)
        with conn.cursor() as cur:
            _break(cur, "min_soak_days")
        report = promotion.render(promotion.evaluate(conn, "SHADOW", NOW))
    assert "EARLIEST a criterion could clear, not a forecast" in report


# ===========================================================================
# pods
# ===========================================================================


@requires_db
def test_the_worst_pod_is_named_on_every_axis():
    """"The desk is unprofitable" is not an actionable sentence."""
    with clean_conn() as conn:
        seed_pod_stats(conn, [
            ("S1_FIXFADE", 50, 30, 1.50, 0.55, 20.0),
            ("S2_VWAPSNAP", 40, 10, -2.00, 0.18, 75.0),   # worst on all three
            ("S3_BASIS", 30, 18, 0.80, 0.44, 35.0),
        ])
        result = promotion.evaluate(conn, "MICRO", NOW)

    assert result["worst_wilson"]["pod"] == "S2_VWAPSNAP"
    assert result["worst_expectancy"]["pod"] == "S2_VWAPSNAP"
    assert result["worst_drag"]["pod"] == "S2_VWAPSNAP"

    report = promotion.render(result)
    assert "Weakest Wilson LB: **S2_VWAPSNAP**" in report


@requires_db
def test_aggregates_are_trade_weighted():
    """
    A four-trade lucky run must not lift the desk above what the evidence
    supports. 100 trades at 0.30 and 4 at 0.90:
      weighted = (0.30*100 + 0.90*4) / 104 = 33.6/104 = 0.3231
    """
    with clean_conn() as conn:
        seed_pod_stats(conn, [
            ("S1_FIXFADE", 100, 40, 0.10, 0.30, 30.0),
            ("S2_VWAPSNAP", 4, 4, 5.00, 0.90, 10.0),
        ])
        rows = promotion.pod_stat_rows(conn)

    assert promotion.aggregate_wilson(rows) == pytest.approx(33.6 / 104)


@requires_db
def test_pods_with_no_trades_are_excluded_from_the_ranking():
    with clean_conn() as conn:
        seed_pod_stats(conn, [
            ("S1_FIXFADE", 10, 6, 1.0, 0.50, 20.0),
            ("S4_NEWSBURST", 0, 0, None, None, None),
        ])
        rows = promotion.pod_stat_rows(conn)

    assert [r["pod"] for r in rows] == ["S1_FIXFADE"]


@requires_db
def test_no_pod_history_says_so_rather_than_ranking_nothing():
    with clean_conn() as conn:
        report = promotion.render(promotion.evaluate(conn, "SHADOW", NOW))
    assert "No pod has traded yet" in report


# ===========================================================================
# stage history
# ===========================================================================


@requires_db
def test_days_at_stage_counts_only_the_contiguous_run():
    """
    A system that ran SHADOW, dropped to PAPER and returned has not been at
    SHADOW throughout — counting from the first-ever boot would credit time it
    did not serve.
    """
    with clean_conn() as conn:
        with conn.cursor() as cur:
            for days_ago, stage in ((40, "SHADOW"), (30, "PAPER"), (5, "SHADOW"), (1, "SHADOW")):
                cur.execute(
                    "INSERT INTO stage_events (ts, event, env_stage, effective_stage) "
                    "VALUES (%s,'BOOT',%s,%s)",
                    (NOW - timedelta(days=days_ago), stage, stage),
                )
        days = promotion.days_at_stage(conn, "SHADOW", NOW)

    assert days == pytest.approx(5.0, abs=0.01), "only the current run counts"


@requires_db
def test_current_stage_comes_from_the_newest_boot_row():
    with clean_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stage_events (ts, event, env_stage, effective_stage) "
                "VALUES (%s,'BOOT','SHADOW','PAPER')", (NOW,)
            )
        assert promotion.current_stage(conn) == "PAPER", "the ADOPTED stage, not the env's"


# ===========================================================================
# drawdown
# ===========================================================================


@requires_db
def test_drawdown_walk_is_hand_computed():
    """
    Account 5000. Daily realised: +500, -1000, +100
      equity 5500 (peak) -> 4500 -> 4600
      max drawdown = (5500 - 4500) / 5500 = 18.18%
    """
    with clean_conn() as conn:
        with conn.cursor() as cur:
            for i, (day, pnl) in enumerate(((3, 500.0), (2, -1000.0), (1, 100.0))):
                cur.execute(
                    "INSERT INTO positions (client_order_id, source, direction, lots, "
                    "state, closed_at, realized_pnl_usd) "
                    "VALUES (%s,'POD','LONG',0.01,'CLOSED',%s,%s)",
                    (f"TSTD-{i}", NOW - timedelta(days=day), pnl),
                )
        assert promotion.max_drawdown_pct(conn) == pytest.approx(1000 / 5500 * 100, abs=0.01)


@requires_db
def test_drawdown_is_none_without_history():
    with clean_conn() as conn:
        assert promotion.max_drawdown_pct(conn) is None


def test_the_drawdown_approximation_is_documented():
    """It understates; that must be stated where someone will read it."""
    import inspect

    doc = inspect.getdoc(promotion.max_drawdown_pct) or ""
    assert "APPROXIMATION" in doc
    assert "at least this bad" in doc


# ===========================================================================
# the module cannot touch a stage
# ===========================================================================


def test_the_module_never_reads_or_writes_a_stage():
    """
    The one thing a promotion pack must never do is promote. Grep-level, as
    specified: a future edit that reaches for the stage machinery trips here.
    """
    import inspect

    source = inspect.getsource(promotion)
    for banned in ("NEXUS_STAGE", "write_demotion", "DEMOTED_FLAG_PATH",
                   "set_stage", "emergency_flatten"):
        assert banned not in source, f"promotion.py must not mention {banned}"


def test_the_module_imports_no_stage_or_kernel_module():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(promotion))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    for module in imported:
        assert module not in ("risk.stage", "risk.kernel")


def test_an_unknown_target_is_refused():
    with pytest.raises(SystemExit, match="unknown target stage"):
        promotion.evaluate(None, "GODMODE", NOW)


@pytest.mark.parametrize("target", ["SHADOW", "MICRO", "SCALED"])
def test_every_configured_target_is_evaluable(target):
    assert target in config.PROMOTION_CRITERIA
    assert config.PROMOTION_CRITERIA[target]
