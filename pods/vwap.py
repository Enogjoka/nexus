"""
Session VWAP, and an honest account of its resolution.

THIS IS AN H1 APPROXIMATION OF TICK VWAP.
Real VWAP is computed over every trade in the session. This class is fed HOURLY
BARS and uses each bar's typical price ((H+L+C)/3) as a stand-in for the
thousands of prints inside it. Within a quiet hour the two agree closely;
within a violent hour they do not, and the approximation is worst exactly when
the pods most want to trade.

That is acceptable for PAPER and for replay, where the alternative is no VWAP
at all. It is NOT acceptable for live money. When the Windows box arrives with
a real tick feed, THE FEED MUST BE REPLACED, NOT THE INTERFACE: vwap(),
stdev() and bars_seen stay exactly as they are, update() starts taking ticks,
and nothing in s1_fixfade.py or s2_vwapsnap.py changes. Any future work that
finds itself editing a pod to accommodate better VWAP data has taken a wrong
turn.

FEWER THAN THREE BARS IS NOT A SESSION. Both vwap() and stdev() return None
until MIN_BARS have arrived. A standard deviation over two points is a number,
not a measurement, and S2 sizes its entire trigger off that number.
"""
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Below this, the statistics are noise wearing a decimal point.
MIN_BARS = 3


def session_of(moment: datetime) -> str:
    """
    London 07-16 UTC, NY 12-21 UTC, overlap 12-16 UTC, Asia 23-07 UTC.

    NOTE: data/gold_agent.py owns the authoritative copy of this mapping. It is
    reimplemented here rather than imported because importing it would drag
    yfinance, pandas and the whole market-data stack into the pod package and
    into the replay harness. The two must agree; if the ranges ever change,
    they change in both places.
    """
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    hour = moment.hour

    if 12 <= hour < 16:
        return "OVERLAP"
    if 7 <= hour < 12:
        return "LONDON"
    if 16 <= hour < 21:
        return "NY"
    if hour >= 23 or hour < 7:
        return "ASIA"
    return "OFF"


def _finite(value: Any) -> Optional[float]:
    """A real finite float, or None. Rejects bools and unparseable values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class SessionVWAP:
    """
    Volume-weighted average price for the CURRENT session only.

    Resets whenever the session label changes, because a VWAP that spans
    London and Asia describes a mean no participant ever traded around.
    """

    def __init__(self) -> None:
        self._session: Optional[str] = None
        self._weighted_sum = 0.0
        self._weight_total = 0.0
        self._typicals: list = []

    # -- feeding -----------------------------------------------------------

    def update(self, bar: Dict[str, Any], session_label: Optional[str] = None) -> None:
        """
        Add one H1 bar. `session_label` defaults to the label derived from the
        bar's own timestamp, so the replay harness and the live agent can drive
        this identically.

        A bar missing any of high/low/close is skipped entirely rather than
        guessed at — a VWAP built on a fabricated price is worse than a VWAP
        that is briefly None.
        """
        high = _finite(bar.get("high"))
        low = _finite(bar.get("low"))
        close = _finite(bar.get("close"))
        if high is None or low is None or close is None:
            logger.debug("SessionVWAP: bar missing high/low/close; skipping it")
            return

        if session_label is None:
            ts = bar.get("ts")
            session_label = session_of(ts) if isinstance(ts, datetime) else self._session

        if session_label != self._session:
            self.reset(session_label)

        typical = (high + low + close) / 3.0

        # Weight by volume when we have it. An unusable or zero volume gets
        # weight 1.0, which means a session with NO usable volume anywhere
        # degrades gracefully to an equal-weighted mean of typical prices
        # rather than to nothing at all.
        volume = _finite(bar.get("volume"))
        weight = volume if (volume is not None and volume > 0) else 1.0

        self._weighted_sum += typical * weight
        self._weight_total += weight
        self._typicals.append(typical)

    def reset(self, session_label: Optional[str] = None) -> None:
        self._session = session_label
        self._weighted_sum = 0.0
        self._weight_total = 0.0
        self._typicals = []

    # -- reading -----------------------------------------------------------

    @property
    def session(self) -> Optional[str]:
        return self._session

    @property
    def bars_seen(self) -> int:
        return len(self._typicals)

    def vwap(self) -> Optional[float]:
        if self.bars_seen < MIN_BARS or self._weight_total <= 0:
            return None
        return self._weighted_sum / self._weight_total

    def stdev(self) -> Optional[float]:
        """
        Population standard deviation of this session's typical prices.

        Unweighted on purpose: this measures how far the session has RANGED,
        which is the question S2 asks. Weighting it by volume would answer a
        different question — how far the average traded dollar sat from the
        mean — and would shrink exactly when a low-volume outlier is the thing
        worth noticing.
        """
        if self.bars_seen < MIN_BARS:
            return None
        mean = sum(self._typicals) / len(self._typicals)
        variance = sum((value - mean) ** 2 for value in self._typicals) / len(self._typicals)
        return math.sqrt(variance)
