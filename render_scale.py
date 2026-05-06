"""
render_scale.py
===============
Rendering scale definitions for the SPECULA Studio node editor.

Provides size constants for SMALL, MEDIUM, and LARGE rendering modes.

  MEDIUM  –  the current production baseline values.
  LARGE   –  approximately 180 % of MEDIUM (rounded to integers).
  SMALL   –  approximately  50 % of MEDIUM (rounded to integers).

All call-sites should use the accessor functions (font_size(), etc.) so
that a preference change propagates immediately without re-importing.
"""

RENDER_SIZES = ["SMALL", "MEDIUM", "LARGE"]
DEFAULT_RENDER_SIZE = "MEDIUM"

# ── Scale tables ──────────────────────────────────────────────────────────────
# Each entry describes one complete set of graphical properties.
# MEDIUM contains the original hard-coded values; LARGE / SMALL are derived
# as ~180 % / ~50 % of those baseline values.

SCALE_DEFS: dict = {
    "MEDIUM": {
        # Font
        "font_size":                   18,

        # Node-editor spacers (set at node-creation time)
        "node_header_spacer_width":   200,   # static header row inside each node
        "node_output_spacer_width":   100,   # indent before output-pin labels

        # Auto-layout grid
        "layout_horizontal_spacing":  350,   # pixels between layout columns
        "layout_vertical_spacing":    235,   # pixels between rows in a column
        "auto_layout_base_x":          30,   # left margin of the first column
        "auto_layout_base_y":          30,   # top margin of the first row
    },
    "LARGE": {
        # ~180 % of MEDIUM
        "font_size":                   30,   # 18 × 1.8 = 32.4  → 32
        "node_header_spacer_width":   360,   # 200 × 1.8
        "node_output_spacer_width":   180,   # 100 × 1.8
        "layout_horizontal_spacing":  594,   # 330 × 1.8
        "layout_vertical_spacing":    396,   # 220 × 1.8
        "auto_layout_base_x":          54,   #  30 × 1.8
        "auto_layout_base_y":          54,
    },
    "SMALL": {
        # ~50 % of MEDIUM
        "font_size":                   10,   # 18 × 0.5
        "node_header_spacer_width":   100,   # 200 × 0.5
        "node_output_spacer_width":    50,   # 100 × 0.5
        "layout_horizontal_spacing":  200,   # 330 × 0.5
        "layout_vertical_spacing":    150,   # 220 × 0.5
        "auto_layout_base_x":          15,   #  30 × 0.5
        "auto_layout_base_y":          15,
    },
}

# ── Active scale state ────────────────────────────────────────────────────────

_current_size: str = DEFAULT_RENDER_SIZE


def set_size(size: str) -> None:
    """Set the active rendering size.  *size* must be one of RENDER_SIZES."""
    global _current_size
    if size in SCALE_DEFS:
        _current_size = size
    else:
        print(f"[RENDER_SCALE] Unknown size '{size}'; keeping '{_current_size}'")


def get_size() -> str:
    """Return the currently active rendering size name."""
    return _current_size


def get(key: str):
    """Return the numeric value for *key* under the current scale."""
    return SCALE_DEFS[_current_size][key]


# ── Convenience accessors (avoid typos at call-sites) ─────────────────────────

def font_size() -> int:
    return SCALE_DEFS[_current_size]["font_size"]


def node_header_spacer_width() -> int:
    return SCALE_DEFS[_current_size]["node_header_spacer_width"]


def node_output_spacer_width() -> int:
    return SCALE_DEFS[_current_size]["node_output_spacer_width"]


def layout_horizontal_spacing() -> int:
    return SCALE_DEFS[_current_size]["layout_horizontal_spacing"]


def layout_vertical_spacing() -> int:
    return SCALE_DEFS[_current_size]["layout_vertical_spacing"]


def auto_layout_base_x() -> int:
    return SCALE_DEFS[_current_size]["auto_layout_base_x"]


def auto_layout_base_y() -> int:
    return SCALE_DEFS[_current_size]["auto_layout_base_y"]