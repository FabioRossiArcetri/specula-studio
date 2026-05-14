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

Design
------
We hook FigureCanvasAgg.draw at the class level.  Every time specula (or
any library code) renders pixels into a figure, our hook fires on the
simulation thread, captures raw RGBA bytes from the Agg buffer, and stores
them in a per-figure "latest render" dict (_pending).

The main DPG render loop calls tick() once per frame.  tick() snapshots
_pending and calls _dpg_show_figure() for each figure that has a pending
update.  Because we keep only the latest render per figure, the dict never
builds up regardless of how fast the simulation draws.

Generation counter
------------------
Each close_all() call increments _close_generation.  Every captured render
is tagged with the generation at capture time.  tick() silently discards
any render whose generation is older than _close_generation, preventing
stale close_all commands (queued during a previous simulation run) from
wiping out windows that belong to a new run.
"""
from __future__ import annotations

import threading
import time
import queue
from typing import Dict

import numpy as np
import dearpygui.dearpygui as dpg

_TEX_REGISTRY_TAG = "mpl_dpg_bridge_tex_registry"


class MatplotlibDPGBridge:

    _lock = threading.Lock()
    _installed = False

    # Originals saved for uninstall
    _orig_canvas_draw      = None
    _orig_canvas_draw_idle = None
    _orig_show             = None
    _orig_draw             = None
    _orig_pause            = None
    _orig_ion              = None

    # Generation counter — incremented by every close_all() call.
    # Renders captured before the latest close_all are silently dropped.
    _close_generation: int = 0

    # Latest pending render per figure:
    #   fig_num → (title, w, h, flat_float32_ndarray, generation)
    # Written from simulation thread, consumed+replaced from main thread.
    # CPython dict writes are GIL-atomic — no explicit lock needed.
    _pending: Dict[int, tuple] = {}

    # Control commands (close_all only)
    _ctrl_queue: queue.Queue = queue.Queue()

    # DPG window/texture bookkeeping: fig_num → dict
    _figure_windows: Dict[int, dict] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    @classmethod
    def install(cls) -> None:
        """
        Switch matplotlib to Agg and hook the canvas draw path.

        Hooks installed:
          FigureCanvasAgg.draw / draw_idle — captures pixels on every render
          plt.show  — forces canvas.draw() on all open figures
          plt.draw  — same
          plt.pause — same (does NOT sleep; DPG loop provides pacing)
          plt.ion   — no-op (our bridge is the interactive display)

        Safe to call multiple times — subsequent calls are no-ops.
        """
        with cls._lock:
            if cls._installed:
                return
            try:
                import matplotlib
                matplotlib.use('Agg', force=True)

                from matplotlib.backends.backend_agg import FigureCanvasAgg
                import matplotlib.pyplot as plt

                cls._orig_canvas_draw      = FigureCanvasAgg.draw
                cls._orig_canvas_draw_idle = FigureCanvasAgg.draw_idle
                cls._orig_show             = plt.show
                cls._orig_draw             = plt.draw
                cls._orig_pause            = plt.pause
                cls._orig_ion              = plt.ion

                bridge     = cls
                _orig_draw = cls._orig_canvas_draw

                def _patched_canvas_draw(canvas_self):
                    # ── 1. Perform the actual Agg render ──────────────────────
                    _orig_draw(canvas_self)
                    # ── 2. Capture raw RGBA pixels tagged with current gen ────
                    try:
                        fig     = canvas_self.figure
                        fig_num = fig.number
                        w, h    = canvas_self.get_width_height()

                        # buffer_rgba() is a memoryview into Agg's pixel buffer.
                        # We copy immediately (ascontiguousarray) before the
                        # next draw call can overwrite the backing buffer.
                        buf  = canvas_self.buffer_rgba()
                        flat = np.frombuffer(buf, dtype=np.uint8) \
                                 .reshape(h, w, 4) \
                                 .astype(np.float32)
                        flat /= 255.0
                        flat  = np.ascontiguousarray(flat.ravel())

                        try:
                            title = canvas_self.manager.get_window_title()
                        except Exception:
                            title = f"Figure {fig_num}"

                        # Tag with the current generation so tick() can detect
                        # renders that predate the most recent close_all().
                        gen = bridge._close_generation
                        bridge._pending[fig_num] = (title, w, h, flat, gen)
                    except Exception:
                        pass   # never crash the simulation loop

                def _patched_canvas_draw_idle(canvas_self):
                    _patched_canvas_draw(canvas_self)

                # plt-level hooks: force canvas.draw() which triggers our hook
                def _patched_show(*args, **kwargs):
                    try:
                        for fn in plt.get_fignums():
                            plt.figure(fn).canvas.draw()
                    except Exception:
                        pass

                def _patched_draw(*args, **kwargs):
                    _patched_show()

                def _patched_pause(interval):
                    # Do NOT sleep — DPG loop provides the frame pacing.
                    _patched_show()

                def _patched_ion():
                    pass  # no-op: our bridge IS the interactive display

                FigureCanvasAgg.draw      = _patched_canvas_draw
                FigureCanvasAgg.draw_idle = _patched_canvas_draw_idle
                plt.show  = _patched_show
                plt.draw  = _patched_draw
                plt.pause = _patched_pause
                plt.ion   = _patched_ion

                cls._installed = True
                print("[MPL-DPG] Installed: FigureCanvasAgg.draw hooked.")
            except Exception as exc:
                print(f"[MPL-DPG] install() failed: {exc}")

    @classmethod
    def uninstall(cls) -> None:
        """Restore all patched functions. Call on application exit."""
        with cls._lock:
            if not cls._installed:
                return
            try:
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                import matplotlib.pyplot as plt
                if cls._orig_canvas_draw:
                    FigureCanvasAgg.draw      = cls._orig_canvas_draw
                if cls._orig_canvas_draw_idle:
                    FigureCanvasAgg.draw_idle = cls._orig_canvas_draw_idle
                if cls._orig_show:  plt.show  = cls._orig_show
                if cls._orig_draw:  plt.draw  = cls._orig_draw
                if cls._orig_pause: plt.pause = cls._orig_pause
                if cls._orig_ion:   plt.ion   = cls._orig_ion
                cls._orig_canvas_draw = cls._orig_canvas_draw_idle = None
                cls._orig_show = cls._orig_draw = cls._orig_pause = cls._orig_ion = None
                cls._installed = False
                print("[MPL-DPG] Uninstalled.")
            except Exception as exc:
                print(f"[MPL-DPG] uninstall() error: {exc}")

    @classmethod
    def close_all(cls) -> None:
        """
        Close all DPG figure windows and free in-memory figures.
        Called from InProcessBackend._cleanup_matplotlib() and abort().
        """
        # Bump generation BEFORE clearing _pending so that any render the
        # simulation thread stores between now and the next tick() will
        # carry the new generation and will NOT be discarded.
        cls._close_generation += 1
        cls._pending.clear()
        cls._ctrl_queue.put("close_all")
        try:
            import matplotlib.pyplot as plt
            plt.close('all')
        except Exception:
            pass

    @classmethod
    def tick(cls) -> None:
        """
        Consume pending renders and control commands; update DPG.

        Must be called from the DPG main thread once per render frame.

        Order of operations
        -------------------
        1. Drain the control queue (close_all commands from previous runs).
        2. Consume pending renders, discarding any whose generation is older
           than _close_generation (i.e. captured before the last close_all).
        """
        # ── 1. Process control commands ───────────────────────────────────────
        while True:
            try:
                cmd = cls._ctrl_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "close_all":
                cls._dpg_destroy_all()

        # ── 2. Consume pending renders ────────────────────────────────────────
        if cls._pending:
            # Atomic swap: replace with empty dict, process the snapshot.
            pending, cls._pending = cls._pending, {}
            for fig_num, entry in pending.items():
                title, w, h, flat, gen = entry
                # Discard renders captured before the most recent close_all.
                if gen < cls._close_generation:
                    continue
                try:
                    cls._dpg_show_figure(fig_num, title, w, h, flat)
                except Exception as exc:
                    import traceback
                    print(f"[MPL-DPG] tick error (fig {fig_num}): {exc}")
                    traceback.print_exc()

    # ── Private helpers — main thread only ───────────────────────────────────

    @classmethod
    def _ensure_tex_registry(cls) -> None:
        if not dpg.does_item_exist(_TEX_REGISTRY_TAG):
            dpg.add_texture_registry(tag=_TEX_REGISTRY_TAG, show=False)

    @classmethod
    def _dpg_show_figure(
        cls, fig_num: int, title: str, w: int, h: int, flat: np.ndarray
    ) -> None:
        """Create or update the DPG window for figure *fig_num*."""
        cls._ensure_tex_registry()
        info         = cls._figure_windows.get(fig_num)
        window_alive = info is not None and dpg.does_item_exist(info["win_tag"])

        if not window_alive:
            ts      = int(time.time() * 1000)
            win_tag = f"mpl_fig_win_{fig_num}_{ts}"
            tex_tag = f"mpl_fig_tex_{fig_num}_{ts}"
            img_tag = f"mpl_fig_img_{fig_num}_{ts}"

            dpg.add_raw_texture(
                width=w, height=h,
                default_value=flat,
                tag=tex_tag,
                format=dpg.mvFormat_Float_rgba,
                parent=_TEX_REGISTRY_TAG,
            )
            with dpg.window(
                label=title, tag=win_tag,
                width=w + 24, height=h + 48,
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
            old_w, old_h = info["width"], info["height"]

            if w != old_w or h != old_h:
                if dpg.does_item_exist(info["img_tag"]):
                    dpg.delete_item(info["img_tag"])
                if dpg.does_item_exist(info["tex_tag"]):
                    dpg.delete_item(info["tex_tag"])

                ts      = int(time.time() * 1000)
                new_tex = f"mpl_fig_tex_{fig_num}_{ts}"
                new_img = f"mpl_fig_img_{fig_num}_{ts}"

                dpg.add_raw_texture(
                    width=w, height=h, default_value=flat,
                    tag=new_tex, format=dpg.mvFormat_Float_rgba,
                    parent=_TEX_REGISTRY_TAG,
                )
                dpg.add_image(new_tex, tag=new_img, width=w, height=h,
                              parent=info["win_tag"])
                info.update(tex_tag=new_tex, img_tag=new_img, width=w, height=h)

            else:
                # Same size — stream pixels into existing texture (fast path)
                dpg.set_value(info["tex_tag"], flat)

    @classmethod
    def _on_window_close(cls, fig_num: int) -> None:
        """DPG on_close callback — remove tracking and clean up texture."""
        info = cls._figure_windows.pop(fig_num, None)
        cls._pending.pop(fig_num, None)
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
        # Note: do NOT clear _pending here. Renders captured after close_all()
        # was called carry a newer generation and must be kept for display.
        print("[MPL-DPG] All DPG matplotlib windows destroyed.")