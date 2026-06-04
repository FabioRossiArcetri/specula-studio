import dearpygui.dearpygui as dpg

SOCKETIO_SERVER = "http://127.0.0.1:5000"
STATUS_QUEUE_SIZE = 50
MONITOR_QUEUE_SIZE = 100
MAX_PLOT_HISTORY = 200
DEFAULT_PLOT_WIDTH = 780
DEFAULT_PLOT_HEIGHT = 400
MAX_QUEUE_ITEMS_PER_FRAME = 5

# ── Communication protocol ────────────────────────────────────────────────────
# Increment when the data envelope format changes so mismatched clients can be
# detected early (studio checks against SPECULA's advertised version).
PROTOCOL_VERSION = 2

# ── Stream watchdog (Issue 2) ─────────────────────────────────────────────────
# Seconds to wait for a 'done' event before the studio re-arms the pull cycle.
STREAM_STALL_TIMEOUT = 10.0

# Seconds between server-side heartbeat events (SPECULA side).
STREAM_HEARTBEAT_INTERVAL = 5.0

# ── Drop-frame diagnostics (Issue 6) ─────────────────────────────────────────
# Log a warning to stdout every time this many frames have been silently dropped.
MONITOR_DROP_LOG_INTERVAL = 200

# Note: FONT_SIZE, LAYOUT_HORIZONTAL_SPACING and LAYOUT_VERTICAL_SPACING have
# been moved to render_scale.py so they can be varied at runtime via the
# Preferences → Render Size option (MICRO / SMALL / MEDIUM / LARGE).

# Pin shapes for data inputs
DATA_SHAPE_EMPTY           = dpg.mvNode_PinShape_Triangle
DATA_SHAPE_FILLED          = dpg.mvNode_PinShape_TriangleFilled
DATA_MULTIPLE_SHAPE_EMPTY  = dpg.mvNode_PinShape_Circle
DATA_MULTIPLE_SHAPE_FILLED = dpg.mvNode_PinShape_CircleFilled

# Pin shapes for references
REF_SHAPE_EMPTY  = dpg.mvNode_PinShape_Quad
REF_SHAPE_FILLED = dpg.mvNode_PinShape_QuadFilled

DEFAULT_AUTO_SIMUL_PARAMS = True
DEFAULT_RENDER_SIZE = "MEDIUM"