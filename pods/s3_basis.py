"""
S3 BASIS-DISLOC.

GC=F and XAUUSD=X are two prices for the same metal, so the spread between
them is small, driven by carry and funding, and mean-reverting. When it leaves
its own recent band the market is briefly disagreeing with itself, and that
disagreement tends to close.

WE TRADE THE FUTURE, NOT THE SPREAD. A true basis trade is long one leg and
short the other, and this system has exactly one instrument. So the pod takes
the directional side of the reversion on the traded instrument and accepts
that it is carrying outright price risk the real arbitrage would not have.
That is a weaker trade than the name suggests and it is worth being blunt
about: the basis is the SIGNAL here, not the position.

    basis rich  (future expensive vs spot) -> the future should fall -> SHORT
    basis cheap (future cheap vs spot)     -> the future should rise -> LONG

THE BAND IS THE WHOLE THESIS, WHICH MAKES A THIN BAND THE MAIN HAZARD.
mean and stdev over a handful of readings describe the sensor's first few
minutes, not the market. sensors/basis.py refuses to produce a band below
config.S3_MIN_READINGS and this pod refuses to trade without one — it declines
rather than treating a tight startup band as a strong signal, which is exactly
the mistake that would make S3 fire constantly on nothing.

Deterministic and stateless, like S1 and S2: everything comes from `tick` and
`state`, so the same inputs always produce the same Intent.
"""
import logging
import math
from typing import Any, Dict, Optional

import config
from pods.base import Intent

logger = logging.getLogger(__name__)

POD_NAME = "S3_BASIS"


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class S3Basis:
    """Trade the reversion when the futures/spot spread leaves its band."""

    name = POD_NAME

    def evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        """Return an Intent, or None. NEVER raises (INVARIANT 6)."""
        try:
            return self._evaluate(tick or {}, state or {})
        except Exception:
            logger.exception("%s: evaluate raised; treating as no-trade", self.name)
            return None

    def _evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        reading = state.get("basis")
        if not isinstance(reading, dict):
            logger.debug("%s: no basis reading in state; no trade", self.name)
            return None

        basis = _finite(reading.get("basis"))
        mean = _finite(reading.get("mean"))
        stdev = _finite(reading.get("stdev"))
        n = _finite(reading.get("n")) or 0

        if basis is None or mean is None or stdev is None:
            logger.debug("%s: band unavailable; no trade", self.name)
            return None
        if n < config.S3_MIN_READINGS:
            # A band this thin describes the sensor, not the market.
            logger.debug(
                "%s: %d reading(s), need %d; no trade", self.name, int(n), config.S3_MIN_READINGS
            )
            return None
        if stdev <= 0:
            # A motionless spread has no band to leave.
            return None

        deviation = basis - mean
        if abs(deviation) <= config.S3_BAND_SIGMA * stdev:
            return None

        mid = _finite(tick.get("mid"))
        atr = _finite(state.get("atr_h1"))
        if mid is None or mid <= 0:
            logger.debug("%s: no usable mid price; no trade", self.name)
            return None
        if atr is None or atr <= 0:
            logger.debug("%s: atr_h1 unavailable; no trade", self.name)
            return None

        # Rich basis means the future is the expensive leg: sell it.
        direction = "SHORT" if deviation > 0 else "LONG"
        entry = mid

        stop_distance = config.S3_STOP_ATR_MULT * atr
        stop = entry - stop_distance if direction == "LONG" else entry + stop_distance

        # The reversion distance is the spread's excursion beyond its mean,
        # expressed in price terms on the instrument we actually trade, and
        # taken only fractionally: the spread need not return the whole way for
        # the trade to work.
        revert = abs(deviation) * config.S3_TP_REVERT_FRACTION
        tp = entry + revert if direction == "LONG" else entry - revert

        if stop <= 0 or tp <= 0:
            logger.debug("%s: computed a non-positive stop/tp; no trade", self.name)
            return None

        edge = (
            abs(tp - entry)
            * config.CONTRACT_SIZE_OZ
            * config.S3_LOTS
            * config.EDGE_HAIRCUT
        )
        if edge <= 0:
            return None

        return Intent(
            pod=self.name,
            direction=direction,
            lots=config.S3_LOTS,
            entry_price=entry,
            stop_price=stop,
            tp_price=tp,
            expected_edge_usd=edge,
            reason=(
                f"basis-disloc: basis {basis:+.3f} is {deviation:+.3f} from mean "
                f"{mean:+.3f} ({abs(deviation) / stdev:.2f}σ > {config.S3_BAND_SIGMA}) "
                f"over n={int(n)}"
            ),
        )
