"""
Monitor data handling shared by every monitor window (in-process and
subprocess) and by the in-process probes.

Payload format (also produced by SPECULA's DisplayServer in "data" mode)::

    {"type": "scalar" | "1d_array" | "2d_array" | "nd_array" | "multi_data",
     "data": <list or numpy array>, "shape": [..]}

Only numpy here — the DearPyGui calls go through a ``DPGPlotter``.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)


# ── producing payloads (in-process probes) ────────────────────────────────────

def to_cpu_array(value) -> np.ndarray | None:
    """Copy *value* (numpy / cupy / scalar / list) to a float32 numpy array."""
    if value is None:
        return None
    if hasattr(value, "get") and not isinstance(value, (np.ndarray, dict)):
        try:                      # cupy array
            value = value.get()
        except Exception:
            pass
    try:
        if np.iscomplexobj(value):
            value = np.abs(np.asarray(value))
        arr = np.array(value, dtype=np.float32, copy=True)
    except (TypeError, ValueError):
        return None
    return arr if arr.size else None


def array_type(arr: np.ndarray) -> str:
    if arr.ndim == 0 or arr.size == 1:
        return "scalar"
    return {1: "1d_array", 2: "2d_array"}.get(arr.ndim, "nd_array")


def make_payload(arr: np.ndarray) -> dict:
    return {"type": array_type(arr), "data": arr, "shape": list(arr.shape)}


def extract_display_array(data_obj, output_name: str = "") -> np.ndarray | None:
    """
    The array to display for a SPECULA data object, copied to the CPU.

    Uses the object's own ``array_for_display()`` first — the same method
    SPECULA's DisplayServer uses — then ``get_value()``, then a few common
    attribute names.
    """
    for getter in ("array_for_display", "get_value"):
        fn = getattr(data_obj, getter, None)
        if callable(fn):
            try:
                arr = to_cpu_array(fn())
            except Exception:
                arr = None
            if arr is not None:
                return arr
    names = [output_name, output_name.removeprefix("out_")] if output_name else []
    names += ["value", "slopes", "pixels", "modes", "phaseInNm", "commands", "ef"]
    for name in names:
        arr = to_cpu_array(getattr(data_obj, name, None)) if name else None
        if arr is not None:
            return arr
    return None


# ── consuming payloads (monitor windows) ──────────────────────────────────────

def payload_to_array(payload: dict) -> np.ndarray | None:
    """Convert a payload to a float32 array (reshaped when a shape is given)."""
    if not isinstance(payload, dict):
        return None
    data_type = payload.get("type")
    data = payload.get("data")
    if data is None:
        return None
    try:
        if data_type == "multi_data":
            if not isinstance(data, list) or not data:
                return None
            arr = np.asarray(data[0], dtype=np.float32)
            shapes = payload.get("shapes")
            if shapes and int(np.prod(shapes[0])) == arr.size:
                arr = arr.reshape(tuple(shapes[0]))
            return arr
        if data_type not in (None, "scalar", "1d_array", "2d_array", "nd_array"):
            log.debug("Unhandled payload type '%s'", data_type)
            return None
        if isinstance(data, (list, np.ndarray)):
            arr = np.asarray(data, dtype=np.float32)
        else:
            arr = np.array([float(data)], dtype=np.float32)
        shape = payload.get("shape")
        if shape is not None and data_type != "scalar":
            shape = tuple(shape)
            if int(np.prod(shape)) == arr.size:
                arr = arr.reshape(shape)
        return arr
    except (TypeError, ValueError) as exc:
        log.warning("Monitor data conversion error: %s", exc)
        return None


def reduce_to_2d(arr: np.ndarray) -> np.ndarray | None:
    """Image to show for arrays with more than two dimensions."""
    if arr.ndim < 3:
        return arr
    if arr.shape[-1] <= 3:
        return np.mean(arr, axis=tuple(range(arr.ndim - 2)))
    return arr.reshape(-1, arr.shape[-1])[:1000]


def min_update_interval(arr: np.ndarray) -> float:
    """Seconds between redraws: large images are redrawn less often."""
    if arr.ndim < 2:
        return 0.05
    px = arr.shape[-1] * arr.shape[-2]
    return 0.5 if px > 1_000_000 else 0.25 if px > 250_000 else 0.1 if px > 10_000 else 0.05


def plot_array(plotter, arr: np.ndarray) -> bool:
    """Draw *arr* with the most suitable plot of a ``DPGPlotter``."""
    if arr.ndim == 0 or arr.size == 1:
        return plotter.plot_history(float(arr.reshape(-1)[0]))
    if arr.ndim == 1:
        return plotter.plot_vector(arr) or plotter.plot_scatter(arr)
    image = reduce_to_2d(arr)
    if image is not None and image.ndim == 2:
        return plotter.plot_2d_image_clean(image)
    return False


def describe_array(arr: np.ndarray) -> tuple[str, str, str]:
    """(type, shape, range) strings for the monitor info labels."""
    dtype_str = f"ndarray ({arr.dtype})"
    shape_str = str(arr.shape)
    if arr.size and np.issubdtype(arr.dtype, np.number):
        finite = arr[np.isfinite(arr)]
        range_str = f"[{finite.min():.4g}, {finite.max():.4g}]" if finite.size else "N/A"
    else:
        range_str = "N/A"
    return dtype_str, shape_str, range_str
