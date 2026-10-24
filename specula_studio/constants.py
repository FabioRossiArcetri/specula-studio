import dearpygui.dearpygui as dpg

SOCKETIO_SERVER = "http://127.0.0.1:5000"
MONITOR_QUEUE_SIZE = 100
MAX_PLOT_HISTORY = 200
DEFAULT_PLOT_WIDTH = 780
DEFAULT_PLOT_HEIGHT = 400
MAX_QUEUE_ITEMS_PER_FRAME = 5

# Seconds a monitor waits for a 'done' event before it re-arms the pull cycle;
# the wait doubles at each re-arm (a paused simulation answers nothing) up to
# STREAM_STALL_MAX_TIMEOUT, and is reset by the next 'done'.
STREAM_STALL_TIMEOUT = 10.0
STREAM_STALL_MAX_TIMEOUT = 60.0

# Failed connection attempts after which a monitor reports the display server
# as unreachable (it keeps retrying).
MONITOR_UNREACHABLE_ATTEMPTS = 3

# A monitor logs its dropped frames every time this many have been dropped.
MONITOR_DROP_LOG_INTERVAL = 200

# Pin shapes for data inputs
DATA_SHAPE_EMPTY           = dpg.mvNode_PinShape_Triangle
DATA_SHAPE_FILLED          = dpg.mvNode_PinShape_TriangleFilled
DATA_MULTIPLE_SHAPE_EMPTY  = dpg.mvNode_PinShape_Circle
DATA_MULTIPLE_SHAPE_FILLED = dpg.mvNode_PinShape_CircleFilled

# Pin shapes for references
REF_SHAPE_EMPTY  = dpg.mvNode_PinShape_Quad
REF_SHAPE_FILLED = dpg.mvNode_PinShape_QuadFilled

DEFAULT_RENDER_SIZE = "MEDIUM"
