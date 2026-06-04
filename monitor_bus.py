"""
monitor_bus.py
==============
Thread-safe publish/subscribe bus that routes simulation data to in-process
monitor windows.

Data flow
---------
1. ``SocketIOClient`` receives a ``data_update`` event from specula's
   ``DisplayServer``.
2. ``NodeManager._on_data_update()`` calls ``MonitorBus.push(output_name, data)``.
3. Every ``InProcessMonitor`` that subscribed to *output_name* is notified on
   the caller thread (the Socket.IO background thread).  The monitors queue the
   payload and consume it on the DPG main thread.

Thread-safe publish/subscribe bus that routes simulation data to in-process
monitor windows.

Changes vs. previous version
-----------------------------
* ``push()`` tracks per-topic push and drop counts for diagnostics (Issue 6).
* ``drop_counts()`` / ``push_counts()`` expose the counters for logging.
"""

from __future__ import annotations

import threading
from constants import MONITOR_DROP_LOG_INTERVAL


class MonitorBus:
    """Thread-safe publish/subscribe bus for live simulation data."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list] = {}
        self._push_counts: dict[str, int]  = {}
        self._drop_counts: dict[str, int]  = {}
        self._lock = threading.Lock()

    # ── Subscription management ────────────────────────────────────────────────

    def subscribe(self, output_name: str, callback) -> None:
        with self._lock:
            self._subscribers.setdefault(output_name, []).append(callback)

    def unsubscribe(self, output_name: str, callback) -> None:
        with self._lock:
            subs = self._subscribers.get(output_name)
            if subs:
                try:
                    subs.remove(callback)
                except ValueError:
                    pass

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._push_counts.clear()
            self._drop_counts.clear()

    # ── Data delivery ──────────────────────────────────────────────────────────

    def push(self, topic: str, payload: dict) -> None:
        """Push data to all subscribers of a topic."""
        callbacks = self._subscribers.get(topic, [])
        if not callbacks:
            print(f"[BUS] No subscribers for topic '{topic}'")
            return
        
        print(f"[BUS] Pushing to topic '{topic}': {len(callbacks)} subscriber(s)")
        self._push_counts[topic] = self._push_counts.get(topic, 0) + 1
        
        for callback in callbacks:
            try:
                callback(payload)
            except _DropFrame:
                self._drop_counts[topic] = self._drop_counts.get(topic, 0) + 1
            except Exception as e:
                print(f"[BUS] Error calling subscriber for '{topic}': {e}")

    # ── Introspection ──────────────────────────────────────────────────────────

    def subscriber_count(self, output_name: str) -> int:
        with self._lock:
            return len(self._subscribers.get(output_name, []))

    def all_subscribed_outputs(self) -> list[str]:
        with self._lock:
            return [k for k, v in self._subscribers.items() if v]

    def drop_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._drop_counts)

    def push_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._push_counts)


class _DropFrame(Exception):
    """Sentinel raised by monitor callbacks to signal a dropped frame."""