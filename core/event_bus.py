"""
ZeroBot — Event Bus (Pub/Sub)
Decouples components: strategies publish signals,
execution subscribes to signals, alerts subscribe to fills, etc.

Usage:
    from core.event_bus import bus
    bus.subscribe("signal", my_handler)
    bus.publish("signal", signal_data)

ENHANCEMENT: Added news_alert and sentiment_change events so the engine
can react instantly to news threshold crossings without waiting for the
60-second strategy polling cycle.
"""
import asyncio
from collections import defaultdict
from typing import Any, Callable, Dict, List
from core.logger import log


class EventBus:
    """Simple async pub/sub event bus."""

    EVENTS = [
        "signal",            # New trading signal
        "order_placed",      # Order submitted
        "order_filled",      # Order confirmed filled
        "order_cancelled",   # Order cancelled
        "order_rejected",    # Order rejected by broker
        "position_opened",   # New position opened
        "position_closed",   # Position closed
        "stop_hit",          # Stop loss triggered
        "target_hit",        # Profit target reached
        "risk_breach",       # Risk limit hit
        "system_halt",       # Emergency halt
        "system_resume",     # Resume after halt
        "daily_report",      # EOD report trigger
        "model_retrain",     # ML retrain trigger
        "heartbeat",         # Watchdog ping
        "tick",              # New market tick received
        "candle",            # New candle formed
        "alert",             # Send notification
        # ── NEWS EVENTS (new) ──────────────────────────────────────
        "news_alert",        # High-impact headline crossed threshold
                             # payload: {symbol, title, score, source, published_at}
        "sentiment_change",  # Symbol sentiment flipped direction (bull→bear or vice-versa)
                             # payload: {symbol, old_score, new_score, direction_change}
    ]

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._history: List[Dict] = []  # Last 100 events for debugging
        self._max_history = 100

    def subscribe(self, event: str, handler: Callable):
        """Register a handler for an event type. Idempotent — safe to call multiple times."""
        if event not in self.EVENTS:
            log.warning(f"EventBus: Unknown event '{event}' — adding anyway")
        # Idempotency: don't add same handler twice (prevents double-fire on re-registration)
        existing = self._subscribers[event]
        if handler not in existing:
            existing.append(handler)
            log.debug(f"EventBus: {handler.__name__} subscribed to '{event}'")
        else:
            log.debug(f"EventBus: {handler.__name__} already subscribed to '{event}' — skipped")

    def unsubscribe(self, event: str, handler: Callable):
        """Remove a handler."""
        self._subscribers[event] = [
            h for h in self._subscribers[event] if h != handler
        ]

    async def publish(self, event: str, data: Any = None):
        """Publish an event to all subscribers."""
        record = {"event": event, "data": data}
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history.pop(0)

        handlers = self._subscribers.get(event, [])
        if not handlers:
            return

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    handler(data)
            except Exception as e:
                log.error(f"EventBus: Error in handler '{handler.__name__}' for event '{event}': {e}")

    def publish_sync(self, event: str, data: Any = None):
        """Sync wrapper for non-async contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.publish(event, data))
            else:
                loop.run_until_complete(self.publish(event, data))
        except Exception as e:
            log.error(f"EventBus publish_sync error: {e}")

    def get_history(self, event: str = None) -> List[Dict]:
        if event:
            return [h for h in self._history if h["event"] == event]
        return self._history.copy()


# Global singleton
bus = EventBus()
