"""
RING 0 — the sovereign risk kernel.

Nothing trades except through permit(). Nothing disables it. It boots first
and dies last, and it is written on the assumption that every other ring is
already compromised: the analyst hallucinates, the resolver mis-resolves, the
executor double-sends. The kernel re-checks what those rings have supposedly
already checked, because a defence that trusts its callers is decoration.

ZERO AI. This module imports nothing from ai/ or fusion/, and a test proves it
by walking the import table. The kernel must return correct verdicts with
every model dead, every API key revoked, and the network unplugged. Its only
dependencies are the standard library, pydantic, config, core.database (audit
only), and risk/stage.py (the policy for the current rung).

FAIL CLOSED, ALWAYS. Every unknown is a denial. If equity cannot be read, if
the spread is None, if the tick age is unavailable, if a callable raises — the
answer is no. There is deliberately no code path anywhere in this file where a
missing or unreadable value results in allowed=True. An order not sent costs
an opportunity; an order sent blind costs the account.

THE HALT IS ONE-WAY. Once a breaker halts the kernel, only a process restart
clears it (the risk/stage.py discipline: safety state is import-time state).
The single exception is the daily-loss halt, which carries an explicit expiry
at the next UTC midnight — a new trading day is a real event, not a reset
button. There is no resume(), no clear_halt(), and no operator override.

WHAT THIS FILE DOES NOT DO
It does not send orders, cancel them, or talk to a broker. flatten_all and
broker_positions are injected callables, so Ring 0 has no idea what an MT5
connection is and cannot be broken by one. Task 16 wires the real ones in and
registers run_kernel_watchdog() in backend.py's agent registry.
"""
import ast
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

import config
from core import database
from risk import stage

logger = logging.getLogger(__name__)

# Breaker names. These are the vocabulary of the kernel_events audit table and
# of every denial reason a human will ever read; they are stable identifiers,
# not display strings.
KILL_SWITCH = "KILL_SWITCH"
HALTED = "HALTED"
EQUITY_UNKNOWN = "EQUITY_UNKNOWN"
DAILY_LOSS = "DAILY_LOSS"
MAX_DRAWDOWN = "MAX_DRAWDOWN"
MAX_POSITIONS = "MAX_POSITIONS"
POSITIONS_UNKNOWN = "POSITIONS_UNKNOWN"
LOT_CAP = "LOT_CAP"
TOTAL_LOTS = "TOTAL_LOTS"
SPREAD_UNKNOWN = "SPREAD_UNKNOWN"
SPREAD_CEILING = "SPREAD_CEILING"
STALE_DATA = "STALE_DATA"
STOP_GEOMETRY = "STOP_GEOMETRY"
RECONCILIATION = "RECONCILIATION"
NONE = "NONE"  # kernel_events.breaker is NOT NULL; an allow still needs a value


class Verdict(BaseModel):
    """
    The kernel's answer. Frozen because a verdict that a caller can edit is not
    a verdict — an executor must not be able to flip allowed=False to True and
    claim Ring 0 said so.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    allowed: bool
    breaker: Optional[str] = None
    reason: str
    clamped_lots: Optional[float] = None


class OrderRequest(BaseModel):
    """
    What an executor asks permission for. Strict by construction: a malformed
    request raises rather than reaching a breaker, so the kernel never has to
    reason about a negative lot size or a missing stop.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    direction: Literal["LONG", "SHORT"]
    lots: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    source: Literal["SWING", "POD"]  # pods arrive in Task 18
    client_order_id: str


def _utc_midnight_after(moment: datetime) -> datetime:
    """The next 00:00 UTC strictly after `moment`."""
    start_of_day = moment.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start_of_day + timedelta(days=1)


def _position_signature(positions: List[Dict[str, Any]]) -> List[tuple]:
    """
    A comparable fingerprint of a position book: (direction, lots) sorted.

    Lots are rounded to the broker's lot step before comparison so that float
    representation noise (0.1 + 0.2) is never mistaken for a real divergence
    between our book and the broker's.
    """
    signature = []
    for position in positions or []:
        direction = str(position.get("direction", "")).upper()
        try:
            lots = round(float(position.get("lots", 0.0)), 2)
        except (TypeError, ValueError):
            lots = None  # unparseable is its own distinct value, never 0.0
        signature.append((direction, lots))
    return sorted(signature, key=lambda item: (item[0], -1e9 if item[1] is None else item[1]))


class Kernel:
    """
    Constructed once by the execution layer with its world injected. The kernel
    owns no connections and reaches for nothing global except config, the audit
    table, and the stage policy — which is why it is testable without a broker,
    a market, or a model.
    """

    def __init__(
        self,
        get_equity: Callable[[], float],
        get_open_positions: Callable[[], List[Dict[str, Any]]],
        get_spread: Callable[[], Optional[float]],
        get_tick_age_seconds: Callable[[], Optional[float]],
        flatten_all: Callable[[str], bool],
        broker_positions: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        now_utc: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._get_equity = get_equity
        self._get_open_positions = get_open_positions
        self._get_spread = get_spread
        self._get_tick_age_seconds = get_tick_age_seconds
        self._flatten_all = flatten_all
        self._broker_positions = broker_positions
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))

        # Read ONCE, at construction. The policy cannot change under a running
        # kernel any more than the stage can (INVARIANT 1).
        self._policy = stage.execution_policy()

        self._halted = False
        self._halt_reason: Optional[str] = None
        self._halt_breaker: Optional[str] = None
        self._halt_until: Optional[datetime] = None

        # Equity baselines. Seeded from the first observation; if that
        # observation fails we seed at 0.0, which makes the drawdown check
        # vacuous but leaves the EQUITY_UNKNOWN breaker to refuse everything
        # anyway — an unreadable equity never becomes a permissive baseline.
        seed = self._read_equity_or_none()
        self._peak_equity: float = seed if seed is not None else 0.0
        self._day_start_equity: float = seed if seed is not None else 0.0
        self._day_start_date = self._now_utc().astimezone(timezone.utc).date()

        logger.info(
            "kernel: constructed | stage=%s fill_mode=%s max_lot_per_order=%s "
            "max_total_lots=%s max_concurrent=%s | seed_equity=%s broker_reconcile=%s",
            self._policy.stage.value,
            self._policy.fill_mode,
            self._policy.max_lot_per_order,
            self._policy.max_total_lots,
            self._policy.max_concurrent_positions,
            seed,
            self._broker_positions is not None,
        )

    # -- properties (read-only views; no setters anywhere in this class) ----

    @property
    def policy(self):
        return self._policy

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def day_start_equity(self) -> float:
        return self._day_start_equity

    # -- audit -------------------------------------------------------------

    def _record(
        self,
        breaker: str,
        action: str,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        One row per decision. Best-effort (INVARIANT 6): the kernel's verdict is
        already computed from injected state, so a dead database costs the black
        box, never the safety check. It must never raise into permit().
        """
        try:
            database.execute(
                "INSERT INTO kernel_events (breaker, action, reason, context) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (breaker, action, reason, json.dumps(context or {}, default=str)),
            )
            return True
        except Exception:
            logger.warning(
                "kernel: could not record %s/%s audit row; continuing", breaker, action,
                exc_info=True,
            )
            return False

    def _deny(
        self, breaker: str, reason: str, context: Optional[Dict[str, Any]] = None
    ) -> Verdict:
        logger.warning("kernel: DENY [%s] %s", breaker, reason)
        self._record(breaker, "DENY", reason, context)
        return Verdict(allowed=False, breaker=breaker, reason=reason)

    def _allow(
        self, reason: str, clamped_lots: Optional[float], context: Optional[Dict[str, Any]]
    ) -> Verdict:
        logger.info("kernel: ALLOW %s", reason)
        self._record(NONE, "ALLOW", reason, context)
        return Verdict(allowed=True, breaker=None, reason=reason, clamped_lots=clamped_lots)

    # -- halting -----------------------------------------------------------

    def _halt(self, breaker: str, reason: str, until: Optional[datetime] = None) -> None:
        """
        Stop permitting orders. Clears only on process restart, unless `until`
        is supplied (the daily-loss case), in which case it lapses at that
        instant and not one second earlier.
        """
        self._halted = True
        self._halt_breaker = breaker
        self._halt_reason = reason
        self._halt_until = until
        logger.critical(
            "kernel: HALTED [%s] %s%s",
            breaker,
            reason,
            f" until {until.isoformat()}" if until else " until process restart",
        )
        self._record(
            breaker, "HALT", reason, {"halt_until": until.isoformat() if until else None}
        )

    def _halt_has_lapsed(self) -> bool:
        """True only for an expiring halt whose expiry has actually passed."""
        if self._halt_until is None:
            return False
        return self._now_utc().astimezone(timezone.utc) >= self._halt_until

    def emergency_flatten(
        self,
        reason: str,
        breaker: str = HALTED,
        halt_until: Optional[datetime] = None,
    ) -> bool:
        """
        Close everything, now, and halt. Returns whether the injected
        flatten_all reported success — but the halt is applied either way: a
        flatten that FAILED is a worse reason to keep trading than one that
        succeeded.

        `breaker` names the cause for the audit trail, and `halt_until` is only
        ever supplied by the daily-loss breaker; every other caller leaves the
        halt open-ended, clearable solely by restart.
        """
        logger.critical("kernel: EMERGENCY FLATTEN — %s", reason)
        success = False
        try:
            success = bool(self._flatten_all(reason))
        except Exception:
            logger.critical(
                "kernel: flatten_all raised while flattening for %s", reason, exc_info=True
            )
            success = False

        if not success:
            logger.critical(
                "kernel: FLATTEN DID NOT CONFIRM for %s — positions may still be open",
                reason,
            )
        self._record(breaker, "FLATTEN", reason, {"success": success})
        self._halt(breaker, reason, until=halt_until)
        return success

    # -- injected reads, each fail-closed ----------------------------------

    def _read_equity_or_none(self) -> Optional[float]:
        try:
            value = float(self._get_equity())
        except Exception:
            logger.error("kernel: get_equity raised", exc_info=True)
            return None
        if value != value or value in (float("inf"), float("-inf")):
            logger.error("kernel: get_equity returned a non-finite value")
            return None
        return value

    def _observe_equity(self, equity: float) -> None:
        """Track the peak and roll the daily baseline at UTC midnight."""
        if equity > self._peak_equity:
            self._peak_equity = equity

        today = self._now_utc().astimezone(timezone.utc).date()
        if today != self._day_start_date:
            logger.info(
                "kernel: new UTC day %s — daily baseline %.2f -> %.2f",
                today, self._day_start_equity, equity,
            )
            self._day_start_date = today
            self._day_start_equity = equity

    # -- the gate ----------------------------------------------------------

    def permit(self, order: OrderRequest) -> Verdict:
        """
        The only door. Checks run in a fixed order and the first denial wins;
        the order is deliberately safety-first (kill switch and halt before
        anything that costs a round-trip to read).
        """
        ctx: Dict[str, Any] = {
            "client_order_id": order.client_order_id,
            "direction": order.direction,
            "lots": order.lots,
            "entry_price": order.entry_price,
            "stop_price": order.stop_price,
            "source": order.source,
            "stage": self._policy.stage.value,
        }

        # 1. KILL FILE — a human (or an operator script) said stop.
        if os.path.exists(config.KILL_FILE_PATH):
            self.emergency_flatten("kill switch", breaker=KILL_SWITCH)
            return self._deny(
                KILL_SWITCH,
                f"kill file present at {config.KILL_FILE_PATH}",
                ctx,
            )

        # 2. HALTED — a breaker already fired. Expiring halts lapse here.
        if self._halted:
            if self._halt_has_lapsed():
                logger.info(
                    "kernel: halt from [%s] lapsed at %s — resuming",
                    self._halt_breaker, self._halt_until.isoformat(),
                )
                self._halted = False
                self._halt_reason = None
                self._halt_breaker = None
                self._halt_until = None
            else:
                return self._deny(
                    HALTED,
                    f"kernel halted: {self._halt_reason}",
                    {**ctx, "halt_breaker": self._halt_breaker},
                )

        # 3. EQUITY — unknown or implausible permits nothing.
        equity = self._read_equity_or_none()
        if equity is None:
            return self._deny(EQUITY_UNKNOWN, "equity could not be read", ctx)
        if equity <= config.KERNEL_EQUITY_FLOOR:
            return self._deny(
                EQUITY_UNKNOWN,
                f"equity {equity:.2f} at or below floor {config.KERNEL_EQUITY_FLOOR:.2f}",
                {**ctx, "equity": equity},
            )
        self._observe_equity(equity)
        ctx["equity"] = equity

        # 4. DAILY LOSS — equity change since the day's start, realized and not.
        daily_pnl = equity - self._day_start_equity
        daily_cap = -abs(self._day_start_equity * config.DAILY_LOSS_CAP_PCT / 100.0)
        if self._day_start_equity > 0 and daily_pnl <= daily_cap:
            reason = (
                f"daily loss {daily_pnl:.2f} breached cap {daily_cap:.2f} "
                f"({config.DAILY_LOSS_CAP_PCT}% of {self._day_start_equity:.2f})"
            )
            # The ONLY halt that expires: a new trading day is a real event,
            # not a reset button.
            self.emergency_flatten(
                reason,
                breaker=DAILY_LOSS,
                halt_until=_utc_midnight_after(self._now_utc()),
            )
            return self._deny(
                DAILY_LOSS, reason, {**ctx, "daily_pnl": daily_pnl, "cap": daily_cap}
            )

        # 5. DRAWDOWN — the deepest breaker: flatten, demote a rung, halt.
        dd_floor = self._peak_equity * (1.0 - config.MAX_DRAWDOWN_PCT / 100.0)
        if self._peak_equity > 0 and equity <= dd_floor:
            reason = (
                f"drawdown: equity {equity:.2f} at or below {dd_floor:.2f} "
                f"({config.MAX_DRAWDOWN_PCT}% under peak {self._peak_equity:.2f})"
            )
            self.emergency_flatten(reason, breaker=MAX_DRAWDOWN)
            try:
                stage.write_demotion(f"kernel drawdown breaker: {reason}")
                self._record(MAX_DRAWDOWN, "DEMOTE", reason, {"equity": equity})
            except Exception:
                # A failed demotion must not swallow the denial — the halt and
                # the flatten already stand.
                logger.critical(
                    "kernel: could not write demotion flag after drawdown breach",
                    exc_info=True,
                )
            return self._deny(
                MAX_DRAWDOWN,
                reason,
                {**ctx, "peak_equity": self._peak_equity, "drawdown_floor": dd_floor},
            )

        # 6. CONCURRENT POSITIONS.
        try:
            open_positions = list(self._get_open_positions() or [])
        except Exception:
            logger.error("kernel: get_open_positions raised", exc_info=True)
            return self._deny(
                POSITIONS_UNKNOWN, "open positions could not be read", ctx
            )
        if len(open_positions) >= self._policy.max_concurrent_positions:
            return self._deny(
                MAX_POSITIONS,
                f"{len(open_positions)} open at cap {self._policy.max_concurrent_positions}",
                {**ctx, "open_positions": len(open_positions)},
            )

        # 7. LOT CAPS — per-order clamps, book total denies.
        clamped_lots: Optional[float] = None
        effective_lots = order.lots
        if order.lots > self._policy.max_lot_per_order:
            clamped_lots = self._policy.max_lot_per_order
            effective_lots = clamped_lots
            logger.warning(
                "kernel: CLAMPED %s from %s to %s lots (per-order cap)",
                order.client_order_id, order.lots, clamped_lots,
            )
            self._record(
                LOT_CAP,
                "CLAMP",
                f"requested {order.lots} exceeds per-order cap "
                f"{self._policy.max_lot_per_order}",
                {**ctx, "clamped_lots": clamped_lots},
            )

        open_lots = 0.0
        for position in open_positions:
            try:
                open_lots += float(position.get("lots", 0.0))
            except (TypeError, ValueError):
                # An unparseable position size is an unknown: fail closed.
                return self._deny(
                    POSITIONS_UNKNOWN,
                    f"open position has unreadable lots: {position!r}",
                    ctx,
                )

        projected = open_lots + effective_lots
        if projected > self._policy.max_total_lots:
            return self._deny(
                TOTAL_LOTS,
                f"projected book {projected:.2f} lots exceeds total cap "
                f"{self._policy.max_total_lots}",
                {**ctx, "open_lots": open_lots, "projected_lots": projected},
            )

        # 8. SPREAD — unknown is a denial, not a shrug.
        try:
            spread = self._get_spread()
        except Exception:
            logger.error("kernel: get_spread raised", exc_info=True)
            spread = None
        if spread is None:
            return self._deny(SPREAD_UNKNOWN, "spread unavailable", ctx)
        spread = float(spread)
        if spread > config.SPREAD_CEILING_USD:
            return self._deny(
                SPREAD_CEILING,
                f"spread {spread:.3f} above ceiling {config.SPREAD_CEILING_USD}",
                {**ctx, "spread": spread},
            )
        ctx["spread"] = spread

        # 9. STALE DATA — an old quote is a memory, not a price.
        try:
            tick_age = self._get_tick_age_seconds()
        except Exception:
            logger.error("kernel: get_tick_age_seconds raised", exc_info=True)
            tick_age = None
        if tick_age is None:
            return self._deny(STALE_DATA, "tick age unavailable", ctx)
        tick_age = float(tick_age)
        if tick_age > config.STALE_TICK_SECONDS:
            return self._deny(
                STALE_DATA,
                f"tick age {tick_age:.1f}s over {config.STALE_TICK_SECONDS}s",
                {**ctx, "tick_age_seconds": tick_age},
            )
        ctx["tick_age_seconds"] = tick_age

        # 10. STOP GEOMETRY — the resolver already checked this. So do we.
        if order.direction == "LONG" and order.stop_price >= order.entry_price:
            return self._deny(
                STOP_GEOMETRY,
                f"LONG stop {order.stop_price} must be below entry {order.entry_price}",
                ctx,
            )
        if order.direction == "SHORT" and order.stop_price <= order.entry_price:
            return self._deny(
                STOP_GEOMETRY,
                f"SHORT stop {order.stop_price} must be above entry {order.entry_price}",
                ctx,
            )

        return self._allow(
            f"{order.direction} {effective_lots} lots permitted at "
            f"{self._policy.stage.value}",
            clamped_lots,
            {**ctx, "effective_lots": effective_lots},
        )

    # -- reconciliation ----------------------------------------------------

    def reconcile(
        self,
        local_positions: List[Dict[str, Any]],
        broker_positions: List[Dict[str, Any]],
    ) -> bool:
        """
        Compare our book against the broker's. Any divergence in the multiset of
        (direction, lots) means one of the two is lying about real money, and
        the only safe response is to flatten and halt.

        With no broker_positions callable injected this is a documented no-op
        returning True: PAPER and SHADOW have no broker to disagree with, and
        inventing a mismatch there would halt a system that is behaving.
        """
        if self._broker_positions is None:
            logger.debug("kernel: reconcile skipped — no broker in this stage")
            return True

        local_signature = _position_signature(local_positions)
        broker_signature = _position_signature(broker_positions)
        if local_signature == broker_signature:
            logger.info("kernel: reconciled %d position(s)", len(local_signature))
            return True

        reason = "reconciliation mismatch"
        logger.critical(
            "kernel: RECONCILIATION MISMATCH local=%s broker=%s",
            local_signature, broker_signature,
        )
        self._record(
            RECONCILIATION,
            "DENY",
            reason,
            {"local": [list(s) for s in local_signature],
             "broker": [list(s) for s in broker_signature]},
        )
        self.emergency_flatten(reason, breaker=RECONCILIATION)
        return False

    # -- watchdog ----------------------------------------------------------

    def watchdog_tick(self) -> Dict[str, Any]:
        """
        One supervision pass, factored out of the loop so it is testable without
        threads or sleeps. Never raises: the watchdog surviving is the point.
        """
        result: Dict[str, Any] = {"kill": False, "equity": None, "reconciled": None}
        try:
            if os.path.exists(config.KILL_FILE_PATH):
                # An idle system must still honour the kill switch — no order
                # flow means permit() is never called to notice it.
                result["kill"] = True
                if not self._halted:
                    self.emergency_flatten("kill switch", breaker=KILL_SWITCH)
                return result

            equity = self._read_equity_or_none()
            result["equity"] = equity
            if equity is not None:
                self._observe_equity(equity)

            if self._broker_positions is not None:
                local = list(self._get_open_positions() or [])
                remote = list(self._broker_positions() or [])
                result["reconciled"] = self.reconcile(local, remote)
        except Exception:
            logger.error("kernel: watchdog tick raised; continuing", exc_info=True)
        return result

    def run_kernel_watchdog(self) -> None:
        """
        Loop forever. Registered in backend.py as kind="loop" by Task 16 — this
        module deliberately does not touch the registry.
        """
        logger.info(
            "kernel watchdog starting: every %ss", config.RECONCILE_INTERVAL_SECONDS
        )
        while True:
            self.watchdog_tick()
            time.sleep(config.RECONCILE_INTERVAL_SECONDS)


def imported_top_level_modules() -> List[str]:
    """
    The module's own import table, read from its source.

    Used by the test suite to prove Ring 0 imports nothing from ai/ or fusion/.
    Reading the AST rather than sys.modules makes the answer about THIS file
    rather than about whatever else a test session happened to import.
    """
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    modules: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.append(node.module)
    return sorted(set(modules))
