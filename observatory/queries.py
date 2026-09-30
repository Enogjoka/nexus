"""
The observatory's read model. Every statement is a parameterized SELECT bounded
by LIMIT; every function takes the clock as `now` so tests are deterministic.

Honesty rules carried over from the Phase 1 verification:
  * Missing pieces are returned as null plus a note in "missing", never faked
    (F-40: some signals have no validator rows).
  * signals.filled_at and outcome_ts are H1 bar timestamps, not wall-clock
    fill or close times (F-39), and are labelled that way.
  * A PARSE_FALLBACK doctrine with no raw response is an API failure, not a
    parse failure (F-37), and is flagged "api_failed".
  * Performance is reported per row AND per decision; rows from one thesis
    are not independent (F-07), and small samples say so.
  * cot_reports, econ_events and news_articles have no ts column (F-44);
    sensor freshness comes from the heartbeat, which measures fetched_at.
"""
import math
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .db import ReadOnlyDB

# Mirrors config.YF_SYMBOL in the trading repo, which the observatory must not
# import. The candles table stores the futures series under this label.
DEFAULT_SYMBOL = "GC=F"

TIMEFRAMES = ("1h", "4h", "1d")
WINDOWS = {"day": timedelta(days=1), "week": timedelta(days=7), "all": None}

BAR_TIME_NOTE = (
    "H1 bar open time on which the paper engine detected the event, not a "
    "wall-clock time (plan F-39)"
)
KILL_SWITCH_NOTE = (
    "The kill switch is a flag file on the trading host, not a database row; "
    "the observatory cannot read it."
)
SMALL_SAMPLE_DECISIONS = 100
WILSON_Z = 1.96

_ROW_LIMIT = 1000
_PERF_LIMIT = 10000


# ==========================================================================
# helpers
# ==========================================================================


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def wilson(successes: int, n: int, z: float = WILSON_Z) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for successes/n; (None, None) when n == 0."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom, (centre + margin) / denom


def _doctrine_view(row: Optional[Dict[str, Any]], at: datetime) -> Optional[Dict[str, Any]]:
    """Shape one doctrines row, with its expiry evaluated at `at`."""
    if row is None:
        return None
    issued = _parse_ts(row["ts"])
    horizon = row.get("review_horizon_min")
    expires_at = issued + timedelta(minutes=horizon) if issued and horizon is not None else None
    return {
        "id": row["id"],
        "ts": row["ts"],
        "bias": row["bias"],
        "conviction": row["conviction"],
        "risk_multiplier": row["risk_multiplier"],
        "enabled_pods": row["enabled_pods"] or [],
        "swing_signals_allowed": row["swing_signals_allowed"],
        "source": row["source"],
        "no_trade_reason": row["no_trade_reason"],
        "review_horizon_min": horizon,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "seconds_to_expiry": int((expires_at - at).total_seconds()) if expires_at else None,
        "expired": bool(expires_at and at > expires_at),
        # F-37: the doctrine agent records a failed model call as
        # PARSE_FALLBACK with no raw response.
        "api_failed": row["source"] == "PARSE_FALLBACK" and bool(row["raw_missing"]),
    }


_DOCTRINE_COLUMNS = (
    "id, ts, bias, conviction, risk_multiplier, enabled_pods, swing_signals_allowed, "
    "source, no_trade_reason, review_horizon_min, "
    "(raw_response IS NULL OR raw_response = '') AS raw_missing"
)

_HEARTBEAT_COLUMNS = (
    "id, ts, stage, alive, registered, dead, rss_mb, last_market_update_at, "
    "model_ok_at, doctrine_bias, doctrine_source, doctrine_age_s, budget_today_usd, sensors"
)


def _latest_heartbeat(db: ReadOnlyDB) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not db.table_exists("ops_heartbeats"):
        return None, "ops_heartbeats table not present (migration 017 not applied on this database)"
    row = db.one(f"SELECT {_HEARTBEAT_COLUMNS} FROM ops_heartbeats ORDER BY id DESC LIMIT 1")
    if row is None:
        return None, "ops_heartbeats is empty (the trading process has not written a heartbeat yet)"
    return row, None


# ==========================================================================
# /state
# ==========================================================================


def state(db: ReadOnlyDB, now: datetime) -> Dict[str, Any]:
    missing: Dict[str, str] = {}

    boot = db.one(
        "SELECT id, ts, event, env_stage, effective_stage, reason FROM stage_events "
        "WHERE ts <= %s ORDER BY ts DESC, id DESC LIMIT 1",
        (now,),
    )
    heartbeat, heartbeat_missing = _latest_heartbeat(db)
    if heartbeat_missing:
        missing["heartbeat"] = heartbeat_missing

    doctrine_row = db.one(f"SELECT {_DOCTRINE_COLUMNS} FROM doctrines ORDER BY ts DESC, id DESC LIMIT 1")
    if doctrine_row is None:
        missing["doctrine"] = "no doctrines rows"

    heartbeat_view = None
    freshness = None
    if heartbeat is not None:
        heartbeat_age = int((now - _parse_ts(heartbeat["ts"])).total_seconds())
        heartbeat_view = {
            "ts": heartbeat["ts"],
            "age_s": heartbeat_age,
            "stage": heartbeat["stage"],
            "alive": heartbeat["alive"],
            "registered": heartbeat["registered"],
            "dead": heartbeat["dead"],
            "rss_mb": heartbeat["rss_mb"],
            "last_market_update_at": heartbeat["last_market_update_at"],
            "model_ok_at": heartbeat["model_ok_at"],
            "budget_today_usd": heartbeat["budget_today_usd"],
        }
        freshness = freshness_from_heartbeat(heartbeat, now)
    else:
        missing["freshness"] = "sensor freshness comes from ops_heartbeats (F-44)"

    return {
        "as_of": now.isoformat(),
        "stage": {
            "heartbeat": heartbeat["stage"] if heartbeat else None,
            "last_stage_event": boot,
            "note": "the live stage is the heartbeat's; stage_events records boots and demotions",
        },
        "doctrine": _doctrine_view(doctrine_row, now),
        "heartbeat": heartbeat_view,
        "freshness": freshness,
        "kill_switch": {"state": "unknown", "note": KILL_SWITCH_NOTE},
        "missing": missing or None,
    }


def freshness_from_heartbeat(heartbeat: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """
    Per-sensor age from the heartbeat's `sensors` map, re-aged to `now` by the
    heartbeat's own age. Null when the heartbeat could not measure a sensor.
    """
    heartbeat_age = (now - _parse_ts(heartbeat["ts"])).total_seconds()
    sensors = heartbeat.get("sensors") or {}
    return {
        name: {
            "age_at_heartbeat_s": age,
            "age_now_s": int(age + heartbeat_age) if isinstance(age, (int, float)) else None,
        }
        for name, age in sorted(sensors.items())
    }


def freshness(db: ReadOnlyDB, now: datetime) -> Dict[str, Any]:
    heartbeat, heartbeat_missing = _latest_heartbeat(db)
    if heartbeat is None:
        return {"sensors": None, "missing": heartbeat_missing}
    return {"sensors": freshness_from_heartbeat(heartbeat, now), "heartbeat_ts": heartbeat["ts"], "missing": None}


# ==========================================================================
# /trades
# ==========================================================================


def trades(db: ReadOnlyDB, now: datetime, days: int) -> Dict[str, Any]:
    since = now - timedelta(days=days)
    swing_rows = db.rows(
        "SELECT id, ts, symbol, direction, grade, confidence, status, lots, prices, "
        "filled_at, outcome_ts, outcome_r, mae_r, mfe_r, left(thesis, 300) AS thesis, "
        "market_snapshot->>'doctrine_bias' AS doctrine_bias, "
        "market_snapshot->>'risk_multiplier' AS doctrine_risk_multiplier "
        "FROM signals WHERE ts >= %s AND ts <= %s ORDER BY ts DESC, id DESC LIMIT %s",
        (since, now, _ROW_LIMIT),
    )
    swing = []
    for row in swing_rows:
        prices = row.pop("prices") or {}
        row["entry"] = _num(prices.get("entry"))
        row["stop"] = _num(prices.get("stop"))
        row["tp1"] = _num(prices.get("tp1"))
        row["tp2"] = _num(prices.get("tp2"))
        row["bar_time"] = row.pop("filled_at")
        row["close_bar_time"] = row.pop("outcome_ts")
        row["doctrine_risk_multiplier"] = _num(row["doctrine_risk_multiplier"])
        swing.append(row)

    router = db.rows(
        "SELECT id, client_order_id, source, pod, direction, lots, entry_px, stop_px, "
        "tp1_px, tp2_px, state, opened_at, closed_at, realized_pnl_usd, close_reason, created_at "
        "FROM positions WHERE created_at >= %s AND created_at <= %s "
        "ORDER BY created_at DESC, id DESC LIMIT %s",
        (since, now, _ROW_LIMIT),
    )
    return {
        "window_days": days,
        "since": since.isoformat(),
        "until": now.isoformat(),
        "swing": {
            "book": "swing: analyst signals managed by the paper engine (signals table)",
            "count": len(swing),
            "truncated": len(swing) >= _ROW_LIMIT,
            "rows": swing,
        },
        "router": {
            "book": "router: pod orders through router -> kernel -> bridge (positions table)",
            "count": len(router),
            "truncated": len(router) >= _ROW_LIMIT,
            "rows": router,
        },
        "notes": {
            "bar_time": BAR_TIME_NOTE,
            "close_bar_time": BAR_TIME_NOTE,
            "thesis": "first 300 characters",
        },
    }


# ==========================================================================
# /trace/signal/{id}
# ==========================================================================


def trace_signal(db: ReadOnlyDB, signal_id: int) -> Optional[Dict[str, Any]]:
    """
    One signal and everything that correlates with it. None when the signal
    does not exist. Each missing piece is null with a reason in "missing".
    """
    signal = db.one(
        "SELECT id, ts, symbol, direction, grade, confidence, status, lots, anchors, prices, "
        "filled_at, outcome_ts, outcome_r, outcome_pips, mae_r, mfe_r, thesis, execution_mode, "
        "market_snapshot->>'doctrine_ts' AS snapshot_doctrine_ts, "
        "market_snapshot->>'doctrine_bias' AS snapshot_doctrine_bias, "
        "market_snapshot->>'risk_multiplier' AS snapshot_risk_multiplier "
        "FROM signals WHERE id = %s LIMIT 1",
        (signal_id,),
    )
    if signal is None:
        return None
    signal["bar_time"] = signal.pop("filled_at")
    signal["close_bar_time"] = signal.pop("outcome_ts")
    signal_ts = _parse_ts(signal["ts"])
    missing: Dict[str, str] = {}

    # The correlation key verified in Phase 1: the cycle's validator_log rows
    # carry exactly the signal's ts.
    validator = db.rows(
        "SELECT id, ts, symbol, rule_name, rule_result, details FROM validator_log "
        "WHERE ts = %s ORDER BY id LIMIT 50",
        (signal_ts,),
    )
    if not validator:
        validator = None
        missing["validator"] = (
            "no validator_log rows share this signal's ts; the audit write failed or "
            "was never made (plan F-40)"
        )

    doctrine_row = db.one(
        f"SELECT {_DOCTRINE_COLUMNS} FROM doctrines WHERE ts <= %s ORDER BY ts DESC, id DESC LIMIT 1",
        (signal_ts,),
    )
    doctrine = _doctrine_view(doctrine_row, signal_ts)
    if doctrine is None:
        missing["doctrine"] = "no doctrine was issued at or before this signal"

    fills = db.rows(
        "SELECT id, ts, client_order_id, direction, lots, requested_px, fill_px, slippage, "
        "spread_at_send, fill_mode, status, kernel_reason FROM fills "
        "WHERE signal_id = %s ORDER BY id LIMIT 50",
        (signal_id,),
    )
    client_order_ids = sorted({f["client_order_id"] for f in fills if f.get("client_order_id")})
    kernel = None
    if client_order_ids:
        kernel = db.rows(
            "SELECT id, ts, breaker, action, reason, context FROM kernel_events "
            "WHERE context->>'client_order_id' = ANY(%s) ORDER BY id LIMIT 200",
            (client_order_ids,),
        )
        if not kernel:
            kernel = None
            missing["kernel_events"] = "no kernel_events mention this signal's client_order_id"
    else:
        missing["kernel_events"] = (
            "no order references this signal: the swing book is filled by the paper "
            "engine outside the router and kernel, so there is no client_order_id (plan F-02)"
        )

    return {
        "signal": signal,
        "correlation": {"key": "ts", "value": signal["ts"]},
        "validator": validator,
        "doctrine_in_force": doctrine,
        "fills": fills or None,
        "client_order_ids": client_order_ids or None,
        "kernel_events": kernel,
        "missing": missing or None,
        "notes": {"bar_time": BAR_TIME_NOTE, "close_bar_time": BAR_TIME_NOTE},
    }


# ==========================================================================
# /candles
# ==========================================================================


def candles(db: ReadOnlyDB, now: datetime, tf: str, days: int, symbol: str = DEFAULT_SYMBOL) -> Dict[str, Any]:
    if tf not in TIMEFRAMES:
        raise ValueError(f"tf must be one of {TIMEFRAMES}")
    since = now - timedelta(days=days)
    rows = db.rows(
        "SELECT ts, open, high, low, close, volume, is_anomaly FROM candles "
        "WHERE symbol = %s AND timeframe = %s AND ts >= %s AND ts <= %s ORDER BY ts LIMIT 5000",
        (symbol, tf, since, now),
    )
    return {"symbol": symbol, "tf": tf, "since": since.isoformat(), "count": len(rows), "candles": rows}


# ==========================================================================
# /perf
# ==========================================================================


def compute_perf(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Rows and decisions side by side, from closed swing signals.

    A decision is one (fill bar, direction) group: signals that filled on the
    same H1 bar in the same direction are one bet cloned N times (F-03/F-07).
    A decision's R is the mean R of its rows; it wins when that mean is > 0.
    """
    closed = [r for r in rows if _num(r.get("outcome_r")) is not None]
    rs = [_num(r["outcome_r"]) for r in closed]
    wins = sum(1 for r in rs if r > 0)
    low, high = wilson(wins, len(rs))

    groups: Dict[Tuple[Any, Any], List[float]] = {}
    for row in closed:
        key = (row.get("filled_at") or f"unfilled:{row.get('id')}", row.get("direction"))
        groups.setdefault(key, []).append(_num(row["outcome_r"]))
    decision_rs = [sum(v) / len(v) for v in groups.values()]
    decision_wins = sum(1 for r in decision_rs if r > 0)
    d_low, d_high = wilson(decision_wins, len(decision_rs))

    return {
        "rows": {
            "label": "rows: every closed signal counted separately (not independent)",
            "n": len(rs),
            "r_sum": round(sum(rs), 4),
            "wins": wins,
            "win_rate": wins / len(rs) if rs else None,
            "wilson95": [low, high],
            "expectancy_r": sum(rs) / len(rs) if rs else None,
        },
        "decisions": {
            "label": "decisions: distinct (fill bar, direction) groups; mean R per group",
            "n": len(decision_rs),
            "r_sum": round(sum(decision_rs), 4),
            "wins": decision_wins,
            "win_rate": decision_wins / len(decision_rs) if decision_rs else None,
            "wilson95": [d_low, d_high],
        },
        "statistically_meaningful": len(decision_rs) >= SMALL_SAMPLE_DECISIONS,
        "note": (
            f"{len(decision_rs)} independent decisions (fewer than {SMALL_SAMPLE_DECISIONS}): "
            "not statistically meaningful"
            if len(decision_rs) < SMALL_SAMPLE_DECISIONS
            else None
        ),
    }


def perf(db: ReadOnlyDB, now: datetime, window: str, symbol: Optional[str] = None) -> Dict[str, Any]:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {tuple(WINDOWS)}")
    span = WINDOWS[window]
    since = now - span if span is not None else None

    clauses = ["outcome_r IS NOT NULL", "outcome_ts <= %s"]
    params: List[Any] = [now]
    if since is not None:
        clauses.append("outcome_ts >= %s")
        params.append(since)
    if symbol is not None:
        clauses.append("symbol = %s")
        params.append(symbol)
    rows = db.rows(
        "SELECT id, direction, filled_at, outcome_r FROM signals WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id LIMIT %s",
        (*params, _PERF_LIMIT),
    )

    expired_clauses = ["status = 'EXPIRED'", "outcome_ts <= %s"]
    expired_params: List[Any] = [now]
    if since is not None:
        expired_clauses.append("outcome_ts >= %s")
        expired_params.append(since)
    if symbol is not None:
        expired_clauses.append("symbol = %s")
        expired_params.append(symbol)
    expired = db.one(
        "SELECT count(1) AS n FROM signals WHERE " + " AND ".join(expired_clauses) + " LIMIT 1",
        tuple(expired_params),
    )

    router_clauses = ["closed_at IS NOT NULL", "closed_at <= %s"]
    router_params: List[Any] = [now]
    if since is not None:
        router_clauses.append("closed_at >= %s")
        router_params.append(since)
    router = db.one(
        "SELECT count(1) AS n, coalesce(sum(realized_pnl_usd), 0) AS pnl_usd, "
        "count(1) FILTER (WHERE realized_pnl_usd > 0) AS wins FROM positions WHERE "
        + " AND ".join(router_clauses)
        + " LIMIT 1",
        tuple(router_params),
    ) or {"n": 0, "pnl_usd": 0.0, "wins": 0}
    r_low, r_high = wilson(router["wins"], router["n"])

    return {
        "window": window,
        "since": since.isoformat() if since else None,
        "until": now.isoformat(),
        "swing": {
            **compute_perf(rows),
            "expired_unfilled": expired["n"] if expired else None,
            "truncated": len(rows) >= _PERF_LIMIT,
            "window_basis": "outcome_ts, an H1 bar time (plan F-39)",
        },
        "router": {
            "label": "router book: closed positions, in USD (no R is recorded for pods)",
            "n": router["n"],
            "pnl_usd": router["pnl_usd"],
            "wins": router["wins"],
            "wilson95": [r_low, r_high],
        },
    }


# ==========================================================================
# /heartbeats
# ==========================================================================


def heartbeats(db: ReadOnlyDB, now: datetime, minutes: int) -> Dict[str, Any]:
    if not db.table_exists("ops_heartbeats"):
        return {
            "minutes": minutes,
            "rows": None,
            "missing": "ops_heartbeats table not present (migration 017 not applied on this database)",
        }
    since = now - timedelta(minutes=minutes)
    rows = db.rows(
        f"SELECT {_HEARTBEAT_COLUMNS} FROM ops_heartbeats WHERE ts >= %s AND ts <= %s "
        "ORDER BY ts DESC, id DESC LIMIT 3000",
        (since, now),
    )
    return {"minutes": minutes, "since": since.isoformat(), "count": len(rows), "rows": rows, "missing": None}
