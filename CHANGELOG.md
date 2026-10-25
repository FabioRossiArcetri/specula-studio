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
- Monitor options: *Image filtering* (off by default: image pixels stay
  sharp when enlarged) and snapshot vectors drawn as a line or a histogram.
- Menus: *Simulation* is now *Run* (*Control Panel*, and *HPC → Submit /
  Track*, formerly the *HPC* menu); *Display YAML* moved to *Edit*.
- Monitors of a Local Run (and those opened without a run) are separate OS
  windows, like the monitors of Remote runs: the editor sends each window
  the newest frame of its output over a local link.
- Changing the render size or the font could crash the application: the
  font was replaced from DearPyGui's callback thread while a frame was
  being drawn, and the font in use was deleted.  Fonts are now switched by
  the main loop between two frames, and loaded once per file and size.
- Local runs: the display-server port configured for a run is trusted
  (a port is never read from a line mixed with the simulation's output;
  automatic port = a free port chosen by Studio), and the terminal shows
  SPECULA's output as it arrives, including the stepping prompt.
- Four colour themes, chosen in *Preferences*: *Dark - Graphite*, *Dark -
  Midnight*, *Light - Daylight* and *Light - Paper*.  Each theme is a small
  palette from which every colour of the GUI is derived, and it now covers
  all widgets, plots and the node editor (title bars, pins, selection);
  text meets WCAG contrast minimums in all four (tested).  The separate
  monitor windows use the same theme (parts of them were drawn white).
- HPC: the *Batch Jobs* window shows the SLURM output (`slurm-<id>.out` /
  `.err`) of the selected job, read from the cluster while the job runs and
  when it ends; the window opens on a job as soon as it is submitted.
  Loading a job script keeps the user's account, modules and environment
  setup (optional), so scripts of other projects can be reused.
- HPC: *Load script...* reads an existing SLURM job script into the form
  (#SBATCH options, modules, environment setup, specula options) and its
  YAML files into the new *Simulation files* list (base + overrides, each
  replaceable from a local file; by default the editor's simulation and
  overrides); *Save script...* writes the script to a file.
- HPC: *Path mapping* for data mirrored on the cluster (default `/raid1`,
  `/raid2`, `/home` → the same paths below `$WORK`), applied to root_dir and
  every other path of the simulation; the mapped paths are checked on the
  cluster before submitting.  The environment setup defaults to
  `source $WORK/specula/bin/activate`, and *Check connection* reports which
  SPECULA checkout the job environment imports.
- *HPC → Submit Batch Job* and *HPC → Batch Jobs* open in their own OS
  windows, like the simulation control panel (Preferences: "HPC windows in
  separate windows"); jobs are still followed by the editor when they are
  closed.
- A display server lost while the run goes on is reported in the control
  panel (terminal and status); monitor windows say when the server is not
  reachable, and while the simulation is paused they ask for data less and
  less often instead of every 10 s.  Stepping runs with a display server
  warn that older SPECULA versions stop it after 60 s without a step.

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
