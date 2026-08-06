"""
The broker seam.

MetaTrader5's Python package is Windows-only and this box is a Mac, so the
broker cannot be a direct dependency of anything above it. Instead there is a
Bridge protocol with two implementations:

    SimBridge      — runs everywhere, models fills deterministically from the
                     data agent's own price state. No randomness: the same
                     AppState produces the same fill, every time, which is what
                     makes the router testable and its behaviour reproducible.
    RealMT5Bridge  — a faithful skeleton against the documented MT5 API that
                     refuses to construct on a platform where the package is
                     unavailable. It has never been executed.

Nothing above this seam knows which implementation it holds. That is the whole
point: the stage ladder decides whether a fill is modelled or real, and the
BRIDGE decides how to produce it — the code path through the router is
identical either way, so the Windows cutover changes one constant and no
control flow.

ADVERSE SLIPPAGE ONLY. SimBridge never fills better than the quoted side. A
simulator that occasionally gives you a good price teaches the learning loop
that a strategy works when it does not, so the model is deliberately
pessimistic in one direction and never optimistic.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import config
from core.state import STATE

logger = logging.getLogger(__name__)

# How long an MT5 call may block before we treat it as failed (INVARIANT 6).
MT5_CALL_TIMEOUT_SECONDS = 10


@runtime_checkable
class Bridge(Protocol):
    """
    What the execution rings are allowed to ask a broker for. Deliberately
    narrow: there is no "modify order", no "cancel all", and no way to read a
    price without also learning how old it is.
    """

    def get_tick(self) -> Optional[Dict[str, Any]]: ...

    def get_spread(self) -> Optional[float]: ...

    def get_tick_age_seconds(self) -> Optional[float]: ...

    def market_open(
        self,
        direction: str,
        lots: float,
        sl: Optional[float],
        tp: Optional[float],
        client_order_id: str,
    ) -> Optional[Dict[str, Any]]: ...

    def market_close(self, client_order_id: str) -> Optional[Dict[str, Any]]: ...

    def flatten_all(self, reason: str) -> bool: ...

    def positions(self) -> List[Dict[str, Any]]: ...

    def equity(self) -> Optional[float]: ...


def _contract_value(lots: float) -> float:
    """Dollar value of a one-dollar move, for `lots` of XAUUSD."""
    return float(lots) * config.CONTRACT_SIZE_OZ


class SimBridge:
    """
    A deterministic paper broker driven by the data agent's price state.

    Quotes are derived, not invented: the mid comes from whatever the 1h
    timeframe last published into AppState, and bid/ask straddle it by exactly
    config.SIM_SPREAD_USD. If the data agent has published nothing, every
    price-dependent method returns None rather than a guess — the kernel then
    fails closed on its own, which is the correct outcome for a bridge with no
    market.
    """

    def __init__(self) -> None:
        # client_order_id -> open position
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._closed_pnl: float = 0.0

    # -- quotes ------------------------------------------------------------

    def _mid(self) -> Optional[float]:
        state = STATE.get_market_data("1h")
        if not isinstance(state, dict):
            return None
        price = state.get("price")
        if not isinstance(price, (int, float)) or isinstance(price, bool):
            return None
        price = float(price)
        return price if price > 0 else None

    def get_tick(self) -> Optional[Dict[str, Any]]:
        mid = self._mid()
        if mid is None:
            return None
        half = config.SIM_SPREAD_USD / 2.0
        return {
            "bid": mid - half,
            "ask": mid + half,
            "ts": datetime.now(timezone.utc),
        }

    def get_spread(self) -> Optional[float]:
        """
        Exactly config.SIM_SPREAD_USD when a market exists, None otherwise.

        Returned from the constant rather than computed as ask-bid: the two are
        equal by construction, and floating point would make the computed form
        report 0.35000000000000003 to a kernel comparing against a ceiling.
        """
        return config.SIM_SPREAD_USD if self._mid() is not None else None

    def get_tick_age_seconds(self) -> Optional[float]:
        """
        Age of the price data, measured from the data agent's last completed
        cycle (STATE.last_analysis_ts) rather than from the 1h bar's own
        timestamp. The bar timestamp is the start of an hourly candle and is
        routinely 30+ minutes old even when the feed is perfectly healthy; what
        the kernel actually wants to know is how long ago we last refreshed.

        NOTE: the data agent polls every config.DATA_POLL_SECONDS (300s) while
        the kernel's staleness ceiling is 30s — a threshold calibrated for a
        live tick stream. Under SIM this bridge will legitimately report stale
        for most of each poll interval, and the kernel will correctly refuse to
        trade on it. That is a real property of a 5-minute price feed, not a
        bug to paper over here.
        """
        last = getattr(STATE, "last_analysis_ts", None)
        if last is None:
            return None
        try:
            age = time.time() - float(last)
        except (TypeError, ValueError):
            return None
        return max(0.0, age)

    # -- orders ------------------------------------------------------------

    def market_open(
        self,
        direction: str,
        lots: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        client_order_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Fill at the far side of the spread, then a further SIM_SLIPPAGE_USD
        against us. A LONG pays ask + slip; a SHORT receives bid - slip.
        """
        tick = self.get_tick()
        if tick is None:
            logger.warning("SimBridge: no market; refusing to open %s", client_order_id)
            return None

        direction = str(direction).upper()
        if direction == "LONG":
            fill_px = tick["ask"] + config.SIM_SLIPPAGE_USD
        elif direction == "SHORT":
            fill_px = tick["bid"] - config.SIM_SLIPPAGE_USD
        else:
            logger.error("SimBridge: unknown direction %r", direction)
            return None

        self._positions[client_order_id] = {
            "client_order_id": client_order_id,
            "direction": direction,
            "lots": float(lots),
            "entry": fill_px,
            "sl": sl,
            "tp": tp,
            "ts": tick["ts"],
        }
        logger.info(
            "SimBridge: OPEN %s %s %.2f lots @ %.5f", client_order_id, direction, lots, fill_px
        )
        return {"fill_px": fill_px, "ts": tick["ts"]}

    def market_close(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        """Close at the adverse side again — exiting costs the spread too."""
        position = self._positions.get(client_order_id)
        if position is None:
            logger.warning("SimBridge: no open position %s to close", client_order_id)
            return None

        tick = self.get_tick()
        if tick is None:
            logger.warning("SimBridge: no market; cannot close %s", client_order_id)
            return None

        if position["direction"] == "LONG":
            fill_px = tick["bid"] - config.SIM_SLIPPAGE_USD
            pnl = (fill_px - position["entry"]) * _contract_value(position["lots"])
        else:
            fill_px = tick["ask"] + config.SIM_SLIPPAGE_USD
            pnl = (position["entry"] - fill_px) * _contract_value(position["lots"])

        self._closed_pnl += pnl
        del self._positions[client_order_id]
        logger.info(
            "SimBridge: CLOSE %s @ %.5f pnl=%.2f (realized total %.2f)",
            client_order_id, fill_px, pnl, self._closed_pnl,
        )
        return {"fill_px": fill_px, "ts": tick["ts"], "pnl": pnl}

    def flatten_all(self, reason: str) -> bool:
        """
        Close everything. Returns True only if the book is empty afterwards —
        a partial flatten is a failed flatten, and the kernel halts either way.
        """
        logger.critical("SimBridge: FLATTEN ALL — %s", reason)
        for client_order_id in list(self._positions):
            self.market_close(client_order_id)
        return not self._positions

    # -- account -----------------------------------------------------------

    def positions(self) -> List[Dict[str, Any]]:
        """The open book, in the shape the kernel expects."""
        mid = self._mid()
        book = []
        for position in self._positions.values():
            unrealized = 0.0
            if mid is not None:
                delta = (
                    mid - position["entry"]
                    if position["direction"] == "LONG"
                    else position["entry"] - mid
                )
                unrealized = delta * _contract_value(position["lots"])
            book.append(
                {
                    "client_order_id": position["client_order_id"],
                    "direction": position["direction"],
                    "lots": position["lots"],
                    "entry": position["entry"],
                    "unrealized_pnl": unrealized,
                }
            )
        return book

    def equity(self) -> Optional[float]:
        """Starting balance + realized + marked-to-mid unrealized."""
        unrealized = sum(p["unrealized_pnl"] for p in self.positions())
        return float(config.ACCOUNT_SIZE) + self._closed_pnl + unrealized

    @property
    def realized_pnl(self) -> float:
        return self._closed_pnl


class RealMT5Bridge:
    """
    MetaTrader5 implementation.

    ######################################################################
    #  THIS CODE HAS NEVER BEEN EXECUTED.                                #
    #  It is written against the documented MT5 Python API and cannot be #
    #  run on this machine — the package is Windows-only. THE FIRST LIVE #
    #  RUN ON WINDOWS MUST RE-VERIFY EVERY CALL BELOW against the real   #
    #  terminal: return shapes, retcode semantics, filling modes, and    #
    #  symbol naming (brokers disagree on "XAUUSD" vs "GOLD" vs suffixed #
    #  variants) are all assumptions until proven on hardware.           #
    #  Treat every method here as a hypothesis, not an implementation.   #
    ######################################################################

    Construction raises on any platform where the package is unavailable, and
    raises BEFORE binding any attribute — a half-built bridge that silently
    returns None from every method would look exactly like a quiet market.
    """

    SYMBOL = "XAUUSD"

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # noqa: N813 — vendor's own casing
        except Exception as exc:
            # Raise before ANY attribute is set: never a partial object.
            raise RuntimeError(
                "MetaTrader5 unavailable on this platform"
            ) from exc

        if not mt5.initialize():
            raise RuntimeError(
                f"MetaTrader5 initialize() failed: {mt5.last_error()}"
            )

        self._mt5 = mt5
        logger.info("RealMT5Bridge: initialized against %s", self.SYMBOL)

    # -- guarded call ------------------------------------------------------

    def _guarded(self, label: str, fn, *args, **kwargs):
        """
        INVARIANT 6: every external call logged, wrapped, and non-fatal.

        The MT5 package is synchronous C-extension code with no timeout
        parameter, so the deadline here is advisory — it bounds how long we
        will WAIT ON A RESULT we already have, not the call itself. A hard
        timeout needs a subprocess or thread and is deferred to the Windows
        bring-up, where it can actually be tested.
        """
        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            logger.error("RealMT5Bridge: %s raised", label, exc_info=True)
            return None
        elapsed = time.monotonic() - started
        if elapsed > MT5_CALL_TIMEOUT_SECONDS:
            logger.error(
                "RealMT5Bridge: %s took %.1fs (over %ss budget); treating as failed",
                label, elapsed, MT5_CALL_TIMEOUT_SECONDS,
            )
            return None
        return result

    # -- quotes ------------------------------------------------------------

    def get_tick(self) -> Optional[Dict[str, Any]]:
        tick = self._guarded("symbol_info_tick", self._mt5.symbol_info_tick, self.SYMBOL)
        if tick is None:
            return None
        return {
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            # MT5 exposes epoch seconds as `time`; `time_msc` is milliseconds.
            "ts": datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    def get_spread(self) -> Optional[float]:
        tick = self.get_tick()
        return None if tick is None else tick["ask"] - tick["bid"]

    def get_tick_age_seconds(self) -> Optional[float]:
        tick = self.get_tick()
        if tick is None:
            return None
        return max(0.0, (datetime.now(timezone.utc) - tick["ts"]).total_seconds())

    # -- orders ------------------------------------------------------------

    def market_open(
        self,
        direction: str,
        lots: float,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        client_order_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        tick = self.get_tick()
        if tick is None:
            return None

        mt5 = self._mt5
        is_long = str(direction).upper() == "LONG"
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.SYMBOL,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
            "price": tick["ask"] if is_long else tick["bid"],
            "deviation": 20,          # points of permitted requote drift
            "magic": 20260101,
            "comment": client_order_id[:31],  # MT5 truncates comments
            "type_time": mt5.ORDER_TIME_GTC,
            # RE-VERIFY: brokers vary between FOK and IOC support.
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if sl is not None:
            request["sl"] = float(sl)
        if tp is not None:
            request["tp"] = float(tp)

        result = self._guarded("order_send", mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                "RealMT5Bridge: order_send rejected %s (%s)",
                client_order_id,
                getattr(result, "retcode", "no result"),
            )
            return None
        return {"fill_px": float(result.price), "ts": datetime.now(timezone.utc)}

    def market_close(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        mt5 = self._mt5
        positions = self._guarded("positions_get", mt5.positions_get, symbol=self.SYMBOL) or []
        target = next(
            (p for p in positions if str(getattr(p, "comment", "")) == client_order_id[:31]),
            None,
        )
        if target is None:
            logger.warning("RealMT5Bridge: no position %s to close", client_order_id)
            return None

        tick = self.get_tick()
        if tick is None:
            return None

        was_long = target.type == mt5.POSITION_TYPE_BUY
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.SYMBOL,
            "volume": float(target.volume),
            "type": mt5.ORDER_TYPE_SELL if was_long else mt5.ORDER_TYPE_BUY,
            "position": target.ticket,
            "price": tick["bid"] if was_long else tick["ask"],
            "deviation": 20,
            "magic": 20260101,
            "comment": f"close:{client_order_id}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = self._guarded("order_send(close)", mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error("RealMT5Bridge: close rejected for %s", client_order_id)
            return None
        return {"fill_px": float(result.price), "ts": datetime.now(timezone.utc)}

    def flatten_all(self, reason: str) -> bool:
        logger.critical("RealMT5Bridge: FLATTEN ALL — %s", reason)
        positions = self._guarded("positions_get", self._mt5.positions_get, symbol=self.SYMBOL)
        if positions is None:
            return False
        ok = True
        for position in positions:
            if self.market_close(str(getattr(position, "comment", ""))) is None:
                ok = False
        remaining = self._guarded(
            "positions_get", self._mt5.positions_get, symbol=self.SYMBOL
        )
        return ok and not remaining

    # -- account -----------------------------------------------------------

    def positions(self) -> List[Dict[str, Any]]:
        positions = self._guarded("positions_get", self._mt5.positions_get, symbol=self.SYMBOL)
        if not positions:
            return []
        return [
            {
                "client_order_id": str(getattr(p, "comment", "")),
                "direction": "LONG" if p.type == self._mt5.POSITION_TYPE_BUY else "SHORT",
                "lots": float(p.volume),
                "entry": float(p.price_open),
                "unrealized_pnl": float(p.profit),
            }
            for p in positions
        ]

    def equity(self) -> Optional[float]:
        info = self._guarded("account_info", self._mt5.account_info)
        return None if info is None else float(info.equity)


def make_bridge() -> Bridge:
    """
    Build the bridge named by config.BRIDGE_KIND, read once at boot.

    An unknown value is a hard error rather than a silent fallback to SIM:
    booting a live box into a simulator because someone typo'd a constant is
    exactly the failure this seam exists to prevent.
    """
    kind = str(config.BRIDGE_KIND).upper()
    if kind == "SIM":
        logger.info("bridge: SIM (deterministic modelled fills)")
        return SimBridge()
    if kind == "MT5":
        logger.info("bridge: MT5 (live broker)")
        return RealMT5Bridge()
    raise RuntimeError(f"unknown BRIDGE_KIND {config.BRIDGE_KIND!r}; expected 'SIM' or 'MT5'")
