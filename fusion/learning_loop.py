"""
NEXUS learning loop — Phase 2's capstone: the system starts studying itself.

Two jobs:

  nightly_job()   link signals to their world-state, embed what is unembedded,
                  rank every numeric state-vector dim against realized outcome_r
                  by Spearman correlation, retrain the regime brain, and write
                  a JSON summary to reports/.

  weekly_report() a numbers-only Markdown self-report: hit rate with a Wilson
                  lower bound, expectancy decomposed, per-grade breakdown, the
                  dims that actually correlate with outcomes, and which
                  validator rules have been firing.

Two design commitments worth stating:

  * EVERY nightly step is independently survivable. A step that raises is
    logged and the next one still runs — a failed link must never cost us the
    night's dim rankings. Each step reports its own status in the summary.

  * NO AI call anywhere in here. The weekly report is arithmetic and tables;
    Fable-written prose reports arrive with the v7 doctrine stack. A
    self-assessment that hallucinates is worse than no self-assessment.

Spearman (rank) rather than Pearson: the relationship between a macro dim and
R is monotonic at best and rarely linear, and rank correlation is robust to
the fat tails a stop-loss distribution guarantees.
"""
import argparse
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import numpy as np

# CLI-only: `python3 -m fusion.learning_loop --nightly` is a standalone entry
# point, so .env must be loaded here, BEFORE `import config` below reads the
# environment (config.py reads env vars at module-import time). A library
# import of this module does NOT hit this branch -- mirrors the sensors.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from core.state import STATE
from fusion import rag, regime

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = ("STOPPED", "TP2", "BE")


# ==========================================================================
# report file plumbing
# ==========================================================================


def _reports_dir() -> str:
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    return config.REPORTS_DIR


def _atomic_write(path: str, text: str) -> str:
    """
    Write via a temp file in the SAME directory then os.replace, so a reader
    (or a crash) never observes a half-written report. Same-directory matters:
    os.replace is only atomic within a filesystem.
    """
    directory = os.path.dirname(os.path.abspath(path))
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-report-", suffix=".partial")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp_path, path)
    except Exception:
        # Never leave a partial file behind on failure.
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
    return path


# ==========================================================================
# nightly steps
# ==========================================================================


def _step_link(conn) -> Dict[str, Any]:
    """Back-link every signal that still has no state_vector_id."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM signals WHERE state_vector_id IS NULL ORDER BY id")
        pending = [row[0] for row in cur.fetchall()]
    linked = sum(1 for signal_id in pending if rag.link_latest(conn, signal_id) is not None)
    return {"status": "ok", "candidates": len(pending), "linked": linked}


def _step_embed(conn) -> Dict[str, Any]:
    """Embed every state vector that still has no embedding."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM state_vectors WHERE embedding IS NULL ORDER BY id")
        pending = [row[0] for row in cur.fetchall()]
    embedded = sum(1 for sv_id in pending if rag.embed_and_store(conn, sv_id) is not None)
    return {"status": "ok", "candidates": len(pending), "embedded": embedded}


def numeric_dims() -> List[str]:
    """
    The numeric state-vector dims eligible for ranking. Reuses the RAG range
    table as the single definition of "numeric dim" so the two never drift.
    """
    return list(config.RAG_DIM_RANGES)


def _load_outcome_pairs(conn) -> Dict[str, List[tuple]]:
    """
    For every terminal signal with a linked AND embedded state vector, collect
    (dim_value, outcome_r) pairs per dim, skipping NULL dim values.
    """
    dims = numeric_dims()
    columns = ", ".join(f"sv.{dim}" for dim in dims)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns}, s.outcome_r "
            "FROM signals s JOIN state_vectors sv ON s.state_vector_id = sv.id "
            "WHERE s.status IN %s AND s.outcome_r IS NOT NULL AND sv.embedding IS NOT NULL",
            (_TERMINAL_STATUSES,),
        )
        rows = cur.fetchall()

    pairs: Dict[str, List[tuple]] = {dim: [] for dim in dims}
    for row in rows:
        outcome = row[-1]
        if outcome is None:
            continue
        for index, dim in enumerate(dims):
            value = row[index]
            if value is None:
                continue
            pairs[dim].append((float(value), float(outcome)))
    return pairs


def _step_rank_dims(conn, now_utc: datetime) -> Dict[str, Any]:
    """
    Spearman rank correlation between each numeric dim and outcome_r, persisted
    to dim_rankings. Refuses below config.LEARN_MIN_SAMPLES terminal signals:
    a correlation computed from a handful of trades is noise wearing a number's
    clothes.
    """
    from scipy.stats import spearmanr

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM signals s JOIN state_vectors sv ON s.state_vector_id = sv.id "
            "WHERE s.status IN %s AND s.outcome_r IS NOT NULL AND sv.embedding IS NOT NULL",
            (_TERMINAL_STATUSES,),
        )
        sample_count = int(cur.fetchone()[0])

    if sample_count < config.LEARN_MIN_SAMPLES:
        logger.info(
            "rank_dims: skipping — %d terminal signal(s) with linked+embedded vectors, need %d",
            sample_count,
            config.LEARN_MIN_SAMPLES,
        )
        return {"status": "skipped_min_samples", "samples": sample_count, "ranked": 0}

    pairs = _load_outcome_pairs(conn)
    ranked = 0
    skipped: List[str] = []

    for dim, observations in pairs.items():
        if len(observations) < config.LEARN_MIN_SAMPLES:
            skipped.append(dim)
            continue
        values = np.array([observation[0] for observation in observations], dtype=float)
        outcomes = np.array([observation[1] for observation in observations], dtype=float)
        # A dim that never varies has no rank order to correlate; scipy returns
        # NaN for it. Persisting NaN would poison every later |spearman| sort.
        if np.all(values == values[0]) or np.all(outcomes == outcomes[0]):
            skipped.append(dim)
            continue

        statistic = spearmanr(values, outcomes).statistic
        if statistic is None or not np.isfinite(statistic):
            skipped.append(dim)
            continue

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dim_rankings (computed_at, dim, spearman, n_samples) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (computed_at, dim) DO NOTHING",
                (now_utc, dim, float(statistic), len(observations)),
            )
        ranked += 1

    return {"status": "ok", "samples": sample_count, "ranked": ranked, "skipped_dims": skipped}


def _step_retrain_regime(conn) -> Dict[str, Any]:
    """Retrain the macro regime brain. fit() already refuses below
    config.REGIME_MIN_ROWS and logs why, so this simply records the outcome."""
    model = regime.fit(conn)
    if model is None:
        return {"status": "not_trained", "rows": 0}
    return {"status": "trained", "rows": model.n_rows, "labels": {str(k): v for k, v in model.labels.items()}}


def _pod_costs(conn, window_start: datetime) -> Dict[str, float]:
    """
    Round-turn cost per client_order_id, joined from the fills ledger.

    A position with no matching fills row is ABSENT from this map, not zero.
    The caller must then report NULL costs rather than estimate them: a
    fabricated cost flows straight into cost_drag_pct and from there into a
    judgement about whether a strategy pays for itself.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_order_id, spread_at_send, slippage, lots FROM fills "
            "WHERE status = 'FILLED' AND ts >= %s",
            (window_start,),
        )
        rows = cur.fetchall()

    costs: Dict[str, float] = {}
    for client_order_id, spread, slippage, lots in rows:
        if spread is None or lots is None:
            continue
        lots = float(lots)
        # Spread is a round-turn cost in a bid/ask quote; observed slippage is
        # counted on both sides; commission is quoted per lot.
        slip = abs(float(slippage)) if slippage is not None else 0.0
        costs[client_order_id] = (
            (float(spread) + 2.0 * slip) * config.CONTRACT_SIZE_OZ * lots
            + config.COMMISSION_USD_PER_LOT * lots
        )
    return costs


def _step_pod_stats(conn, now_utc: datetime) -> Dict[str, Any]:
    """
    Per-pod rolling performance over config.POD_STATS_WINDOW_DAYS.

    Every configured pod gets a row, including pods that did nothing: "this
    strategy has not traded in two weeks" is a finding, and it is invisible if
    an absence of trades is also an absence of rows.
    """
    window_days = config.POD_STATS_WINDOW_DAYS
    window_start = now_utc - timedelta(days=window_days)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pod, client_order_id, realized_pnl_usd FROM positions "
            "WHERE source = 'POD' AND state = 'CLOSED' AND closed_at >= %s "
            "AND pod IS NOT NULL ORDER BY closed_at",
            (window_start,),
        )
        rows = cur.fetchall()

    costs = _pod_costs(conn, window_start)

    by_pod: Dict[str, List[tuple]] = {name: [] for name in config.POD_NAMES}
    for pod, client_order_id, pnl in rows:
        by_pod.setdefault(pod, []).append(
            (client_order_id, float(pnl) if pnl is not None else 0.0)
        )

    written = 0
    for pod, trades in by_pod.items():
        pnls = [pnl for _cid, pnl in trades]
        n = len(pnls)
        wins = sum(1 for value in pnls if value > 0)

        # Wilson via the RAG helper — one definition of "how sure are we about
        # this win rate" across the whole system.
        stats = rag.wilson_stats([{"outcome_r": value} for value in pnls]) if pnls else None
        wilson_lb = stats["wilson_lower"] if stats else None
        gross = sum(pnls) if pnls else None
        expectancy = (sum(pnls) / n) if n else None

        # Costs only where EVERY trade in the window has a fills row; a partial
        # join would understate the drag, which is the direction that flatters.
        known = [costs.get(cid) for cid, _pnl in trades]
        if n and all(value is not None for value in known):
            total_costs = sum(known)
            drag = (total_costs / abs(gross) * 100.0) if gross else None
        else:
            total_costs, drag = None, None

        worst = streak = 0
        for value in pnls:
            if value <= 0:
                streak += 1
                worst = max(worst, streak)
            else:
                streak = 0

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pod_stats (computed_at, pod, window_days, n_trades, wins, "
                "expectancy_usd, wilson_lb, gross_pnl_usd, total_costs_usd, cost_drag_pct, "
                "max_consecutive_losses) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (computed_at, pod) DO NOTHING",
                (now_utc, pod, window_days, n, wins, expectancy, wilson_lb,
                 gross, total_costs, drag, worst),
            )
        written += 1

    logger.info("pod_stats: wrote %d pod row(s) over a %dd window", written, window_days)
    return {"status": "ok", "pods": written, "window_days": window_days,
            "trades": len(rows)}


def pod_stats_snapshot(conn) -> Optional[Dict[str, Any]]:
    """
    Newest pod_stats row per pod, shaped for the doctrine prompt.

    ai/doctrine.py::_render_pod_stats renders each value with str(), so the
    shape is whatever reads well in a prompt — a compact dict of the numbers
    that should influence which pods the desk enables. Verified against that
    function rather than assumed.

    Returns None when nothing has been computed, which the prompt already
    renders as "no pod history".
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT ON (pod) pod, n_trades, wins, expectancy_usd, wilson_lb, "
                "cost_drag_pct, max_consecutive_losses, window_days "
                "FROM pod_stats ORDER BY pod, computed_at DESC"
            )
            rows = cur.fetchall()
    except Exception:
        logger.warning("pod_stats_snapshot: query failed", exc_info=True)
        return None

    if not rows:
        return None

    snapshot: Dict[str, Any] = {}
    for pod, n, wins, expectancy, wilson, drag, worst, window_days in rows:
        n = int(n or 0)
        snapshot[pod] = {
            "trades": n,
            "wins": int(wins or 0),
            "win_rate": round(wins / n, 3) if n else None,
            "expectancy_usd": round(float(expectancy), 4) if expectancy is not None else None,
            "wilson_lb": round(float(wilson), 3) if wilson is not None else None,
            "cost_drag_pct": round(float(drag), 1) if drag is not None else None,
            "max_consec_losses": int(worst or 0),
            "window_days": int(window_days or 0),
        }
    return snapshot


def pod_stats_snapshot_from_pool() -> Optional[Dict[str, Any]]:
    """
    Provider entry point for ai.doctrine — checks out its own connection so the
    doctrine agent needs no ambient one. Never raises: a failure here must
    degrade the prompt to "no pod history", not break doctrine issuance.
    """
    try:
        with database.get_conn() as conn:
            return pod_stats_snapshot(conn)
    except Exception:
        logger.warning("pod_stats_snapshot_from_pool: unavailable", exc_info=True)
        return None


def nightly_job(conn, now_utc: datetime) -> Dict[str, Any]:
    """
    Run every nightly step in order, each independently survivable, and write
    the summary to reports/nightly-YYYY-MM-DD.json.
    """
    summary: Dict[str, Any] = {"ran_at": now_utc.isoformat(), "steps": {}}

    for name, step in (
        ("link", lambda: _step_link(conn)),
        ("embed", lambda: _step_embed(conn)),
        ("rank_dims", lambda: _step_rank_dims(conn, now_utc)),
        ("pod_stats", lambda: _step_pod_stats(conn, now_utc)),
        ("retrain_regime", lambda: _step_retrain_regime(conn)),
    ):
        try:
            summary["steps"][name] = step()
        except Exception as exc:
            logger.exception("nightly_job: step %s failed; continuing to the next step", name)
            summary["steps"][name] = {"status": "failed", "error": type(exc).__name__}

    path = os.path.join(_reports_dir(), f"nightly-{now_utc.date().isoformat()}.json")
    try:
        _atomic_write(path, json.dumps(summary, indent=2, default=str))
        summary["report_path"] = path
    except Exception:
        logger.exception("nightly_job: could not write the summary report")
        summary["report_path"] = None

    return summary


# ==========================================================================
# weekly report
# ==========================================================================


# ==========================================================================
# the essayist
# ==========================================================================


def _get_client():
    """
    Lazy Anthropic client. Its own seam (rather than ai.analysis's) so the
    weekly prose can use its own token ceiling and so tests monkeypatch one
    obvious place without touching the analyst.
    """
    import anthropic

    return anthropic.Anthropic(
        api_key=config.ANTHROPIC_API_KEY, timeout=config.ANALYSIS_TIMEOUT_SECONDS
    )


def build_prose_prompt(facts: str) -> str:
    """
    The numbers are ground truth; the model's job is to READ them, not to add
    to them. Every instruction here exists to keep it on that side of the line.
    """
    return "\n".join(
        [
            "You are the trading desk's own weekly self-review. You are writing",
            "for the one person who runs this system and already distrusts it.",
            "",
            "The numbers below are GROUND TRUTH, computed from the database.",
            "Do NOT invent, estimate, extrapolate or restate any number that is",
            "not present. If something important cannot be determined from these",
            "figures, say that it cannot be determined.",
            "",
            "Write at most six short paragraphs of plain prose. No headings, no",
            "bullet lists, no markdown. Cover: what actually happened, any",
            "anomaly worth attention, and — as your final paragraph — the SINGLE",
            "biggest concern, stated plainly.",
            "",
            "Do not congratulate. Do not reassure. A week where nothing traded",
            "is a legitimate finding, not a failure to explain away.",
            "",
            "## THE WEEK'S NUMBERS",
            facts,
        ]
    )


def write_prose(facts: str) -> Optional[str]:
    """
    One guarded Claude call. Returns the prose, or None.

    None is a completely acceptable outcome: weekly_report treats a missing
    essayist as a missing section, never as a failed report. INVARIANT 6.
    """
    if STATE.budget_spent_today > config.MAX_DAILY_COST:
        logger.error(
            "weekly prose: daily budget cap reached (spent=$%.4f); skipping the narrative",
            STATE.budget_spent_today,
        )
        return None

    try:
        client = _get_client()
        resp = client.messages.create(
            model=config.ANALYSIS_MODEL,
            max_tokens=config.WEEKLY_PROSE_MAX_TOKENS,
            messages=[{"role": "user", "content": build_prose_prompt(facts)}],
        )
    except Exception as exc:
        logger.error("weekly prose: API call failed (%s)", exc)
        return None

    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = (
        input_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_INPUT
        + output_tokens / 1_000_000 * config.ANALYSIS_COST_PER_MTOK_OUTPUT
    )
    STATE.budget_spent_today += cost
    logger.info(
        "weekly prose: usage in=%d out=%d est_cost=$%.4f", input_tokens, output_tokens, cost
    )

    parts = []
    for block in getattr(resp, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip() or None


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _collect_weekly(conn, now_utc: datetime) -> Dict[str, Any]:
    window_start = now_utc - timedelta(days=config.LEARN_REPORT_WINDOW_DAYS)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM signals WHERE ts >= %s", (window_start,))
        issued = int(cur.fetchone()[0])

        cur.execute(
            "SELECT grade, outcome_r FROM signals "
            "WHERE ts >= %s AND status IN %s AND outcome_r IS NOT NULL",
            (window_start, _TERMINAL_STATUSES),
        )
        terminal = [(grade, float(outcome)) for grade, outcome in cur.fetchall()]

        cur.execute(
            "SELECT rule_name, rule_result, count(*) FROM validator_log "
            "WHERE ts >= %s GROUP BY rule_name, rule_result ORDER BY rule_name, rule_result",
            (window_start,),
        )
        rule_counts = cur.fetchall()

        cur.execute("SELECT max(computed_at) FROM dim_rankings")
        latest = cur.fetchone()[0]
        dims: List[tuple] = []
        if latest is not None:
            cur.execute(
                "SELECT dim, spearman, n_samples FROM dim_rankings WHERE computed_at = %s "
                "ORDER BY abs(spearman) DESC",
                (latest,),
            )
            dims = cur.fetchall()

        # Doctrine accounting. A brain that is mostly FALLBACK is a finding:
        # the desk was flat not because it judged flat, but because nobody
        # answered. Those two look identical in the doctrines table until you
        # count by source.
        cur.execute(
            "SELECT source, count(*) FROM doctrines WHERE created_at >= %s GROUP BY source",
            (window_start,),
        )
        doctrine_sources = {row[0]: int(row[1]) for row in cur.fetchall()}

        cur.execute(
            "SELECT avg(conviction) FROM doctrines "
            "WHERE created_at >= %s AND source = 'FABLE'",
            (window_start,),
        )
        avg_conviction = cur.fetchone()[0]

        # Horizon-hours spent in a fallback posture, as a share of all
        # horizon-hours issued. review_horizon_min weights each doctrine by how
        # long it was meant to govern, so a 15-minute fallback does not count
        # the same as a 120-minute one.
        cur.execute(
            "SELECT COALESCE(SUM(review_horizon_min) FILTER "
            "  (WHERE source <> 'FABLE'), 0)::float, "
            "COALESCE(SUM(review_horizon_min), 0)::float "
            "FROM doctrines WHERE created_at >= %s",
            (window_start,),
        )
        fallback_min, total_min = cur.fetchone()

        cur.execute(
            "SELECT DISTINCT ON (pod) pod, n_trades, wins, expectancy_usd, wilson_lb, "
            "cost_drag_pct, max_consecutive_losses FROM pod_stats "
            "ORDER BY pod, computed_at DESC"
        )
        pod_rows = cur.fetchall()

    return {
        "window_start": window_start,
        "issued": issued,
        "terminal": terminal,
        "rule_counts": rule_counts,
        "dims": dims,
        "dims_computed_at": latest,
        "doctrine_sources": doctrine_sources,
        "doctrine_avg_conviction": float(avg_conviction) if avg_conviction is not None else None,
        "doctrine_fallback_pct": (
            (fallback_min / total_min * 100.0) if total_min else None
        ),
        "pod_rows": pod_rows,
    }


def weekly_report(conn, now_utc: datetime) -> str:
    """
    Write reports/weekly-YYYY-MM-DD.md and return its path. Numbers only — no
    AI call, no narrative, nothing that could hallucinate a conclusion.
    """
    data = _collect_weekly(conn, now_utc)
    terminal = data["terminal"]
    outcomes = [outcome for _grade, outcome in terminal]

    wins = [r for r in outcomes if r > 0]
    losses = [r for r in outcomes if r <= 0]
    stats = rag.wilson_stats([{"outcome_r": r} for r in outcomes]) if outcomes else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    win_rate = stats["win_rate"] if stats else None
    # Expectancy decomposed into its components; equals avg R by construction,
    # but the components are what tell you WHICH half needs work.
    expectancy = (
        win_rate * avg_win + (1 - win_rate) * avg_loss
        if (win_rate is not None and avg_win is not None and avg_loss is not None)
        else (stats["avg_r"] if stats else None)
    )

    lines = [
        f"# NEXUS weekly self-report — {now_utc.date().isoformat()}",
        "",
        f"Window: {data['window_start'].date().isoformat()} → {now_utc.date().isoformat()} "
        f"({config.LEARN_REPORT_WINDOW_DAYS}d) · stage `{config.get_stage().value}`",
        "",
        "## Performance",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Signals issued | {data['issued']} |",
        f"| Signals resolved | {len(terminal)} |",
        f"| Win rate | {_fmt(win_rate * 100, 1) if win_rate is not None else 'n/a'}% |",
        f"| Wilson 95% lower bound | {_fmt(stats['wilson_lower'] * 100, 1) if stats else 'n/a'}% |",
        f"| Average R | {_fmt(stats['avg_r'], 3) if stats else 'n/a'} |",
        f"| Expectancy (R/trade) | {_fmt(expectancy, 3)} |",
        f"| Average win | {_fmt(avg_win, 3)} R |",
        f"| Average loss | {_fmt(avg_loss, 3)} R |",
        "",
    ]

    if stats and stats["n"] < config.LEARN_MIN_SAMPLES:
        lines += [
            f"> Only {stats['n']} resolved signal(s) this window — below the "
            f"{config.LEARN_MIN_SAMPLES}-sample floor. Treat every number above as "
            "indicative, not evidence.",
            "",
        ]

    # ---- per grade
    lines += ["## By grade", "", "| Grade | n | Win rate | Avg R |", "| --- | ---: | ---: | ---: |"]
    grades: Dict[str, List[float]] = {}
    for grade, outcome in terminal:
        grades.setdefault(grade or "n/a", []).append(outcome)
    if grades:
        for grade in sorted(grades):
            values = grades[grade]
            grade_wins = sum(1 for r in values if r > 0)
            lines.append(
                f"| {grade} | {len(values)} | {grade_wins / len(values) * 100:.1f}% | "
                f"{sum(values) / len(values):.3f} |"
            )
    else:
        lines.append("| _no resolved signals_ | 0 | n/a | n/a |")
    lines.append("")

    # ---- dims
    lines += [
        "## Dims vs outcome (Spearman)",
        "",
        f"_Latest ranking: {data['dims_computed_at'].isoformat() if data['dims_computed_at'] else 'never computed'}_",
        "",
    ]
    dims = data["dims"]
    if dims:
        lines += ["| Rank | Dim | Spearman | n |", "| --- | --- | ---: | ---: |"]
        for position, (dim, spearman, n_samples) in enumerate(dims[:5], start=1):
            lines.append(f"| top {position} | `{dim}` | {float(spearman):+.4f} | {n_samples} |")
        for position, (dim, spearman, n_samples) in enumerate(reversed(dims[-5:]), start=1):
            lines.append(f"| bottom {position} | `{dim}` | {float(spearman):+.4f} | {n_samples} |")
    else:
        lines.append("_No dim rankings computed yet._")
    lines.append("")

    # ---- validator
    lines += ["## Validator rule activity", "", "| Rule | Result | Count |", "| --- | --- | ---: |"]
    if data["rule_counts"]:
        for rule_name, rule_result, count in data["rule_counts"]:
            lines.append(f"| {rule_name} | {rule_result} | {count} |")
    else:
        lines.append("| _no validator activity_ | — | 0 |")
    lines.append("")

    # ---- budget
    from core.state import STATE

    lines += [
        "## Budget",
        "",
        f"AI spend today (this process): ${STATE.budget_spent_today:.2f} of "
        f"${config.MAX_DAILY_COST:.2f} cap",
        "",
        "> Budget is tracked in-process, so a report generated by the learning "
        "loop sees only its own process's spend — not the analyst's. Durable "
        "cost accounting is not yet built.",
        "",
    ]

    # ---- pods
    lines += ["## Pods", "",
              "| pod | trades | wins | expectancy | wilson LB | cost drag | worst streak |",
              "|---|---|---|---|---|---|---|"]
    if data["pod_rows"]:
        for pod, n, wins, expectancy, wilson, drag, worst in data["pod_rows"]:
            lines.append(
                f"| {pod} | {int(n or 0)} | {int(wins or 0)} | "
                f"${_fmt(float(expectancy) if expectancy is not None else None, 4)} | "
                f"{_fmt(float(wilson) if wilson is not None else None, 3)} | "
                f"{_fmt(float(drag) if drag is not None else None, 1)}% | {int(worst or 0)} |"
            )
    else:
        lines.append("| _no pod stats computed yet_ | — | — | — | — | — | — |")
    lines.append("")

    # ---- doctrine accounting
    sources = data["doctrine_sources"]
    total_doctrines = sum(sources.values()) if sources else 0
    lines += ["## Doctrine", "",
              f"| Doctrines issued | {total_doctrines} |", "|---|---|"]
    for source in sorted(sources):
        lines.append(f"| {source} | {sources[source]} |")
    lines.append(f"| Avg conviction (FABLE) | {_fmt(data['doctrine_avg_conviction'], 1)} |")
    lines.append(f"| Horizon-hours on fallback | {_fmt(data['doctrine_fallback_pct'], 1)}% |")
    lines.append("")
    if (data["doctrine_fallback_pct"] or 0) > 50:
        lines += [
            "> More than half of this week's governed time ran on a FALLBACK "
            "doctrine. The desk was flat because nobody answered, not because "
            "it judged flat — those are different problems.",
            "",
        ]

    # ---- the narrative, last, and never load-bearing
    facts = "\n".join(lines)
    prose = write_prose(facts)
    if prose:
        lines += ["## Desk notes (Fable)", "", prose, ""]
    else:
        lines += [
            "## Desk notes: unavailable (API)",
            "",
            "The narrative call did not return. Every number above is unaffected — "
            "the report is computed from the database and the essayist is a "
            "commentator, not a source.",
            "",
        ]

    path = os.path.join(_reports_dir(), f"weekly-{now_utc.date().isoformat()}.md")
    return _atomic_write(path, "\n".join(lines))


# ==========================================================================
# scheduler
# ==========================================================================


def _seconds_until(now_utc: datetime, hour: int) -> float:
    target = now_utc.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now_utc:
        target += timedelta(days=1)
    return (target - now_utc).total_seconds()


def run_learning_loop() -> None:
    """
    Sleep until config.LEARN_NIGHTLY_UTC_HOUR, run the nightly job, and
    additionally the weekly report on config.LEARN_WEEKLY_DAY. Deliberately
    asyncio-free: a plain sleeping loop, matching every other NEXUS agent.
    Any failure is logged and the loop continues (INVARIANT 6).
    """
    logger.info(
        "learning loop starting: nightly at %02d:00 UTC, weekly on weekday %d",
        config.LEARN_NIGHTLY_UTC_HOUR,
        config.LEARN_WEEKLY_DAY,
    )
    while True:
        try:
            now = datetime.now(timezone.utc)
            time.sleep(_seconds_until(now, config.LEARN_NIGHTLY_UTC_HOUR))

            now = datetime.now(timezone.utc)
            with database.get_conn() as conn:
                summary = nightly_job(conn, now)
                logger.info("learning loop: nightly complete %s", summary.get("steps"))
                if now.weekday() == config.LEARN_WEEKLY_DAY:
                    path = weekly_report(conn, now)
                    logger.info("learning loop: weekly report written to %s", path)
        except Exception:
            logger.exception("run_learning_loop: cycle failed; continuing")
            time.sleep(60)  # don't spin on a persistent failure


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS learning loop")
    parser.add_argument("--nightly", action="store_true", help="Run the nightly job once, now")
    parser.add_argument("--weekly", action="store_true", help="Write the weekly report once, now")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not (args.nightly or args.weekly):
        parser.error("nothing to do; pass --nightly and/or --weekly")

    now = datetime.now(timezone.utc)
    with database.get_conn() as conn:
        if args.nightly:
            print(json.dumps(nightly_job(conn, now), indent=2, default=str))
        if args.weekly:
            print(weekly_report(conn, now))


if __name__ == "__main__":
    main()
