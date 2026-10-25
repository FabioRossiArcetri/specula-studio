"""
Rendering sizes of the node editor (Preferences → Render Size).

Each size (MICRO, SMALL, MEDIUM, LARGE) defines the font size the UI font is
loaded at, the spacers inside nodes and the auto-layout spacing.  MEDIUM is
the baseline; the other sizes are roughly 50 %, 70 % and 180 % of it.
Changing the size reloads the font at the new size.
"""
import logging

log = logging.getLogger(__name__)


RENDER_SIZES = ["MICRO", "SMALL", "MEDIUM", "LARGE"]
DEFAULT_RENDER_SIZE = "MEDIUM"

# ── Scale tables ──────────────────────────────────────────────────────────────

SCALE_DEFS: dict = {
    "MEDIUM": {
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
        "font_size":                   22,
        "node_header_spacer_width":   360,   # 200 × 1.8
        "node_output_spacer_width":   180,   # 100 × 1.8
        "layout_horizontal_spacing":  126,   # 70 × 1.8
        "layout_vertical_spacing":     83,   # 46 × 1.8
        "auto_layout_base_x":          90,   # 50 × 1.8
        "auto_layout_base_y":          90,   # 50 × 1.8
    },
    "SMALL": {
        # ~70 % of MEDIUM
        "font_size":                   15,
        "node_header_spacer_width":   140,   # 200 × 0.7
        "node_output_spacer_width":    70,   # 100 × 0.7
        "layout_horizontal_spacing":   46,   # 330 × 0.7
        "layout_vertical_spacing":     30,   # 220 × 0.7
        "auto_layout_base_x":          30,   #  30 × 0.7
        "auto_layout_base_y":          30,
    },
    "MICRO": {
        # ~50 % of MEDIUM
        "font_size":                   12,
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
        log.warning(f"[RENDER_SCALE] Unknown size '{size}'; keeping '{_current_size}'")


def get_size() -> str:
    """Return the currently active rendering size name."""
    return _current_size


def get(key: str):
    """Return the numeric value for *key* under the current scale."""
    return SCALE_DEFS[_current_size][key]


# ── Convenience accessors ─────────────────────────────────────────────────────

def font_size() -> int:
    """Pixel size the UI font is loaded at."""
    return SCALE_DEFS[_current_size]["font_size"]


# The tool windows (control panel, HPC) were laid out with DearPyGui's
# built-in font, 13 px high: their fixed sizes follow the UI font.
LAYOUT_FONT_SIZE = 13


def px(n: float) -> int:
    """*n* pixels of a tool window's layout, at the current font size."""
    return round(n * font_size() / LAYOUT_FONT_SIZE)


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
