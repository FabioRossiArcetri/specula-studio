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
os.environ.setdefault("SPECULA_STUDIO_CACHE", tempfile.mkdtemp(prefix="specula_studio_cache_"))

import os
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
    step("issues", editor._show_issues_window)
    step("find_object", editor._show_find_object)
    step("find_object_again", editor._show_find_object)
    step("add_object_search", editor._show_add_object_search)
    step("focus_node", editor.focus_node, next(iter(nm.graph.nodes)))
    step("diff_with_saved", editor._show_diff_with_saved)
    import tempfile
    step("export_diagram", editor._export_diagram,
         os.path.join(tempfile.mkdtemp(), "graph.dot"))

    # ── HPC dialogs (no network: nothing is submitted) ────────────────────────
    from specula_studio import hpc
    step("hpc_window", editor._show_hpc_window)
    step("hpc_preset", editor._hpc_apply_preset, None, "Booster production (24 h)")
    dpg.set_value("hpc_account", "try25_test")
    step("hpc_script", editor._hpc_generate_script)
    if "#SBATCH --account=try25_test" not in dpg.get_value("hpc_script"):
        raise RuntimeError("HPC script not generated")
    editor._hpc_jobs.append(hpc.HpcJob(job_id="1", name="t", login_host="h",
                                       transfer_host="h", job_dir="/w/j", state="RUNNING"))
    editor._hpc_jobs.append(hpc.HpcJob(job_id="2", name="t", login_host="h",
                                       transfer_host="h", job_dir="/w/j", state="COMPLETED"))
    step("hpc_jobs_window", editor._show_hpc_jobs_window)
    editor._hpc_jobs_version += 1
    step("hpc_jobs_table", editor._poll_hpc_ui)
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
    from specula_studio.core import catalog
    root = catalog.specula_root()
    if root is not None:
        data, proc = catalog.build_catalog(root, runtime=False)
        step("catalog_swap", editor._apply_catalog, data, proc,
             catalog.CatalogInfo("installed", str(root)))
    step("new", editor._startup_create_new, None, None)

    # ── in-process run with a live monitor (needs SPECULA) ────────────────────
    import importlib.util
    if importlib.util.find_spec("specula") is not None:
        import tempfile
        import time

        import yaml

        sim = os.path.join(tempfile.mkdtemp(), "tiny.yml")
        with open(sim, "w") as f:
            yaml.safe_dump({
                "main": {"class": "SimulParams", "root_dir": ".", "pixel_pupil": 8,
                         "pixel_pitch": 0.1, "total_time": 0.2, "time_step": 0.001},
                "gen": {"class": "WaveGenerator", "constant": [1.0], "slope": [10.0]},
            }, f)
        editor._open_simulation(sim)
        dpg.set_value("sim_stepping", False)
        dpg.set_value("sim_cpu", True)
        editor.sim_control.start_sim()
        gen = editor.graph.find_by_name("gen")
        nm.monitors.open_monitor(None, None, (gen, "output"))
        t0 = time.time()
        while time.time() - t0 < 120:
            nm.monitors.tick()
            if not editor.sim_control.is_running:
                break
            time.sleep(0.01)
        for _ in range(5):
            nm.monitors.tick()
        monitors = list(nm.monitors._inprocess_monitors.values())
        if not monitors or monitors[0].update_count == 0:
            raise RuntimeError("monitor received no data: "
                               + "".join(editor.sim_control.terminal_data)[-2000:])
        steps.append("inprocess_run_monitor")
    dpg.destroy_context()
    print("@@STEPS@@" + ",".join(steps))


if __name__ == "__main__":
    main(sys.argv[1])
