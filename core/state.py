"""
In-process application state and a minimal pub/sub event bus.

Both are ephemeral (in-memory only) — no persistence, no configuration.
Thread-safety is provided by a lock guarding mutation/iteration, not by
any framework magic.
"""
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict, List, Optional


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
        with self._lock:
            callbacks = list(self._subscribers.get(event, []))
        for callback in callbacks:
            callback(payload)
