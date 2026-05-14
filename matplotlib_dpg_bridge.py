"""
matplotlib_dpg_bridge.py
========================
Bridges matplotlib (Agg backend) into DearPyGui for the In-Process
simulation mode in Specula Studio.

Problem solved
--------------
Using a native GUI matplotlib backend (TkAgg, Qt5Agg, …) inside a
background simulation thread is inherently unsafe:

  * Tk/Qt event loops are **not** thread-safe.
  * Closing a TkAgg figure (or calling plt.close/plt.show from a
    non-main thread) can call sys.exit() or destroy the root Tk window,
    terminating the whole application immediately.
  * os._exit() called deep inside Tk teardown cannot be intercepted by
    the sys.exit patch already present in the codebase.

Solution
--------
1. Force matplotlib to use the 'Agg' (off-screen) backend.  This backend
   never creates an OS window and never calls sys.exit() or os._exit().
2. Patch plt.show() so that every currently open Figure is rendered to a
   PNG byte buffer and put on a thread-safe queue.
3. A DearPyGui window is created/updated for each figure on the **main
   thread** by draining that queue inside ``MatplotlibDPGBridge.tick()``.
4. Aborting the simulation is now trivially safe: the background thread
   stops, matplotlib figures remain as in-memory Agg objects (no OS
   windows to worry about), and ``close_all()`` removes the DPG windows
   cleanly from the main thread.

Integration points
------------------
``InProcessBackend._patch_matplotlib()``
    Call ``MatplotlibDPGBridge.install()`` here instead of the old
    TkAgg / sys.exit-patching approach.

``InProcessBackend._cleanup_matplotlib()``
    Call ``MatplotlibDPGBridge.close_all()`` here.

Main DPG render loop (e.g. next to monitor_manager._inprocess_tick_direct())
    Call ``MatplotlibDPGBridge.tick()`` once per frame.

``InProcessBackend._restore_sys_exit()``
    No longer needed; sys.exit is never patched by this bridge.

Dependencies
------------
* DearPyGui  (always present in Specula Studio)
* Pillow     (preferred for PNG decoding — ``pip install Pillow``)
* numpy      (always present)

If Pillow is absent the bridge falls back to matplotlib's own PNG reader,
which is always available.
"""
from __future__ import annotations

import io
import queue
import threading
import time
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TEX_REGISTRY_TAG = "mpl_dpg_bridge_tex_registry"


def _png_to_dpg_rgba(png_bytes: bytes):
    """
    Decode *png_bytes* into a flat RGBA float32 list for dpg.add_raw_texture.

    Returns ``(width, height, flat_list)`` or ``(None, None, None)`` on error.

    Tries Pillow first (fastest), then matplotlib's own PNG reader.
    """
    # ── Pillow path ──────────────────────────────────────────────────────────
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

    # ── matplotlib fallback ──────────────────────────────────────────────────
    try:
        import numpy as np
        import matplotlib.image as mpimg
        arr = mpimg.imread(io.BytesIO(png_bytes))   # float32, RGB or RGBA
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


# ---------------------------------------------------------------------------
# Bridge singleton
# ---------------------------------------------------------------------------

class MatplotlibDPGBridge:
    """
    Thread-safe singleton that bridges matplotlib Agg figures into DPG windows.

    All public entry points are class-methods; no instance is needed.
    """

    _lock          = threading.Lock()
    _installed     = False
    _original_show = None
    _cmd_queue: queue.Queue = queue.Queue()

    # fig_num (int) → {win_tag, tex_tag, img_tag, width, height}
    _figure_windows: Dict[int, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def install(cls) -> None:
        """
        Switch matplotlib to the Agg backend and patch plt.show().

        Safe to call multiple times — subsequent calls are no-ops.
        Should be called before the simulation thread creates any Figure.
        """
        with cls._lock:
            if cls._installed:
                return
            try:
                import matplotlib
                # Force Agg — non-interactive, no OS windows, no sys.exit.
                matplotlib.use('Agg', force=True)

                import matplotlib.pyplot as plt
                cls._original_show = plt.show

                bridge = cls  # capture for the closure below

                def _patched_show(*args, **kwargs):
                    """Render open figures to PNG and queue them for DPG."""
                    try:
                        for fig_num in plt.get_fignums():
                            fig = plt.figure(fig_num)
                            buf = io.BytesIO()
                            fig.savefig(buf, format='png', dpi=96,
                                        bbox_inches='tight')
                            buf.seek(0)
                            png_bytes = buf.read()

                            # Best-effort window title
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
                print("[MPL-DPG] Installed: matplotlib Agg backend + "
                      "plt.show() → DPG bridge active.")
            except Exception as exc:
                print(f"[MPL-DPG] install() failed: {exc}")

    @classmethod
    def uninstall(cls) -> None:
        """
        Restore the original plt.show().

        Call this when the In-Process backend is fully torn down (i.e., after
        the last possible simulation run has finished).  Distinct from
        ``close_all()`` which only removes DPG windows.
        """
        with cls._lock:
            if not cls._installed:
                return
            try:
                import matplotlib.pyplot as plt
                if cls._original_show is not None:
                    plt.show = cls._original_show
                    cls._original_show = None
                cls._installed = False
                print("[MPL-DPG] Uninstalled: plt.show() restored.")
            except Exception as exc:
                print(f"[MPL-DPG] uninstall() error: {exc}")

    @classmethod
    def close_all(cls) -> None:
        """
        Queue a request to close all DPG figure windows.

        Also closes all matplotlib in-memory figures (safe with Agg — there
        are no OS windows; this only frees memory).

        Call from InProcessBackend._cleanup_matplotlib() after each run.
        """
        cls._cmd_queue.put(("close_all", None, None, None))
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
            print("[MPL-DPG] Closed all in-memory matplotlib figures.")
        except Exception:
            pass

    @classmethod
    def tick(cls) -> None:
        """
        Drain the command queue and update DPG state.

        **Must be called from the DPG main thread**, once per render frame,
        alongside other per-frame callbacks such as
        ``MonitorManager._inprocess_tick_direct()``.

        Processes up to 20 commands per call to avoid frame stalls.
        """
        try:
            import dearpygui.dearpygui as dpg
        except ImportError:
            return

        for _ in range(20):
            try:
                cmd, fig_num, title, data = cls._cmd_queue.get_nowait()
            except queue.Empty:
                break

            try:
                if cmd == "show":
                    cls._dpg_show_figure(dpg, fig_num, title, data)
                elif cmd == "close_all":
                    cls._dpg_destroy_all(dpg)
            except Exception as exc:
                import traceback
                print(f"[MPL-DPG] tick error ({cmd}): {exc}")
                traceback.print_exc()

    # ── Private helpers — main thread only ───────────────────────────────────

    @classmethod
    def _ensure_tex_registry(cls, dpg) -> None:
        """Create the shared texture registry if it does not exist yet."""
        if not dpg.does_item_exist(_TEX_REGISTRY_TAG):
            dpg.add_texture_registry(tag=_TEX_REGISTRY_TAG, show=False)

    @classmethod
    def _dpg_show_figure(
        cls,
        dpg,
        fig_num: int,
        title: str,
        png_bytes: bytes,
    ) -> None:
        """Create or update a DPG window for the given matplotlib figure."""
        w, h, rgba_flat = _png_to_dpg_rgba(png_bytes)
        if w is None:
            print(f"[MPL-DPG] Could not decode PNG for Figure {fig_num}.")
            return

        cls._ensure_tex_registry(dpg)
        info = cls._figure_windows.get(fig_num)
        window_exists = info is not None and dpg.does_item_exist(info["win_tag"])

        if not window_exists:
            # ── Create a new DPG window ───────────────────────────────────────
            ts      = int(time.time() * 1000)
            win_tag = f"mpl_fig_win_{fig_num}_{ts}"
            tex_tag = f"mpl_fig_tex_{fig_num}_{ts}"
            img_tag = f"mpl_fig_img_{fig_num}_{ts}"

            dpg.add_raw_texture(
                width=w,
                height=h,
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

            cls._figure_windows[fig_num] = {
                "win_tag": win_tag,
                "tex_tag": tex_tag,
                "img_tag": img_tag,
                "width":   w,
                "height":  h,
            }
            print(f"[MPL-DPG] Created DPG window for Figure {fig_num}: '{title}'")

        else:
            # ── Update existing window ────────────────────────────────────────
            old_w = info["width"]
            old_h = info["height"]

            if w != old_w or h != old_h:
                # Figure size changed — recreate texture and image widget
                if dpg.does_item_exist(info["img_tag"]):
                    dpg.delete_item(info["img_tag"])
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])

                ts          = int(time.time() * 1000)
                new_tex_tag = f"mpl_fig_tex_{fig_num}_{ts}"
                new_img_tag = f"mpl_fig_img_{fig_num}_{ts}"

                dpg.add_raw_texture(
                    width=w,
                    height=h,
                    default_value=rgba_flat,
                    tag=new_tex_tag,
                    format=dpg.mvFormat_Float_rgba,
                    parent=_TEX_REGISTRY_TAG,
                )
                dpg.add_image(
                    new_tex_tag,
                    tag=new_img_tag,
                    width=w,
                    height=h,
                    parent=info["win_tag"],
                )

                info.update(
                    tex_tag=new_tex_tag,
                    img_tag=new_img_tag,
                    width=w,
                    height=h,
                )
            else:
                # Same size — update texture data in-place (fast path)
                dpg.set_value(info["tex_tag"], rgba_flat)

    @classmethod
    def _on_window_close(cls, fig_num: int) -> None:
        """DPG on_close callback — clean up texture for this figure."""
        info = cls._figure_windows.pop(fig_num, None)
        if info:
            try:
                import dearpygui.dearpygui as dpg
                # The window itself is already being destroyed by DPG;
                # we only need to delete the orphaned texture.
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])
            except Exception:
                pass
        print(f"[MPL-DPG] DPG window for Figure {fig_num} closed by user.")

    @classmethod
    def _dpg_destroy_all(cls, dpg) -> None:
        """Destroy every open DPG figure window (main thread only)."""
        for fig_num, info in list(cls._figure_windows.items()):
            try:
                if dpg.does_item_exist(info["win_tag"]):
                    dpg.delete_item(info["win_tag"])
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])
            except Exception as exc:
                print(f"[MPL-DPG] Error destroying Figure {fig_num} window: {exc}")
        cls._figure_windows.clear()
        print("[MPL-DPG] All DPG matplotlib windows destroyed.")