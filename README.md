# SPECULA Studio

A node-based visual editor and run controller for
[SPECULA](https://github.com/ArcetriAdaptiveOptics/SPECULA) adaptive-optics
simulations, built on [Dear PyGui](https://github.com/hoffstadt/DearPyGui).

* Build a simulation graph by adding processing / data objects and wiring
  their inputs, outputs and references.
* Load and save standard SPECULA YAML files (node positions are stored in the
  `gui_pos` key, which SPECULA ignores).
* Apply SPECULA override files on top of a simulation.
* Run the simulation inside the editor (*Local Run*), as a local subprocess or
  on a remote host via SSH (*Remote*), step it, and watch any output live in
  monitor windows.  The simulation control panel opens in its own window (it
  can be moved to another screen).  Remote hosts
  can be picked from the `Host` entries of your OpenSSH client configuration
  (`~/.ssh/config`, i.e. `%USERPROFILE%\.ssh\config` on Windows); the alias is
  passed to `ssh`/`scp` as is, so `User`, `Port`, `IdentityFile`, `ProxyJump`…
  all apply.  See [Remote runs: setting up SSH](#remote-runs-setting-up-ssh).
* *HPC → Submit Batch Job* runs a simulation as a SLURM job on a cluster
  (written for CINECA Leonardo): check the connection, log in with the 2FA
  `step` client, optionally upload the calibration data, generate and edit
  the job script, submit, follow the job in *HPC → Batch Jobs* (state,
  output, cancel) and download the results when it ends.  Batch jobs run
  without a display server, so live monitors are not available for them.
  The job's simulation files default to the editor's simulation and
  overrides, and can be any local YAML files; an existing job script can be
  loaded (*Load script...*) to fill the form and the file list.  Both HPC
  windows open as separate windows.
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

SPECULA itself must be installed in the same environment for *Local Run*,
tooltips and online help.

## Running

```bash
specula-studio              # installed entry point
python -m specula_studio    # equivalent
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

## Remote runs: setting up SSH

A *Remote* run copies the simulation to a server and runs the `specula`
command there.  SPECULA Studio uses the `ssh` and `scp` commands of your
computer, without a terminal to type into: **the login must work without any
prompt**, with an SSH key.  Set this up once, as below; then pick the server
in the control panel.

### 1. Create a key and give its public half to the server

Open a terminal (on Windows: PowerShell) and create a key pair.  A
passphrase is recommended: the agent (step 2) will remember it for you.

```bash
ssh-keygen -t ed25519 -C "you@your-lab"     # accept the default file
```

This writes a private key, `~/.ssh/id_ed25519` (on Windows
`%USERPROFILE%\.ssh\id_ed25519`), which never leaves your computer, and a
public key, `id_ed25519.pub`, which goes to the server:

* **Linux, macOS:** `ssh-copy-id -i ~/.ssh/id_ed25519.pub you@server.example.org`
* **Windows** (no `ssh-copy-id`), in PowerShell:

  ```powershell
  type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh you@server.example.org "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
  ```

  (this also works on macOS and Linux, with `cat ~/.ssh/id_ed25519.pub` in
  place of `type ...`).

You type your server password one last time; after that the key logs you in.

### 2. Let the agent hold the key

With a passphrase, the key must be loaded in the *ssh-agent*, which unlocks
it once for all the logins that follow.

* **Windows 10/11.**  The OpenSSH client is built in (check with `ssh -V`;
  if it is missing, add *OpenSSH Client* in *Settings > System > Optional
  features*).  Its agent is a Windows service, off by default: start it once,
  in a PowerShell *run as administrator*, then add the key in a normal one.
  The key stays in the agent across reboots.

  ```powershell
  Get-Service ssh-agent | Set-Service -StartupType Automatic   # as administrator
  Start-Service ssh-agent                                      # as administrator
  ssh-add $env:USERPROFILE\.ssh\id_ed25519
  ```

  Start SPECULA Studio from PowerShell, cmd or the Start menu.  Git Bash puts
  its own `ssh` first on the `PATH`, and that one does not use the Windows
  agent.

* **macOS.**  The agent runs already; let it keep the passphrase in the
  Keychain:

  ```bash
  ssh-add --apple-use-keychain ~/.ssh/id_ed25519
  ```

  and add these lines at the end of `~/.ssh/config`, so the key is loaded
  again after a restart (`UseKeychain` exists only on macOS):

  ```
  Host *
      AddKeysToAgent yes
      UseKeychain yes
      IdentityFile ~/.ssh/id_ed25519
  ```

* **Linux.**  Most desktops start an agent with the session (GNOME Keyring,
  KDE Wallet): `ssh-add -l` answers without an error.  Add the key with
  `ssh-add ~/.ssh/id_ed25519`, or put `AddKeysToAgent yes` in
  `~/.ssh/config` to add it at its first use.  Without a desktop agent, start
  one in the shell you launch SPECULA Studio from:
  `eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519`.

### 3. Give the server a short name

Describe the server in your SSH configuration file: `~/.ssh/config` on Linux
and macOS, `%USERPROFILE%\.ssh\config` on Windows (a plain text file with no
extension: Notepad may add `.txt`, check the name).  Every `Host` entry
appears in SPECULA Studio's *SSH config hosts* list.

```
Host gpu-server                       # the name you will use: any word
    HostName gpu.example.org          # the server's address (or its IP)
    User you                          # your login on the server
    IdentityFile ~/.ssh/id_ed25519
    # Port 2222                       # if the server does not use port 22
    # ProxyJump you@gateway.example.org   # if it is reached through a gateway
```

Entries with wildcards (`Host *`) set options but are not listed; `Include`
files are read too.

### 4. Check, then run

Connect once from a terminal: the first time, ssh asks you to confirm the
server's identity (answer `yes`), which SPECULA Studio cannot do for you.
Then check that nothing is asked any more, and that `specula` is found the
way SPECULA Studio starts it (through an interactive `bash`):

```bash
ssh gpu-server hostname                          # prints the name, asks nothing
ssh gpu-server "bash -ic 'which specula'"        # prints the path of specula
```

(The second command may also print two harmless lines about job control.)
If `specula` is not found, activate its environment at the end of your
`~/.bashrc` **on the server** (e.g. `conda activate specula`).

On the server, the simulation reads its calibration data from
`main.root_dir`: the files must be there, at that path (change `root_dir` in
the editor if the server keeps them elsewhere).  Live monitors connect to
SPECULA's display server on the *Display server port* (5000 by default): the
server's firewall must let your computer reach it.  Use another port if 5000
is taken, or `0` to let a recent SPECULA choose a free one.

In SPECULA Studio: *Simulation > Control Panel*, *Execution Mode*: **Remote**,
choose the server in *SSH config hosts*, and press **Test SSH**: it must say
*Logged in to ... without prompts*.  Then **START SIMULATION**.  The files are
copied to a temporary directory on the server, which is removed at the end;
*Abort Simulation* stops the remote process too.
