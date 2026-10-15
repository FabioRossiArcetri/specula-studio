"""
Undo / redo history and the "unsaved changes" flag.

The editor mutates the model from many UI callbacks, so instead of wrapping
every mutation in a command object the history works on *states*: the app
periodically compares a fingerprint of the model with the last recorded one
and records a new state when it differs.  Undo/redo then simply restore a
recorded state.  Graphs are small (tens of objects), so this is cheap and
cannot miss a change.
"""

from __future__ import annotations

import json

from . import yaml_io
from .model import Graph


def fingerprint(graph: Graph) -> str:
    """Stable text identifying the model *content* (positions excluded)."""
    data = yaml_io.export_graph(graph, include_defaults=False)
    for node in data.values():
        node.pop("gui_pos", None)
    return json.dumps(data, default=str)


class History:
    def __init__(self, limit: int = 200):
        self.limit = limit
        self._undo: list = []
        self._redo: list = []
        self._current: tuple | None = None          # (state, fingerprint)

    @property
    def current_fingerprint(self) -> str | None:
        return self._current[1] if self._current else None

    def reset(self, state, fp: str) -> None:
        """Forget all history; *state* becomes the starting point."""
        self._undo.clear()
        self._redo.clear()
        self._current = (state, fp)

    def record(self, state, fp: str) -> bool:
        """Record *state* if its fingerprint differs from the current one."""
        if self._current is not None and fp == self._current[1]:
            return False
        if self._current is not None:
            self._undo.append(self._current)
            del self._undo[:-self.limit]
        self._redo.clear()
        self._current = (state, fp)
        return True

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self):
        """Return the previous state (or None)."""
        if not self._undo:
            return None
        self._redo.append(self._current)
        self._current = self._undo.pop()
        return self._current[0]

    def redo(self):
        if not self._redo:
            return None
        self._undo.append(self._current)
        self._current = self._redo.pop()
        return self._current[0]
