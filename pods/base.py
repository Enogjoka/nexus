"""
The scalp-pod framework.

A pod is a small, deterministic strategy that watches ticks and occasionally
says "I want this trade". It emits an Intent — never an order. The Intent goes
to the kernel like everything else, and the kernel decides.

PODS COMPUTE PRICES; THE AI DOES NOT.
Intent carries entry_price, stop_price and tp_price as real numbers, which
looks like a violation of INVARIANT 2 until you notice who is speaking. The
anchor-enum cage in ai/price_resolver.py exists because a LANGUAGE MODEL cannot
be trusted with a price: it hallucinates, it drifts, it repeats a number from
its context window. A pod is fixed arithmetic over a tick — it has no more
discretion about its entry than a moving average has about its mean. Caging it
behind anchors would add indirection without adding safety.

WHAT A POD MAY NOT DO is decide whether its trade is worth taking. That is the
cost gate's job, and the cost gate lives in the kernel where no pod can reach
it. A pod states its expected edge honestly and is judged on it.

THE BREAKERS ARE PER POD AND THEY ARE NOT SUGGESTIONS.
Three consecutive losses, a daily loss cap, or a daily trade count — any of
them disables the pod. The distinction that matters:

    consecutive losses  -> latched. Cleared ONLY when the next doctrine
                           re-enables the pod, i.e. when the head of desk has
                           looked at the world again. Time does not heal it.
    daily loss / count  -> boxed until UTC midnight. A new trading day is a
                           real event; a new hour is not.

A pod that has lost three in a row is not unlucky, it is wrong about current
conditions, and the thing qualified to say conditions changed is the doctrine.

ALL STATE IS IN MEMORY and the clock is injected, following the validator's
precedent: a supervisor that reads datetime.now() internally cannot be tested
across a midnight rollover without waiting for one.
"""
import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Set

from pydantic import BaseModel, Field, field_validator

import config

logger = logging.getLogger(__name__)

# The COST_MULT floor is enforced at import of risk/kernel.py, not here.
# The gate that uses the multiplier is the gate that should refuse to load
# with a bad one: guarding it here would leave the kernel free to run on a
# lowered value whenever nothing happened to import a pod.


class Intent(BaseModel):
    """
    A pod's request. Frozen so a supervisor or router cannot widen a pod's own
    stated edge and present it to the cost gate as the pod's claim.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    pod: str
    direction: Literal["LONG", "SHORT"]
    lots: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    tp_price: float = Field(gt=0)
    expected_edge_usd: float = Field(gt=0)
    reason: str

    @field_validator("pod")
    @classmethod
    def _must_be_a_known_pod(cls, value: str) -> str:
        if value not in config.POD_NAMES:
            raise ValueError(
                f"unknown pod {value!r}; expected one of {config.POD_NAMES}"
            )
        return value


class Pod(ABC):
    """
    Base class for a scalp pod. Concrete pods are Tasks 19-20.

    evaluate() returns an Intent or None, and returning None is the normal
    case: a pod that always has an opinion is not a strategy, it is a
    random number generator with extra steps.
    """

    name: str = ""

    @abstractmethod
    def evaluate(self, tick: Dict[str, Any], state: Dict[str, Any]) -> Optional[Intent]:
        """Look at the world; return an Intent only if this trade is worth asking for."""
        raise NotImplementedError


class _PodRuntime:
    """Mutable per-pod bookkeeping. In memory only, never persisted."""

    def __init__(self, day: date) -> None:
        self.day = day
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.daily_trades = 0
        # Latched by the consecutive-loss breaker; only a doctrine clears it.
        self.loss_latched = False
        # Set by the daily breakers; lapses at the stored instant.
        self.disabled_until: Optional[datetime] = None
        self.reason: Optional[str] = None


def _next_utc_midnight(moment: datetime) -> datetime:
    start = moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start + timedelta(days=1)


class PodSupervisor:
    """
    Owns the pod instances and decides which of them get to speak.

    Two independent gates stand between a pod and an Intent:
      1. the doctrine's enabled set — the head of desk's opinion
      2. this pod's breakers — its own recent behaviour
    A pod must pass both. Neither can override the other, and there is no
    method here that force-enables a pod.
    """

    def __init__(
        self,
        pods: Optional[Iterable[Pod]] = None,
        now_utc: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._pods: Dict[str, Pod] = {}
        for pod in pods or ():
            self._pods[pod.name] = pod

        self._now = now_utc or (lambda: datetime.now(timezone.utc))
        # Nothing runs until a doctrine says so. An empty set is the correct
        # starting posture: pods are opt-in, and silence enables nothing.
        self._enabled: Set[str] = set()
        today = self._now().astimezone(timezone.utc).date()
        self._runtime: Dict[str, _PodRuntime] = {
            name: _PodRuntime(today) for name in self._pods
        }

    # -- registry ----------------------------------------------------------

    @property
    def pod_names(self) -> List[str]:
        return list(self._pods)

    @property
    def enabled(self) -> Set[str]:
        return set(self._enabled)

    def _runtime_for(self, pod_name: str) -> _PodRuntime:
        runtime = self._runtime.get(pod_name)
        if runtime is None:
            runtime = _PodRuntime(self._now().astimezone(timezone.utc).date())
            self._runtime[pod_name] = runtime
        return runtime

    def _roll_day(self, runtime: _PodRuntime, now_utc: datetime) -> None:
        """Reset the daily counters when the UTC date changes."""
        today = now_utc.astimezone(timezone.utc).date()
        if today == runtime.day:
            return
        logger.info(
            "pods: UTC day rollover %s -> %s; resetting daily counters", runtime.day, today
        )
        runtime.day = today
        runtime.daily_pnl = 0.0
        runtime.daily_trades = 0

    # -- doctrine ----------------------------------------------------------

    def update_from_doctrine(self, doctrine: Any) -> Set[str]:
        """
        Adopt a doctrine's enabled_pods.

        Deliberately duck-typed rather than importing ai.doctrine: the pod
        framework needs one attribute, and a hard import would tie the
        execution side to the AI package for no benefit. Anything exposing
        enabled_pods works, which is also what makes this testable without
        constructing a full Doctrine.

        A FLAT doctrine carries an empty enabled_pods by construction (Task 15
        rejects any FLAT that leaves something armed), so standing the desk
        down disables every pod through this one path.
        """
        raw = getattr(doctrine, "enabled_pods", None)
        if raw is None:
            logger.warning("pods: doctrine has no enabled_pods; disabling everything")
            raw = ()

        # PodEnum members are str subclasses; normalise either form to plain str.
        new_enabled = {getattr(pod, "value", pod) for pod in raw}
        new_enabled = {str(name) for name in new_enabled}

        unknown = new_enabled - set(self._pods)
        if unknown:
            logger.warning("pods: doctrine enabled unknown pod(s) %s; ignoring", sorted(unknown))
            new_enabled -= unknown

        # A doctrine is the head of desk looking at the world again, which is
        # the ONLY thing that clears a consecutive-loss latch.
        for name in new_enabled:
            runtime = self._runtime_for(name)
            if runtime.loss_latched:
                logger.info(
                    "pods: %s re-enabled by doctrine; clearing the consecutive-loss latch",
                    name,
                )
                runtime.loss_latched = False
                runtime.consecutive_losses = 0
                runtime.reason = None

        previous, self._enabled = self._enabled, new_enabled
        if previous != new_enabled:
            logger.info("pods: enabled set %s -> %s", sorted(previous), sorted(new_enabled))
        return set(self._enabled)

    # -- breakers ----------------------------------------------------------

    def record_outcome(self, pod_name: str, pnl_usd: float, now_utc: datetime) -> None:
        """
        Book a closed trade and trip any breaker it crosses.

        A zero-pnl trade counts as a trade but does not extend a losing streak:
        scratching out is not the same as being wrong.
        """
        runtime = self._runtime_for(pod_name)
        self._roll_day(runtime, now_utc)

        runtime.daily_trades += 1
        runtime.daily_pnl += float(pnl_usd)
        if pnl_usd < 0:
            runtime.consecutive_losses += 1
        else:
            runtime.consecutive_losses = 0

        if runtime.consecutive_losses >= config.POD_MAX_CONSECUTIVE_LOSSES:
            runtime.loss_latched = True
            runtime.reason = (
                f"{runtime.consecutive_losses} consecutive losses"
            )
            logger.warning(
                "POD_DISABLED %s: %s — stays off until the next doctrine re-enables it",
                pod_name, runtime.reason,
            )

        if runtime.daily_pnl <= -abs(config.POD_DAILY_LOSS_CAP_USD):
            runtime.disabled_until = _next_utc_midnight(now_utc)
            runtime.reason = f"daily loss {runtime.daily_pnl:.2f} USD"
            logger.warning(
                "POD_DISABLED %s: %s — off until %s",
                pod_name, runtime.reason, runtime.disabled_until.isoformat(),
            )

        if runtime.daily_trades >= config.POD_MAX_TRADES_PER_DAY:
            runtime.disabled_until = _next_utc_midnight(now_utc)
            runtime.reason = f"{runtime.daily_trades} trades today"
            logger.warning(
                "POD_DISABLED %s: %s — off until %s",
                pod_name, runtime.reason, runtime.disabled_until.isoformat(),
            )

    def is_active(self, pod_name: str, now_utc: datetime) -> bool:
        """Enabled by doctrine AND not held off by one of its own breakers."""
        if pod_name not in self._enabled:
            return False

        runtime = self._runtime_for(pod_name)
        self._roll_day(runtime, now_utc)

        if runtime.loss_latched:
            return False
        if runtime.disabled_until is not None:
            if now_utc.astimezone(timezone.utc) < runtime.disabled_until:
                return False
            # The box has lapsed; clear it so the next check is cheap.
            runtime.disabled_until = None
            runtime.reason = None
        return True

    def status(self, now_utc: datetime) -> Dict[str, Dict[str, Any]]:
        """A snapshot for logging and the future /status command."""
        report = {}
        for name in self._pods:
            runtime = self._runtime_for(name)
            report[name] = {
                "enabled": name in self._enabled,
                "active": self.is_active(name, now_utc),
                "consecutive_losses": runtime.consecutive_losses,
                "daily_pnl": runtime.daily_pnl,
                "daily_trades": runtime.daily_trades,
                "loss_latched": runtime.loss_latched,
                "disabled_until": (
                    runtime.disabled_until.isoformat() if runtime.disabled_until else None
                ),
                "reason": runtime.reason,
            }
        return report

    # -- the sweep ---------------------------------------------------------

    def evaluate_all(
        self, tick: Dict[str, Any], state: Dict[str, Any], now_utc: datetime
    ) -> List[Intent]:
        """
        Ask every active pod for an Intent.

        A pod that raises is logged and skipped (INVARIANT 6). One broken
        strategy must never take the others down with it — the sweep is the
        thing that keeps trading, and a pod is only ever an opinion.
        """
        intents: List[Intent] = []
        for name, pod in self._pods.items():
            if not self.is_active(name, now_utc):
                continue
            try:
                intent = pod.evaluate(tick, state)
            except Exception:
                logger.exception("pods: %s raised during evaluate; skipping it this sweep", name)
                continue

            if intent is None:
                continue
            if not isinstance(intent, Intent):
                logger.error(
                    "pods: %s returned %s, not an Intent; discarding", name, type(intent).__name__
                )
                continue
            if intent.pod != name:
                # A pod signing another pod's name would route its losses to
                # the wrong breaker, which is how a disabled pod keeps trading.
                logger.error(
                    "pods: %s returned an Intent labelled %r; discarding", name, intent.pod
                )
                continue
            intents.append(intent)
        return intents
