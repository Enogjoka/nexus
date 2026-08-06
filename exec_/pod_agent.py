"""
The pod agent — where pods stop being theory.

On every bar it refreshes the session VWAP and ATR, asks the doctrine which
pods may run, sweeps them, and hands each Intent to the router. Outcomes come
back on the BUS and are booked against the originating pod's breakers.

S4 IS DELIBERATELY NOT REGISTERED, AND THAT IS NOT AN OVERSIGHT.
S4_NEWSBURST is tick-native: its burst and its pullback occur inside a single
H1 bar, so at this resolution the agent cannot tell whether the pullback it
would be entering has already happened. Registering it would produce trades,
and those trades would produce a P&L, and that P&L would eventually be quoted
as evidence for a strategy nobody has actually tested. It stays out until tick
data exists, and the agent says so out loud at boot.

ONE ATR DEFINITION. The pods are scaled in ATR, so replay and live must
compute it identically or a pod that backtested well is a different pod in
production. AtrTracker below is the same SMA-of-true-range that
tests/replay.py uses. See the note on it: the two copies must be merged the
next time either file is in scope.

THE POD STATE CONTRACT (established in Task 19, honoured here):
    atr_h1, vwap, vwap_stdev, regime_h1, session_label, volume, prev_volume
    basis            (S3)
    recent_high_events, upcoming_events, burst_bar   (S4, unused today)
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from core import database
from core.state import BUS, STATE
from pods.base import PodSupervisor
from pods.s1_fixfade import S1FixFade
from pods.s2_vwapsnap import S2VwapSnap
from pods.s3_basis import S3Basis
from pods.vwap import SessionVWAP, session_of

logger = logging.getLogger(__name__)

ATR_PERIOD = 14


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


class AtrTracker:
    """
    SMA of true range over ATR_PERIOD bars.

    DUPLICATE OF tests/replay.py::_AtrTracker, and deliberately identical line
    for line. The replay harness and the live agent MUST agree on what an ATR
    is, or a pod that backtested at one stop distance trades at another. The
    honest fix is a single shared helper; neither pods/ nor tests/replay.py was
    in scope for this task beyond one unrelated change, so the duplication is
    flagged here rather than smuggled in. Merge them next time either is open.
    """

    def __init__(self, period: int = ATR_PERIOD) -> None:
        self._period = period
        self._ranges: List[float] = []
        self._previous_close: Optional[float] = None

    def update(self, bar: Dict[str, Any]) -> None:
        high, low, close = bar.get("high"), bar.get("low"), bar.get("close")
        high, low, close = _num(high), _num(low), _num(close)
        if high is None or low is None or close is None:
            return
        span = high - low
        if self._previous_close is None:
            true_range = span
        else:
            true_range = max(
                span, abs(high - self._previous_close), abs(low - self._previous_close)
            )
        self._ranges.append(true_range)
        if len(self._ranges) > self._period:
            self._ranges.pop(0)
        self._previous_close = close

    def value(self) -> Optional[float]:
        if len(self._ranges) < self._period:
            return None
        return sum(self._ranges) / len(self._ranges)


def build_supervisor(now_utc=None) -> PodSupervisor:
    """
    The live pod roster. S4 is absent — see the module docstring.

    The absence is logged at WARNING rather than INFO so it appears in any
    reasonable production log filter: a missing strategy should be visible.
    """
    supervisor = PodSupervisor([S1FixFade(), S2VwapSnap(), S3Basis()], now_utc=now_utc)
    logger.warning(
        "pod_agent: S4_NEWSBURST is NOT registered — it is tick-native and cannot be "
        "evaluated at H1. Any P&L it produced here would be meaningless. It joins "
        "when tick data does."
    )
    logger.info("pod_agent: registered pods %s", supervisor.pod_names)
    return supervisor


def build_pod_state(
    payload: Any,
    bar: Dict[str, Any],
    vwap_tracker: SessionVWAP,
    atr_tracker: AtrTracker,
    previous_volume: Optional[float],
    now_utc: datetime,
) -> Dict[str, Any]:
    """Assemble the Task 19 state contract from this bar and AppState."""
    timeframes = (payload or {}).get("timeframes") or {}
    h1 = timeframes.get("1h") or {}

    # Prefer the data agent's own ATR when it published one; fall back to our
    # tracker so the agent still works from bare bars.
    atr = _num((h1.get("indicators") or {}).get("atr14")) or atr_tracker.value()

    return {
        "atr_h1": atr,
        "vwap": vwap_tracker.vwap(),
        "vwap_stdev": vwap_tracker.stdev(),
        "regime_h1": h1.get("regime"),
        "session_label": session_of(bar["ts"]) if isinstance(bar.get("ts"), datetime) else None,
        "volume": _num(bar.get("volume")),
        "prev_volume": previous_volume,
        "basis": STATE.get_market_data("basis"),
        "recent_high_events": STATE.get_market_data("recent_high_events") or [],
        "upcoming_events": STATE.get_market_data("upcoming_events") or [],
    }


def submit_intents(router, intents, now_utc: datetime) -> List[Dict[str, Any]]:
    """
    Hand each Intent to the router as a POD order.

    The intent's own stated edge travels with it: the cost gate judges the pod
    on the number the pod claimed, not on one this layer invented.
    """
    outcomes = []
    for intent in intents:
        signal = {
            "id": f"{intent.pod}-{int(now_utc.timestamp())}",
            "direction": intent.direction,
            "lots": intent.lots,
            "source": "POD",
            "pod": intent.pod,
            "expected_edge_usd": intent.expected_edge_usd,
            "prices": {
                "entry": intent.entry_price,
                "stop": intent.stop_price,
                "tp1": intent.tp_price,
                "tp2": intent.tp_price,
            },
        }
        try:
            outcome = router.submit(signal)
        except Exception:
            logger.exception("pod_agent: router.submit raised for %s", intent.pod)
            continue

        logger.info(
            "pod_agent: %s -> %s (%s)",
            intent.pod, outcome.get("outcome"), outcome.get("reason", ""),
        )
        outcomes.append(outcome)
    return outcomes


def run_pod_agent(router) -> None:
    """
    Subscribe to "market_update" and run the pod sweep on each bar.

    The router is INJECTED by backend.py, not imported from it: backend.py runs
    as __main__, so `import backend` from an agent thread would build a second
    module object whose execution stack is None.

    Subscription-style: returns after subscribing (kind="subscriber").
    """
    from exec_.paper_engine import _latest_h1_bar

    supervisor = build_supervisor()
    vwap_tracker = SessionVWAP()
    atr_tracker = AtrTracker()
    seen = {"prev_volume": None}

    def _on_market_update(payload) -> None:
        try:
            bar = _latest_h1_bar(config.YF_SYMBOL)
            if bar is None:
                logger.warning("pod_agent: no H1 bar available; no sweep")
                return

            now = datetime.now(timezone.utc)
            ts = bar.get("ts") if isinstance(bar.get("ts"), datetime) else now
            vwap_tracker.update(bar, session_of(ts))
            atr_tracker.update(bar)

            state = build_pod_state(
                payload, bar, vwap_tracker, atr_tracker, seen["prev_volume"], now
            )
            seen["prev_volume"] = _num(bar.get("volume"))

            # The desk decides which pods may speak at all.
            try:
                from ai.doctrine import HOLDER

                supervisor.update_from_doctrine(HOLDER.current(now))
            except Exception:
                logger.warning(
                    "pod_agent: could not read the doctrine; pods stay as they were",
                    exc_info=True,
                )

            close = _num(bar.get("close"))
            if close is None:
                return
            spread = config.SIM_SPREAD_USD
            tick = {
                "ts": ts,
                "bid": close - spread / 2.0,
                "ask": close + spread / 2.0,
                "mid": close,
            }

            intents = supervisor.evaluate_all(tick, state, now)
            if intents:
                submit_intents(router, intents, now)
        except Exception:
            logger.exception("pod_agent: market_update handler raised; continuing")

    def _on_position_event(payload) -> None:
        """Book a closed pod position against its own breakers."""
        try:
            event = (payload or {}).get("event")
            pod = (payload or {}).get("pod")
            if event != "CLOSED" or not pod:
                return
            pnl = _num((payload or {}).get("realized_pnl_usd"))
            if pnl is None:
                return
            supervisor.record_outcome(pod, pnl, datetime.now(timezone.utc))
            logger.info("pod_agent: booked %.2f for %s", pnl, pod)
        except Exception:
            logger.exception("pod_agent: position_event handler raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    BUS.subscribe("position_event", _on_position_event)
    logger.info("pod agent subscribed to market_update and position_event")
