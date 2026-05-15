"""
render_scale.py
===============
Rendering scale definitions for the SPECULA Studio node editor.

Provides size constants for SMALL, MEDIUM, and LARGE rendering modes.

  MEDIUM  –  the current production baseline values.
  LARGE   –  approximately 180 % of MEDIUM (rounded to integers).
  SMALL   –  approximately  70 % of MEDIUM (rounded to integers).

Font sizing strategy
--------------------
A single font is loaded at MEDIUM size (18 px).  The ImGui global font
scale (dpg.set_global_font_scale) is then used to scale ALL rendered text
uniformly.  This approach is guaranteed to work in both directions (up and
down) because it operates on the rasterised glyph atlas at render time,
whereas dpg.bind_font() can silently fail when switching to a larger handle
than the one that was active when setup_dearpygui() was called.

Scale factors
~~~~~~~~~~~~~
  SMALL  → 0.70  (effective ~13 px)
  MEDIUM → 1.00  (effective ~18 px)
  LARGE  → 1.80  (effective ~32 px)
"""

RENDER_SIZES = ["MICRO", "SMALL", "MEDIUM", "LARGE"]
DEFAULT_RENDER_SIZE = "MEDIUM"

# ── Scale tables ──────────────────────────────────────────────────────────────
# MEDIUM contains the original hard-coded values.
# LARGE / SMALL are derived as ~180 % / ~70 % of those baseline values.

SCALE_DEFS: dict = {
    "MEDIUM": {
        # Font (base size loaded into the atlas; actual display size is
        # controlled by the global_font_scale factor below)
        "font_size":                   18,

        # Node-editor spacers (set at node-creation time)
        "node_header_spacer_width":   200,   # static header row inside each node
        "node_output_spacer_width":   100,   # indent before output-pin labels

        # Auto-layout grid
        "layout_horizontal_spacing":   70,   # pixels between layout columns
        "layout_vertical_spacing":     46,   # pixels between rows in a column
        "auto_layout_base_x":          50,   # left margin of the first column
        "auto_layout_base_y":          50,   # top margin of the first row
    },
    "LARGE": {
        # ~180 % of MEDIUM
        "font_size":                   22,   # same base; display scaled by 1.8
        "node_header_spacer_width":   360,   # 200 × 1.8
        "node_output_spacer_width":   180,   # 100 × 1.8
        "layout_horizontal_spacing":  126,   # 70 × 1.8
        "layout_vertical_spacing":     83,   # 46 × 1.8
        "auto_layout_base_x":          90,   # 50 × 1.8
        "auto_layout_base_y":          90,   # 50 × 1.8
    },
    "SMALL": {
        # ~70 % of MEDIUM
        "font_size":                   15,   # same base; display scaled by 0.7
        "node_header_spacer_width":   140,   # 200 × 0.7
        "node_output_spacer_width":    70,   # 100 × 0.7
        "layout_horizontal_spacing":   46,   # 330 × 0.7
        "layout_vertical_spacing":     30,   # 220 × 0.7
        "auto_layout_base_x":          30,   #  30 × 0.7
        "auto_layout_base_y":          30,
    },
    "MICRO": {
        # ~50 % of MEDIUM
        "font_size":                   12,   # same base; display scaled by 0.5
        "node_header_spacer_width":   90,   # 200 × 0.5
        "node_output_spacer_width":    45,   # 100 × 0.5
        "layout_horizontal_spacing":   30,   # 70 × 0.5
        "layout_vertical_spacing":     25,   # 220 × 0.7
        "auto_layout_base_x":          25,   #  30 × 0.7
        "auto_layout_base_y":          25,
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


# ── Convenience accessors ─────────────────────────────────────────────────────

def font_size() -> int:
    """Base font size (actual display controlled by global scale)."""
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