"""Backward-compatible re-exports; the model now lives in :mod:`core`."""

from .core.model import Graph, GraphManager  # noqa: F401
from .core.syntax import (  # noqa: F401
    REQUIRED,
    add_ref_value,
    is_list_ref,
    remove_ref_value,
    validate_node_name,
)
