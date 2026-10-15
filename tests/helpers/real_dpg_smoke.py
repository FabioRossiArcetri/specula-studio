"""
Build the real editor UI with the real DearPyGui, headless.

Only the calls that need a GPU window are stubbed (viewport creation/showing,
frame synchronisation).  Everything else — every widget, theme, font, node,
link and callback signature — goes through the real DPG library, so wrong
keyword arguments or item types fail here.

Usage: python -m tests.helpers.real_dpg_smoke <simulation.yml>
"""

import os
import tempfile

# Never touch the developer's real settings, even when run by hand.
os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)

import sys

import dearpygui.dearpygui as dpg

for _name in ("create_viewport", "setup_dearpygui", "show_viewport",
              "set_primary_window", "split_frame", "set_viewport_resize_callback",
              "set_viewport_title"):
    setattr(dpg, _name, lambda *a, **k: None)
dpg.get_viewport_width = lambda *a, **k: 1600
dpg.get_viewport_height = lambda *a, **k: 900
dpg.get_viewport_client_width = lambda *a, **k: 1600
dpg.get_viewport_client_height = lambda *a, **k: 900

from specula_studio import logs  # noqa: E402

logs.setup_logging("WARNING")

from specula_studio.app import TEMPLATES_DIR, SpeculaEditor  # noqa: E402


def main(path):
    editor = SpeculaEditor(yaml_folder=str(TEMPLATES_DIR))
    steps = []

    def step(name, fn, *a):
        fn(*a)
        steps.append(name)

    step("open", editor._open_simulation, path)
    nm = editor.nm
    for node_uuid in list(nm.graph.nodes)[:50]:
        nm.update_property_panel(node_uuid, "property_panel")
    steps.append("property_panels")
    for link_id in list(nm.link_registry)[:20]:
        nm.update_connection_panel(link_id, "property_panel")
    steps.append("connection_panels")
    step("preferences", editor._show_preferences_dialog)
    step("log_window", editor._show_log_window)
    step("sim_control", editor.sim_control.show_control_window)
    step("yaml_window", editor.sim_control.show_yaml_window)
    step("add_multiple", editor._show_add_multiple_dialog)
    step("about", editor._show_about_dialog)
    step("create_node", editor._on_menu_create, None, None, "Integrator")
    step("rebuild", nm.rebuild_all_nodes_ui)
    step("theme_light", editor.on_theme_changed, None, "light")
    step("theme_dark", editor.on_theme_changed, None, "dark")
    for size in ("SMALL", "MEDIUM"):
        step(f"render_{size}", editor._on_render_size_changed, None, size)
    from specula_studio.core import clipboard
    uuids = list(nm.graph.nodes)[:3]
    step("duplicate", editor._paste, clipboard.copy_nodes(nm.graph, uuids))
    step("undo", editor.undo)
    step("redo", editor.redo)
    for key in (dpg.mvKey_Z, dpg.mvKey_Delete, dpg.mvKey_D, dpg.mvKey_H):
        editor._on_key_press(None, key)
    steps.append("keys")
    step("poll", editor._poll_history, True)
    step("new", editor._startup_create_new, None, None)
    dpg.destroy_context()
    print("@@STEPS@@" + ",".join(steps))


if __name__ == "__main__":
    main(sys.argv[1])
