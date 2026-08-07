"""
The promotion pack — the document that makes lying to yourself impossible.

THIS MODULE CANNOT PROMOTE ANYTHING. It reads the database, measures the
system against config.PROMOTION_CRITERIA, and renders a verdict. Promotion
itself remains what it has always been: a human editing a config value and
restarting the process. There is deliberately no function here that changes a
stage, and INVARIANT 1 means there could not be one even if this module wanted
it.

WHY BOTHER, THEN. Because the failure mode this guards against is not
technical. It is the moment — after a good week, on a Friday, with the market
doing something interesting — when the operator decides the criteria were
always a bit conservative. Numbers fixed in advance and printed as a table
cannot be renegotiated by whoever is impatient at the time. The report exists
to be argued with, and losing that argument to your own earlier judgement is
the entire mechanism.

FAIL CLOSED ON ABSENT EVIDENCE. A criterion that cannot be computed FAILS with
"insufficient data". It does not skip, warn, or pass with a caveat. You cannot
promote on evidence you do not have, and a missing measurement is the most
common way a system looks ready when it is not.

THE KILL DRILL IS A CRITERION. An untested kill switch fails you outright,
whatever the P&L says. A safety mechanism nobody has pulled is a belief, not a
control, and the whole ladder is built on being able to stop.

ELIGIBILITY IS NOT PROMOTION. Even a clean sweep prints a cooling-off
requirement, because the gap between "the numbers allow it" and "I am doing it"
is where the last mistake gets caught.
"""
import argparse
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

# CLI-only: `python3 -m ops.promotion` is a standalone entry point, so .env
# must load BEFORE `import config` below reads the environment.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database

logger = logging.getLogger(__name__)

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

INSUFFICIENT = "insufficient data"

EPILOGUE = (
    "Eligibility is not promotion. Announce the intent to the architect, wait "
    f"{config.COOLING_OFF_HOURS} hours, then make the config edit and restart. "
    "The wait is the control."
)


# ==========================================================================
# measurements — each returns (actual, note) with actual=None meaning unknown
# ==========================================================================


def _scalar(conn, sql: str, params=()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None


def current_stage(conn) -> Optional[str]:
    """
    The stage the most recent boot actually adopted, read from stage_events.

    Deliberately NOT read from the stage module or the environment: this file
    must contain no path that touches the live stage mechanism, and the boot
    record is the more honest source anyway — it says what the process did,
    not what a variable currently says.
    """
    return _scalar(
        conn,
        "SELECT effective_stage FROM stage_events WHERE event = 'BOOT' "
        "ORDER BY id DESC LIMIT 1",
    )


def days_at_stage(conn, stage: str, now_utc: datetime) -> Optional[float]:
    """
    Days since the CONTIGUOUS run at `stage` began.

    Contiguous matters: a system that ran at SHADOW, dropped to PAPER, and
    returned has not been at SHADOW for the whole span, and counting from the
    first-ever boot would hand it credit for time it did not serve.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, effective_stage FROM stage_events WHERE event = 'BOOT' "
            "ORDER BY id DESC"
        )
        rows = cur.fetchall()

    if not rows:
        return None

    started_at = None
    for ts, effective in rows:
        if effective != stage:
            break
        started_at = ts

    if started_at is None:
        return 0.0
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - started_at).total_seconds() / 86400.0)


def stage_span_days(conn, stage: str, now_utc: datetime) -> Optional[float]:
    """
    Days since the FIRST-EVER boot at `stage`, ignoring interruptions.

    Only ever compared against the contiguous figure to detect a fragmented
    record — never used as the criterion itself, because uninterrupted time is
    the thing being demanded.
    """
    first = _scalar(
        conn,
        "SELECT min(ts) FROM stage_events WHERE event = 'BOOT' AND effective_stage = %s",
        (stage,),
    )
    if first is None:
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - first).total_seconds() / 86400.0)


def pod_trade_count(conn) -> int:
    return int(
        _scalar(
            conn,
            "SELECT count(*) FROM positions WHERE source = 'POD' AND state = 'CLOSED'",
        )
        or 0
    )


def swing_signal_count(conn) -> int:
    return int(_scalar(conn, "SELECT count(*) FROM signals") or 0)


def fallback_pct(conn, now_utc: datetime) -> Optional[float]:
    """
    Horizon-weighted share of governed time spent on a fallback doctrine.

    Reuses fusion/learning_loop._collect_weekly rather than reimplementing the
    weighting — one definition of "how much of the time was the desk flying on
    a fallback", so the promotion pack and the weekly report can never disagree
    about it. The window is the weekly report's (config.LEARN_REPORT_WINDOW_DAYS).
    """
    try:
        from fusion.learning_loop import _collect_weekly

        return _collect_weekly(conn, now_utc)["doctrine_fallback_pct"]
    except Exception:
        logger.warning("promotion: fallback share unavailable", exc_info=True)
        return None


def pod_stat_rows(conn) -> List[Dict[str, Any]]:
    """Newest pod_stats row per pod that has actually traded."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (pod) pod, n_trades, wins, expectancy_usd, wilson_lb, "
            "cost_drag_pct FROM pod_stats ORDER BY pod, computed_at DESC"
        )
        rows = cur.fetchall()

    return [
        {
            "pod": r[0],
            "n_trades": int(r[1] or 0),
            "wins": int(r[2] or 0),
            "expectancy_usd": float(r[3]) if r[3] is not None else None,
            "wilson_lb": float(r[4]) if r[4] is not None else None,
            "cost_drag_pct": float(r[5]) if r[5] is not None else None,
        }
        for r in rows
        if int(r[1] or 0) > 0
    ]


def worst_pod(rows: List[Dict[str, Any]], key: str, lower_is_better: bool = False):
    """The pod dragging the aggregate down — named so it can be fixed."""
    candidates = [r for r in rows if r.get(key) is not None]
    if not candidates:
        return None
    return (max if lower_is_better else min)(candidates, key=lambda r: r[key])


def aggregate_wilson(rows: List[Dict[str, Any]]) -> Optional[float]:
    """
    Trade-weighted Wilson lower bound across pods.

    Weighted by trades so a pod with four trades and a lucky run cannot lift
    the desk's number above what the evidence supports.
    """
    usable = [r for r in rows if r["wilson_lb"] is not None and r["n_trades"] > 0]
    if not usable:
        return None
    total = sum(r["n_trades"] for r in usable)
    return sum(r["wilson_lb"] * r["n_trades"] for r in usable) / total


def aggregate_expectancy(rows: List[Dict[str, Any]]) -> Optional[float]:
    usable = [r for r in rows if r["expectancy_usd"] is not None and r["n_trades"] > 0]
    if not usable:
        return None
    total = sum(r["n_trades"] for r in usable)
    return sum(r["expectancy_usd"] * r["n_trades"] for r in usable) / total


def aggregate_cost_drag(rows: List[Dict[str, Any]]) -> Optional[float]:
    usable = [r for r in rows if r["cost_drag_pct"] is not None and r["n_trades"] > 0]
    if not usable:
        return None
    total = sum(r["n_trades"] for r in usable)
    return sum(r["cost_drag_pct"] * r["n_trades"] for r in usable) / total


def max_drawdown_pct(conn) -> Optional[float]:
    """
    Peak-to-trough drawdown of a DAILY EQUITY WALK over realised P&L.

    APPROXIMATION, and the direction of the error matters. Equity is
    reconstructed by accumulating realised P&L per closed day onto
    config.ACCOUNT_SIZE, so it cannot see INTRADAY excursions or open-position
    mark-to-market — the real drawdown is therefore at least this bad and
    probably worse. It stands in until the broker supplies genuine equity
    history on the Windows box, at which point this function should be
    replaced rather than adjusted.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (closed_at AT TIME ZONE 'UTC')::date AS d, "
            "COALESCE(SUM(realized_pnl_usd), 0) FROM positions "
            "WHERE state = 'CLOSED' AND closed_at IS NOT NULL AND realized_pnl_usd IS NOT NULL "
            "GROUP BY d ORDER BY d"
        )
        rows = cur.fetchall()

    if not rows:
        return None

    equity = float(config.ACCOUNT_SIZE)
    peak = equity
    worst = 0.0
    for _day, pnl in rows:
        equity += float(pnl)
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak * 100.0)
    return worst


def reconciliation_breaches(conn, days: int, now_utc: datetime) -> int:
    since = now_utc - timedelta(days=days)
    return int(
        _scalar(
            conn,
            "SELECT count(*) FROM kernel_events WHERE breaker = 'RECONCILIATION' AND ts >= %s",
            (since,),
        )
        or 0
    )


def days_since_kill_drill(conn, now_utc: datetime) -> Optional[float]:
    """
    Days since a /killswitch was actually CONFIRMED from Telegram.

    CONFIRMED, not merely sent: arming a confirmation proves nothing about
    whether the file gets written and the kernel reacts. The drill is a
    promotion criterion because a safety mechanism nobody has pulled is a
    belief, not a control.
    """
    last = _scalar(
        conn,
        "SELECT max(ts) FROM command_audit WHERE command = '/killswitch' "
        "AND outcome = 'CONFIRMED'",
    )
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - last).total_seconds() / 86400.0)


# ==========================================================================
# criteria
# ==========================================================================


def _row(name, required, actual, status, note="", earliest=None) -> Dict[str, Any]:
    return {
        "criterion": name,
        "required": required,
        "actual": actual,
        "status": status,
        "note": note,
        "earliest": earliest,
    }


def _min_count(name, required, actual, unit="", rate_per_day=None, now_utc=None):
    """A "you need at least N" criterion, with an earliest-date where derivable."""
    if actual is None:
        return _row(name, f">= {required}", INSUFFICIENT, FAIL, INSUFFICIENT)
    ok = actual >= required
    earliest = None
    if not ok and rate_per_day and now_utc:
        missing = required - actual
        earliest = (now_utc + timedelta(days=missing / rate_per_day)).date().isoformat()
    return _row(
        name, f">= {required}", f"{actual}{unit}", PASS if ok else FAIL, earliest=earliest
    )


def _min_days(name, required, actual, now_utc):
    if actual is None:
        return _row(name, f">= {required}d", INSUFFICIENT, FAIL, INSUFFICIENT)
    ok = actual >= required
    earliest = None
    if not ok:
        # Pure arithmetic: days accrue at one per day.
        earliest = (now_utc + timedelta(days=required - actual)).date().isoformat()
    return _row(
        name, f">= {required}d", f"{actual:.1f}d", PASS if ok else FAIL, earliest=earliest
    )


def _max_pct(name, required, actual):
    if actual is None:
        return _row(name, f"<= {required}%", INSUFFICIENT, FAIL, INSUFFICIENT)
    ok = actual <= required
    return _row(name, f"<= {required}%", f"{actual:.1f}%", PASS if ok else FAIL)


def _min_ratio(name, required, actual):
    if actual is None:
        return _row(name, f">= {required}", INSUFFICIENT, FAIL, INSUFFICIENT)
    ok = actual >= required
    return _row(name, f">= {required}", f"{actual:.3f}", PASS if ok else FAIL)


def evaluate(conn, target_stage: str, now_utc: datetime) -> Dict[str, Any]:
    """
    Measure the system against the criteria for `target_stage`.

    Returns a dict carrying every criterion row, the pod detail, and the
    verdict. Computes nothing about stages beyond reading their history, and
    changes nothing anywhere.
    """
    target = str(target_stage).upper()
    criteria = config.PROMOTION_CRITERIA.get(target)
    if criteria is None:
        raise SystemExit(
            f"unknown target stage {target_stage!r}; "
            f"expected one of {sorted(config.PROMOTION_CRITERIA)}"
        )

    stage_now = current_stage(conn)
    pods = pod_stat_rows(conn)
    rows: List[Dict[str, Any]] = []

    # Each criterion is computed only if the target asks for it, so the table
    # shows exactly what this rung requires and nothing else.
    if "min_pod_trades" in criteria:
        rows.append(_min_count("min_pod_trades", criteria["min_pod_trades"], pod_trade_count(conn)))
    if "min_swing_signals" in criteria:
        rows.append(
            _min_count("min_swing_signals", criteria["min_swing_signals"], swing_signal_count(conn))
        )
    if "min_soak_days" in criteria:
        contiguous = days_at_stage(conn, stage_now, now_utc) if stage_now else None
        soak_row = _min_days("min_soak_days", criteria["min_soak_days"], contiguous, now_utc)
        span = stage_span_days(conn, stage_now, now_utc) if stage_now else None
        # A contiguous run far shorter than the total history at this stage
        # means the boot record is FRAGMENTED — interleaved boots at other
        # stages. Report it rather than quietly serving a 0: a promotion
        # decision taken from a corrupted record is exactly the self-deception
        # this module exists to prevent.
        if contiguous is not None and span and span - contiguous > 1.0:
            soak_row["note"] = (
                f"boot record is FRAGMENTED — {span:.1f}d since the first boot at "
                f"{stage_now} but only {contiguous:.1f}d uninterrupted; boots at other "
                "stages are interleaved. Verify nothing but the live system is writing "
                "stage_events before trusting this row."
            )
        rows.append(soak_row)
    if "min_shadow_days" in criteria:
        rows.append(
            _min_days("min_shadow_days", criteria["min_shadow_days"],
                      days_at_stage(conn, "SHADOW", now_utc), now_utc)
        )
    if "min_micro_days" in criteria:
        rows.append(
            _min_days("min_micro_days", criteria["min_micro_days"],
                      days_at_stage(conn, "MICRO", now_utc), now_utc)
        )
    if "max_fallback_pct" in criteria:
        rows.append(_max_pct("max_fallback_pct", criteria["max_fallback_pct"], fallback_pct(conn, now_utc)))
    if "wilson_lb_floor" in criteria:
        rows.append(_min_ratio("wilson_lb_floor", criteria["wilson_lb_floor"], aggregate_wilson(pods)))
    if "cost_drag_max_pct" in criteria:
        rows.append(_max_pct("cost_drag_max_pct", criteria["cost_drag_max_pct"], aggregate_cost_drag(pods)))
    if "max_drawdown_pct" in criteria:
        rows.append(_max_pct("max_drawdown_pct", criteria["max_drawdown_pct"], max_drawdown_pct(conn)))
    if "expectancy_positive" in criteria:
        expectancy = aggregate_expectancy(pods)
        if expectancy is None:
            rows.append(_row("expectancy_positive", "> 0", INSUFFICIENT, FAIL, INSUFFICIENT))
        else:
            rows.append(
                _row("expectancy_positive", "> 0", f"${expectancy:+.4f}",
                     PASS if expectancy > 0 else FAIL)
            )
    if "reconcile_clean_days" in criteria:
        days = criteria["reconcile_clean_days"]
        breaches = reconciliation_breaches(conn, days, now_utc)
        rows.append(
            _row("reconcile_clean_days", f"0 breaches in {days}d", f"{breaches} breaches",
                 PASS if breaches == 0 else FAIL)
        )
    if "kill_drill_within_days" in criteria:
        limit = criteria["kill_drill_within_days"]
        since = days_since_kill_drill(conn, now_utc)
        if since is None:
            rows.append(
                _row("kill_drill_within_days", f"<= {limit}d ago",
                     "never drilled", FAIL,
                     "an untested kill switch is a belief, not a control")
            )
        else:
            rows.append(
                _row("kill_drill_within_days", f"<= {limit}d ago", f"{since:.1f}d ago",
                     PASS if since <= limit else FAIL)
            )

    failures = [r for r in rows if r["status"] == FAIL]
    return {
        "target": target,
        "current_stage": stage_now,
        "now": now_utc,
        "rows": rows,
        "failures": failures,
        "eligible": not failures,
        "pods": pods,
        "worst_wilson": worst_pod(pods, "wilson_lb"),
        "worst_expectancy": worst_pod(pods, "expectancy_usd"),
        "worst_drag": worst_pod(pods, "cost_drag_pct", lower_is_better=True),
    }


# ==========================================================================
# rendering
# ==========================================================================


def render(result: Dict[str, Any]) -> str:
    eligible = result["eligible"]
    banner = "ELIGIBLE" if eligible else "NOT ELIGIBLE"

    lines = [
        f"# Promotion pack — {result['current_stage'] or 'UNKNOWN'} → {result['target']}",
        "",
        f"Generated {result['now'].isoformat()}",
        "",
        f"## Verdict: {banner}",
        "",
    ]

    lines += [
        "| Criterion | Required | Actual | |",
        "| --- | --- | --- | --- |",
    ]
    for row in result["rows"]:
        mark = {PASS: "PASS", FAIL: "**FAIL**", INFO: "info"}[row["status"]]
        lines.append(f"| `{row['criterion']}` | {row['required']} | {row['actual']} | {mark} |")
    lines.append("")

    # Per-pod detail: an aggregate that fails should name the pod responsible,
    # because "the desk is unprofitable" is not an actionable sentence.
    if result["pods"]:
        lines += ["## Pod detail", "",
                  "| pod | trades | wilson LB | expectancy | cost drag |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for pod in result["pods"]:
            lines.append(
                f"| {pod['pod']} | {pod['n_trades']} | "
                f"{pod['wilson_lb'] if pod['wilson_lb'] is not None else 'n/a'} | "
                f"{pod['expectancy_usd'] if pod['expectancy_usd'] is not None else 'n/a'} | "
                f"{pod['cost_drag_pct'] if pod['cost_drag_pct'] is not None else 'n/a'} |"
            )
        lines.append("")
        for label, key in (
            ("Weakest Wilson LB", "worst_wilson"),
            ("Weakest expectancy", "worst_expectancy"),
            ("Heaviest cost drag", "worst_drag"),
        ):
            pod = result[key]
            if pod:
                lines.append(f"- {label}: **{pod['pod']}** ({pod['n_trades']} trades)")
        lines.append("")
    else:
        lines += ["## Pod detail", "",
                  "_No pod has traded yet, so there is nothing to rank._", ""]

    if eligible:
        lines += ["## What happens now", "", EPILOGUE, ""]
    else:
        lines += ["## What is missing", ""]
        for row in result["failures"]:
            detail = f"- **{row['criterion']}**: needs {row['required']}, has {row['actual']}"
            if row["note"]:
                detail += f" — {row['note']}"
            if row["earliest"]:
                detail += f" — earliest possible: **{row['earliest']}**"
            lines.append(detail)
        lines += [
            "",
            "Dates above assume the current rate continues and nothing regresses. "
            "They are the EARLIEST a criterion could clear, not a forecast.",
            "",
        ]

    lines += [
        "---",
        "",
        "This report changes nothing. Promotion is a human config edit plus a "
        "restart; no code in NEXUS can move a stage.",
        "",
    ]
    return "\n".join(lines)


def _atomic_write(path: str, text: str) -> str:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".promotion.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def write_report(result: Dict[str, Any]) -> str:
    path = os.path.join(
        config.REPORTS_DIR,
        f"promotion-{result['target']}-{result['now'].date().isoformat()}.md",
    )
    return _atomic_write(path, render(result))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="NEXUS promotion pack — have you earned the next rung?"
    )
    parser.add_argument("--target", required=True, help="SHADOW, MICRO or SCALED")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")

    now = datetime.now(timezone.utc)
    with database.get_conn() as conn:
        result = evaluate(conn, args.target, now)

    report = render(result)
    path = write_report(result)
    print()
    print(report)
    print(f"(written to {path})")


if __name__ == "__main__":
    main()
