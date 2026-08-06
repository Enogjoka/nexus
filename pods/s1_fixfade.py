"""
S1 FIX-FADE.

The 15:00 UTC PM gold fix concentrates benchmark-driven order flow into a short
window. That flow is price-insensitive — it has to trade regardless — so it
drags price away from where the session has actually been changing hands. The
pod fades that stretch back toward session VWAP.

ARMED ONLY AROUND THE FIX, AND THAT IS THE WHOLE THESIS.
The same stretch at 03:00 UTC is not fix flow; it is a trend, and fading a
trend is how a mean-reversion pod dies. The window is the edge. A version of
this pod that ran all day would be a different, worse strategy wearing the same
name, so the window check comes first and there is no override.

NEVER TARGET PAST THE MEAN YOU ARE REVERTING TO.
The take-profit is capped at VWAP. Reverting to the mean is the entire claim;
asking for more than the mean is asking for a trend, which the pod has just
finished betting against.

Deterministic and stateless: the clock comes from tick["ts"], VWAP and ATR come
from `state`. Nothing here reads a global, so the same inputs always produce
the same Intent — which is what makes tests/replay.py's verdict mean anything.
"""
import logging
import math
from datetime import datetime, time, timezone
from typing import Any, Dict, Optional

import config
from pods.base import Intent

logger = logging.getLogger(__name__)

POD_NAME = "S1_FIXFADE"
FIX_UTC = time(15, 0)


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def in_fix_window(moment: datetime) -> bool:
    """True inside [15:00 - BEFORE, 15:00 + AFTER] UTC, endpoints included."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    minutes_from_fix = (
        (moment.hour * 60 + moment.minute)
        - (FIX_UTC.hour * 60 + FIX_UTC.minute)
    )
    return -config.S1_WINDOW_BEFORE_MIN <= minutes_from_fix <= config.S1_WINDOW_AFTER_MIN


class S1FixFade:
    """
    Fade the PM-fix stretch back to session VWAP.

    Not a subclass of pods.base.Pod by inheritance alone — it satisfies the
    same interface (name + evaluate) and PodSupervisor duck-types it. Wiring
    into the supervisor is Task 21.
    """

    name = POD_NAME

    def evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        """
        Return an Intent, or None. NEVER raises: a pod that can crash the sweep
        is a pod that can silence every other pod (INVARIANT 6).
        """
        try:
            return self._evaluate(tick or {}, state or {})
        except Exception:
            logger.exception("%s: evaluate raised; treating as no-trade", self.name)
            return None

    def _evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        now = tick.get("ts")
        if not isinstance(now, datetime):
            logger.debug("%s: tick has no usable ts; no trade", self.name)
            return None

        # 1. The window IS the thesis.
        if not in_fix_window(now):
            return None

        mid = _finite(tick.get("mid"))
        atr = _finite(state.get("atr_h1"))
        vwap = _finite(state.get("vwap"))

        if mid is None or mid <= 0:
            logger.debug("%s: no usable mid price; no trade", self.name)
            return None
        if atr is None or atr <= 0:
            logger.debug("%s: atr_h1 unavailable; no trade", self.name)
            return None
        if vwap is None or vwap <= 0:
            logger.debug("%s: session vwap unavailable; no trade", self.name)
            return None

        # 2. Is the stretch big enough to be fix flow rather than noise?
        stretch = mid - vwap
        threshold = config.S1_STRETCH_ATR_MULT * atr
        if abs(stretch) <= threshold:
            return None

        # 3. Fade it: stretched above VWAP means sell, below means buy.
        direction = "SHORT" if stretch > 0 else "LONG"
        entry = mid
        stop_distance = config.S1_STOP_ATR_MULT * atr
        tp_distance = config.S1_TP_ATR_MULT * atr

        if direction == "LONG":
            stop = entry - stop_distance
            tp = min(entry + tp_distance, vwap)   # capped at the mean
        else:
            stop = entry + stop_distance
            tp = max(entry - tp_distance, vwap)   # capped at the mean

        if stop <= 0 or tp <= 0:
            logger.debug("%s: computed a non-positive stop/tp; no trade", self.name)
            return None

        edge = (
            abs(tp - entry)
            * config.CONTRACT_SIZE_OZ
            * config.S1_LOTS
            * config.EDGE_HAIRCUT
        )
        if edge <= 0:
            # Entry sitting exactly on VWAP after the cap. No move to claim.
            return None

        return Intent(
            pod=self.name,
            direction=direction,
            lots=config.S1_LOTS,
            entry_price=entry,
            stop_price=stop,
            tp_price=tp,
            expected_edge_usd=edge,
            reason=(
                f"fix-fade: stretched {stretch:+.2f} from vwap {vwap:.2f} "
                f"({abs(stretch) / atr:.2f} ATR > {config.S1_STRETCH_ATR_MULT}) "
                f"at {now.strftime('%H:%M')} UTC"
            ),
        )
