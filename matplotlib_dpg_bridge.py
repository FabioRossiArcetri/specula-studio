"""
matplotlib_dpg_bridge.py
========================
Bridges matplotlib (Agg backend) figures into DearPyGui windows for
the In-Process simulation mode in Specula Studio.

Integration points (all already present in the repo):
  - simulation_backend.InProcessBackend._patch_matplotlib()
        calls MatplotlibDPGBridge.install()
  - simulation_backend.InProcessBackend._cleanup_matplotlib()
        calls MatplotlibDPGBridge.close_all()
  - simulation_backend.InProcessBackend.abort()
        calls MatplotlibDPGBridge.close_all()
  - main.SpeculaEditor.run() render loop
        calls MatplotlibDPGBridge.tick() once per frame
"""
from __future__ import annotations

import io
import queue
import threading
import time
from typing import Dict, Optional

import dearpygui.dearpygui as dpg

_TEX_REGISTRY_TAG = "mpl_dpg_bridge_tex_registry"


def _png_to_rgba_flat(png_bytes: bytes):
    """
    Decode *png_bytes* → (width, height, flat float32 RGBA list).

    Returns (None, None, None) on failure.
    Tries Pillow first (fast), falls back to matplotlib's own reader.
    """
    # ── Pillow ───────────────────────────────────────────────────────────────
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        w, h = img.size
        arr = np.asarray(img, dtype=np.float32) / 255.0
        return w, h, arr.flatten().tolist()
    except ImportError:
        pass
    except Exception as exc:
        print(f"[MPL-DPG] Pillow decode error: {exc}")

    # ── matplotlib fallback ───────────────────────────────────────────────────
    try:
        import numpy as np
        import matplotlib.image as mpimg
        arr = mpimg.imread(io.BytesIO(png_bytes))          # float32 RGB or RGBA
        if arr.ndim == 3 and arr.shape[2] == 3:
            ones = np.ones((*arr.shape[:2], 1), dtype=arr.dtype)
            arr = np.concatenate([arr, ones], axis=2)
        if arr.ndim != 3 or arr.shape[2] != 4:
            return None, None, None
        h, w = arr.shape[:2]
        return w, h, arr.astype(np.float32).flatten().tolist()
    except Exception as exc:
        print(f"[MPL-DPG] matplotlib PNG decode error: {exc}")

    return None, None, None


class MatplotlibDPGBridge:
    """
    Thread-safe singleton bridging matplotlib Agg figures → DPG windows.

    Design
    ------
    The simulation runs on a background thread. When specula (or any code
    it calls) invokes plt.show(), our patched version renders every open
    figure to a PNG byte-buffer and puts a ("show", ...) command on a
    thread-safe queue.

    The main DPG render loop calls tick() once per frame. tick() drains
    the queue and creates / updates DPG windows with the rendered images —
    all safely on the main thread.

    When the simulation is aborted or finishes, close_all() queues a
    ("close_all", ...) command so tick() tears down the DPG windows on
    the next frame.

    The Agg backend never creates OS windows, never calls sys.exit() or
    os._exit(), so closing figures is always safe.
    """

    _lock           = threading.Lock()
    _installed      = False
    _original_show  = None
    _cmd_queue: queue.Queue               = queue.Queue()
    # fig_num → {"win_tag", "tex_tag", "img_tag", "width", "height"}
    _figure_windows: Dict[int, dict]      = {}

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def install(cls) -> None:
        """
        Switch matplotlib to the Agg backend and patch plt.show().

        Safe to call multiple times — subsequent calls are no-ops.
        Must be called before the simulation thread creates any Figure.
        """
        with cls._lock:
            if cls._installed:
                return
            try:
                import matplotlib
                matplotlib.use('Agg', force=True)

                import matplotlib.pyplot as plt
                cls._original_show = plt.show
                bridge = cls

                def _patched_show(*args, **kwargs):
                    """Render all open figures to PNG and queue for DPG."""
                    try:
                        for fig_num in plt.get_fignums():
                            fig = plt.figure(fig_num)
                            buf = io.BytesIO()
                            fig.savefig(buf, format='png', dpi=96,
                                        bbox_inches='tight')
                            buf.seek(0)
                            png_bytes = buf.read()
                            try:
                                title = fig.canvas.manager.get_window_title()
                            except Exception:
                                title = f"Figure {fig_num}"
                            bridge._cmd_queue.put(
                                ("show", fig_num, title, png_bytes)
                            )
                    except Exception as exc:
                        print(f"[MPL-DPG] plt.show() patch error: {exc}")

                plt.show = _patched_show
                cls._installed = True
                print("[MPL-DPG] Installed: Agg backend + plt.show() → DPG bridge.")
            except Exception as exc:
                print(f"[MPL-DPG] install() failed: {exc}")

    @classmethod
    def uninstall(cls) -> None:
        """Restore the original plt.show(). Call on application exit."""
        with cls._lock:
            if not cls._installed:
                return
            try:
                import matplotlib.pyplot as plt
                if cls._original_show is not None:
                    plt.show = cls._original_show
                    cls._original_show = None
                cls._installed = False
                print("[MPL-DPG] Uninstalled.")
            except Exception as exc:
                print(f"[MPL-DPG] uninstall() error: {exc}")

    @classmethod
    def close_all(cls) -> None:
        """
        Queue a request to close all DPG figure windows.

        Also frees all in-memory matplotlib figures (safe with Agg — no
        OS windows are involved, this only releases memory).

        Called from InProcessBackend._cleanup_matplotlib() and abort().
        """
        cls._cmd_queue.put(("close_all", None, None, None))
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
        except Exception:
            pass

    @classmethod
    def tick(cls) -> None:
        """
        Drain the command queue and update DPG state.

        MUST be called from the DPG main thread, once per render frame,
        e.g. alongside MonitorManager._inprocess_tick_direct().

        Processes up to 20 commands per call to avoid frame stalls.
        """
        for _ in range(20):
            try:
                cmd, fig_num, title, data = cls._cmd_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if cmd == "show":
                    cls._dpg_show_figure(fig_num, title, data)
                elif cmd == "close_all":
                    cls._dpg_destroy_all()
            except Exception as exc:
                import traceback
                print(f"[MPL-DPG] tick error ({cmd}): {exc}")
                traceback.print_exc()

    # ── Private helpers — main thread only ───────────────────────────────────

    @classmethod
    def _ensure_tex_registry(cls) -> None:
        if not dpg.does_item_exist(_TEX_REGISTRY_TAG):
            dpg.add_texture_registry(tag=_TEX_REGISTRY_TAG, show=False)

    @classmethod
    def _dpg_show_figure(cls, fig_num: int, title: str, png_bytes: bytes) -> None:
        """Create or update a DPG window for the given matplotlib figure."""
        w, h, rgba_flat = _png_to_rgba_flat(png_bytes)
        if w is None:
            print(f"[MPL-DPG] Could not decode PNG for Figure {fig_num}.")
            return

        cls._ensure_tex_registry()
        info         = cls._figure_windows.get(fig_num)
        window_alive = info is not None and dpg.does_item_exist(info["win_tag"])

        if not window_alive:
            # ── Create new DPG window ─────────────────────────────────────────
            ts      = int(time.time() * 1000)
            win_tag = f"mpl_fig_win_{fig_num}_{ts}"
            tex_tag = f"mpl_fig_tex_{fig_num}_{ts}"
            img_tag = f"mpl_fig_img_{fig_num}_{ts}"

            dpg.add_raw_texture(
                width=w, height=h,
                default_value=rgba_flat,
                tag=tex_tag,
                format=dpg.mvFormat_Float_rgba,
                parent=_TEX_REGISTRY_TAG,
            )
            with dpg.window(
                label=title,
                tag=win_tag,
                width=w + 24,
                height=h + 48,
                on_close=lambda _s, _a, fn=fig_num: cls._on_window_close(fn),
                no_scrollbar=True,
            ):
                dpg.add_image(tex_tag, tag=img_tag, width=w, height=h)

            cls._figure_windows[fig_num] = dict(
                win_tag=win_tag, tex_tag=tex_tag, img_tag=img_tag,
                width=w, height=h,
            )
            print(f"[MPL-DPG] Created DPG window for Figure {fig_num}: '{title}'")

        else:
            # ── Update existing window ────────────────────────────────────────
            old_w, old_h = info["width"], info["height"]

            if w != old_w or h != old_h:
                # Size changed — recreate texture and image widget
                if dpg.does_item_exist(info["img_tag"]):
                    dpg.delete_item(info["img_tag"])
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])

                ts          = int(time.time() * 1000)
                new_tex_tag = f"mpl_fig_tex_{fig_num}_{ts}"
                new_img_tag = f"mpl_fig_img_{fig_num}_{ts}"

                dpg.add_raw_texture(
                    width=w, height=h,
                    default_value=rgba_flat,
                    tag=new_tex_tag,
                    format=dpg.mvFormat_Float_rgba,
                    parent=_TEX_REGISTRY_TAG,
                )
                dpg.add_image(
                    new_tex_tag, tag=new_img_tag,
                    width=w, height=h,
                    parent=info["win_tag"],
                )
                info.update(tex_tag=new_tex_tag, img_tag=new_img_tag,
                            width=w, height=h)
            else:
                # Same size — stream new pixels into the existing texture (fast)
                dpg.set_value(info["tex_tag"], rgba_flat)

    @classmethod
    def _on_window_close(cls, fig_num: int) -> None:
        """DPG on_close callback — clean up orphaned texture."""
        info = cls._figure_windows.pop(fig_num, None)
        if info:
            try:
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])
            except Exception:
                pass
        print(f"[MPL-DPG] DPG window for Figure {fig_num} closed by user.")

    @classmethod
    def _dpg_destroy_all(cls) -> None:
        """Destroy every open DPG figure window (main thread only)."""
        for fig_num, info in list(cls._figure_windows.items()):
            try:
                if dpg.does_item_exist(info["win_tag"]):
                    dpg.delete_item(info["win_tag"])
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])
            except Exception as exc:
                print(f"[MPL-DPG] Error destroying Figure {fig_num}: {exc}")
        cls._figure_windows.clear()
        print("[MPL-DPG] All DPG matplotlib windows destroyed.")