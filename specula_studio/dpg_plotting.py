"""DearPyGui plots for monitor windows: line, history, scatter and image views."""
import numpy as np
import dearpygui.dearpygui as dpg
from matplotlib import colormaps
import traceback
from .constants import MAX_PLOT_HISTORY, DEFAULT_PLOT_WIDTH, DEFAULT_PLOT_HEIGHT
import logging

log = logging.getLogger(__name__)


def _histogram_series(x, y, **kwargs):
    """Bar series with adjacent bars: one bar per vector element."""
    return dpg.add_bar_series(x, y, weight=1.0, **kwargs)


_COLORMAPS: dict = {}   # matplotlib colormap name -> DPG colormap item


def _dpg_colormap(name: str):
    """DPG colormap item sampled from the matplotlib colormap *name* (cached)."""
    item = _COLORMAPS.get(name)
    if item is not None and dpg.does_item_exist(item):
        return item
    colors = (colormaps[name](np.linspace(0.0, 1.0, 256)) * 255).round().astype(int)
    with dpg.colormap_registry(show=False):
        item = dpg.add_colormap([list(c) for c in colors], False)
    _COLORMAPS[name] = item
    return item


class InteractiveImageViewer:
    """
    Interactive image viewer built on a DPG *plot*.

    Zoom/pan/reset are handled entirely by the DPG plot widget:
      * Scroll wheel  → zoom in / out
      * Left-drag     → pan
      * Double-click  → reset to full image

    Aspect-ratio preservation
    -------------------------
    ``equal_aspects=True`` on the plot ensures that one data unit always maps
    to the same number of screen pixels in X and Y.  Because the image spans
    [0, img_width] × [0, img_height] in data space, this guarantees the
    original pixel aspect ratio is kept regardless of how the window is resized
    (letterboxing / pillarboxing is added automatically by DPG).

    Filtering
    ---------
    DearPyGui samples textures bilinearly and has no nearest-neighbour
    option, so the two modes use different series:

    * ``filtering=False`` (default): a *heat series* — every image pixel is a
      flat rectangle, so pixels stay sharp at any zoom.  The matplotlib
      colormap is installed as a DPG colormap of the plot.  Images with more
      than ``_MAX_HEAT_CELLS`` pixels use the texture instead (drawing that
      many rectangles every frame would be too slow).
    * ``filtering=True``: an RGBA texture drawn with ``add_image_series``.

    Both place image row 0 at the bottom (y = 0).

    Resizing
    --------
    ``update_size(width, height)`` reconfigures the DPG plot widget to match
    the container dimensions on every frame in which a resize is detected.
    ``equal_aspects=True`` takes care of the content aspect ratio inside the
    (potentially non-square) plot area.
    """

    # Reserve this many pixels for the one-line info bar above the plot
    _INFO_ROW_PX = 22

    # Largest image drawn as a heat series (one rectangle per pixel)
    _MAX_HEAT_CELLS = 512 * 512

    def __init__(self, parent_tag, width: int = -1, height: int = -1,
                 filtering: bool = False):
        self.parent = parent_tag
        self.filtering = filtering
        # -1 means "fill remaining space"; explicit px values are set by update_size()
        self.width  = width
        self.height = height

        # DPG item tags
        self.texture_registry_tag = None
        self.texture_tag          = None
        self.plot_tag             = None
        self.x_axis_tag           = None
        self.y_axis_tag           = None
        self.image_series_tag     = None   # texture mode
        self.heat_series_tag      = None   # pixel (heat series) mode
        self.container_tag        = None
        self.info_text_tag        = None

        # Current image dimensions
        self.img_width  = 0
        self.img_height = 0
        self.mode: str | None = None       # "pixels" or "texture"
        self.current_data = None
        self._colormap = "seismic"

    # =========================================================================
    # Internal builders
    # =========================================================================

    def _build_plot(self) -> None:
        """Create the outer group, info bar, texture registry and plot widget."""
        self.container_tag = dpg.add_group(parent=self.parent, horizontal=False)

        # One-line info bar
        self.info_text_tag = dpg.add_text(
            "Scroll to zoom  │  Drag to pan  │  Double-click to reset",
            color=[150, 150, 150],
            parent=self.container_tag,
        )

        # Texture registry (must exist before any texture/series)
        self.texture_registry_tag = dpg.add_texture_registry(show=False)

        # Initial plot height: subtract info bar height
        plot_h = (self.height - self._INFO_ROW_PX) if self.height > 0 else -1

        with dpg.plot(
            label="",
            parent=self.container_tag,
            width=self.width,
            height=plot_h,
            # equal_aspects=True: 1 data unit = same pixel count in X and Y.
            # This is what preserves the image's original aspect ratio when the
            # plot widget is wider or taller than the image proportions.
            equal_aspects=True,
            no_title=True,
        ) as plot_id:
            self.plot_tag   = plot_id
            self.x_axis_tag = dpg.add_plot_axis(dpg.mvXAxis, label="")
            self.y_axis_tag = dpg.add_plot_axis(dpg.mvYAxis, label="")

    def _delete_series(self) -> None:
        for attr in ("image_series_tag", "heat_series_tag", "texture_tag"):
            tag = getattr(self, attr)
            if tag and dpg.does_item_exist(tag):
                dpg.delete_item(tag)
            setattr(self, attr, None)

    def _fit(self) -> None:
        """Show the whole image.

        ``fit_axis_data()`` rather than ``set_axis_limits()``: the latter
        *locks* the axis, so the user could no longer zoom or pan; the former
        sets the current view range and leaves the axis interactive.
        """
        dpg.fit_axis_data(self.x_axis_tag)
        dpg.fit_axis_data(self.y_axis_tag)

    def _draw_pixels(self, data_2d: np.ndarray, dmin: float, dmax: float,
                     colormap: str, rebuild: bool) -> None:
        """Heat series: one flat rectangle per pixel (no filtering)."""
        # ImPlot draws heatmap row 0 at the top: flip so row 0 is at y = 0,
        # as in texture mode.
        values = np.ascontiguousarray(data_2d[::-1], dtype=np.float64).ravel()
        if dmax <= dmin:
            dmax = dmin + 1.0
        if rebuild:
            self._delete_series()
            self.heat_series_tag = dpg.add_heat_series(
                values, self.img_height, self.img_width,
                scale_min=dmin, scale_max=dmax, format="",
                bounds_min=(0, 0), bounds_max=(self.img_width, self.img_height),
                parent=self.y_axis_tag,
            )
        else:
            dpg.set_value(self.heat_series_tag, [values])
            dpg.configure_item(self.heat_series_tag, scale_min=dmin, scale_max=dmax)
        if rebuild or colormap != self._colormap:
            dpg.bind_colormap(self.plot_tag, _dpg_colormap(colormap))

    def _draw_texture(self, data_2d: np.ndarray, dmin: float, dmax: float,
                      colormap: str, rebuild: bool) -> None:
        """RGBA texture drawn with bilinear filtering."""
        if dmax > dmin:
            normalized = (data_2d - dmin) / (dmax - dmin)
        else:
            normalized = np.zeros_like(data_2d, dtype=np.float32)
        rgba = colormaps[colormap](normalized).astype(np.float32)   # (H, W, 4)
        pixel_data = np.ascontiguousarray(rgba).ravel()
        if not rebuild:
            # Same shape: stream new pixels into the existing texture
            dpg.set_value(self.texture_tag, pixel_data)
            return
        self._delete_series()
        self.texture_tag = dpg.add_dynamic_texture(
            width=self.img_width, height=self.img_height,
            default_value=pixel_data, parent=self.texture_registry_tag,
        )
        # Texture row 0 (uv_min) is drawn at bounds_min, i.e. at y = 0.
        self.image_series_tag = dpg.add_image_series(
            self.texture_tag,
            bounds_min=[0, 0],
            bounds_max=[self.img_width, self.img_height],
            uv_min=[0.0, 0.0],
            uv_max=[1.0, 1.0],
            parent=self.y_axis_tag,
        )

    # =========================================================================
    # Public API
    # =========================================================================

    def update_image(self, data_2d: np.ndarray, colormap: str = "seismic") -> bool:
        """Update the viewer with a new 2-D float array."""
        try:
            if data_2d is None or data_2d.size == 0:
                return False

            img_height, img_width = data_2d.shape
            dmin = float(data_2d.min())
            dmax = float(data_2d.max())

            first = self.plot_tag is None or not dpg.does_item_exist(self.plot_tag)
            if first:
                self._build_plot()
            shape_changed = (img_width, img_height) != (self.img_width, self.img_height)
            mode = ("texture" if self.filtering or data_2d.size > self._MAX_HEAT_CELLS
                    else "pixels")
            # New shape or filtering toggled: new series (the zoom is kept
            # unless the shape changed)
            rebuild = first or shape_changed or mode != self.mode
            self.img_width, self.img_height = img_width, img_height

            if mode == "pixels":
                self._draw_pixels(data_2d, dmin, dmax, colormap, rebuild)
            else:
                self._draw_texture(data_2d, dmin, dmax, colormap, rebuild)
            if first or shape_changed:
                self._fit()

            self.mode = mode
            self.current_data = data_2d
            self._colormap = colormap
            self._update_info_text(dmin, dmax)
            return True

        except Exception as e:
            log.error(f"[InteractiveImageViewer.update_image] Error: {e}")
            traceback.print_exc()
            return False

    def _update_info_text(self, dmin: float | None = None, dmax: float | None = None) -> None:
        try:
            if self.info_text_tag and dpg.does_item_exist(self.info_text_tag):
                if dmin is not None:
                    view = {"pixels": "pixels", "texture": "filtered"}.get(self.mode, "")
                    txt = (
                        f"{self.img_width}\u00d7{self.img_height} px ({view})  \u2502  "
                        f"Range: [{dmin:.4g},\u202f{dmax:.4g}]  \u2502  "
                        "Scroll to zoom  \u2502  Drag to pan  \u2502  "
                        "Double-click to reset"
                    )
                else:
                    txt = (
                        f"{self.img_width}\u00d7{self.img_height} px  \u2502  "
                        "Scroll to zoom  \u2502  Drag to pan"
                    )
                dpg.set_value(self.info_text_tag, txt)
        except Exception:
            pass

    def update_size(self, width: int, height: int) -> None:
        """
        Resize the plot widget to fill the given container dimensions.

        Called by ``DPGPlotter.update_size()`` on every frame where the host
        window is resized.  ``_INFO_ROW_PX`` pixels are reserved for the info
        bar above the plot.  The plot's ``equal_aspects=True`` setting then
        letterboxes/pillarboxes the image content to preserve its aspect ratio
        within the (potentially non-square) plot frame.
        """
        self.width = max(width, 50)
        if height > 0:
            plot_h = max(height - self._INFO_ROW_PX, 50)
        else:
            plot_h = -1

        if self.plot_tag and dpg.does_item_exist(self.plot_tag):
            try:
                dpg.configure_item(self.plot_tag, width=self.width, height=plot_h)
            except Exception as e:
                log.error(f"[InteractiveImageViewer.update_size] Error: {e}")

    def set_filtering(self, enabled: bool) -> None:
        """Switch texture filtering and redraw the current image at once."""
        if enabled == self.filtering:
            return
        self.filtering = enabled
        if self.current_data is not None:
            self.update_image(self.current_data, self._colormap)


class DPGPlotter:
    """Unified plotting class with multiple visualization modes and responsive layout."""

    def __init__(self, parent_tag=None, width=DEFAULT_PLOT_WIDTH, height=DEFAULT_PLOT_HEIGHT):
        self.parent = parent_tag
        self.base_width = width
        self.base_height = height
        self.current_width = width
        self.current_height = height

        # Plot elements (for line plots and heatmaps)
        self.plot_tag = None
        self.line_series_tag = None
        self.heat_series_tag = None

        # Interactive image viewer
        self.image_viewer = None

        # Data tracking
        self.history_data = []
        self.max_history = MAX_PLOT_HISTORY
        self.current_mode = None  # 'history', 'vector', 'heatmap', 'image'
        self.current_shape = None

        self.vector_history_buffer = None  # Will hold a 2D array (Time, VectorIndex)
        self.vector_mode = "snapshot"     # "snapshot" or "history"
        self.vector_style = "line"        # snapshot drawn as "line" or "histogram"
        self.vector_line_tags = []        # Track tags for multi-line mode
        self.image_filtering = False      # bilinear texture filtering of images

    @staticmethod
    def _ensure_1d(data: np.ndarray) -> np.ndarray | None:
        """Return a 1-D view of data, or None if not possible."""
        if data.ndim == 1:
            return data
        if data.ndim == 0:
            return np.array([float(data)])
        if data.ndim == 2:
            if data.shape[0] == 1:
                return data[0]
            if data.shape[1] == 1:
                return data[:, 0]
        return data.flatten()

    def set_vector_mode(self, mode):
        """Sets mode to 'snapshot' or 'history' (one line per element over time)."""
        if mode != self.vector_mode:
            self.vector_mode = mode
            self.clear() # Clear to re-setup axes for the new coordinate system

    def set_vector_style(self, style: str) -> None:
        """Draw snapshot vectors as a 'line' or as a 'histogram' (one bar per element)."""
        if style != self.vector_style:
            self.vector_style = style
            if self.current_mode == "vector":
                self._clear_previous()
                self.current_mode = None   # rebuilt with the new series on the next frame

    def set_image_filtering(self, enabled: bool) -> None:
        self.image_filtering = enabled
        if self.image_viewer is not None:
            self.image_viewer.set_filtering(enabled)

    def plot_vector(self, vector: np.ndarray, label: str = "Vector") -> bool:
        try:
            vector = self._ensure_1d(vector)
            if vector is None or vector.size == 0: return False

            if self.vector_mode == "history":
                return self._plot_vector_time_series(vector, label)

            # Default: Snapshot mode
            if self.current_mode != "vector":
                self._clear_previous()
                self.current_mode = "vector"

            factory = _histogram_series if self.vector_style == "histogram" else dpg.add_line_series
            self._create_or_update_line_plot(list(range(len(vector))), vector.tolist(), label,
                                             series_factory=factory)
            return True
        except Exception as e:
            log.error(f"[DPGPlotter.plot_vector] Error: {e}")
            return False

    def _plot_vector_time_series(self, vector: np.ndarray, label: str) -> bool:
        v_len = len(vector)
        # Initialize or update the 2D history buffer
        if self.vector_history_buffer is None or self.vector_history_buffer.shape[1] != v_len:
            self.vector_history_buffer = vector.reshape(1, -1)
        else:
            self.vector_history_buffer = np.vstack([self.vector_history_buffer, vector])
            if len(self.vector_history_buffer) > self.max_history:
                self.vector_history_buffer = self.vector_history_buffer[1:]

        if self.current_mode != "vector_history":
            self._clear_previous()
            self.current_mode = "vector_history"
            self.vector_line_tags = []

        history_len = len(self.vector_history_buffer)
        x_axis_data = list(range(history_len))

        if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
            with dpg.plot(label=label, parent=self.parent, height=self.current_height, width=self.current_width) as plot_id:
                self.plot_tag = plot_id
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Timestep")
                with dpg.plot_axis(dpg.mvYAxis, label="Value") as y_axis:
                    for i in range(v_len):
                        tag = dpg.add_line_series(x_axis_data, self.vector_history_buffer[:, i].tolist(),
                                                label=f"Idx {i}", parent=y_axis)
                        self.vector_line_tags.append(tag)
        else:
            # Update existing lines
            for i, tag in enumerate(self.vector_line_tags):
                if dpg.does_item_exist(tag):
                    dpg.set_value(tag, [x_axis_data, self.vector_history_buffer[:, i].tolist()])
        return True

    def _create_or_update_line_plot(self, x_data, y_data, label, series_factory=dpg.add_line_series):
        """Create a new DPG line-style plot or update the existing series."""
        if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
            with dpg.plot(label=label, parent=self.parent,
                        height=self.current_height, width=self.current_width,
                        equal_aspects=False) as plot_id:
                dpg.add_plot_legend()
                dpg.add_plot_axis(dpg.mvXAxis, label="Index")
                y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Value")
                self.plot_tag = plot_id
                self.line_series_tag = series_factory(x_data, y_data,
                                                    label=label, parent=y_axis)
        else:
            if self.line_series_tag and dpg.does_item_exist(self.line_series_tag):
                dpg.set_value(self.line_series_tag, [x_data, y_data])
            else:
                children = dpg.get_item_children(self.plot_tag, slot=1)
                if len(children) >= 2:
                    self.line_series_tag = series_factory(x_data, y_data, label=label, parent=children[1])

    def _clear_previous(self):
        """Clear previous plot items."""
        try:
            if self.plot_tag and dpg.does_item_exist(self.plot_tag):
                dpg.delete_item(self.plot_tag)
                self.plot_tag = None
            self.line_series_tag = None
            self.heat_series_tag = None
        except Exception as e:
            log.error(f"[DPGPlotter._clear_previous] Error: {e}")

    def _cleanup_image_resources(self):
        """Clean up image viewer resources."""
        try:
            if self.image_viewer:
                if self.image_viewer.container_tag and dpg.does_item_exist(self.image_viewer.container_tag):
                    dpg.delete_item(self.image_viewer.container_tag)
            self.image_viewer = None
        except Exception as e:
            log.error(f"[DPGPlotter._cleanup_image_resources] Error: {e}")

    def clear(self):
        """Clear all plot elements."""
        try:
            self._clear_previous()
            self._cleanup_image_resources()
            self.history_data = []
            self.current_mode = None
        except Exception as e:
            log.error(f"[DPGPlotter.clear] Error: {e}")

    def update_size(self, width: int, height: int) -> None:
        """Update the plot/viewer size for responsive layout."""
        self.current_width  = max(width,  200)
        self.current_height = max(height, 150)

        if self.plot_tag and dpg.does_item_exist(self.plot_tag):
            try:
                dpg.configure_item(self.plot_tag,
                                   width=self.current_width,
                                   height=self.current_height)
            except Exception as e:
                log.error(f"[DPGPlotter.update_size] Error updating plot: {e}")

        if self.image_viewer is not None:
            self.image_viewer.update_size(self.current_width, self.current_height)

    def plot_2d_image_clean(self, data_2d, label="2D Image", colormap='seismic'):
        """
        Plot 2D data as an interactive image using ``InteractiveImageViewer``.

        The viewer uses a DPG plot + add_image_series which provides:
          * Native scroll-wheel zoom and drag-to-pan (no custom event handling needed)
          * equal_aspects=True for pixel-accurate aspect-ratio preservation
          * fit_axis_data() for an unlocked initial view (zoom/pan work immediately)
        """
        try:
            if data_2d is None:
                return False

            if self.current_mode != 'image':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode  = 'image'
                self.image_viewer = InteractiveImageViewer(
                    parent_tag=self.parent,
                    width=-1,
                    height=-1,
                    filtering=self.image_filtering,
                )

            if self.image_viewer is None:
                self.image_viewer = InteractiveImageViewer(
                    parent_tag=self.parent,
                    width=self.current_width,
                    height=self.current_height,
                    filtering=self.image_filtering,
                )

            self.current_shape = data_2d.shape
            return self.image_viewer.update_image(data_2d, colormap)

        except Exception as e:
            log.error(f"[DPGPlotter.plot_2d_image_clean] Error: {e}")
            traceback.print_exc()
            return False

    def plot_line(self, data: np.ndarray, label: str = "Line") -> bool:
        """Plot a 1D array as a line series (alias for plot_vector)."""
        return self.plot_vector(data, label)

    def plot_history(self, value: float, label: str = "History") -> bool:
        """Append a scalar to a rolling history and plot it as a line series."""
        try:
            if self.current_mode != "history":
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = "history"
            self.history_data.append(float(value))
            if len(self.history_data) > self.max_history:
                self.history_data.pop(0)
            self._create_or_update_line_plot(
                list(range(len(self.history_data))),
                self.history_data.copy(),
                label
            )
            return True
        except Exception as e:
            log.error(f"[DPGPlotter.plot_history] Error: {e}")
            traceback.print_exc()
            return False

    def plot_scatter(self, data: np.ndarray, label: str = "Scatter") -> bool:
        """Plot a 1D array as a scatter series."""
        try:
            data = self._ensure_1d(data)
            if data is None:
                return False
            if self.current_mode != "scatter":
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = "scatter"
            self._create_or_update_line_plot(
                list(range(len(data))),
                data.tolist(),
                label,
                series_factory=dpg.add_scatter_series,
            )
            return True
        except Exception as e:
            log.error(f"[DPGPlotter.plot_scatter] Error: {e}")
            traceback.print_exc()
            return False
