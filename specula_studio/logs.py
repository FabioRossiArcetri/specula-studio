"""
Logging configuration for SPECULA Studio.

All modules log through ``logging.getLogger(__name__)``.  ``setup_logging``
installs a console handler plus a :class:`RingBufferHandler` that keeps the
most recent records in memory so the GUI log window can display them.
"""

from __future__ import annotations

import collections
import logging
import threading

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


class _ConsoleHandler(logging.StreamHandler):
    """Marker subclass so setup_logging() can find its own console handler."""


class RingBufferHandler(logging.Handler):
    """Thread-safe handler keeping the last *capacity* formatted records."""

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._records: collections.deque = collections.deque(maxlen=capacity)
        self._lock_buf = threading.Lock()
        self.version = 0  # incremented on every new record

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock_buf:
            self._records.append((record.levelno, line))
            self.version += 1

    def lines(self, min_level: int = logging.DEBUG) -> list[str]:
        with self._lock_buf:
            return [line for lvl, line in self._records if lvl >= min_level]

    def clear(self) -> None:
        with self._lock_buf:
            self._records.clear()
            self.version += 1


_ring: RingBufferHandler | None = None


def setup_logging(level: str | int = "INFO") -> RingBufferHandler:
    """Configure the ``specula_studio`` logger hierarchy (idempotent)."""
    global _ring
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger("specula_studio")
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S")

    if not any(isinstance(h, _ConsoleHandler) for h in logger.handlers):
        console = _ConsoleHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

    if _ring is None:
        _ring = RingBufferHandler()
        _ring.setFormatter(formatter)
        logger.addHandler(_ring)
    return _ring


def ring_buffer() -> RingBufferHandler | None:
    return _ring


def set_level(level: str | int) -> None:
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logging.getLogger("specula_studio").setLevel(level)
