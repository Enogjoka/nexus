"""
Acceptance tests for data/gold_agent.py (Task 1).

No network access anywhere in this file — all DataFrames are synthetic.
Where run_cycle() is exercised, DATABASE_URL is expected to be unset in
the test environment, so persistence is skipped gracefully (that path is
itself part of what INVARIANT 6 requires: a missing DB never crashes the
cycle).
"""
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from data.gold_agent import (
    compute_indicators,
    compute_regime,
    detect_anomalies,
    detect_session,
    run_cycle,
)


def _make_ohlcv(
    close: np.ndarray, freq: str = "h", spread_pct: float = 0.0008, jitter_seed: int = 99
) -> pd.DataFrame:
    """
    Build a plausible OHLCV frame around a given close-price series.

    high/low spread is proportional to price (not a constant absolute
    offset) so atr14/close stays roughly stationary across a drifting
    series — a constant absolute spread would make atr/close drift
    monotonically opposite a price trend and spuriously trip the
    VOLATILE regime override, an artifact of the synthetic data rather
    than a real volatility signal.

    A small amount of random jitter is added to the spread so ATR
    fluctuates instead of monotonically converging to an exact constant
    (Wilder's ATR is a recursive smoother; a perfectly deterministic
    spread makes the newest bar's ratio a razor-thin floating-point
    tie-break against its own percentile threshold). The last few bars
    are pinned to the unjittered mean spread so the volatility check
    isn't a coin flip depending on the random tail.
    """
    n = len(close)
    rng = np.random.default_rng(jitter_seed)
    noise = rng.uniform(0.85, 1.15, size=n)
    noise[-5:] = 1.0
    spread = spread_pct * noise
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = close.copy()
    volume = np.full(n, 1000.0)
    ts = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def _synthetic_uptrend(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = rng.normal(loc=0.5, scale=1.5, size=n)
    close = 2000.0 + np.cumsum(returns)
    return _make_ohlcv(close)


# --------------------------------------------------------------------------
# compute_indicators
# --------------------------------------------------------------------------


def test_compute_indicators_sanity_on_synthetic_series():
    df = _synthetic_uptrend(n=300)
    indicators = compute_indicators(df)

    assert 0.0 <= indicators["rsi14"] <= 100.0
    # Sustained uptrend -> shorter EMAs sit above longer EMAs.
    assert indicators["ema20"] > indicators["ema50"] > indicators["ema200"]
    assert 0.0 <= indicators["bb_position_pct"] <= 100.0
    assert indicators["atr14"] > 0
    assert indicators["swing_high"] >= indicators["swing_low"]
    assert indicators["volume_ratio"] is not None


# --------------------------------------------------------------------------
# detect_session
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour,expected",
    [
        (3, "ASIA"),
        (9, "LONDON"),
        (13, "OVERLAP"),
        (18, "NY"),
        (22, "OFF"),
        (23, "ASIA"),
    ],
)
def test_detect_session(hour, expected):
    dt = datetime(2026, 1, 1, hour, 0, tzinfo=timezone.utc)
    assert detect_session(dt) == expected


# --------------------------------------------------------------------------
# anomaly detection
# --------------------------------------------------------------------------


def test_detect_anomaly_on_synthetic_gap_bar():
    # 2000 -> 2100 is a +5% jump, above the 3.0% default threshold.
    closes = np.array([2000.0, 2000.0, 2000.0, 2100.0, 2100.0])
    df = _make_ohlcv(closes)

    flags = detect_anomalies(df)

    assert bool(flags.iloc[3]) is True
    assert not flags.iloc[:3].any()
    assert bool(flags.iloc[4]) is False


# --------------------------------------------------------------------------
# fetch failure path
# --------------------------------------------------------------------------


def test_fetch_failure_returns_none_and_cycle_survives():
    with patch("data.gold_agent.yf.Ticker") as mock_ticker_cls:
        mock_ticker_cls.side_effect = RuntimeError("network down")

        from data.gold_agent import fetch_candles

        result = fetch_candles("GC=F", "1h", 500)
        assert result is None

        # A fully-failing fetch layer must not raise out of run_cycle().
        summary = run_cycle()

    assert set(summary["timeframes"]) == {"1h", "4h", "1d"}
    for tf_summary in summary["timeframes"].values():
        assert tf_summary.get("error") == "fetch_failed"


# --------------------------------------------------------------------------
# regime rule
# --------------------------------------------------------------------------


def test_regime_trend_up():
    n = 250
    close = 2000.0 + np.arange(n) * 1.5
    df = _make_ohlcv(close)
    assert compute_regime(df) == "TREND_UP"


def test_regime_trend_down():
    n = 250
    close = 3000.0 - np.arange(n) * 1.5
    df = _make_ohlcv(close)
    assert compute_regime(df) == "TREND_DOWN"


def test_regime_range():
    # A flat, tightly-noisy series does NOT reliably produce RANGE: with
    # ema50 and ema200 both hovering within noise of each other, the
    # ordering of close/ema50/ema200 at any single snapshot is close to a
    # coin flip across all three regimes. A deterministic oscillation
    # (no RNG) whose final bar lands with ema50 and ema200 on opposite
    # sides of close is a reliable, reproducible way to hit the "neither
    # trend-up nor trend-down" case.
    n = 250
    t = np.arange(n)
    close = 2000.0 + 15.0 * np.sin(2 * np.pi * t / 35.0)
    df = _make_ohlcv(close)
    assert compute_regime(df) == "RANGE"
