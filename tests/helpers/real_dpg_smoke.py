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

from specula_studio.app import SpeculaEditor  # noqa: E402
from specula_studio.core.templates import TEMPLATES_DIR  # noqa: E402


def _check_monitor_plot_options():
    """Image filtering and line/histogram snapshots with the real DPG items."""
    import numpy as np

    from specula_studio.dpg_plotting import DPGPlotter

    with dpg.window(tag="smoke_plot_win"):
        dpg.add_group(tag="smoke_plot_grp")
    plotter = DPGPlotter(parent_tag="smoke_plot_grp")
    img = np.arange(12.0).reshape(3, 4)
    plotter.plot_2d_image_clean(img)
    viewer = plotter.image_viewer
    if viewer.mode != "pixels" or not dpg.does_item_exist(viewer.heat_series_tag):
        raise RuntimeError(f"unfiltered image should be a heat series: {viewer.mode}")
    values = dpg.get_value(viewer.heat_series_tag)[0]
    if values[:4] != img[-1].tolist():                  # row 0 drawn at the bottom
        raise RuntimeError(f"heat series rows not flipped: {values[:4]}")
    plotter.plot_2d_image_clean(img * 2)                # same series, streamed
    if dpg.get_item_configuration(viewer.heat_series_tag)["scale_max"] != 22.0:
        raise RuntimeError("heat series scale not updated")
    plotter.set_image_filtering(True)
    if viewer.mode != "texture" or dpg.does_item_exist(viewer.heat_series_tag or 0):
        raise RuntimeError(f"filtered image should be a texture: {viewer.mode}")
    plotter.plot_2d_image_clean(np.ones((3, 4)))        # same texture, streamed
    plotter.set_image_filtering(False)
    if viewer.mode != "pixels":
        raise RuntimeError("filtering off again should go back to pixels")

    plotter.plot_vector(np.arange(5.0))
    if dpg.get_item_type(plotter.line_series_tag) != "mvAppItemType::mvLineSeries":
        raise RuntimeError(dpg.get_item_type(plotter.line_series_tag))
    plotter.set_vector_style("histogram")
    plotter.plot_vector(np.arange(5.0))
    plotter.plot_vector(np.arange(5.0) * 2)
    if dpg.get_item_type(plotter.line_series_tag) != "mvAppItemType::mvBarSeries":
        raise RuntimeError(dpg.get_item_type(plotter.line_series_tag))
    if dpg.get_value(plotter.line_series_tag)[1][:5] != [0.0, 2.0, 4.0, 6.0, 8.0]:
        raise RuntimeError("histogram not updated")
    dpg.delete_item("smoke_plot_win")


def _check_hpc_load_script(view):
    """Load a job script: SBATCH options, modules, setup and the YAML file rows."""
    folder = tempfile.mkdtemp(prefix="specula_studio_job_")
    for name in ("andes.yml", "ov_seeing.yml"):
        with open(os.path.join(folder, name), "w") as f:
            f.write("main:\n  class: SimulParams\n  root_dir: /raid2/gcarla/data\n")
    script = os.path.join(folder, "andes.slurm")
    with open(script, "w") as f:
        f.write("#!/bin/bash\n#SBATCH -J andes\n#SBATCH -A IscrC_SPECIAL\n"
                "#SBATCH --gres=gpu:2\nmodule load cuda/12.2\n"
                "source $WORK/specula/bin/activate\n"
                "srun specula --nsimul 2 andes.yml ov_seeing.yml missing.yml\n")
    view.load_script(script)
    if (dpg.get_value("hpc_job_name"), dpg.get_value("hpc_gpus_per_node"),
            dpg.get_value("hpc_nsimul")) != ("andes", 2, 2):
        raise RuntimeError("job script options not loaded")
    if view.base_file != os.path.join(folder, "andes.yml") or \
            [os.path.basename(p) for p in view.override_files] != ["ov_seeing.yml",
                                                                    "missing.yml"]:
        raise RuntimeError(f"simulation files not loaded: {view.base_file} "
                           f"{view.override_files}")
    status = dpg.get_value(view._row_tag(1, "status"))
    if status != "not found on this machine":
        raise RuntimeError(f"missing override not flagged: {status!r}")
    srun = next(ln for ln in dpg.get_value("hpc_script").splitlines() if ln.startswith("srun"))
    if "andes_simul.yml override1_ov_seeing.yml override2_missing.yml" not in srun:
        raise RuntimeError(f"specula line does not use the job's file names: {srun}")
    if "$WORK/raid2/gcarla/data" not in dpg.get_value("hpc_root_preview"):
        raise RuntimeError("root_dir of the loaded base file not mapped")
    sent = []
    view.command = sent.append
    view._on_submit()
    if sent:
        raise RuntimeError("submitted with a missing simulation file")
    view._remove_override(1)
    view._on_submit()
    if not sent or sent[0]["files"] != {"base": view.base_file,
                                        "overrides": view.override_files}:
        raise RuntimeError(f"submit command without the files: {sent}")
    view.use_editor_files()
    if view.base_file or view.files_custom:
        raise RuntimeError("could not go back to the editor's files")


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
    # The separate control window is another process (needs a display): the
    # smoke build uses the docked panel, which shares all the widget code.
    step("sim_control", editor.sim_control.show_docked_panel)
    editor.sim_control.view.set_ssh_test("ok", True)
    editor.sim_control.view.append_terminal("hello\n")
    if not editor.sim_control.view.options()["stepping"]:
        raise RuntimeError("control panel options not read")
    # remote connection widgets are greyed out for In-Process, enabled for Remote
    if dpg.get_item_configuration("sim_remote_ip")["enabled"]:
        raise RuntimeError("remote fields should be disabled for In-Process")
    dpg.set_value("sim_backend", "Remote")
    editor.sim_control.view.update_remote_enabled()
    if not dpg.get_item_configuration("sim_remote_ip")["enabled"]:
        raise RuntimeError("remote fields should be enabled for Remote")
    dpg.set_value("sim_backend", "In-Process")
    editor.sim_control.view.update_remote_enabled()
    steps.append("remote_fields_toggle")
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
    # The separate HPC windows are other processes: build the in-editor ones,
    # which share all the widget code.
    from specula_studio.hpc_control import JOBS, SUBMIT
    step("hpc_window", editor.hpc.show_docked, SUBMIT)
    submit_view = editor.hpc._docked[SUBMIT]
    step("hpc_preset", submit_view.apply_preset, None, "Booster production (24 h)")
    dpg.set_value("hpc_account", "try25_test")
    step("hpc_script", submit_view.generate_script)
    if "#SBATCH --account=try25_test" not in dpg.get_value("hpc_script"):
        raise RuntimeError("HPC script not generated")
    step("hpc_load_script", _check_hpc_load_script, submit_view)
    editor.hpc._log("hello")
    editor.hpc.poll()
    if "hello" not in dpg.get_value("hpc_log"):
        raise RuntimeError("HPC log event not delivered")
    editor.hpc.jobs.append(hpc.HpcJob(job_id="1", name="t", login_host="h",
                                      transfer_host="h", job_dir="/w/j", state="RUNNING"))
    editor.hpc.jobs.append(hpc.HpcJob(job_id="2", name="t", login_host="h",
                                      transfer_host="h", job_dir="/w/j", state="COMPLETED"))
    step("hpc_jobs_window", editor.hpc.show_docked, JOBS)
    editor.hpc._jobs_version += 1
    step("hpc_jobs_table", editor.hpc.poll)
    jobs_view = editor.hpc._docked[JOBS]
    if [j.job_id for j in jobs_view.jobs] != ["1", "2"]:
        raise RuntimeError("HPC jobs table not updated")
    step("hpc_job_output", jobs_view.apply, {"type": "job_output", "job_id": "1",
                                             "text": "slurm output"})
    if dpg.get_value("hpc_out_1_text") != "slurm output":
        raise RuntimeError("HPC job output not shown")
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
    step("monitor_plot_options", _check_monitor_plot_options)

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
        editor.sim_control.start()              # options read from the docked panel
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
