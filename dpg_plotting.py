# dpg_plotting.py - Unified and optimized DPGPlotter class with responsive layout
import numpy as np
import dearpygui.dearpygui as dpg
import time
from matplotlib import cm
import traceback
from constants import MAX_PLOT_HISTORY, DEFAULT_PLOT_WIDTH, DEFAULT_PLOT_HEIGHT


class InteractiveImageViewer:
    """
    Interactive image viewer built on a DPG *plot* + ``add_image_series``.

    Zoom/pan/reset are handled entirely by the DPG plot widget:
      * Scroll wheel  → zoom in / out
      * Left-drag     → pan
      * Double-click  → reset to full image

    Aspect-ratio preservation
    -------------------------
    ``equal_aspects=True`` on the plot ensures that one data unit always maps
    to the same number of screen pixels in X and Y.  Because the image series
    spans [0, img_width] × [0, img_height] in data space, this guarantees the
    original pixel aspect ratio is kept regardless of how the window is resized
    (letterboxing / pillarboxing is added automatically by DPG).

    Resizing
    --------
    ``update_size(width, height)`` reconfigures the DPG plot widget to match
    the container dimensions on every frame in which a resize is detected.
    ``equal_aspects=True`` takes care of the content aspect ratio inside the
    (potentially non-square) plot area.

    The ``handle_mouse_*`` / ``start_drag`` / ``end_drag`` public methods are
    kept as no-ops for API compatibility with ``monitor_window.py`` callers.
    """

    # Reserve this many pixels for the one-line info bar above the plot
    _INFO_ROW_PX = 22

    def __init__(self, parent_tag, width: int = -1, height: int = -1):
        self.parent = parent_tag
        # -1 means "fill remaining space"; explicit px values are set by update_size()
        self.width  = width
        self.height = height

        # DPG item tags
        self.texture_registry_tag = None
        self.texture_tag          = None
        self.plot_tag             = None
        self.x_axis_tag           = None
        self.y_axis_tag           = None
        self.image_series_tag     = None
        self.container_tag        = None
        self.info_text_tag        = None

        # Current image dimensions
        self.img_width  = 0
        self.img_height = 0
        self.current_data = None

        # Legacy compat attributes — values kept for info display only;
        # actual zoom/pan is handled by the DPG plot widget.
        self.zoom         = 1.0
        self.pan_x        = 0.0
        self.pan_y        = 0.0
        self.is_dragging  = False
        self.last_mouse_x = 0
        self.last_mouse_y = 0

    # =========================================================================
    # Internal builders
    # =========================================================================

    def _build_plot(self, img_width: int, img_height: int) -> None:
        """Create the outer group, info bar, texture registry and plot widget."""
        self.container_tag = dpg.add_group(parent=self.parent, horizontal=False)

        # One-line info bar
        self.info_text_tag = dpg.add_text(
            f"{img_width}\u00d7{img_height} px  \u2502  "
            "Scroll to zoom  \u2502  Drag to pan  \u2502  Double-click to reset",
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
            with dpg.plot_axis(dpg.mvYAxis, label="") as y_axis:
                self.y_axis_tag = y_axis
                # image_series is added in _make_image_series() once the
                # texture tag exists

        self.img_width  = img_width
        self.img_height = img_height

    def _make_texture(self, pixel_data: list, img_width: int, img_height: int) -> None:
        """(Re)create the dynamic RGBA texture inside the registry."""
        if self.texture_tag and dpg.does_item_exist(self.texture_tag):
            dpg.delete_item(self.texture_tag)
            self.texture_tag = None

        self.texture_tag = dpg.add_dynamic_texture(
            width=img_width,
            height=img_height,
            default_value=pixel_data,
            parent=self.texture_registry_tag,
        )


    def _make_image_series(self) -> None:
        """(Re)create the image series and fit the axes to the image extents.

        Key design decision
        -------------------
        We use ``fit_axis_data()`` rather than ``set_axis_limits()`` for the
        initial view.

        * ``set_axis_limits(axis, lo, hi)`` **locks** the axis — the user can
          no longer zoom or pan on that axis.  This was the bug that prevented
          interaction in earlier versions.
        * ``fit_axis_data(axis)`` sets the *current* view range to cover all
          series data but leaves the axis fully interactive, so subsequent
          scroll-zoom and drag-pan work as expected.
        """
        if self.image_series_tag and dpg.does_item_exist(self.image_series_tag):
            dpg.delete_item(self.image_series_tag)
            self.image_series_tag = None

        # bounds_min/max place the image in data space so that the top-left
        # pixel is at (0, img_height) and the bottom-right at (img_width, 0).
        # This way DPG's default upward-increasing Y axis displays the image
        # right-side-up.
        # uv_min/max are texture coordinates [0, 1] that map to the actual
        # texture data. Keeping them at [0, 0] to [1, 1] ensures full coverage.
        self.image_series_tag = dpg.add_image_series(
            self.texture_tag,
            bounds_min=[0,              0             ],
            bounds_max=[self.img_width, self.img_height],
            uv_min=[0.0, 0.0],
            uv_max=[1.0, 1.0],
            parent=self.y_axis_tag,
        )

        # fit_axis_data() shows all data on first display WITHOUT locking the
        # axis limits, so zoom and pan remain fully functional afterwards.
        dpg.fit_axis_data(self.x_axis_tag)
        dpg.fit_axis_data(self.y_axis_tag)


    # =========================================================================
    # Public API
    # =========================================================================

    def update_image(self, data_2d: np.ndarray, colormap: str = "seismic") -> bool:
        """Update the viewer with a new 2-D float array."""
        try:
            if data_2d is None or data_2d.size == 0:
                return False

            img_height, img_width = data_2d.shape

            # Normalize to [0, 1]
            dmin = float(data_2d.min())
            dmax = float(data_2d.max())
            if dmax > dmin:
                normalized = (data_2d - dmin) / (dmax - dmin)
            else:
                normalized = np.zeros_like(data_2d, dtype=np.float32)

            # Apply colormap → flat RGBA float32 list
            mapper     = cm.get_cmap(colormap)
            rgba       = mapper(normalized).astype(np.float32)  # (H, W, 4)
            pixel_data = rgba.flatten().tolist()

            shape_changed = (
                img_width  != self.img_width or
                img_height != self.img_height
            )

            if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
                # First call: build the full widget tree
                self._build_plot(img_width, img_height)
                self._make_texture(pixel_data, img_width, img_height)
                self._make_image_series()

            elif shape_changed:
                # Image shape changed: recreate texture + series, re-fit axes
                self.img_width  = img_width
                self.img_height = img_height
                self._make_texture(pixel_data, img_width, img_height)
                self._make_image_series()

            else:
                # Same shape: stream new pixels into the existing texture
                dpg.set_value(self.texture_tag, pixel_data)

            self.current_data = data_2d
            self._update_info_text(dmin, dmax)
            return True

        except Exception as e:
            print(f"[InteractiveImageViewer.update_image] Error: {e}")
            traceback.print_exc()
            return False

    def _update_info_text(self, dmin: float | None = None, dmax: float | None = None) -> None:
        try:
            if self.info_text_tag and dpg.does_item_exist(self.info_text_tag):
                if dmin is not None:
                    txt = (
                        f"{self.img_width}\u00d7{self.img_height} px  \u2502  "
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

    # Legacy alias
    def _update_info(self) -> None:
        self._update_info_text()

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
                print(f"[InteractiveImageViewer.update_size] Error: {e}")

    # ─��� Legacy compat no-ops ──────────────────────────────────────────────────
    # The DPG plot widget handles all mouse interaction natively.
    # These methods are preserved so monitor_window.py compiles unchanged.

    def handle_mouse_move(self, mouse_x: float, mouse_y: float) -> None:
        self.last_mouse_x = mouse_x
        self.last_mouse_y = mouse_y

    def handle_mouse_scroll(self, scroll_delta: float) -> None:
        pass  # handled by DPG plot widget

    def start_drag(self, mouse_x: float, mouse_y: float) -> None:
        self.last_mouse_x = mouse_x
        self.last_mouse_y = mouse_y

    def end_drag(self) -> None:
        pass  # handled by DPG plot widget


class DPGPlotter:
    """Unified plotting class with multiple visualization modes and responsive layout."""

    def __init__(self, parent_tag=None, width=DEFAULT_PLOT_WIDTH, height=DEFAULT_PLOT_HEIGHT, debug=True):
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
        self.vector_mode = "snapshot"     # "snapshot" or "time_series"
        self.vector_line_tags = []        # Track tags for multi-line mode

        # Debug info
        self.debug = debug

        if self.debug:
            print(f"[DPGPlotter] Initialized with parent: {parent_tag}, size: {width}x{height}")

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
        """Sets mode to 'snapshot' or 'time_series'."""
        if mode != self.vector_mode:
            self.vector_mode = mode
            self.clear() # Clear to re-setup axes for the new coordinate system

    def plot_vector(self, vector: np.ndarray, label: str = "Vector") -> bool:
        try:
            vector = self._ensure_1d(vector)
            if vector is None or vector.size == 0: return False

            if self.vector_mode == "time_series":
                return self._plot_vector_time_series(vector, label)
            
            # Default: Snapshot mode
            if self.current_mode != "vector":
                self._clear_previous()
                self.current_mode = "vector"
            
            self._create_or_update_line_plot(list(range(len(vector))), vector.tolist(), label)
            return True
        except Exception as e:
            print(f"[DPGPlotter.plot_vector] Error: {e}")
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
                x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Index")
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

    def _debug(self, message):
        if self.debug:
            print(f"[DPGPlotter] {message}")

    def _clear_previous(self):
        """Clear previous plot items."""
        try:
            if self.plot_tag and dpg.does_item_exist(self.plot_tag):
                dpg.delete_item(self.plot_tag)
                self.plot_tag = None
            self.line_series_tag = None
            self.heat_series_tag = None
        except Exception as e:
            print(f"[DPGPlotter._clear_previous] Error: {e}")

    def _cleanup_image_resources(self):
        """Clean up image viewer resources."""
        try:
            if self.image_viewer:
                if self.image_viewer.container_tag and dpg.does_item_exist(self.image_viewer.container_tag):
                    dpg.delete_item(self.image_viewer.container_tag)
            self.image_viewer = None
        except Exception as e:
            print(f"[DPGPlotter._cleanup_image_resources] Error: {e}")

    def clear(self):
        """Clear all plot elements."""
        try:
            self._clear_previous()
            self._cleanup_image_resources()
            self.history_data = []
            self.current_mode = None
        except Exception as e:
            print(f"[DPGPlotter.clear] Error: {e}")

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
                print(f"[DPGPlotter.update_size] Error updating plot: {e}")

        if self.image_viewer is not None:
            self.image_viewer.update_size(self.current_width, self.current_height)

    def plot_2d_heatmap(self, data_2d, label="2D Heatmap"):
        """Plot 2D data as a heatmap using DPG's heat series."""
        try:
            if data_2d.ndim != 2:
                if data_2d.ndim == 3:
                    if data_2d.shape[2] == 1:
                        data_2d = data_2d[:, :, 0]
                    else:
                        data_2d = np.mean(data_2d[:, :, :3], axis=2)
                else:
                    print(f"[DPGPlotter] Cannot convert shape {data_2d.shape} to 2D")
                    return False

            rows, cols = data_2d.shape

            if self.current_mode != 'heatmap':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'heatmap'

            data_min = data_2d.min()
            data_max = data_2d.max()
            if data_max > data_min:
                normalized_data = (data_2d - data_min) / (data_max - data_min)
            else:
                normalized_data = np.zeros_like(data_2d)

            flat_data = normalized_data.flatten().tolist()

            can_update = (
                self.current_mode == 'heatmap' and
                self.heat_series_tag is not None and
                dpg.does_item_exist(self.heat_series_tag) and
                self.current_shape == (rows, cols)
            )

            if can_update:
                self._debug(f"Updating existing heatmap for {cols}x{rows}")
                dpg.set_value(self.heat_series_tag, flat_data)
                return True
            else:
                self._debug(f"Creating new heatmap for {cols}x{rows}")
                self._clear_previous()
                self.current_shape = (rows, cols)

                with dpg.plot(label=label, parent=self.parent,
                             height=self.current_height, width=self.current_width) as plot_id:
                    self.plot_tag = plot_id
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="X", no_gridlines=True)
                    with dpg.plot_axis(dpg.mvYAxis, label="Y", no_gridlines=True) as y_axis:
                        self.heat_series_tag = dpg.add_heat_series(
                            flat_data, rows, cols, label=label, parent=y_axis,
                            scale_min=0.0, scale_max=1.0,
                            bounds_min=(0, 0), bounds_max=(cols, rows)
                        )
                    dpg.set_axis_limits(x_axis, 0, cols)
                    dpg.set_axis_limits(y_axis, 0, rows)

                return True

        except Exception as e:
            print(f"[DPGPlotter.plot_2d_heatmap] Error: {e}")
            traceback.print_exc()
            return False

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
                )

            if self.image_viewer is None:
                self.image_viewer = InteractiveImageViewer(
                    parent_tag=self.parent,
                    width=self.current_width,
                    height=self.current_height,
                )

            self.current_shape = data_2d.shape
            return self.image_viewer.update_image(data_2d, colormap)

        except Exception as e:
            print(f"[DPGPlotter.plot_2d_image_clean] Error: {e}")
            traceback.print_exc()
            return False

    def update_existing_plot(self, data_array):
        """Update existing plot without recreating everything."""
        try:
            if data_array.ndim == 0 or (data_array.ndim == 1 and data_array.size == 1):
                scalar_value = float(data_array.item() if data_array.ndim == 0 else data_array[0])
                return self.plot_history(scalar_value)
            elif data_array.ndim == 1:
                success = self.plot_vector(data_array)
                if not success:
                    success = self.plot_line(data_array)
                return success
            elif data_array.ndim == 2:
                return self.plot_2d_image_clean(data_array)
            elif data_array.ndim == 3:
                if data_array.shape[2] == 1:
                    data_2d = data_array[:, :, 0]
                else:
                    data_2d = np.mean(data_array[:, :, :3], axis=2)
                return self.plot_2d_image_clean(data_2d)
            else:
                print(f"[DPGPlotter.update_existing_plot] Unsupported shape: {data_array.shape}")
                return False
        except Exception as e:
            print(f"[DPGPlotter.update_existing_plot] Error: {e}")
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
            print(f"[DPGPlotter.plot_history] Error: {e}")
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
            print(f"[DPGPlotter.plot_scatter] Error: {e}")
            traceback.print_exc()
            return False