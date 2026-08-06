"""
The position manager — one engine for both sources.

Swing positions and pod positions differ in where they came from and in
nothing else that matters here: both have an entry, a stop, targets, and a
need for exactly one component to decide when they close. Two managers would
eventually disagree about who owns a stop, and that disagreement surfaces as a
position nobody closed.

IT MANAGES ONLY ITS OWN TABLE, AND THAT IS THE ANTI-DOUBLE-MANAGEMENT RULE.
At PAPER, exec_/paper_engine.py still owns signal-based swing fills end to
end — it models its own fills against `signals` and always has. This engine
touches only rows in `positions`, which today means router-opened positions,
which today means pods. When the swing cutover happens at SHADOW on the
Windows box, swing orders start flowing through the router and appear here
automatically. Until then the two never see the same position, because they
read different tables.

STOP FIRST, ALWAYS. When a bar's range contains both the stop and a target,
the stop wins. H1 cannot say which came first and the pessimistic reading is
the only honest one — the paper engine's precedent, and it must stay
pessimistic here for the same reason: a manager that resolves its own
ambiguity favourably reports profits it never made.

THE LIFECYCLE
    OPEN      -> stop hit            -> CLOSED (STOP)
    OPEN      -> tp1 hit             -> half closed, stop to entry -> BE
    BE        -> price advances      -> stop follows at ATR distance -> TRAILING
    TRAILING  -> tp2 hit             -> CLOSED (TP2)
    any       -> doctrine flip       -> stop tightened to entry (SWING only)
    any POD   -> Friday 20:30 UTC    -> CLOSED (EOW_FLAT)

THE TRAILING STOP NEVER MOVES BACKWARD. Not by a cent, not "temporarily". A
stop that can retreat is not a stop, it is a suggestion, and the one thing a
trailing stop exists to guarantee is that realised risk only ever shrinks.

Every transition writes the positions row, logs, and publishes "position_event"
on the BUS so the pod supervisor can book the outcome against the right pod's
breakers.
"""
import logging
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional

import config
from core import database
from core.state import BUS
from exec_ import router as router_mod

logger = logging.getLogger(__name__)

# The stop-multiple used when trailing, per pod. Swing positions have no pod
# and use the swing default.
_POD_TRAIL_MULT = {
    "S1_FIXFADE": "S1_STOP_ATR_MULT",
    "S2_VWAPSNAP": "S2_STOP_ATR_MULT",
    "S3_BASIS": "S3_STOP_ATR_MULT",
    "S4_NEWSBURST": "S4_STOP_ATR_MULT",
}


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def trail_mult_for(pod: Optional[str]) -> float:
    """The ATR multiple this position trails at."""
    name = _POD_TRAIL_MULT.get(pod or "")
    if name is None:
        return float(config.TRAIL_ATR_MULT_SWING)
    return float(getattr(config, name, config.TRAIL_ATR_MULT_SWING))


def is_eow_flat_time(now_utc: datetime) -> bool:
    """Friday at/after EOW_FLAT_HOUR:MINUTE UTC — the weekend gap is coming."""
    if not config.EOW_FLAT_ENABLED:
        return False
    moment = now_utc.astimezone(timezone.utc)
    if moment.weekday() != config.EOW_FLAT_WEEKDAY:
        return False
    cutoff = time(config.EOW_FLAT_HOUR_UTC, config.EOW_FLAT_MINUTE_UTC)
    return moment.timetz().replace(tzinfo=None) >= cutoff


def doctrine_conflicts(bias: Optional[str], direction: str) -> bool:
    """
    True when the desk's posture has turned against an open position.

    FLAT conflicts with everything: standing down is a view about all
    exposure, not only about new exposure.
    """
    if bias is None:
        return False
    if bias == "FLAT":
        return True
    if bias == "LONG_ONLY":
        return direction == "SHORT"
    if bias == "SHORT_ONLY":
        return direction == "LONG"
    return False


def trailed_stop(direction: str, current_stop: float, price: float, distance: float) -> float:
    """
    The new stop, which is never worse than the old one.

    max() for a LONG and min() for a SHORT is the entire guarantee: whatever
    the price does, the stop only ratchets toward it.
    """
    if direction == "LONG":
        return max(current_stop, price - distance)
    return min(current_stop, price + distance)


def load_live_positions(conn) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT client_order_id, source, pod, direction, lots, entry_px, stop_px, "
            "tp1_px, tp2_px, state FROM positions WHERE state = ANY(%s) ORDER BY id",
            (list(router_mod.LIVE_STATES),),
        )
        rows = cur.fetchall()

    return [
        {
            "client_order_id": r[0],
            "source": r[1],
            "pod": r[2],
            "direction": r[3],
            "lots": _num(r[4]),
            "entry_px": _num(r[5]),
            "stop_px": _num(r[6]),
            "tp1_px": _num(r[7]),
            "tp2_px": _num(r[8]),
            "state": r[9],
        }
        for r in rows
    ]


class PositionEngine:
    """
    Evaluates every live position against each new bar.

    The router is injected rather than imported-and-constructed so the engine
    can be driven in tests without a bridge, and so the process has exactly one
    router (backend.py builds it at boot).
    """

    def __init__(self, router) -> None:
        self._router = router

    # -- persistence -------------------------------------------------------

    def _update(self, client_order_id: str, **fields) -> None:
        assignments = ", ".join(f"{key} = %s" for key in fields)
        try:
            database.execute(
                f"UPDATE positions SET {assignments} WHERE client_order_id = %s",
                (*fields.values(), client_order_id),
            )
        except Exception:
            logger.error(
                "position_engine: could not update %s (%s)", client_order_id, fields, exc_info=True
            )

    def _publish(self, position: Dict[str, Any], event: str, **extra) -> None:
        payload = {
            "client_order_id": position["client_order_id"],
            "source": position["source"],
            "pod": position["pod"],
            "direction": position["direction"],
            "event": event,
            **extra,
        }
        try:
            BUS.publish("position_event", payload)
        except Exception:
            logger.exception("position_engine: position_event publish failed")

    def _close(self, position: Dict[str, Any], exit_px: float, reason: str) -> None:
        """Close through the router, book the P&L, tell everyone."""
        try:
            self._router.close(position["client_order_id"])
        except Exception:
            logger.exception(
                "position_engine: router.close raised for %s; recording the close anyway",
                position["client_order_id"],
            )

        pnl = self.realized_pnl(position, exit_px)
        self._update(
            position["client_order_id"],
            state=router_mod.STATE_CLOSED,
            closed_at=datetime.now(timezone.utc),
            realized_pnl_usd=pnl,
            close_reason=reason,
        )
        logger.info(
            "position_engine: CLOSED %s (%s) at %.5f pnl=%.2f",
            position["client_order_id"], reason, exit_px, pnl,
        )
        self._publish(
            position, "CLOSED", reason=reason, exit_px=exit_px, realized_pnl_usd=pnl
        )

    @staticmethod
    def realized_pnl(position: Dict[str, Any], exit_px: float) -> float:
        entry = position.get("entry_px")
        lots = position.get("lots") or 0.0
        if entry is None:
            return 0.0
        move = exit_px - entry if position["direction"] == "LONG" else entry - exit_px
        return move * config.CONTRACT_SIZE_OZ * lots

    # -- the sweep ---------------------------------------------------------

    def evaluate(
        self,
        position: Dict[str, Any],
        bar: Dict[str, Any],
        now_utc: datetime,
        atr: Optional[float] = None,
        bias: Optional[str] = None,
    ) -> str:
        """
        Advance ONE position against ONE bar. Returns the action taken.

        The order of checks is the risk order: stop, then end-of-week, then
        doctrine, then targets. Anything that closes the position comes before
        anything that merely adjusts it.
        """
        direction = position["direction"]
        stop = position["stop_px"]
        high = _num(bar.get("high"))
        low = _num(bar.get("low"))
        close = _num(bar.get("close"))
        if high is None or low is None or close is None or stop is None:
            return "NO_BAR"

        # 1. STOP FIRST. An H1 bar that spans both cannot say which came first.
        stop_hit = low <= stop if direction == "LONG" else high >= stop
        if stop_hit:
            self._close(position, stop, "STOP")
            return "STOP"

        # 2. End-of-week flat, pods only. A swing thesis survives a weekend; a
        #    scalp held over a gap is a different trade than the one taken.
        if position["source"] == "POD" and is_eow_flat_time(now_utc):
            self._close(position, close, "EOW_FLAT")
            return "EOW_FLAT"

        # 3. tp2 closes the remainder.
        tp2 = position["tp2_px"]
        if tp2 is not None:
            tp2_hit = high >= tp2 if direction == "LONG" else low <= tp2
            if tp2_hit:
                self._close(position, tp2, "TP2")
                return "TP2"

        # 4. tp1, once, takes half off and moves the stop to entry.
        tp1 = position["tp1_px"]
        if tp1 is not None and position["state"] == router_mod.STATE_OPEN:
            tp1_hit = high >= tp1 if direction == "LONG" else low <= tp1
            if tp1_hit:
                return self._take_partial(position, tp1)

        # 5. Doctrine flip — tighten a SWING to break-even, never close it.
        if position["source"] == "SWING" and doctrine_conflicts(bias, direction):
            return self._tighten_to_be(position, bias)

        # 6. Trail, once break-even has been reached.
        if position["state"] in (router_mod.STATE_BE, router_mod.STATE_TRAILING) and atr:
            return self._trail(position, close, atr)

        return "HOLD"

    def _take_partial(self, position: Dict[str, Any], tp1: float) -> str:
        half = round((position["lots"] or 0.0) * config.POSITION_TP1_FRACTION, 2)
        pnl = self.realized_pnl({**position, "lots": half}, tp1)
        entry = position["entry_px"]

        try:
            self._router.close(position["client_order_id"])
        except Exception:
            logger.exception(
                "position_engine: partial close via router failed for %s",
                position["client_order_id"],
            )

        remaining = round((position["lots"] or 0.0) - half, 2)
        self._update(
            position["client_order_id"],
            state=router_mod.STATE_BE,
            lots=remaining,
            stop_px=entry,
            realized_pnl_usd=pnl,
        )
        logger.info(
            "position_engine: TP1 on %s — closed %.2f lots for %.2f, stop to break-even %.5f",
            position["client_order_id"], half, pnl, entry,
        )
        self._publish(position, "PARTIAL", reason="TP1", exit_px=tp1, realized_pnl_usd=pnl)
        return "TP1_PARTIAL"

    def _tighten_to_be(self, position: Dict[str, Any], bias: Optional[str]) -> str:
        if config.DOCTRINE_FLIP_POLICY != "TIGHTEN_BE":
            logger.error(
                "position_engine: DOCTRINE_FLIP_POLICY %r is not implemented; holding",
                config.DOCTRINE_FLIP_POLICY,
            )
            return "HOLD"

        entry = position["entry_px"]
        if entry is None:
            return "HOLD"

        tightened = trailed_stop(
            position["direction"], position["stop_px"], entry, 0.0
        )
        if tightened == position["stop_px"]:
            return "HOLD"   # already at or beyond break-even

        self._update(position["client_order_id"], stop_px=entry)
        logger.warning(
            "position_engine: doctrine flipped to %s against %s %s — stop tightened to "
            "break-even %.5f",
            bias, position["direction"], position["client_order_id"], entry,
        )
        self._publish(position, "DOCTRINE_FLIP", reason=bias, stop_px=entry)
        return "DOCTRINE_FLIP"

    def _trail(self, position: Dict[str, Any], price: float, atr: float) -> str:
        distance = trail_mult_for(position["pod"]) * atr
        new_stop = trailed_stop(position["direction"], position["stop_px"], price, distance)
        if new_stop == position["stop_px"]:
            return "HOLD"

        self._update(
            position["client_order_id"], stop_px=new_stop, state=router_mod.STATE_TRAILING
        )
        logger.info(
            "position_engine: trailing %s %.5f -> %.5f",
            position["client_order_id"], position["stop_px"], new_stop,
        )
        self._publish(position, "TRAIL", stop_px=new_stop)
        return "TRAIL"

    def evaluate_all(
        self, bar: Dict[str, Any], now_utc: datetime, atr: Optional[float], bias: Optional[str]
    ) -> Dict[str, str]:
        """One pass over every live position. Never raises (INVARIANT 6)."""
        actions: Dict[str, str] = {}
        try:
            with database.get_conn() as conn:
                positions = load_live_positions(conn)
        except Exception:
            logger.exception("position_engine: could not load positions; skipping this bar")
            return actions

        for position in positions:
            try:
                actions[position["client_order_id"]] = self.evaluate(
                    position, bar, now_utc, atr=atr, bias=bias
                )
            except Exception:
                logger.exception(
                    "position_engine: evaluating %s raised; leaving it untouched",
                    position["client_order_id"],
                )
        return actions


def run_position_engine(router) -> None:
    """
    Subscribe to "market_update" and manage every live position on each bar.

    The router is INJECTED by backend.py rather than imported from it. Importing
    would look harmless and would not be: backend.py runs as __main__, so
    `import backend` from an agent thread builds a SECOND module object whose
    execution stack was never constructed, and every lookup returns None.
    Injection also keeps this module testable without a process.

    Subscription-style: returns after subscribing (kind="subscriber").
    """
    from exec_.paper_engine import _latest_h1_bar  # the existing bar reader

    engine = PositionEngine(router)

    def _on_market_update(payload) -> None:
        try:
            bar = _latest_h1_bar(config.YF_SYMBOL)
            if bar is None:
                logger.warning("position_engine: no H1 bar available; nothing to evaluate")
                return

            now = datetime.now(timezone.utc)
            atr = _atr_from_payload(payload)
            bias = _current_bias(now)
            actions = engine.evaluate_all(bar, now, atr, bias)
            moved = {k: v for k, v in actions.items() if v != "HOLD"}
            if moved:
                logger.info("position_engine: %s", moved)
        except Exception:
            logger.exception("position_engine: market_update handler raised; continuing")

    BUS.subscribe("market_update", _on_market_update)
    logger.info("position engine subscribed to market_update")


def _atr_from_payload(payload: Any) -> Optional[float]:
    try:
        return _num(((payload or {}).get("timeframes") or {}).get("1h", {})
                    .get("indicators", {}).get("atr14"))
    except Exception:
        return None


def _current_bias(now_utc: datetime) -> Optional[str]:
    """
    The desk's current posture, or None if it cannot be read.

    Imported lazily so this module carries no import-time dependency on the AI
    package — the engine must manage positions with every model dead.
    """
    try:
        from ai.doctrine import HOLDER

        return HOLDER.current(now_utc).bias
    except Exception:
        logger.warning("position_engine: could not read the doctrine; holding", exc_info=True)
        return None
