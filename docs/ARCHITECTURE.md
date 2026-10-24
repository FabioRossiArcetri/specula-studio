# SPECULA Studio architecture

```
specula_studio/
  core/                 pure Python, no DearPyGui — unit-tested
    syntax.py           SPECULA conventions: connection strings, suffixes, names
    model.py            Graph: nodes, connections, snapshots
    yaml_io.py          SPECULA YAML  <->  Graph (load / export / dump)
    templates.py        bundled class templates (specula_yaml_docs/)
    catalog.py          class catalog of the installed SPECULA (cached), fallback
                        to the bundled templates; catalog_parser.py extracts it
    validation.py       incomplete-node rules -> Issue objects
    overrides.py        override files: SPECULA combine rules, preview, rebase
    history.py          undo/redo states + "unsaved changes" fingerprint
    clipboard.py        copy / paste / duplicate of objects
    run_prep.py         run directory files, specula command line
    slurm.py            sbatch script + HPC override (see below)
    ssh_config.py       Host entries of ~/.ssh/config
    diagram.py          Graphviz export
  ui/                   DearPyGui mixins of the main window (SpeculaEditor)
    dialogs.py, add_objects.py, preferences.py, overrides_menu.py,
    log_window.py, edit_actions.py, tools.py;
    control_panel.py, hpc_views.py (views of the tool windows)
  app.py                SpeculaEditor shell, menus, run loop, CLI entry point
  node_manager.py       node canvas: draws the Graph, pins, links, selection;
                        owns the DPG item <-> graph lookup tables
  property_panel.py     inspector of the selected node / link
  file_handler.py       glue: core.yaml_io <-> canvas, positions
  simulation_*.py       running SPECULA (in-process, local / remote process)
  control_link.py, control_window.py
                        tool windows (control panel, HPC) in their own
                        processes (see below)
  sshutil.py, hpc.py    prompt-free ssh/scp; SLURM jobs
  hpc_control.py        HPC menu: submission, job list and its polling
  monitor_bus.py        in-process data bus (probes -> monitor windows)
  monitor_*.py, inprocess_monitor.py, dpg_plotting.py
                        live data monitors
  socketio_client.py    editor connection to a DisplayServer: maps nodes to
                        the running objects (monitor subprocesses connect
                        on their own)
  theme_manager.py      the four colour themes: a palette per theme, every
                        logical colour derived from it, bound to all widgets
  settings.py, logs.py  user settings file, logging + in-app log buffer
```

## Data flow

* **Load**: `core.yaml_io.load_into()` builds the `Graph` from the YAML; the
  canvas is then drawn from the model with `NodeManager.sync_ui_from_graph()`
  (which never modifies the model).
* **Edit**: UI callbacks modify the model (and the matching canvas items).
* **Save / run**: `core.yaml_io.export_graph()` turns the model back into
  SPECULA YAML.
* **Undo/redo**: once per ~0.3 s the app fingerprints the model; a change is
  recorded as a new history state (model snapshot + node positions).  Undo
  restores a state and redraws the canvas.  The same fingerprint drives the
  unsaved-changes flag (`*` in the title).
* **Overrides**: enabling an override stores the current simulation as the
  *base*, then reloads `base + overrides`.  Edits made during this preview
  are folded back into the base (`rebase_edits`) when saving or toggling,
  so saved files never contain override values.

## Class catalog

The parameters / inputs / outputs / help of every SPECULA class come from the
*installed* SPECULA: on first start (or after SPECULA changes) the catalog is
generated in a subprocess (`python -m specula_studio.core.catalog`) — so the
editor process never imports SPECULA or touches a GPU just to show metadata —
and cached in the user cache dir, keyed on the sources' sizes and mtimes.
Until it is ready, and when SPECULA is not installed, the bundled templates
are used; the switch happens live.  Input optionality is read from the
catalog ("(optional)" in SPECULA's input descriptions) instead of
instantiating classes.

## Tests

* `tests/golden/` — expected export of each config in `tests/fixtures/`;
  checked both through the pure core and through the UI-coupled loader
  (driven by `tests/fakedpg.py`).
* `tests/helpers/real_dpg_smoke.py` builds the complete window with the real
  DearPyGui, headless (only viewport / frame-sync calls are stubbed).

## Running SPECULA

`SimulationControl` writes each run into a private temporary directory:
the simulation (`<name>_simul.yml`), copies of the enabled override files
(passed to SPECULA after the base file, so SPECULA itself applies them,
including `_<N>` keys for `--nsimul` runs) and `server.json` for monitor
subprocesses.  `main.root_dir / total_time / time_step` are filled with
SimulParams' defaults when missing, because `Simul` reads them from the YAML.

* **In-process** (`InProcessBackend`): `Simul(*files).run()` in a thread.
  Before each run every `specula.*` submodule imported earlier is dropped and
  `specula.init(device, precision)` is called first — SPECULA modules copy
  `xp` / `global_precision` at import time.  `LoopControl.run/iter` are
  wrapped only during the run: probes for open monitors are added before the
  first step, later ones between steps, and abort stops the loop.  Probes read
  `array_for_display()` (as SPECULA's DisplayServer does) into a CPU copy.
* **Local / SSH** (`RemoteBackend`): the `specula` command, with a
  DisplayServer (data mode, session id) injected.  Port `0` lets SPECULA pick a
  free port, read back from its `[SPECULA_PORT:n]` output line.  Remote runs
  use a private `/tmp/specula_studio_*` directory and `ssh -tt`, so aborting
  also stops the remote process; the directory is removed afterwards.

### Possible SPECULA-side hooks

The in-process backend would not need to wrap `LoopControl` if SPECULA
offered (1) a way to build a `Simul` without running it and get its
`LoopControl`, and (2) a public `LoopControl.add_observer(obj)` / per-step
callback and a `stop()` method.  Stepping could then use a callback instead
of replacing `sys.stdin`.

## HPC batch jobs

`core/slurm.py` (pure) renders the sbatch script — modelled on SPECULA's
`config/leonardo/jobScripts` (`srun --mpi=none bash -c 'specula [--mpi] …'`,
a CUDA module for CuPy; MPI runs also need an MPI module) — and builds a
last override file
(`zz_studio_hpc_override.yml`) that adapts the simulation to the cluster:
`root_dir` → uploaded calibration data, no display server, local absolute
DataStore paths → `results/` in the job directory.  `hpc.py` does the
transport with the system `ssh`/`scp` in `BatchMode` (Leonardo: 12-hour
smallstep certificates in the ssh agent; an optional data-mover host is used
for scp), resolves `$WORK` on the login node, submits, polls squeue/sacct and
downloads results.  Submitted jobs are stored in the settings and followed
again after a restart.  Files are written with LF line endings (sbatch rejects
CRLF scripts).

## Tool windows (simulation control, HPC)

DearPyGui has one OS window per process, so the simulation control panel and
the two HPC windows (*Submit Batch Job*, *Batch Jobs*) "in their own window"
are separate processes (`control_window.py --kind control|hpc_submit|
hpc_jobs`, one per window).  They are only remote controls: runs stay in the
editor, where the in-process backend, its probes and the monitors live, and
so do the ssh work, the batch-job list (followed with the windows closed) and
the settings file.  `control_link.py` carries plain-dict messages over an
authenticated localhost `multiprocessing.connection` (the key is passed in the
child's environment): commands from a window (e.g. start with the options
dict, step, abort; check_connection, submit, cancel_job) are queued and
executed on the editor's GUI thread; terminal output, status, logs and the
job list go back.  The separate windows and the optional in-editor ones
(preferences *control_panel* / *hpc_windows* = "docked") use the same views,
`ui/control_panel.ControlPanelView` and `ui/hpc_views`.  `HpcControl`'s
background threads never touch DearPyGui: they queue events, which its
`poll()` delivers every frame.
