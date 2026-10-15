# SPECULA Studio architecture

```
specula_studio/
  core/                 pure Python, no DearPyGui — unit-tested
    syntax.py           SPECULA conventions: connection strings, suffixes, names
    model.py            Graph: nodes, connections, snapshots
    yaml_io.py          SPECULA YAML  <->  Graph (load / export / dump)
    templates.py        bundled class templates (specula_yaml_docs/)
    validation.py       incomplete-node rules -> Issue objects
    overrides.py        override files: SPECULA combine rules, preview, rebase
    history.py          undo/redo states + "unsaved changes" fingerprint
    clipboard.py        copy / paste / duplicate of objects
  ui/                   DearPyGui mixins of the main window (SpeculaEditor)
    dialogs.py, add_objects.py, preferences.py, overrides_menu.py,
    log_window.py, edit_actions.py
  app.py                SpeculaEditor shell, menus, run loop, CLI entry point
  node_manager.py       node canvas: draws the Graph, pins, links, selection
  property_panel.py     inspector of the selected node / link
  file_handler.py       glue: core.yaml_io <-> canvas, positions
  simulation_*.py       running SPECULA (in-process, local / remote process)
  monitor_*.py, inprocess_monitor.py, socketio_client.py, dpg_plotting.py
                        live data monitors
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

## Tests

* `tests/golden/` — expected export of each config in `tests/fixtures/`;
  checked both through the pure core and through the UI-coupled loader
  (driven by `tests/fakedpg.py`).
* `tests/helpers/real_dpg_smoke.py` builds the complete window with the real
  DearPyGui, headless (only viewport / frame-sync calls are stubbed).
