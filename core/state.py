"""
In-process application state and a minimal pub/sub event bus.

Both are ephemeral (in-memory only) — no persistence, no configuration.
Thread-safety is provided by a lock guarding mutation/iteration, not by
any framework magic.
"""
import logging
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List, Optional

logger = logging.getLogger(__name__)


class AppState:
    """Thread-safe singleton holding process-local runtime state."""

    _instance: Optional["AppState"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "AppState":
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._lock = threading.Lock()
                instance.market_data = {}
                instance.last_analysis_ts = None
                instance.budget_spent_today = 0.0
                cls._instance = instance
        return cls._instance

    def update_market_data(self, symbol: str, data: Any) -> None:
        with self._lock:
            self.market_data[symbol] = data

    def get_market_data(self, symbol: str) -> Any:
        with self._lock:
            return self.market_data.get(symbol)


class EventBus:
    """Thread-safe subscribe/publish event bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: DefaultDict[str, List[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event: str, callback: Callable[[Any], None]) -> None:
        with self._lock:
            self._subscribers[event].append(callback)

    def publish(self, event: str, payload: Any = None) -> None:
        """
        Call every subscriber for `event`. A subscriber that raises is
        logged and skipped — it never prevents other subscribers from
        receiving the event, and never propagates back to the publisher
        (INVARIANT 6: every external-ish call — a subscriber callback is
        effectively arbitrary external code from the bus's point of view —
        is guarded and its failure logged).
        """
        with self._lock:
            callbacks = list(self._subscribers.get(event, []))
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:
                logger.exception("EventBus subscriber raised for event=%s; continuing", event)


# Canonical process-wide instances. Modules should import and use these
# directly (from core.state import BUS, STATE) rather than constructing
# their own AppState()/EventBus() — AppState() already returns the same
# singleton via __new__, but EventBus() does not, so BUS is the one bus
# every publisher/subscriber in the process must share.
STATE = AppState()
BUS = EventBus()
