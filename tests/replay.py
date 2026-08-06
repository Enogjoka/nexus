"""
The judge: replay a pod over stored H1 candles and report what it would have
done, after costs.

WHY THIS EXISTS. A pod is a claim about the market. Without a harness that
charges the same spread, slippage and commission the live system charges, the
claim is untestable and every pod looks profitable. This module is the thing
that says no.

COSTS ARE CHARGED, ALWAYS, AND THE GROSS/NET SPLIT IS THE POINT.
Round-turn cost per trade is
    (SIM_SPREAD_USD + 2 * SLIPPAGE_P75_FALLBACK_USD) * CONTRACT_SIZE_OZ * lots
    + COMMISSION_USD_PER_LOT * lots
which is the same arithmetic the kernel's cost gate uses. The report shows
gross AND net precisely so that a strategy which "works" gross and dies net is
visible as exactly that. At 0.01 lots the cost is a large fraction of any
realistic scalp, which is the entire reason the cost gate exists.

CONSERVATIVE ON AMBIGUITY. When a bar's range contains both the stop and the
take-profit, the STOP is taken. H1 bars cannot say which came first, and
assuming the good one would manufacture an edge out of missing data. This is
the paper engine's precedent (exec_/paper_engine.py) and it must stay
pessimistic: a backtest that resolves its own ambiguity favourably is a
generator of false confidence.

NO indicator_snapshots DEPENDENCY. ATR is recomputed from the bars themselves
so a replay runs against bare candles — including a synthetic script in a test
that never touched the sensor pipeline.

ONE OPEN TRADE PER POD. Real pods are position-limited by the kernel; letting
the replay stack twenty concurrent scalps would measure a strategy nobody
could have run.
"""
import argparse
import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# CLI-only: `python3 -m tests.replay` is a standalone entry point, so .env must
# load BEFORE `import config` below reads the environment.
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

import config
from core import database
from pods.vwap import SessionVWAP, session_of

logger = logging.getLogger(__name__)

ATR_PERIOD = 14
SYMBOL = "GC=F"
TIMEFRAME = "1h"


def round_turn_cost_usd(lots: float) -> float:
    """The same arithmetic the kernel's cost gate charges."""
    market = (
        (config.SIM_SPREAD_USD + 2.0 * config.SLIPPAGE_P75_FALLBACK_USD)
        * config.CONTRACT_SIZE_OZ
        * lots
    )
    return market + config.COMMISSION_USD_PER_LOT * lots


def load_candles(conn, days: int, symbol: str = SYMBOL, timeframe: str = TIMEFRAME) -> List[Dict]:
    """Oldest-first H1 bars from the last `days`. Anomalous bars are excluded."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE symbol = %s AND timeframe = %s AND ts >= %s AND is_anomaly = FALSE "
            "ORDER BY ts ASC",
            (symbol, timeframe, since),
        )
        rows = cur.fetchall()

    return [
        {
            "ts": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]) if row[5] is not None else None,
        }
        for row in rows
    ]


def _hour_bucket(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def load_basis_by_hour(conn, days: int) -> Dict[datetime, Dict[str, Any]]:
    """
    One basis reading per hour bucket, with the band as it stood then.

    The band is recomputed from the readings up to and including each bucket,
    so a replayed bar sees the history the live pod would have seen — not the
    band as it looks today, which would be hindsight.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ts, basis FROM basis_readings "
                "WHERE ts >= %s AND basis IS NOT NULL ORDER BY ts ASC",
                (since,),
            )
            rows = cur.fetchall()
    except Exception:
        logger.warning("replay: basis readings unavailable; S3 will stay silent", exc_info=True)
        return {}

    history: List[float] = []
    by_hour: Dict[datetime, Dict[str, Any]] = {}
    for ts, value in rows:
        history.append(float(value))
        window = history[-config.S3_BAND_LOOKBACK:]
        if len(window) < config.S3_MIN_READINGS:
            continue
        mean = statistics.fmean(window)
        by_hour[_hour_bucket(ts)] = {
            "basis": float(value),
            "mean": mean,
            "stdev": statistics.pstdev(window),
            "n": len(window),
        }
    return by_hour


def _true_range(bar: Dict, previous_close: Optional[float]) -> float:
    span = bar["high"] - bar["low"]
    if previous_close is None:
        return span
    return max(span, abs(bar["high"] - previous_close), abs(bar["low"] - previous_close))


class _AtrTracker:
    """
    Simple moving average of true range over ATR_PERIOD bars.

    An SMA, not Wilder's smoothing — stated plainly rather than implied. The
    pods use ATR as a scale factor for stop and target distances, where the
    two differ by a few percent and neither is more "correct"; what matters is
    that the replay and the live agent eventually use the SAME one.
    """

    def __init__(self, period: int = ATR_PERIOD) -> None:
        self._period = period
        self._ranges: List[float] = []
        self._previous_close: Optional[float] = None

    def update(self, bar: Dict) -> None:
        self._ranges.append(_true_range(bar, self._previous_close))
        if len(self._ranges) > self._period:
            self._ranges.pop(0)
        self._previous_close = bar["close"]

    def value(self) -> Optional[float]:
        if len(self._ranges) < self._period:
            return None
        return sum(self._ranges) / len(self._ranges)


def _simulate(trade: Dict, bar: Dict) -> Optional[Dict]:
    """
    Resolve an open trade against one subsequent bar.

    Stop is checked BEFORE take-profit: an H1 bar that spans both cannot say
    which came first, and the pessimistic reading is the only honest one.
    """
    if trade["direction"] == "LONG":
        if bar["low"] <= trade["stop"]:
            return {"exit": trade["stop"], "outcome": "STOP"}
        if bar["high"] >= trade["tp"]:
            return {"exit": trade["tp"], "outcome": "TP"}
    else:
        if bar["high"] >= trade["stop"]:
            return {"exit": trade["stop"], "outcome": "STOP"}
        if bar["low"] <= trade["tp"]:
            return {"exit": trade["tp"], "outcome": "TP"}
    return None


def _gross_pnl(trade: Dict, exit_price: float) -> float:
    move = (
        exit_price - trade["entry"]
        if trade["direction"] == "LONG"
        else trade["entry"] - exit_price
    )
    return move * config.CONTRACT_SIZE_OZ * trade["lots"]


def replay(pod, conn, days: int = 30, bars: Optional[List[Dict]] = None) -> Dict[str, Any]:
    """
    Walk the bars, ask the pod on each one, and settle whatever it asks for.

    `bars` is an injection seam for tests: pass a synthetic script and no
    database is touched at all.
    """
    if bars is None:
        bars = load_candles(conn, days)

    # S3 reads state["basis"]; every other pod ignores it. Loading it here
    # rather than per-bar keeps the walk a single pass over the data.
    basis_by_hour = load_basis_by_hour(conn, days) if conn is not None else {}

    vwap_tracker = SessionVWAP()
    atr_tracker = _AtrTracker()

    trades: List[Dict] = []
    open_trade: Optional[Dict] = None
    previous_volume: Optional[float] = None

    for bar in bars:
        # 1. Settle any open trade against THIS bar before considering a new
        #    one — a pod may not hold two positions at once.
        if open_trade is not None:
            resolution = _simulate(open_trade, bar)
            if resolution is not None:
                gross = _gross_pnl(open_trade, resolution["exit"])
                cost = round_turn_cost_usd(open_trade["lots"])
                trades.append(
                    {
                        **open_trade,
                        "exit_ts": bar["ts"],
                        "exit": resolution["exit"],
                        "outcome": resolution["outcome"],
                        "gross_pnl": gross,
                        "cost": cost,
                        "net_pnl": gross - cost,
                    }
                )
                open_trade = None

        # 2. Update the indicators with the bar that has now completed.
        vwap_tracker.update(bar, session_of(bar["ts"]))
        atr_tracker.update(bar)

        if open_trade is not None:
            previous_volume = bar["volume"]
            continue

        # 3. Ask the pod.
        spread = config.SIM_SPREAD_USD
        close = bar["close"]
        tick = {
            "ts": bar["ts"],
            "bid": close - spread / 2.0,
            "ask": close + spread / 2.0,
            "mid": close,
        }
        state = {
            "atr_h1": atr_tracker.value(),
            "vwap": vwap_tracker.vwap(),
            "vwap_stdev": vwap_tracker.stdev(),
            "regime_h1": bar.get("regime_h1", "RANGE"),
            "session_label": session_of(bar["ts"]),
            "volume": bar["volume"],
            "prev_volume": previous_volume,
            # Task 21: S3's evidence, joined by hour bucket. Absent -> the pod
            # is silent, exactly as it is live when the sensor has no band.
            "basis": basis_by_hour.get(_hour_bucket(bar["ts"])),
        }

        intent = pod.evaluate(tick, state)
        if intent is not None:
            open_trade = {
                "pod": intent.pod,
                "entry_ts": bar["ts"],
                "direction": intent.direction,
                "lots": intent.lots,
                "entry": intent.entry_price,
                "stop": intent.stop_price,
                "tp": intent.tp_price,
                "expected_edge_usd": intent.expected_edge_usd,
                "reason": intent.reason,
            }

        previous_volume = bar["volume"]

    return summarize(trades, bars, still_open=open_trade is not None)


def summarize(trades: List[Dict], bars: List[Dict], still_open: bool = False) -> Dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["net_pnl"] > 0)
    gross = sum(t["gross_pnl"] for t in trades)
    net = sum(t["net_pnl"] for t in trades)
    costs = sum(t["cost"] for t in trades)

    worst_streak = streak = 0
    for trade in trades:
        if trade["net_pnl"] <= 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    return {
        "trades": trades,
        "n": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": (wins / n) if n else None,
        "gross_usd": gross,
        "cost_usd": costs,
        "net_usd": net,
        "expectancy_usd": (net / n) if n else None,
        "max_consecutive_losses": worst_streak,
        "bars": len(bars),
        "first_bar": bars[0]["ts"] if bars else None,
        "last_bar": bars[-1]["ts"] if bars else None,
        "open_at_end": still_open,
    }


def format_report(pod_name: str, days: int, result: Dict[str, Any]) -> str:
    """The table. A zero-trade result is a finding, not an empty page."""
    lines = [
        "",
        f"REPLAY — {pod_name} over the last {days} days",
        "=" * 62,
        f"  bars replayed      : {result['bars']}",
        f"  period             : {result['first_bar']} -> {result['last_bar']}",
        f"  trades             : {result['n']}",
    ]

    if result["n"] == 0:
        lines += [
            "-" * 62,
            "  NO TRADES. The pod's conditions were never met in this window.",
            "  That is an honest answer, not a failure: a pod that fires on",
            "  nothing is telling you its setup did not occur.",
            "=" * 62,
            "",
        ]
        if result["open_at_end"]:
            lines.insert(-2, "  (one trade was still open when the data ran out)")
        return "\n".join(lines)

    lines += [
        f"  wins / losses      : {result['wins']} / {result['losses']}",
        f"  win rate           : {result['win_rate'] * 100:.1f}%",
        f"  max consec. losses : {result['max_consecutive_losses']}",
        "-" * 62,
        f"  gross              : ${result['gross_usd']:+.2f}",
        f"  costs              : ${result['cost_usd']:-.2f}",
        f"  net                : ${result['net_usd']:+.2f}",
        f"  expectancy / trade : ${result['expectancy_usd']:+.4f}",
        "-" * 62,
    ]

    drag = result["cost_usd"]
    if abs(result["gross_usd"]) > 0:
        lines.append(
            f"  cost drag          : {drag / abs(result['gross_usd']) * 100:.1f}% of gross"
        )
    if result["gross_usd"] > 0 >= result["net_usd"]:
        lines.append("  VERDICT: profitable GROSS, unprofitable NET — the costs ate it.")
    elif result["net_usd"] > 0:
        lines.append("  VERDICT: net positive over this window.")
    else:
        lines.append("  VERDICT: net negative over this window.")

    if result["open_at_end"]:
        lines.append("  (one trade was still open when the data ran out; excluded)")

    lines += ["=" * 62, ""]
    return "\n".join(lines)


def build_pod(name: str):
    """Map a CLI name to a pod instance. Imported lazily to keep --help cheap."""
    key = name.strip().upper()
    if key in ("S1", "S1_FIXFADE"):
        from pods.s1_fixfade import S1FixFade

        return S1FixFade()
    if key in ("S2", "S2_VWAPSNAP"):
        from pods.s2_vwapsnap import S2VwapSnap

        return S2VwapSnap()
    if key in ("S3", "S3_BASIS"):
        from pods.s3_basis import S3Basis

        return S3Basis()
    # S4 is deliberately absent. It is TICK-NATIVE: its burst and pullback
    # happen inside a single H1 bar, so this harness cannot see the sequence it
    # would be judging. Adding it here would produce a number, and that number
    # would be quoted. See the banner in pods/s4_newsburst.py.
    raise SystemExit(f"unknown pod {name!r}; expected S1, S2 or S3")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS pod replay harness")
    parser.add_argument("--pod", required=True, help="S1 or S2")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")

    pod = build_pod(args.pod)
    with database.get_conn() as conn:
        result = replay(pod, conn, days=args.days)

    print(format_report(pod.name, args.days, result))


if __name__ == "__main__":
    main()
