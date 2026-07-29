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

    return {
        "window_start": window_start,
        "issued": issued,
        "terminal": terminal,
        "rule_counts": rule_counts,
        "dims": dims,
        "dims_computed_at": latest,
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
