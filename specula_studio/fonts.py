"""Default font locations (shipped with matplotlib)."""

import pathlib

import matplotlib

MATPLOTLIB_FONTS_PATH = pathlib.Path(matplotlib.get_data_path()) / "fonts" / "ttf"
FONT_PATH = str(MATPLOTLIB_FONTS_PATH / "DejaVuSerif.ttf")
