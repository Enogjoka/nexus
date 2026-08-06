"""
S4 NEWS-BURST.

################################################################################
#  THIS IS A TICK-NATIVE POD.                                                  #
#                                                                              #
#  Its entire premise lives inside the first minutes after a high-impact        #
#  release: a violent print, then a partial retrace that the pod enters. At H1  #
#  resolution the burst and the pullback occur INSIDE A SINGLE BAR, so an H1    #
#  backtest cannot see the sequence it is supposedly trading — it can only see  #
#  where the hour happened to close.                                           #
#                                                                              #
#  Therefore: H1 EVALUATION PROVES THE ARMING AND BLACKOUT LOGIC ONLY.          #
#  ANY REPLAY P&L FOR S4 IS NOT EVIDENCE OF ANYTHING. It is not a weak result,  #
#  a preliminary result, or a result to be quoted with a caveat — the harness   #
#  is measuring a strategy this pod does not implement. Performance claims      #
#  require tick data, which arrives with the Windows box.                      #
#                                                                              #
#  What CAN be verified today, and is, in tests/test_s3_s4.py: that the pod     #
#  arms only inside its window, that it refuses to trade into a blackout, and   #
#  that its pullback arithmetic is correct.                                    #
################################################################################

ENTER THE PULLBACK, NEVER THE PRINT. The arm window opens at
config.S4_ARM_AFTER_MIN, deliberately AFTER the release, so the pod is never
in the market for the spike itself. The spike is where the spread gaps, the
fills are worst and the direction is least knowable; the pod waits for it to
happen to someone else.

THE BLACKOUT BELT IS THE POD'S OWN. risk/validator.py RULE 1 and the kernel
both guard against trading into an imminent high-impact event. This pod checks
independently, because the situation it is uniquely exposed to — armed by one
release while a SECOND is minutes away — is precisely the case where being
right about the first event does not help.
"""
import logging
import math
from typing import Any, Dict, List, Optional

import config
from pods.base import Intent

logger = logging.getLogger(__name__)

POD_NAME = "S4_NEWSBURST"


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def armed_event(recent_events: Any) -> Optional[Dict[str, Any]]:
    """The first released event inside the arm window, or None."""
    if not isinstance(recent_events, (list, tuple)):
        return None
    for event in recent_events:
        if not isinstance(event, dict):
            continue
        minutes_since = _finite(event.get("minutes_since"))
        if minutes_since is None:
            continue
        if config.S4_ARM_AFTER_MIN <= minutes_since <= config.S4_ARM_UNTIL_MIN:
            return event
    return None


def in_blackout(upcoming_events: Any) -> bool:
    """
    True if a HIGH-impact event is due within config.EVENT_BLOCK_MINUTES.

    The pod's own belt. Being correctly armed by one release is no defence
    against walking into the next one.
    """
    if not isinstance(upcoming_events, (list, tuple)):
        return False
    for event in upcoming_events:
        if not isinstance(event, dict):
            continue
        if str(event.get("impact", "")).upper() != "HIGH":
            continue
        minutes_until = _finite(event.get("minutes_until"))
        if minutes_until is None:
            continue
        if 0 <= minutes_until <= config.EVENT_BLOCK_MINUTES:
            return True
    return False


class S4NewsBurst:
    """Enter the pullback after a high-impact USD release. Tick-native."""

    name = POD_NAME

    def evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        """Return an Intent, or None. NEVER raises (INVARIANT 6)."""
        try:
            return self._evaluate(tick or {}, state or {})
        except Exception:
            logger.exception("%s: evaluate raised; treating as no-trade", self.name)
            return None

    def _evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        # 1. Is a release recent enough to still be reverberating, but old
        #    enough that the spike is over?
        event = armed_event(state.get("recent_high_events"))
        if event is None:
            return None

        # 2. The pod's own blackout belt, checked even while armed.
        if in_blackout(state.get("upcoming_events")):
            logger.debug(
                "%s: armed by %r but another HIGH event is imminent; standing down",
                self.name, event.get("name"),
            )
            return None

        burst = state.get("burst_bar")
        if not isinstance(burst, dict):
            logger.debug("%s: no burst bar supplied; no trade", self.name)
            return None

        bar_open = _finite(burst.get("open"))
        bar_close = _finite(burst.get("close"))
        atr = _finite(state.get("atr_h1"))

        if bar_open is None or bar_close is None:
            logger.debug("%s: burst bar missing open/close; no trade", self.name)
            return None
        if atr is None or atr <= 0:
            logger.debug("%s: atr_h1 unavailable; no trade", self.name)
            return None

        burst_move = bar_close - bar_open
        if burst_move == 0:
            # No burst, no direction to continue.
            return None

        # 3. Continue the burst, entered on a retrace of it.
        direction = "LONG" if burst_move > 0 else "SHORT"
        pullback = abs(burst_move) * config.S4_PULLBACK_FRACTION
        entry = bar_close - pullback if direction == "LONG" else bar_close + pullback

        stop_distance = config.S4_STOP_ATR_MULT * atr
        tp_distance = config.S4_TP_ATR_MULT * atr
        if direction == "LONG":
            stop = entry - stop_distance
            tp = entry + tp_distance
        else:
            stop = entry + stop_distance
            tp = entry - tp_distance

        if entry <= 0 or stop <= 0 or tp <= 0:
            logger.debug("%s: computed a non-positive price; no trade", self.name)
            return None

        edge = (
            abs(tp - entry)
            * config.CONTRACT_SIZE_OZ
            * config.S4_LOTS
            * config.EDGE_HAIRCUT
        )
        if edge <= 0:
            return None

        return Intent(
            pod=self.name,
            direction=direction,
            lots=config.S4_LOTS,
            entry_price=entry,
            stop_price=stop,
            tp_price=tp,
            expected_edge_usd=edge,
            reason=(
                f"news-burst: {event.get('name')} released "
                f"{_finite(event.get('minutes_since')):.1f}m ago, burst {burst_move:+.2f}, "
                f"entering {config.S4_PULLBACK_FRACTION:.0%} pullback at {entry:.2f}"
            ),
        )
