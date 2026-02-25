# dpg_plotting.py - Unified and optimized DPGPlotter class
import numpy as np
import dearpygui.dearpygui as dpg
import time
from matplotlib import cm

class DPGPlotter:
    """Unified plotting class with multiple visualization modes and reduced flickering."""
    
    def __init__(self, parent_tag=None, width=780, height=400):
        self.parent = parent_tag
        self.width = width
        self.height = height
        
        # Plot elements (for line plots and heatmaps)
        self.plot_tag = None
        self.line_series_tag = None
        self.heat_series_tag = None
        
        # Image elements (for texture-based 2D displays)
        self.image_texture_tag = None
        self.image_display_tag = None
        self.texture_registry_tag = None
        
        # Data tracking
        self.history_data = []
        self.max_history = 200
        self.current_mode = None  # 'history', 'vector', 'heatmap', 'texture'
        self.current_shape = None
        self.current_image_dtype = None
        
        # Debug info
        self.debug = True
        
        if self.debug:
            print(f"[DPGPlotter] Initialized with parent: {parent_tag}, size: {width}x{height}")

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
            
            # Clear specific series tags
            self.line_series_tag = None
            self.heat_series_tag = None
            
            # Don't clear texture/image by default - they can be reused
            # Only clear them in specific cleanup methods
            
        except Exception as e:
            print(f"[DPGPlotter._clear_previous] Error: {e}")

    def _cleanup_image_resources(self):
        """Clean up texture-based image resources."""
        try:
            # Only delete texture if we're creating a new one
            if self.image_texture_tag and dpg.does_item_exist(self.image_texture_tag):
                dpg.delete_item(self.image_texture_tag)
                self.image_texture_tag = None
            
            # Keep image display for reuse with new texture
            # Only delete if we're completely changing modes
            if self.image_display_tag and dpg.does_item_exist(self.image_display_tag):
                if self.current_mode != 'texture':
                    dpg.delete_item(self.image_display_tag)
                    self.image_display_tag = None
            
            # Keep texture registry for reuse
            # Don't delete texture_registry_tag - we want to reuse it
            
        except Exception as e:
            print(f"[DPGPlotter._cleanup_image_resources] Error: {e}")

    def clear(self):
        """Clear all plot elements."""
        try:
            self._clear_previous()
            self._cleanup_image_resources()
            
            # Also delete texture registry if it exists
            if self.texture_registry_tag and dpg.does_item_exist(self.texture_registry_tag):
                dpg.delete_item(self.texture_registry_tag)
                self.texture_registry_tag = None
            
            # Clear history data
            self.history_data = []
            self.current_mode = None
            
        except Exception as e:
            print(f"[DPGPlotter.clear] Error: {e}")

    def plot_line(self, data, label="Line"):
        """Plot 1D data as a line plot (alternative to plot_vector)."""
        try:
            # If we're not already in vector mode, clear and switch
            if self.current_mode != 'vector':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'vector'
            
            # Ensure data is 1D
            if data.ndim != 1:
                if data.ndim == 2 and data.shape[0] == 1:
                    data = data[0]
                elif data.ndim == 2 and data.shape[1] == 1:
                    data = data[:, 0]
                else:
                    print(f"[DPGPlotter] Cannot convert shape {data.shape} to 1D")
                    return False
            
            x_data = list(range(len(data)))
            y_data = data.tolist()
            
            # Create or update plot
            if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
                # Create new plot
                with dpg.plot(label=label, parent=self.parent, 
                            height=self.height, width=self.width) as plot_id:
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Index")
                    y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Value")
                    self.plot_tag = plot_id
                    
                    # Create line series
                    self.line_series_tag = dpg.add_line_series(
                        x_data, y_data, label=label, parent=y_axis
                    )
            else:
                # Update existing line series
                if self.line_series_tag and dpg.does_item_exist(self.line_series_tag):
                    dpg.set_value(self.line_series_tag, [x_data, y_data])
                else:
                    # Recreate line series if it doesn't exist
                    children = dpg.get_item_children(self.plot_tag, slot=1)
                    if len(children) >= 2:
                        y_axis = children[1]
                        self.line_series_tag = dpg.add_line_series(
                            x_data, y_data, label=label, parent=y_axis
                        )
            
            return True
            
        except Exception as e:
            print(f"[DPGPlotter.plot_line] Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
    def plot_history(self, value, label="History"):
        """Plot scalar values as a history line plot."""
        try:
            # If we're not already in history mode, clear and switch
            if self.current_mode != 'history':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'history'
            
            # Update history
            self.history_data.append(float(value))
            if len(self.history_data) > self.max_history:
                self.history_data.pop(0)
            
            x_data = list(range(len(self.history_data)))
            y_data = self.history_data.copy()
            
            # Create or update plot
            if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
                # Create new plot
                with dpg.plot(label=label, parent=self.parent, 
                             height=self.height, width=self.width) as plot_id:
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Frame")
                    y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Value")
                    self.plot_tag = plot_id
                    
                    # Create line series
                    self.line_series_tag = dpg.add_line_series(
                        x_data, y_data, label=label, parent=y_axis
                    )
            else:
                # Update existing line series
                if self.line_series_tag and dpg.does_item_exist(self.line_series_tag):
                    dpg.set_value(self.line_series_tag, [x_data, y_data])
                else:
                    # Recreate line series if it doesn't exist
                    children = dpg.get_item_children(self.plot_tag, slot=1)
                    if len(children) >= 2:
                        y_axis = children[1]
                        self.line_series_tag = dpg.add_line_series(
                            x_data, y_data, label=label, parent=y_axis
                        )
            
            return True
            
        except Exception as e:
            print(f"[DPGPlotter.plot_history] Error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def plot_vector(self, vector, label="Vector"):
        """Plot a 1D vector."""
        try:
            # Ensure data is 1D
            if vector.ndim != 1:
                print(f"[DPGPlotter] Vector has {vector.ndim} dimensions, trying to flatten...")
                if vector.ndim == 0:
                    # Scalar - convert to 1D array
                    vector = np.array([float(vector)])
                elif vector.ndim == 2:
                    if vector.shape[0] == 1:
                        vector = vector[0]
                    elif vector.shape[1] == 1:
                        vector = vector[:, 0]
                    else:
                        print(f"[DPGPlotter] Cannot convert 2D shape {vector.shape} to 1D")
                        # Try to flatten
                        try:
                            vector = vector.flatten()
                            print(f"[DPGPlotter] Flattened to shape {vector.shape}")
                        except:
                            print(f"[DPGPlotter] Failed to flatten")
                            return False
                else:
                    # Try to flatten
                    try:
                        vector = vector.flatten()
                        print(f"[DPGPlotter] Flattened {vector.ndim}D to 1D")
                    except:
                        print(f"[DPGPlotter] Cannot convert shape {vector.shape} to 1D")
                        return False
            
            print(f"[DPGPlotter] Plotting 1D vector with length {len(vector)}")
            
            # If we're not already in vector mode, clear and switch
            if self.current_mode != 'vector':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'vector'
            
            x_data = list(range(len(vector)))
            y_data = vector.tolist()
            
            # Debug: check data
            if len(y_data) == 0:
                print("[DPGPlotter] Warning: Empty vector data")
                y_data = [0.0]
                x_data = [0]
            
            # Create or update plot
            if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
                print(f"[DPGPlotter] Creating new plot for vector")
                # Create new plot
                with dpg.plot(label=label, parent=self.parent, 
                            height=self.height, width=self.width) as plot_id:
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Index")
                    y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Value")
                    self.plot_tag = plot_id
                    
                    # Create line series
                    self.line_series_tag = dpg.add_line_series(
                        x_data, y_data, label=label, parent=y_axis
                    )
                    print(f"[DPGPlotter] Created new plot with {len(y_data)} points")
            else:
                # Update existing line series
                if self.line_series_tag and dpg.does_item_exist(self.line_series_tag):
                    print(f"[DPGPlotter] Updating existing line series")
                    dpg.set_value(self.line_series_tag, [x_data, y_data])
                else:
                    # Recreate line series if it doesn't exist
                    print(f"[DPGPlotter] Recreating line series")
                    children = dpg.get_item_children(self.plot_tag, slot=1)
                    if len(children) >= 2:
                        y_axis = children[1]
                        self.line_series_tag = dpg.add_line_series(
                            x_data, y_data, label=label, parent=y_axis
                        )
            
            return True
            
        except Exception as e:
            print(f"[DPGPlotter.plot_vector] Error: {e}")
            import traceback
            traceback.print_exc()
            return False


    def plot_2d_heatmap(self, data_2d, label="2D Heatmap"):
        """Plot 2D data as a heatmap using DPG's heat series."""
        try:
            # Ensure data is 2D
            if data_2d.ndim != 2:
                if data_2d.ndim == 3:
                    if data_2d.shape[2] == 1:
                        data_2d = data_2d[:, :, 0]
                    else:
                        # Convert to grayscale
                        data_2d = np.mean(data_2d[:, :, :3], axis=2)
                else:
                    print(f"[DPGPlotter] Cannot convert shape {data_2d.shape} to 2D")
                    return False
            
            rows, cols = data_2d.shape
            
            # If we're not already in heatmap mode, clear and switch
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
            
            # Flatten the data for mvHeatSeries
            flat_data = normalized_data.flatten().tolist()
            
            # Check if we can update existing heatmap
            can_update = (
                self.current_mode == 'heatmap' and
                self.heat_series_tag is not None and
                dpg.does_item_exist(self.heat_series_tag) and
                self.current_shape == (rows, cols)
            )
            
            if can_update:
                # Update existing heatmap (reduced flickering)
                self._debug(f"Updating existing heatmap for {cols}x{rows}")
                dpg.set_value(self.heat_series_tag, flat_data)
                return True
            else:
                # Create new heatmap
                self._debug(f"Creating new heatmap for {cols}x{rows}")
                self._clear_previous()
                self.current_shape = (rows, cols)
                
                with dpg.plot(label=label, parent=self.parent,
                             height=self.height, width=self.width) as plot_id:
                    self.plot_tag = plot_id
                    
                    # Add legend and axes
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="X", no_gridlines=True)
                    with dpg.plot_axis(dpg.mvYAxis, label="Y", no_gridlines=True) as y_axis:
                        # Add heat series with correct bounds
                        self.heat_series_tag = dpg.add_heat_series(
                            flat_data,
                            rows,  # height
                            cols,  # width
                            label=label,
                            parent=y_axis,
                            scale_min=0.0,
                            scale_max=1.0,
                            bounds_min=(0, 0),
                            bounds_max=(cols, rows)  # (x_max, y_max) = (cols, rows)
                        )
                    
                    # Set axis limits
                    dpg.set_axis_limits(x_axis, 0, cols)
                    dpg.set_axis_limits(y_axis, 0, rows)
                
                return True
                
        except Exception as e:
            print(f"[DPGPlotter.plot_2d_heatmap] Error: {e}")
            import traceback
            traceback.print_exc()
            return False
        
    def plot_2d_image_clean(self, data_2d, label="2D Image", colormap='seismic'):
        try:
            if data_2d is None: return False
            
            # 1. Normalize data to 0.0 - 1.0 range
            dmin, dmax = data_2d.min(), data_2d.max()
            height, width = data_2d.shape
            if dmax > dmin:
                normalized = (data_2d - dmin) / (dmax - dmin)
            else:
                normalized = np.zeros_like(data_2d)

            # 2. Apply Matplotlib Colormap
            # This converts a 2D array of (H, W) into (H, W, 4) RGBA float32
            mapper = cm.get_cmap(colormap)
            rgba_data = mapper(normalized).astype(np.float32)
            
            # 3. Flatten for DPG
            pixel_data = rgba_data.flatten()


            # Check if we can simple update existing texture
            if (self.current_mode == 'texture' and 
                self.image_texture_tag and dpg.does_item_exist(self.image_texture_tag) and 
                self.current_shape == (height, width)):
                
                dpg.set_value(self.image_texture_tag, pixel_data)
                return True

            # RECREATE RESOURCES
            self._cleanup_image_resources() # Ensure this deletes old texture/image items
            self.current_shape = (height, width)
            self.current_mode = 'texture'
            
            # Ensure registry exists
            if not self.texture_registry_tag or not dpg.does_item_exist(self.texture_registry_tag):
                self.texture_registry_tag = dpg.add_texture_registry(show=False)

            # USE DYNAMIC TEXTURE (Better for video/updates)
            self.image_texture_tag = dpg.add_dynamic_texture(
                width=width, height=height, default_value=pixel_data,
                parent=self.texture_registry_tag
            )

            # Create Display Image
            display_width = min(width, self.width)
            display_height = int(display_width * (height / width))
            display_width *= 2
            display_height*= 2
            self.image_display_tag = dpg.add_image(
                self.image_texture_tag, parent=self.parent,
                width=display_width, height=display_height
            )
            return True

        except Exception as e:
            print(f"Plot 2D Error: {e}")
            return False

    def update_image_data(self, data_2d):
        """Redirects to main function to handle dynamic updates safely."""
        return self.plot_2d_image_clean(data_2d)
    

    def update_existing_plot(self, data_array):
        """Update existing plot without recreating everything (universal method)."""
        try:
            # Determine data type and update accordingly
            if data_array.ndim == 0 or (data_array.ndim == 1 and data_array.size == 1):
                # Scalar - update history
                scalar_value = float(data_array.item() if data_array.ndim == 0 else data_array[0])
                return self.plot_history(scalar_value)
                
            elif data_array.ndim == 1:
                # Vector - try both methods
                success = self.plot_vector(data_array)
                if not success:
                    # Fall back to line plot
                    success = self.plot_line(data_array)
                return success
                
            elif data_array.ndim == 2:
                # 2D data - use texture-based display (less flickering)
                return self.plot_2d_image_clean(data_array)
                
            elif data_array.ndim == 3:
                # 3D data - convert to 2D
                if data_array.shape[2] == 1:
                    data_2d = data_array[:, :, 0]
                else:
                    data_2d = np.mean(data_array[:, :, :3], axis=2)
                
                # Recursively process as 2D
                return self.plot_2d_image_clean(data_2d)
                
            else:
                print(f"[DPGPlotter.update_existing_plot] Unsupported shape: {data_array.shape}")
                return False
                
        except Exception as e:
            print(f"[DPGPlotter.update_existing_plot] Error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def plot_scatter(self, data, label="Scatter"):
        """Plot 1D data as a scatter plot (simple alternative)."""
        try:
            # Ensure data is 1D
            if data.ndim != 1:
                if data.ndim == 2 and data.shape[0] == 1:
                    data = data[0]
                elif data.ndim == 2 and data.shape[1] == 1:
                    data = data[:, 0]
                else:
                    print(f"[DPGPlotter] Cannot convert shape {data.shape} to 1D for scatter")
                    return False
            
            x_data = list(range(len(data)))
            y_data = data.tolist()
            
            # If we're not already in scatter mode, clear and switch
            if self.current_mode != 'scatter':
                self._clear_previous()
                self._cleanup_image_resources()
                self.current_mode = 'scatter'
            
            # Create or update plot
            if self.plot_tag is None or not dpg.does_item_exist(self.plot_tag):
                # Create new plot
                with dpg.plot(label=label, parent=self.parent, 
                            height=self.height, width=self.width) as plot_id:
                    dpg.add_plot_legend()
                    x_axis = dpg.add_plot_axis(dpg.mvXAxis, label="Index")
                    y_axis = dpg.add_plot_axis(dpg.mvYAxis, label="Value")
                    self.plot_tag = plot_id
                    
                    # Create scatter series
                    self.line_series_tag = dpg.add_scatter_series(
                        x_data, y_data, label=label, parent=y_axis
                    )
            else:
                # Update existing scatter series
                if self.line_series_tag and dpg.does_item_exist(self.line_series_tag):
                    dpg.set_value(self.line_series_tag, [x_data, y_data])
                else:
                    # Recreate scatter series if it doesn't exist
                    children = dpg.get_item_children(self.plot_tag, slot=1)
                    if len(children) >= 2:
                        y_axis = children[1]
                        self.line_series_tag = dpg.add_scatter_series(
                            x_data, y_data, label=label, parent=y_axis
                        )
            
            return True
            
        except Exception as e:
            print(f"[DPGPlotter.plot_scatter] Error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def update_2d_heatmap(self, data_2d):
        """Update existing heatmap plot."""
        try:
            if data_2d.ndim != 2:
                return False
            
            rows, cols = data_2d.shape
            
            # Check if we can update
            if (self.current_mode == 'heatmap' and 
                self.heat_series_tag and 
                dpg.does_item_exist(self.heat_series_tag) and
                self.current_shape == (rows, cols)):
                
                # Normalize data
                data_min = data_2d.min()
                data_max = data_2d.max()
                if data_max > data_min:
                    normalized_data = (data_2d - data_min) / (data_max - data_min)
                else:
                    normalized_data = np.zeros_like(data_2d)
                
                flat_data = normalized_data.flatten().tolist()
                dpg.set_value(self.heat_series_tag, flat_data)
                return True
            
            else:
                # Need to recreate
                return self.plot_2d_heatmap(data_2d)
                
        except Exception as e:
            print(f"[DPGPlotter.update_2d_heatmap] Error: {e}")
            return False