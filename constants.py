import dearpygui.dearpygui as dpg

SOCKETIO_SERVER = "http://127.0.0.1:5000"
STATUS_QUEUE_SIZE = 50
MONITOR_QUEUE_SIZE = 100
MAX_PLOT_HISTORY = 200
DEFAULT_PLOT_WIDTH = 780
DEFAULT_PLOT_HEIGHT = 400
MAX_QUEUE_ITEMS_PER_FRAME = 5

# Note: FONT_SIZE, LAYOUT_HORIZONTAL_SPACING and LAYOUT_VERTICAL_SPACING have
# been moved to render_scale.py so they can be varied at runtime via the
# Preferences → Render Size option (SMALL / MEDIUM / LARGE).

# Pin shapes for data inputs
DATA_SHAPE_EMPTY         = dpg.mvNode_PinShape_Triangle        # empty  triangle – unconnected single input
DATA_SHAPE_FILLED        = dpg.mvNode_PinShape_TriangleFilled  # filled triangle – connected   single input
DATA_MULTIPLE_SHAPE_EMPTY  = dpg.mvNode_PinShape_Circle        # empty  circle   – unconnected variadic input
DATA_MULTIPLE_SHAPE_FILLED = dpg.mvNode_PinShape_CircleFilled  # filled circle   – connected   variadic input

# Pin shapes for references
REF_SHAPE_EMPTY  = dpg.mvNode_PinShape_Quad        # empty  quad – unconnected reference
REF_SHAPE_FILLED = dpg.mvNode_PinShape_QuadFilled  # filled quad – connected   reference

DEFAULT_AUTO_SIMUL_PARAMS = True
DEFAULT_RENDER_SIZE = "MEDIUM"