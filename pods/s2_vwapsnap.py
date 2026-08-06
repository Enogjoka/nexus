"""
S2 VWAP-SNAP.

In a RANGE-bound session, a move far from VWAP on FALLING volume is a move
without participation: price went somewhere the market did not follow it to.
Those moves tend to snap back. The pod trades the snap.

BOTH CONDITIONS MATTER, AND THE VOLUME ONE IS WHY.
Displacement alone is just a move. Displacement on RISING volume is a
breakout — real participants pushing price to a new area — and fading it is
exactly wrong. The falling-volume filter is the difference between "nobody
came with it" and "everybody came with it", and it is the only thing
separating this pod from a strategy that shorts every rally.

THE REGIME GATE IS THE POD'S OWN BELT.
PodSupervisor already refuses to run a pod the doctrine has not enabled. This
check is independent of that: even fully enabled, S2 declines to trade unless
the H1 regime is RANGE, because mean-reversion in a trending market is a
losing trade with a good story. Two layers, deliberately, because they fail
differently — the doctrine can be stale, and the regime can flip inside a
doctrine's review horizon.

VOLUME HONESTY: the "falling volume" test compares consecutive H1 bar volumes,
which is the finest resolution available before the Windows box. Real tick
volume within the bar would be far better and is not available here. Like the
VWAP approximation in pods/vwap.py, this is a FEED limitation to be replaced,
not an interface to be redesigned.
"""
import logging
import math
from datetime import datetime
from typing import Any, Dict, Optional

import config
from pods.base import Intent

logger = logging.getLogger(__name__)

POD_NAME = "S2_VWAPSNAP"
REQUIRED_REGIME = "RANGE"


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class S2VwapSnap:
    """Fade an unparticipated displacement back toward session VWAP."""

    name = POD_NAME

    def evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        """Return an Intent, or None. NEVER raises (INVARIANT 6)."""
        try:
            return self._evaluate(tick or {}, state or {})
        except Exception:
            logger.exception("%s: evaluate raised; treating as no-trade", self.name)
            return None

    def _evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        # 1. The pod's own regime belt, independent of doctrine enablement.
        if state.get("regime_h1") != REQUIRED_REGIME:
            return None

        mid = _finite(tick.get("mid"))
        atr = _finite(state.get("atr_h1"))
        vwap = _finite(state.get("vwap"))
        sigma = _finite(state.get("vwap_stdev"))

        if mid is None or mid <= 0:
            logger.debug("%s: no usable mid price; no trade", self.name)
            return None
        if atr is None or atr <= 0:
            logger.debug("%s: atr_h1 unavailable; no trade", self.name)
            return None
        if vwap is None or vwap <= 0:
            logger.debug("%s: session vwap unavailable; no trade", self.name)
            return None
        if sigma is None or sigma <= 0:
            # A thin session (< 3 bars) has no usable stdev, and S2's whole
            # trigger is sized off it.
            logger.debug("%s: session stdev unavailable; no trade", self.name)
            return None

        # 2. Displaced far enough to be worth fading?
        displacement = mid - vwap
        if abs(displacement) <= config.S2_SIGMA_MULT * sigma:
            return None

        # 3. Did anyone come with it? Rising volume means yes — stand aside.
        volume = _finite(state.get("volume"))
        prev_volume = _finite(state.get("prev_volume"))
        if volume is None or prev_volume is None:
            logger.debug("%s: volume history unavailable; no trade", self.name)
            return None
        if volume >= prev_volume:
            return None

        # 4. Trade back toward VWAP.
        direction = "SHORT" if displacement > 0 else "LONG"
        entry = mid
        stop_distance = config.S2_STOP_ATR_MULT * atr
        stop = entry - stop_distance if direction == "LONG" else entry + stop_distance

        # A fraction of the way back to the mean, so the target is reached
        # before the snap has to complete perfectly.
        tp = entry + config.S2_TP_VWAP_FRACTION * (vwap - entry)

        if stop <= 0 or tp <= 0:
            logger.debug("%s: computed a non-positive stop/tp; no trade", self.name)
            return None

        edge = (
            abs(tp - entry)
            * config.CONTRACT_SIZE_OZ
            * config.S2_LOTS
            * config.EDGE_HAIRCUT
        )
        if edge <= 0:
            return None

        return Intent(
            pod=self.name,
            direction=direction,
            lots=config.S2_LOTS,
            entry_price=entry,
            stop_price=stop,
            tp_price=tp,
            expected_edge_usd=edge,
            reason=(
                f"vwap-snap: {displacement:+.2f} from vwap {vwap:.2f} "
                f"({abs(displacement) / sigma:.2f}σ > {config.S2_SIGMA_MULT}) "
                f"on falling volume {prev_volume:.0f} -> {volume:.0f} in RANGE"
            ),
        )
