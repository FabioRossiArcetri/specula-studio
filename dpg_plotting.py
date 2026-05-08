# dpg_plotting.py - Unified and optimized DPGPlotter class with responsive layout
import numpy as np
import dearpygui.dearpygui as dpg
import time
from matplotlib import cm
import traceback
from constants import MAX_PLOT_HISTORY, DEFAULT_PLOT_WIDTH, DEFAULT_PLOT_HEIGHT

class InteractiveImageViewer:
    """Interactive image viewer with texture-based display."""
    
    def __init__(self, parent_tag, width=400, height=300):
        self.parent = parent_tag
        self.width = width
        self.height = height
        
        # Image data
        self.texture_tag = None
        self.image_tag = None
        self.registry_tag = None
        self.current_data = None
        self.container_tag = None
        self.info_text_tag = None
        
        # Interaction state
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.is_dragging = False
        self.last_mouse_x = 0
        self.last_mouse_y = 0
        
    def update_image(self, data_2d, colormap='seismic'):
        """Update the displayed image with new data."""
        try:
            if data_2d is None or data_2d.size == 0:
                return False
            
            # Normalize data to 0.0 - 1.0 range
            dmin, dmax = data_2d.min(), data_2d.max()
            if dmax > dmin:
                normalized = (data_2d - dmin) / (dmax - dmin)
            else:
                normalized = np.zeros_like(data_2d)
            
            # Apply colormap
            mapper = cm.get_cmap(colormap)
            rgba_data = mapper(normalized).astype(np.float32)
            pixel_data = rgba_data.flatten()
            
            height, width = data_2d.shape
            
            # Create or update texture registry
            if not self.registry_tag or not dpg.does_item_exist(self.registry_tag):
                self.registry_tag = dpg.add_texture_registry(show=False)
            
            # Create or update texture
            if self.texture_tag and dpg.does_item_exist(self.texture_tag):
                dpg.set_value(self.texture_tag, pixel_data)
            else:
                self.texture_tag = dpg.add_dynamic_texture(
                    width=width,
                    height=height,
                    default_value=pixel_data,
                    parent=self.registry_tag
                )
            
            # Create or update image display with group and info text
            if not self.image_tag or not dpg.does_item_exist(self.image_tag):
                # Create container if needed
                if not self.container_tag or not dpg.does_item_exist(self.container_tag):
                    self.container_tag = dpg.add_group(parent=self.parent, horizontal=False)
                
                # Add info text
                self.info_text_tag = dpg.add_text(
                    "Scroll to zoom | Drag to pan",
                    color=[150, 150, 150],
                    parent=self.container_tag
                )
                
                # Add image
                self.image_tag = dpg.add_image(
                    self.texture_tag,
                    parent=self.container_tag
                )
            
            self.current_data = data_2d.copy()
            self._update_info()
            return True
            
        except Exception as e:
            print(f"[InteractiveImageViewer.update_image] Error: {e}")
            traceback.print_exc()
            return False

    def _update_info(self):
        """Update info text display."""
        try:
            if self.info_text_tag and dpg.does_item_exist(self.info_text_tag):
                info = f"Zoom: {self.zoom:.2f}x | Pan: ({self.pan_x:.0f}, {self.pan_y:.0f})"
                dpg.set_value(self.info_text_tag, info)
        except Exception:
            pass

    def update_size(self, width, height):
        """Update viewer size."""
        self.width = max(width, 50)
        self.height = max(height, 50)

    def handle_mouse_move(self, mouse_x, mouse_y):
        """Handle mouse movement for panning."""
        try:
            if self.is_dragging:
                dx = mouse_x - self.last_mouse_x
                dy = mouse_y - self.last_mouse_y
                self.pan_x += dx
                self.pan_y += dy
                self._update_info()
            
            self.last_mouse_x = mouse_x
            self.last_mouse_y = mouse_y
        except Exception:
            pass
    
    def handle_mouse_scroll(self, scroll_delta):
        """Handle mouse wheel zoom."""
        try:
            zoom_factor = 1.1 if scroll_delta > 0 else 0.9
            self.zoom = max(0.1, min(10.0, self.zoom * zoom_factor))
            self._update_info()
        except Exception as e:
            print(f"[InteractiveImageViewer.handle_mouse_scroll] Error: {e}")
    
    def start_drag(self, mouse_x, mouse_y):
        """Start panning operation."""
        self.is_dragging = True
        self.last_mouse_x = mouse_x
        self.last_mouse_y = mouse_y
    
    def end_drag(self):
        """End panning operation."""
        self.is_dragging = False


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
        """Debug logging."""
        if self.debug:
            print(f"[DPGPlotter] {message}")

    def _clear_previous(self):
        """Clear previous plot items based on current mode."""
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
    
    def update_size(self, width, height):
        """Update the plot size for responsive layout."""
        self.current_width = max(width, 200)
        self.current_height = max(height, 150)
        
        if self.plot_tag and dpg.does_item_exist(self.plot_tag):
            try:
                dpg.configure_item(self.plot_tag, width=self.current_width, height=self.current_height)
            except Exception as e:
                print(f"[DPGPlotter.update_size] Error updating plot: {e}")
        
        # Update image viewer if it exists
        if self.image_viewer is not None:
            self.image_viewer.update_size(self.current_width, self.current_height)

    def plot_2d_heatmap(self, data_2d, label="2D Heatmap"):
        """Plot 2D data as a heatmap using DPG's heat series."""
        try:
            # Ensure data is 2D
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
            
            # Normalize data to 0-1 range
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
                            flat_data,
                            rows,
                            cols,
                            label=label,
                            parent=y_axis,
                            scale_min=0.0,
                            scale_max=1.0,
                            bounds_min=(0, 0),
                            bounds_max=(cols, rows)
                        )
                    
                    dpg.set_axis_limits(x_axis, 0, cols)
                    dpg.set_axis_limits(y_axis, 0, rows)
                
                return True
                
        except Exception as e:
            print(f"[DPGPlotter.plot_2d_heatmap] Error: {e}")
            traceback.print_exc()
            return False

    def plot_2d_image_clean(self, data_2d, label="2D Image", colormap='seismic'):
        """Plot 2D data as an interactive image display."""
        try:
            if data_2d is None:
                return False
            
            if self.current_mode != 'image':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'image'
                self.image_viewer = InteractiveImageViewer(
                    parent_tag=self.parent,
                    width=self.current_width,
                    height=self.current_height
                )
            
            if self.image_viewer is None:
                self.image_viewer = InteractiveImageViewer(
                    parent_tag=self.parent,
                    width=self.current_width,
                    height=self.current_height
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

    def plot_vector(self, vector: np.ndarray, label: str = "Vector") -> bool:
        """Plot a 1D vector as a line series."""
        try:
            vector = self._ensure_1d(vector)
            if vector is None:
                return False

            if not len(vector):
                print("[DPGPlotter] Warning: empty vector, substituting zero")
                vector = np.array([0.0])

            if self.current_mode != "vector":
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = "vector"

            self._create_or_update_line_plot(
                list(range(len(vector))), vector.tolist(), label
            )
            return True

        except Exception as e:
            print(f"[DPGPlotter.plot_vector] Error: {e}")
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