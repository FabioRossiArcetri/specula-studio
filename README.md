# SPECULA Studio

A node-based visual editor and run controller for
[SPECULA](https://github.com/ArcetriAdaptiveOptics/SPECULA) adaptive-optics
simulations, built on [Dear PyGui](https://github.com/hoffstadt/DearPyGui).

* Build a simulation graph by adding processing / data objects and wiring
  their inputs, outputs and references.
* Load and save standard SPECULA YAML files (node positions are stored in the
  `gui_pos` key, which SPECULA ignores).
* Apply SPECULA override files on top of a simulation.
* Run the simulation in-process, as a local subprocess or on a remote host via
  SSH, step it, and watch any output live in monitor windows.  Remote hosts
  can be picked from the `Host` entries of your OpenSSH client configuration
  (`~/.ssh/config`, i.e. `%USERPROFILE%\.ssh\config` on Windows); the alias is
  passed to `ssh`/`scp` as is, so `User`, `Port`, `IdentityFile`, `ProxyJump`…
  all apply.
* Undo/redo, copy/paste/duplicate, unsaved-changes tracking.
* *View → Issues* lists incomplete objects (missing required parameters,
  references or inputs); *Edit → Find Object* (Ctrl+F) and *Add Objects →
  Search* (Ctrl+Space) find objects and classes by name.
* *File → Compare with Saved File* shows what changed; *File → Export
  Diagram* writes a Graphviz `.dot` (and `.svg` when Graphviz is installed).

## Installation

```bash
pip install -e .            # editable install from a checkout
pip install -e ".[dev]"     # plus pytest / ruff for development
```

SPECULA itself must be installed in the same environment for in-process runs,
tooltips and online help.

## Running

```bash
specula-studio              # installed entry point
python -m specula_studio    # equivalent
python main.py              # from a source checkout, without installing
specula-studio my_sim.yml   # open a simulation directly
```

Options: `--log-level {DEBUG,INFO,WARNING,ERROR}` (or `-v`), `--version`.
Log messages are also shown in *View → Log*.

Preferences (theme, fonts, render size, recent files, …) are stored in a
single `settings.json` in the platform's user-config directory (e.g.
`~/.config/specula-studio/` on Linux); set `SPECULA_STUDIO_SETTINGS` to use a
different file. Settings from older versions are migrated automatically.

## Development

```bash
pytest          # unit, golden round-trip and headless UI tests (no display needed)
ruff check .    # lint
mypy            # type check of the DearPyGui-free core
```

`tests/golden/` holds the YAML that loading and re-exporting each config in
`tests/fixtures/specula/` must reproduce. If a change to the export format is
intentional, regenerate the golden files with
`python -m tests.helpers.regen_golden` and review the diff.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the code layout.

Class metadata (parameters, inputs, outputs, help) is generated from the
installed SPECULA in the background and cached (see *Help → Regenerate Class
Catalog*). The bundled templates in `specula_studio/specula_yaml_docs/` are
the fallback when SPECULA is not installed; refresh them with:

```bash
specula-studio-parse [path/to/SPECULA/specula] specula_studio/specula_yaml_docs
```

## License

MIT, see [LICENSE](LICENSE).
