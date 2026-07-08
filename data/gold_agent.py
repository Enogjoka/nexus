"""
NEXUS gold market data agent.

Fetches XAUUSD (GC=F) OHLCV on H1/H4/D1 plus DXY, computes indicators
locally, persists candles + indicator snapshots to the database, and
publishes a "market_update" event. No AI/LLM calls anywhere in this module
(that is a hard boundary for this task, not just a style choice).

Schema note: migrations/001_init.sql (locked down in a prior task; this
task may not touch migrations/) defines indicator_snapshots with columns
ema20, ema50, rsi14, atr14, swing_high, swing_low, regime only. There is
no column for ema200, the Bollinger Band fields, or volume_ratio.
compute_indicators() still computes all of those (they are used for the
in-memory AppState snapshot and the CLI summary table) but
persist_indicator_snapshot() only writes the columns that exist.
"""
import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import psycopg2
import yfinance as yf
from psycopg2.extras import execute_values
from ta import momentum, trend, volatility

import config
from core.state import AppState, EventBus

logger = logging.getLogger(__name__)

# core/state.py's EventBus has no built-in process-wide singleton/registry
# (unlike AppState). Since core/ is off-limits for this task, this module
# owns its own bus instance and publishes "market_update" on it; a future
# task will need to promote this to a shared instance if other modules
# need to subscribe.
EVENT_BUS = EventBus()

# yfinance interval string per internal timeframe label (they happen to
# match today, but keep the mapping explicit rather than relying on that).
_TIMEFRAME_YF_INTERVAL = {"1h": "1h", "4h": "4h", "1d": "1d"}

# Calendar period requested from yfinance per timeframe, sized generously
# above config.CANDLE_LOOKBACK so `.tail(lookback)` always has enough bars.
_TIMEFRAME_YF_PERIOD = {"1h": "60d", "4h": "120d", "1d": "2y"}

_FETCH_TIMEOUT_SECONDS = 20
_DXY_TREND_LOOKBACK_BARS = 6  # need bar N and bar N-5 for a 5-bar trend


def _download_history(symbol: str, timeframe: str) -> pd.DataFrame:
    period = _TIMEFRAME_YF_PERIOD.get(timeframe)
    interval = _TIMEFRAME_YF_INTERVAL.get(timeframe)
    if period is None or interval is None:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    ticker = yf.Ticker(symbol)
    return ticker.history(period=period, interval=interval)


def fetch_candles(symbol: str, interval: str, lookback: int) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV candles for `symbol` at `interval` ("1h"/"4h"/"1d") via
    yfinance, returning the most recent `lookback` bars as a DataFrame with
    columns [ts, open, high, low, close, volume], ts UTC tz-aware, sorted
    ascending by ts.

    INVARIANT 6: timeout-wrapped and try/except-guarded. Any failure
    (timeout, network error, empty response) is logged and results in
    None — this function never raises and never hangs the caller.
    """
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_download_history, symbol, interval)
            raw = future.result(timeout=_FETCH_TIMEOUT_SECONDS)
    except FuturesTimeoutError:
        logger.warning(
            "fetch_candles timed out: symbol=%s interval=%s timeout=%ss",
            symbol, interval, _FETCH_TIMEOUT_SECONDS,
        )
        return None
    except Exception:
        logger.exception("fetch_candles failed: symbol=%s interval=%s", symbol, interval)
        return None

    if raw is None or raw.empty:
        logger.warning("fetch_candles returned no data: symbol=%s interval=%s", symbol, interval)
        return None

    try:
        df = raw.tail(lookback).copy()
        df.index = df.index.tz_convert("UTC")
        df.index.name = "ts"
        df = df.rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
        )
        df = df[["open", "high", "low", "close", "volume"]].reset_index()
        df = df.sort_values("ts").reset_index(drop=True)
    except Exception:
        logger.exception("fetch_candles: failed to normalize response: symbol=%s interval=%s", symbol, interval)
        return None

    return df


def detect_anomalies(df: pd.DataFrame) -> pd.Series:
    """
    Boolean Series, True where the bar-to-bar close jump exceeds
    config.ANOMALY_MAX_PCT_JUMP percent. The first bar is never flagged
    (no prior bar to compare against).
    """
    pct_jump = df["close"].pct_change().abs() * 100
    return (pct_jump > config.ANOMALY_MAX_PCT_JUMP).fillna(False)


def compute_indicators(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    """
    Compute the latest-bar indicator set from an OHLCV DataFrame (ascending
    by ts, columns open/high/low/close/volume) using the `ta` library.
    Returns a flat dict of plain python floats (None where undefined, e.g.
    insufficient history for a given window).
    """
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    def _last(series: pd.Series) -> Optional[float]:
        value = series.iloc[-1]
        return None if pd.isna(value) else float(value)

    ema20 = trend.EMAIndicator(close, window=20).ema_indicator()
    ema50 = trend.EMAIndicator(close, window=50).ema_indicator()
    ema200 = trend.EMAIndicator(close, window=200).ema_indicator()
    rsi14 = momentum.RSIIndicator(close, window=14).rsi()
    atr14 = volatility.AverageTrueRange(high, low, close, window=14).average_true_range()
    bb = volatility.BollingerBands(close, window=20, window_dev=2)

    last_bb_upper = _last(bb.bollinger_hband())
    last_bb_lower = _last(bb.bollinger_lband())
    last_close = _last(close)

    bb_position_pct: Optional[float] = None
    if last_close is not None and last_bb_upper is not None and last_bb_lower is not None:
        band_width = last_bb_upper - last_bb_lower
        if band_width > 0:
            raw_pct = (last_close - last_bb_lower) / band_width * 100
            bb_position_pct = float(np.clip(raw_pct, 0.0, 100.0))
        else:
            bb_position_pct = 50.0

    volume_avg20 = volume.tail(20).mean()
    volume_ratio: Optional[float] = None
    if volume_avg20 and not pd.isna(volume_avg20) and volume_avg20 != 0:
        volume_ratio = float(volume.iloc[-1] / volume_avg20)

    return {
        "ema20": _last(ema20),
        "ema50": _last(ema50),
        "ema200": _last(ema200),
        "rsi14": _last(rsi14),
        "atr14": _last(atr14),
        "bb_upper": last_bb_upper,
        "bb_lower": last_bb_lower,
        "bb_mid": _last(bb.bollinger_mavg()),
        "bb_position_pct": bb_position_pct,
        "volume_ratio": volume_ratio,
        "swing_high": float(high.tail(20).max()),
        "swing_low": float(low.tail(20).min()),
    }


def compute_regime(df: pd.DataFrame) -> str:
    """
    Simple regime rule (per spec, "for now"):
      TREND_UP   if close > ema50 > ema200
      TREND_DOWN if close < ema50 < ema200
      else       RANGE
    VOLATILE overrides the above if atr14/close exceeds the 90th
    percentile of that same ratio over the last 100 bars.
    """
    close = df["close"]
    ema50 = trend.EMAIndicator(close, window=50).ema_indicator()
    ema200 = trend.EMAIndicator(close, window=200).ema_indicator()
    atr14 = volatility.AverageTrueRange(df["high"], df["low"], close, window=14).average_true_range()

    last_close = close.iloc[-1]
    last_ema50 = ema50.iloc[-1]
    last_ema200 = ema200.iloc[-1]

    if pd.isna(last_ema50) or pd.isna(last_ema200):
        base_regime = "RANGE"
    elif last_close > last_ema50 > last_ema200:
        base_regime = "TREND_UP"
    elif last_close < last_ema50 < last_ema200:
        base_regime = "TREND_DOWN"
    else:
        base_regime = "RANGE"

    # Compare the current bar's atr/close ratio against the percentile of
    # the *preceding* 100 bars (current bar excluded). Including the
    # current bar in its own reference window is self-referential: Wilder's
    # ATR is a recursive smoother, so on a converging series the newest
    # point is almost always at or near the extreme of a window that
    # contains itself, which would trip VOLATILE on perfectly ordinary data.
    atr_ratio = (atr14 / close).dropna()
    historical_ratio = atr_ratio.iloc[-101:-1]
    if len(historical_ratio) >= 10:
        threshold = np.percentile(historical_ratio, 90)
        if atr_ratio.iloc[-1] > threshold:
            return "VOLATILE"

    return base_regime


def detect_session(utc_now: datetime) -> str:
    """
    London 07-16 UTC, NY 12-21 UTC, overlap 12-16 UTC, Asia 23-07 UTC.
    Ranges are half-open [start, end); OVERLAP is checked first since it
    is the intersection of LONDON and NY. The two uncovered hours (21-23
    UTC, between NY close and Asia open) are "OFF".
    """
    if utc_now.tzinfo is not None:
        utc_now = utc_now.astimezone(timezone.utc)
    hour = utc_now.hour

    if 12 <= hour < 16:
        return "OVERLAP"
    if 7 <= hour < 12:
        return "LONDON"
    if 16 <= hour < 21:
        return "NY"
    if hour >= 23 or hour < 7:
        return "ASIA"
    return "OFF"


def fetch_dxy_trend() -> Optional[Dict[str, Any]]:
    """
    Fetch DXY (config.DXY_SYMBOL) H1 close plus a simple 5-bar trend
    direction ("UP" / "DOWN" / "FLAT"). Never persisted to the database —
    callers are expected to stash the result in AppState.market_data only.
    """
    df = fetch_candles(config.DXY_SYMBOL, "1h", lookback=_DXY_TREND_LOOKBACK_BARS + 4)
    if df is None or len(df) < _DXY_TREND_LOOKBACK_BARS:
        logger.warning("fetch_dxy_trend: insufficient DXY data")
        return None

    last_close = float(df["close"].iloc[-1])
    prior_close = float(df["close"].iloc[-_DXY_TREND_LOOKBACK_BARS])
    if last_close > prior_close:
        dxy_trend = "UP"
    elif last_close < prior_close:
        dxy_trend = "DOWN"
    else:
        dxy_trend = "FLAT"

    return {"close": last_close, "trend": dxy_trend, "ts": df["ts"].iloc[-1].to_pydatetime()}


def _connect():
    """Open a fresh DB connection from config.DATABASE_URL. Raises if unset."""
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(config.DATABASE_URL)


def persist_candles(conn, symbol: str, timeframe: str, df: pd.DataFrame) -> int:
    """
    Upsert candles: ON CONFLICT (symbol, timeframe, ts) DO NOTHING. Bars
    flagged by detect_anomalies() are stored with is_anomaly=TRUE and a
    warning is logged for each — anomalous bars are never dropped.
    Returns the number of rows attempted (some may be skipped by the
    ON CONFLICT clause if already stored).
    """
    anomaly_flags = detect_anomalies(df)

    rows = []
    for idx, row in df.iterrows():
        is_anomaly = bool(anomaly_flags.loc[idx])
        if is_anomaly:
            logger.warning(
                "anomaly detected: symbol=%s timeframe=%s ts=%s close=%.5f",
                symbol, timeframe, row["ts"], row["close"],
            )
        volume = row["volume"]
        rows.append(
            (
                symbol,
                timeframe,
                row["ts"].to_pydatetime(),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                None if pd.isna(volume) else int(volume),
                is_anomaly,
            )
        )

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO candles (symbol, timeframe, ts, open, high, low, close, volume, is_anomaly)
            VALUES %s
            ON CONFLICT (symbol, timeframe, ts) DO NOTHING
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def persist_indicator_snapshot(
    conn, symbol: str, timeframe: str, ts: datetime, indicators: Dict[str, Optional[float]], regime: str
) -> None:
    """
    Persist the latest indicator snapshot for (symbol, timeframe). Only
    columns present in indicator_snapshots are written (ema20, ema50,
    rsi14, atr14, swing_high, swing_low, regime) — see module docstring.
    ON CONFLICT (symbol, timeframe, ts) DO NOTHING keeps repeated polls
    within the same bar idempotent.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indicator_snapshots
                (symbol, timeframe, ts, ema20, ema50, rsi14, atr14, swing_high, swing_low, regime)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, timeframe, ts) DO NOTHING
            """,
            (
                symbol,
                timeframe,
                ts,
                indicators.get("ema20"),
                indicators.get("ema50"),
                indicators.get("rsi14"),
                indicators.get("atr14"),
                indicators.get("swing_high"),
                indicators.get("swing_low"),
                regime,
            ),
        )
    conn.commit()


def run_cycle() -> Dict[str, Any]:
    """
    One full data-agent cycle: fetch every configured timeframe plus DXY,
    compute indicators/regime, persist, update AppState.market_data, and
    publish "market_update". A failed fetch for one timeframe is logged
    and skipped (state falls back to stale + a staleness timestamp) — it
    never aborts the rest of the cycle (INVARIANT 6).
    """
    state = AppState()
    now = datetime.now(timezone.utc)
    session = detect_session(now)

    summary: Dict[str, Any] = {"generated_at": now, "session": session, "timeframes": {}, "dxy": None}

    conn = None
    try:
        conn = _connect()
    except Exception:
        logger.exception("run_cycle: could not obtain a database connection; continuing without persistence")

    try:
        for timeframe in config.TIMEFRAMES:
            lookback = config.CANDLE_LOOKBACK.get(timeframe, 300)
            df = fetch_candles(config.YF_SYMBOL, timeframe, lookback)

            if df is None or df.empty:
                logger.warning("run_cycle: no data for timeframe=%s; keeping stale state", timeframe)
                stale = dict(state.get_market_data(timeframe) or {})
                stale["stale"] = True
                stale["stale_since"] = now.isoformat()
                state.update_market_data(timeframe, stale)
                summary["timeframes"][timeframe] = {"error": "fetch_failed", "stale_since": now.isoformat()}
                continue

            indicators = compute_indicators(df)
            regime = compute_regime(df)
            last_row = df.iloc[-1]
            anomaly_flags = detect_anomalies(df)

            if conn is not None:
                try:
                    persist_candles(conn, config.YF_SYMBOL, timeframe, df)
                    persist_indicator_snapshot(
                        conn, config.YF_SYMBOL, timeframe, last_row["ts"].to_pydatetime(), indicators, regime
                    )
                except Exception:
                    logger.exception("run_cycle: persistence failed for timeframe=%s", timeframe)

            tf_state = {
                "price": float(last_row["close"]),
                "ts": last_row["ts"].to_pydatetime(),
                "indicators": indicators,
                "regime": regime,
                "is_anomaly": bool(anomaly_flags.iloc[-1]),
                "stale": False,
            }
            state.update_market_data(timeframe, tf_state)
            summary["timeframes"][timeframe] = tf_state

        dxy = fetch_dxy_trend()
        if dxy is not None:
            state.update_market_data("dxy", dxy)
        summary["dxy"] = dxy if dxy is not None else state.get_market_data("dxy")

        state.last_analysis_ts = now.timestamp()
    finally:
        if conn is not None:
            conn.close()

    EVENT_BUS.publish("market_update", summary)
    return summary


def run_data_agent(poll_seconds: Optional[int] = None) -> None:
    """
    Plain polling loop (no scheduling library) — run_cycle() every
    `poll_seconds` (default config.DATA_POLL_SECONDS). A single bad cycle
    is logged and never kills the loop (INVARIANT 6): the agent simply
    continues with whatever stale state it already has.
    """
    interval = poll_seconds if poll_seconds is not None else config.DATA_POLL_SECONDS
    logger.info("gold data agent starting: stage=%s poll_seconds=%s", config.get_stage(), interval)
    while True:
        try:
            run_cycle()
        except Exception:
            logger.exception("run_cycle raised unexpectedly; continuing with stale data")
        time.sleep(interval)


def _format_summary_table(summary: Dict[str, Any]) -> str:
    rows: List[Dict[str, Any]] = []
    for timeframe in config.TIMEFRAMES:
        tf = summary["timeframes"].get(timeframe, {})
        if "error" in tf:
            rows.append(
                {
                    "TF": timeframe, "Price": None, "RSI14": None,
                    "EMA20": None, "EMA50": None, "EMA200": None,
                    "Regime": "N/A (fetch failed)", "Anomaly": None,
                }
            )
            continue

        indicators = tf.get("indicators", {}) or {}

        def _round(value: Optional[float], digits: int) -> Optional[float]:
            return round(value, digits) if value is not None else None

        rows.append(
            {
                "TF": timeframe,
                "Price": _round(tf.get("price"), 5),
                "RSI14": _round(indicators.get("rsi14"), 2),
                "EMA20": _round(indicators.get("ema20"), 5),
                "EMA50": _round(indicators.get("ema50"), 5),
                "EMA200": _round(indicators.get("ema200"), 5),
                "Regime": tf.get("regime"),
                "Anomaly": tf.get("is_anomaly"),
            }
        )

    table_df = pd.DataFrame(rows)

    lines = ["NEXUS Gold Data Agent — single cycle summary", f"Session: {summary.get('session')}"]
    dxy = summary.get("dxy")
    if dxy:
        lines.append(f"DXY: {dxy['close']:.3f} ({dxy['trend']}, 5-bar)")
    else:
        lines.append("DXY: unavailable")
    lines.append("")
    lines.append(table_df.to_string(index=False))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="NEXUS gold market data agent")
    parser.add_argument("--once", action="store_true", help="Run a single cycle, print a summary, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        summary = run_cycle()
        print(_format_summary_table(summary))
        return

    run_data_agent()


if __name__ == "__main__":
    main()
