import dearpygui.dearpygui as dpg

SOCKETIO_SERVER = "http://127.0.0.1:5000"
STATUS_QUEUE_SIZE = 50
MONITOR_QUEUE_SIZE = 100
MAX_PLOT_HISTORY = 200
DEFAULT_PLOT_WIDTH = 780
DEFAULT_PLOT_HEIGHT = 400
LAYOUT_HORIZONTAL_SPACING = 330
LAYOUT_VERTICAL_SPACING = 220
MAX_QUEUE_ITEMS_PER_FRAME = 5
FONT_SIZE = 18

# Pin shapes for data inputs
DATA_SHAPE_EMPTY = dpg.mvNode_PinShape_Triangle  # Empty triangle for single inputs with no connection
DATA_SHAPE_FILLED = dpg.mvNode_PinShape_TriangleFilled  # Filled triangle for single inputs with connection
DATA_MULTIPLE_SHAPE_EMPTY = dpg.mvNode_PinShape_Circle  # Empty circle for multiple inputs with no connection
DATA_MULTIPLE_SHAPE_FILLED = dpg.mvNode_PinShape_CircleFilled  # Filled circle for multiple inputs with connection
# Pin shapes for references
REF_SHAPE_EMPTY = dpg.mvNode_PinShape_Quad  # Empty quad for reference inputs with no connection
REF_SHAPE_FILLED = dpg.mvNode_PinShape_QuadFilled  # Filled quad for reference inputs with connection

DEFAULT_AUTO_SIMUL_PARAMS = True
