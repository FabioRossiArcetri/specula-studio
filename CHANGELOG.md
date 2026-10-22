# Changelog

## 0.2.0

### Structure and packaging
- Installable `specula_studio` package with `specula-studio`,
  `specula-studio-parse` and `specula-studio-compare` commands; `python -m
  specula_studio`.  CI (ruff, mypy,
  pytest).
- DearPyGui-free `core` package (model, YAML I/O, validation, overrides,
  undo history, clipboard, class catalog, run preparation) with unit tests
  and golden round-trip tests on SPECULA's own configuration files.
- The main window is split into focused mixins (`specula_studio/ui`).
- Logging instead of prints, with an in-app *View → Log* window; one
  settings file in the user config directory (legacy files migrated).

### Editing
- Undo/redo, copy/paste/duplicate, unsaved-changes flag, Edit menu and
  shortcuts; editing keys only act on the canvas (typing in the property
  panel no longer deletes links or nodes).
- Lossless save: every value from the file or set by the user is written,
  `outputs` lists are kept, `*_list_ref` / `*_dict_ref` are always lists.
- Node names validated (SPECULA forbids `.` `-` `:`) and renames propagated
  to references and AtmoPropagation outputs; generated names never collide.
- Overrides follow SPECULA's rules (`_<N>` simulation index, removal of the
  inputs of removed objects); edits made while an override preview is active
  are kept and never saved into the base file.
- *View → Issues*, *Find Object*, *Add Objects → Search*, *Compare with
  Saved File*, *Export Diagram* (Graphviz); open a file from the command line.

### Class metadata
- Generated from the installed SPECULA in a background subprocess and cached
  per installation; bundled templates as fallback.  Help and input
  optionality come from the catalog (no SPECULA objects are instantiated).

### Running and monitoring
- Each run uses a private temporary directory; override files are passed to
  SPECULA as extra parameter files.
- In-process runs initialise SPECULA before importing its modules, wrap
  `LoopControl` only during the run, and probes see the first step; abort,
  stepping and "advance N steps" (one prompt answer) work.
- SSH runs use a private remote directory, proper quoting and `ssh -tt`
  (abort stops the remote process); recent hosts remembered; DisplayServer
  port configurable (0 = automatic, read from `[SPECULA_PORT:n]`).
- One payload/plot implementation shared by all monitor windows; data is
  always copied before being drawn.

### Fixes
- `REQUIRED` template marker no longer exported as a value.
- Missing `main.root_dir/total_time/time_step` no longer make runs fail.
- Theme switch recolours nodes; recent-files menu no longer breaks;
  "advance N steps" no longer freezes the UI; monitors closed with their
  window button release their probe; subprocess monitors are terminated on
  exit.
- The *Time Series* vector mode of monitor windows works (the plotter
  expected a different mode name than the windows sent).
- *Help → Selected Object* falls back to the node shown in the property
  panel when nothing is selected.
- Bundled templates use `kind: reference` throughout; the old `kind: object`
  spelling, compatibility aliases and no-op stubs of the previous code layout
  are gone.
