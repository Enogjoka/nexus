"""
RING 1's hands — the order router.

This is the ONLY component in the system that calls bridge.market_open. Every
order it sends has passed kernel.permit() first, without exception and without
a bypass flag, because INVARIANT 5 is not a policy that can be switched off:
if permit() denies, the bridge is never touched at all.

IDEMPOTENCY IS A DATABASE CONSTRAINT, NOT A CONVENTION.
client_order_id is derived deterministically from the signal id (NEXUS-<id>)
and the fills table holds it UNIQUE. A retried submit, a double-invoked
scheduler, or a process restart mid-send all collide on that key instead of
opening a second position. This is what makes the retry below safe: retrying a
send whose outcome is unknown is only acceptable when a duplicate cannot
survive.

THE STAGE DOES NOT BRANCH THE CODE PATH.
MODELED, SHADOW_REAL_BIDASK and LIVE_* all run the same lines. The difference
between a modelled fill and a real one lives entirely inside the bridge, so
the path that will one day send real money is the same path exercised by every
paper trade before it — there is no "live mode" branch that has never run.

NOTHING CALLS submit() YET. The paper engine keeps its own modelled fills; the
cutover is Task 21's position engine. This is the organ, not the wiring.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from core import database
from risk import stage
from risk.kernel import Kernel, OrderRequest

logger = logging.getLogger(__name__)

# Outcomes. These are the router's vocabulary and are stable identifiers.
SUBMITTED = "SUBMITTED"
KERNEL_DENIED = "KERNEL_DENIED"
DUPLICATE = "DUPLICATE"
BRIDGE_FAILED = "BRIDGE_FAILED"
INVALID_ORDER = "INVALID_ORDER"

STATUS_FILLED = "FILLED"
STATUS_REJECTED = "REJECTED"
STATUS_CLOSED = "CLOSED"

_DEFAULT_RETRY_DELAY_SECONDS = 2.0


def client_order_id_for(signal_id: Any) -> str:
    return f"NEXUS-{signal_id}"


def _num(value) -> Optional[float]:
    """A real finite float, or None. Rejects bools and unparseable values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


class Router:
    """
    Turns a validated signal row into (at most) one broker order.

    `retry_delay_seconds` is a constructor argument rather than a config knob
    so tests can drive the retry path without sleeping; it is a testability
    seam, not an operational setting.
    """

    def __init__(
        self,
        bridge,
        kernel: Kernel,
        retry_delay_seconds: float = _DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        self._bridge = bridge
        self._kernel = kernel
        self._retry_delay_seconds = retry_delay_seconds

    # -- ledger ------------------------------------------------------------

    def _already_sent(self, client_order_id: str) -> bool:
        """
        True if this order id is already in the ledger.

        Fails CLOSED: if the ledger cannot be read we must assume the order may
        already exist, because the alternative is opening a second position on
        a database blip. A missed trade is recoverable; a doubled one is not.
        """
        try:
            rows = database.fetch(
                "SELECT 1 FROM fills WHERE client_order_id = %s LIMIT 1", (client_order_id,)
            )
            return bool(rows)
        except Exception:
            logger.error(
                "router: could not check the ledger for %s; assuming it exists "
                "(refusing to risk a duplicate)",
                client_order_id,
                exc_info=True,
            )
            return True

    def _record(
        self,
        *,
        client_order_id: str,
        signal_id: Any,
        direction: Optional[str],
        lots: Optional[float],
        requested_px: Optional[float],
        fill_px: Optional[float],
        slippage: Optional[float],
        spread_at_send: Optional[float],
        fill_mode: str,
        status: str,
        kernel_reason: Optional[str] = None,
        ts: Optional[datetime] = None,
    ) -> Optional[int]:
        """Append to the fills ledger. Best-effort; never raises (INVARIANT 6)."""
        try:
            with database.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO fills (ts, client_order_id, signal_id, direction, lots, "
                        "requested_px, fill_px, slippage, spread_at_send, fill_mode, status, "
                        "kernel_reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (client_order_id) DO NOTHING RETURNING id",
                        (
                            ts or datetime.now(timezone.utc),
                            client_order_id,
                            signal_id,
                            direction,
                            lots,
                            requested_px,
                            fill_px,
                            slippage,
                            spread_at_send,
                            fill_mode,
                            status,
                            kernel_reason,
                        ),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else None
        except Exception:
            logger.error(
                "router: could not write the %s fills row for %s", status, client_order_id,
                exc_info=True,
            )
            return None

    @staticmethod
    def _slippage(direction: str, requested_px: Optional[float], fill_px: Optional[float]):
        """
        Signed so that POSITIVE always means adverse, whichever way we faced.
        A LONG filled above its request paid up; a SHORT filled below its
        request received less. Both are losses and both read positive.
        """
        if requested_px is None or fill_px is None:
            return None
        return fill_px - requested_px if direction == "LONG" else requested_px - fill_px

    # -- the one door ------------------------------------------------------

    def submit(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send one signal, or explain why it was not sent. Never raises.

        Returns {"outcome": ..., "client_order_id": ..., ...}. Every outcome
        other than SUBMITTED leaves the bridge untouched.
        """
        signal_id = (signal or {}).get("id")
        client_order_id = client_order_id_for(signal_id)
        policy = stage.execution_policy()
        fill_mode = policy.fill_mode

        # 1. Idempotency, before anything else can have a side effect.
        if self._already_sent(client_order_id):
            logger.warning("router: %s already in the ledger; not sending again", client_order_id)
            return {
                "outcome": DUPLICATE,
                "client_order_id": client_order_id,
                "signal_id": signal_id,
                "reason": "client_order_id already present in fills",
            }

        # 2. Build the contract. A malformed signal is refused here rather than
        #    handed to the kernel as a half-formed order.
        prices = (signal or {}).get("prices") or {}
        entry = _num(prices.get("entry"))
        stop = _num(prices.get("stop"))
        lots = _num((signal or {}).get("lots")) or _num(prices.get("lots"))
        direction = str((signal or {}).get("direction") or "").upper()

        try:
            order = OrderRequest(
                direction=direction,
                lots=lots,
                entry_price=entry,
                stop_price=stop,
                source="SWING",
                client_order_id=client_order_id,
            )
        except Exception as exc:
            logger.error("router: signal %s is not a valid order (%s)", signal_id, exc)
            self._record(
                client_order_id=client_order_id,
                signal_id=signal_id,
                direction=direction or None,
                lots=lots,
                requested_px=entry,
                fill_px=None,
                slippage=None,
                spread_at_send=None,
                fill_mode=fill_mode,
                status=STATUS_REJECTED,
                kernel_reason=f"malformed order: {exc}",
            )
            return {
                "outcome": INVALID_ORDER,
                "client_order_id": client_order_id,
                "signal_id": signal_id,
                "reason": str(exc),
            }

        # 3. RING 0. There is no path past this that skips it.
        verdict = self._kernel.permit(order)
        if not verdict.allowed:
            logger.warning(
                "router: kernel denied %s [%s] %s", client_order_id, verdict.breaker, verdict.reason
            )
            self._record(
                client_order_id=client_order_id,
                signal_id=signal_id,
                direction=order.direction,
                lots=order.lots,
                requested_px=order.entry_price,
                fill_px=None,
                slippage=None,
                spread_at_send=self._safe_spread(),
                fill_mode=fill_mode,
                status=STATUS_REJECTED,
                kernel_reason=f"{verdict.breaker}: {verdict.reason}",
            )
            return {
                "outcome": KERNEL_DENIED,
                "client_order_id": client_order_id,
                "signal_id": signal_id,
                "breaker": verdict.breaker,
                "reason": verdict.reason,
            }

        # 4. The kernel may have shrunk the order. Its number wins.
        send_lots = order.lots
        if verdict.clamped_lots is not None:
            send_lots = verdict.clamped_lots
            logger.info(
                "router: kernel clamped %s from %s to %s lots",
                client_order_id, order.lots, send_lots,
            )

        spread_at_send = self._safe_spread()

        # 5. Send. One retry, because the client_order_id makes it safe.
        result = self._send_with_retry(order, send_lots, prices, client_order_id)
        if result is None:
            logger.error("router: bridge failed twice for %s; giving up", client_order_id)
            self._record(
                client_order_id=client_order_id,
                signal_id=signal_id,
                direction=order.direction,
                lots=send_lots,
                requested_px=order.entry_price,
                fill_px=None,
                slippage=None,
                spread_at_send=spread_at_send,
                fill_mode=fill_mode,
                status=STATUS_REJECTED,
                kernel_reason="bridge returned no fill after one retry",
            )
            return {
                "outcome": BRIDGE_FAILED,
                "client_order_id": client_order_id,
                "signal_id": signal_id,
                "reason": "bridge returned no fill after one retry",
            }

        fill_px = _num(result.get("fill_px"))
        slippage = self._slippage(order.direction, order.entry_price, fill_px)
        row_id = self._record(
            client_order_id=client_order_id,
            signal_id=signal_id,
            direction=order.direction,
            lots=send_lots,
            requested_px=order.entry_price,
            fill_px=fill_px,
            slippage=slippage,
            spread_at_send=spread_at_send,
            fill_mode=fill_mode,
            status=STATUS_FILLED,
            ts=result.get("ts"),
        )
        if row_id is None:
            # The position is OPEN but unrecorded, so the ledger no longer
            # protects against a duplicate: a later submit would find no row
            # and send again. Nothing here can undo the fill, so the only
            # honest response is to make the gap impossible to miss.
            logger.critical(
                "router: %s FILLED BUT NOT RECORDED — position is open and the "
                "ledger does not know; idempotency is not protecting this order. "
                "Reconcile the book against fills before submitting anything else.",
                client_order_id,
            )
        logger.info(
            "router: SUBMITTED %s %s %.2f lots requested=%.5f fill=%.5f slippage=%.5f mode=%s",
            client_order_id, order.direction, send_lots,
            order.entry_price, fill_px if fill_px is not None else float("nan"),
            slippage if slippage is not None else float("nan"), fill_mode,
        )
        return {
            "outcome": SUBMITTED,
            "client_order_id": client_order_id,
            "signal_id": signal_id,
            "direction": order.direction,
            "lots": send_lots,
            "clamped": verdict.clamped_lots is not None,
            "requested_px": order.entry_price,
            "fill_px": fill_px,
            "slippage": slippage,
            "spread_at_send": spread_at_send,
            "fill_mode": fill_mode,
            # False means the fill happened but the ledger write did not.
            "ledger_recorded": row_id is not None,
        }

    def _safe_spread(self) -> Optional[float]:
        try:
            return _num(self._bridge.get_spread())
        except Exception:
            logger.warning("router: bridge.get_spread raised", exc_info=True)
            return None

    def _send_with_retry(self, order, send_lots, prices, client_order_id):
        """
        One send, one retry after a short pause. A bridge returning None is
        treated as transient; a bridge that raises is treated the same way and
        logged, because from here the two are indistinguishable.
        """
        for attempt in (1, 2):
            try:
                result = self._bridge.market_open(
                    order.direction,
                    send_lots,
                    order.stop_price,
                    _num(prices.get("tp1")),
                    client_order_id,
                )
            except Exception:
                logger.error(
                    "router: bridge.market_open raised on attempt %d for %s",
                    attempt, client_order_id, exc_info=True,
                )
                result = None

            if result is not None:
                if attempt == 2:
                    logger.info("router: %s filled on retry", client_order_id)
                return result

            if attempt == 1:
                logger.warning(
                    "router: bridge returned no fill for %s; retrying once in %.1fs",
                    client_order_id, self._retry_delay_seconds,
                )
                time.sleep(self._retry_delay_seconds)
        return None

    # -- exits -------------------------------------------------------------

    def close(self, client_order_id: str) -> Dict[str, Any]:
        """
        Close one position and record the closing fill.

        Recorded under '<client_order_id>-CLOSE' because fills.client_order_id
        is UNIQUE and the opening row must survive: overwriting it would erase
        the entry price and its slippage, which are half the evidence of how
        the trade actually went.
        """
        close_id = f"{client_order_id}-CLOSE"
        try:
            result = self._bridge.market_close(client_order_id)
        except Exception:
            logger.error("router: bridge.market_close raised for %s", client_order_id, exc_info=True)
            result = None

        if result is None:
            return {"outcome": BRIDGE_FAILED, "client_order_id": client_order_id}

        fill_px = _num(result.get("fill_px"))
        self._record(
            client_order_id=close_id,
            signal_id=None,
            direction=None,
            lots=None,
            requested_px=None,
            fill_px=fill_px,
            slippage=None,
            spread_at_send=self._safe_spread(),
            fill_mode=stage.execution_policy().fill_mode,
            status=STATUS_CLOSED,
            ts=result.get("ts"),
        )
        logger.info("router: CLOSED %s @ %s", client_order_id, fill_px)
        return {"outcome": STATUS_CLOSED, "client_order_id": client_order_id, "fill_px": fill_px}

    def flatten_all(self, reason: str) -> Dict[str, Any]:
        """Close everything through the bridge, recording each position closed."""
        try:
            open_ids = [p.get("client_order_id") for p in (self._bridge.positions() or [])]
        except Exception:
            logger.error("router: bridge.positions raised during flatten", exc_info=True)
            open_ids = []

        try:
            ok = bool(self._bridge.flatten_all(reason))
        except Exception:
            logger.critical("router: bridge.flatten_all raised (%s)", reason, exc_info=True)
            ok = False

        fill_mode = stage.execution_policy().fill_mode
        for client_order_id in open_ids:
            if not client_order_id:
                continue
            self._record(
                client_order_id=f"{client_order_id}-CLOSE",
                signal_id=None,
                direction=None,
                lots=None,
                requested_px=None,
                fill_px=None,
                slippage=None,
                spread_at_send=None,
                fill_mode=fill_mode,
                status=STATUS_CLOSED,
                kernel_reason=f"flatten_all: {reason}",
            )
        logger.critical("router: FLATTEN ALL (%s) success=%s closed=%d", reason, ok, len(open_ids))
        return {"outcome": STATUS_CLOSED, "success": ok, "closed": len(open_ids)}


def build_kernel(bridge) -> Kernel:
    """
    Wire Ring 0 to the bridge.

    broker_positions is None under SIM: there is no independent broker to
    disagree with, and handing the kernel the simulator's own book as if it
    were a third party would turn reconciliation into a tautology that can
    never fail — worse than not running it.
    """
    is_sim = str(config.BRIDGE_KIND).upper() == "SIM"
    return Kernel(
        get_equity=bridge.equity,
        get_open_positions=bridge.positions,
        get_spread=bridge.get_spread,
        get_tick_age_seconds=bridge.get_tick_age_seconds,
        flatten_all=bridge.flatten_all,
        broker_positions=None if is_sim else bridge.positions,
    )
