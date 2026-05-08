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
"""

from __future__ import annotations

import threading


class MonitorBus:
    """Thread-safe publish/subscribe bus for live simulation data.

    Subscribers register a callable keyed on the fully-qualified server output
    name (e.g. ``"my_wfs.out_slopes"``).  Publishers call ``push()`` to
    deliver a raw-data payload to all registered subscribers.
    """

    def __init__(self) -> None:
        # output_name -> list[callable]
        self._subscribers: dict[str, list] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    def subscribe(self, output_name: str, callback) -> None:
        """Register *callback* to receive pushes for *output_name*.

        Parameters
        ----------
        output_name : Fully-qualified server output, e.g. ``"node.out_slopes"``.
        callback    : callable(raw_data) – called on the publisher thread.
        """
        with self._lock:
            self._subscribers.setdefault(output_name, []).append(callback)

    def unsubscribe(self, output_name: str, callback) -> None:
        """Remove *callback* from subscribers for *output_name* (no-op if absent)."""
        with self._lock:
            subs = self._subscribers.get(output_name)
            if subs:
                try:
                    subs.remove(callback)
                except ValueError:
                    pass

    def clear(self) -> None:
        """Remove all subscriptions (called on simulation stop)."""
        with self._lock:
            self._subscribers.clear()

    # ------------------------------------------------------------------
    # Data delivery
    # ------------------------------------------------------------------

    def push(self, output_name: str, data) -> None:
        """Deliver *data* to every subscriber of *output_name*.

        Called from the Socket.IO background thread; callbacks must be
        thread-safe (queue a payload, never touch DPG directly).
        """
        with self._lock:
            callbacks = list(self._subscribers.get(output_name, []))
        for cb in callbacks:
            try:
                cb(data)
            except Exception as exc:
                print(f"[MONITOR_BUS] Callback error for '{output_name}': {exc}")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def subscriber_count(self, output_name: str) -> int:
        """Return the number of subscribers for *output_name*."""
        with self._lock:
            return len(self._subscribers.get(output_name, []))

    def all_subscribed_outputs(self) -> list[str]:
        """Return a list of output names that have at least one subscriber."""
        with self._lock:
            return [k for k, v in self._subscribers.items() if v]
