"""
Widgets of the HPC windows: *Submit Batch Job* and *Batch Jobs*.

Like the simulation control panel, each window is shown either in its own
OS window (a separate process, :mod:`specula_studio.control_window`) or as a
window inside the editor.  The views never connect to the cluster: every
action is a plain-dict command passed to *command* (the editor's
:class:`~specula_studio.hpc_control.HpcControl` runs it), and the editor's
replies come back as plain-dict events given to :meth:`apply`.

Commands (``type`` and fields)
    submit view: ``check_connection {profile, setup}``, ``step_login {email}``,
    ``submit {profile, options, script, files}`` (``script`` None = generate
    it; ``files``: ``{base, overrides}``, ``base`` "" = the simulation open in
    the editor), ``save_profile {profile}``, ``show_jobs``;
    jobs view: ``refresh_jobs``, ``job_output {job_id}``, ``cancel_job``,
    ``download_job``, ``forget_job`` ``{job_id}``.
Events
    submit view: ``sim_info {name, n_overrides, overrides, run_files, root_dir,
    local_calib}``, ``log {text}``, ``conn_status {text, ok}``;
    jobs view: ``jobs {jobs: [HpcJob dicts]}``, ``job_output {job_id, text}``.
"""

from __future__ import annotations

import os
import pathlib
import time

import dearpygui.dearpygui as dpg

from .. import hpc
from ..core import slurm, ssh_config, yaml_io


class HpcSubmitView:
    """The SLURM job form: simulation files, cluster, data, job options, script.

    The simulation files are the YAML files passed to specula, in order: the
    base (empty = the simulation open in the editor) and the overrides (by
    default the editor's enabled ones, followed while not changed here).
    """

    def __init__(self, command, theme_manager, profile: dict, sim_info: dict,
                 prefix: str = "hpc"):
        self.command = command
        self.tm = theme_manager
        self.profile = profile
        self.sim_info = dict(sim_info)
        self.p = prefix
        self.generated_script: str | None = None
        self.base_file = ""                          # "" = the editor's simulation
        self.override_files: list[str] = list(self.sim_info.get("overrides") or [])
        self.files_custom = False                    # True once changed in this window
        self._rows = 0                               # widget generation of the file rows
        self._root_cache: tuple[str, float, str] = ("", 0.0, "")

    def t(self, name: str) -> str:
        return f"{self.p}_{name}"

    # ── building ──────────────────────────────────────────────────────────────

    def build(self, parent) -> None:
        t = self.t
        prof = self.profile
        opts = slurm.SlurmOptions.from_dict(prof["slurm"])
        tm = self.tm
        hint = tm.get_color("text_hint")
        head = tm.get_color("control_subsection_header")
        hosts = [h.alias for h in ssh_config.list_ssh_hosts()]
        w = -220

        with dpg.group(parent=parent):
            dpg.add_text("Batch jobs run without a display server: live monitors are "
                         "not available; results are downloaded when the job ends.",
                         color=hint, wrap=820)

            # ── simulation files ─────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Simulation files (passed to specula in this order)", color=head)
            dpg.add_group(tag=t("files"))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Add override...", callback=self._add_override)
                dpg.add_button(label="Use the editor's files", callback=self.use_editor_files)
            dpg.add_text(f"+ {slurm.HPC_OVERRIDE_NAME} (added by Studio, applied last: "
                         "path mapping, no display server, results in the job directory)",
                         color=hint, wrap=820)

            # ── cluster ──────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Cluster", color=head)
            if hosts:
                dpg.add_combo(hosts, label="SSH config hosts", width=w,
                              callback=lambda s, a: dpg.set_value(t("login_host"), a))
            dpg.add_input_text(label="Login host", tag=t("login_host"), width=w,
                               default_value=prof["login_host"],
                               hint="user@login.leonardo.cineca.it or an ssh alias")
            dpg.add_input_text(label="Transfer host (optional)", tag=t("transfer_host"),
                               width=w, default_value=prof["transfer_host"],
                               hint="e.g. user@data.leonardo.cineca.it for large data")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Check connection", callback=self._on_check_connection)
                dpg.add_text("not checked", tag=t("conn_status"), color=hint)
            with dpg.group(horizontal=True):
                dpg.add_input_text(tag=t("step_email"), width=260,
                                   default_value=prof["step_email"],
                                   hint="CINECA account e-mail")
                dpg.add_button(label="Log in (2FA, step)", callback=self._on_step_login)
            dpg.add_text(f"Leonardo needs a 12-hour certificate: step ssh login <email> "
                         f"--provisioner {hpc.STEP_PROVISIONER} (opens the browser).",
                         color=hint, wrap=820)
            dpg.add_input_text(label="Remote working directory", tag=t("remote_base"),
                               width=w, default_value=prof["remote_base"])

            # ── data ─────────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Data", color=head)
            dpg.add_input_text(label="Path mapping", tag=t("path_map"), multiline=True,
                               width=w, height=60, default_value=prof["path_map"],
                               callback=self._show_root_dir)
            dpg.add_text("Data mirrored on the cluster: one 'local prefix -> cluster prefix' "
                         "per line, applied to root_dir and every other path of the "
                         "simulation ($WORK is expanded on the cluster).",
                         color=hint, wrap=820)
            dpg.add_text("", tag=t("root_preview"), color=hint, wrap=820)
            dpg.add_checkbox(label="Upload calibration data before submitting",
                             tag=t("calib_upload"), default_value=prof["calib_upload"],
                             callback=self.update_root_dir)
            with dpg.group(horizontal=True):
                dpg.add_input_text(label="", tag=t("calib_local"), width=w + 120,
                                   default_value=(prof["calib_local"]
                                                  or self.sim_info.get("local_calib", "")),
                                   callback=self.update_root_dir)
                dpg.add_text("Local directory")
                dpg.add_button(label="...", callback=self._browse_calib)
            dpg.add_input_text(label="Remote parent directory", tag=t("remote_calib_parent"),
                               width=w, default_value=prof["remote_calib_parent"],
                               callback=self.update_root_dir)
            dpg.add_input_text(label="root_dir on the cluster", tag=t("remote_root_dir"),
                               width=w, default_value=prof["remote_root_dir"],
                               hint="empty = the simulation's root_dir (mapped)",
                               callback=self._show_root_dir)
            dpg.add_text("SimulParams.root_dir is set to this directory for the job only.",
                         color=hint)

            # ── SLURM ────────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("SLURM job", color=head)
            dpg.add_combo(list(slurm.PRESETS), label="Preset", width=w,
                          callback=self.apply_preset)
            dpg.add_input_text(label="Job name", tag=t("job_name"), width=w,
                               default_value=opts.job_name)
            dpg.add_input_text(label="Account (--account)", tag=t("account"), width=w,
                               default_value=opts.account, hint="e.g. try25_rossi")
            dpg.add_combo(slurm.PARTITIONS, label="Partition", tag=t("partition"), width=w,
                          default_value=opts.partition)
            dpg.add_combo(slurm.QOS_CHOICES, label="QOS", tag=t("qos"), width=w,
                          default_value=opts.qos)
            dpg.add_input_text(label="Time limit", tag=t("time"), width=w,
                               default_value=opts.time, hint="HH:MM:SS or D-HH:MM:SS")
            with dpg.group(horizontal=True):
                for name, label in (("nodes", "Nodes"), ("ntasks_per_node", "Tasks/node"),
                                    ("cpus_per_task", "CPUs/task"),
                                    ("gpus_per_node", "GPUs/node")):
                    dpg.add_input_int(label=label, tag=t(name), width=90,
                                      default_value=int(getattr(opts, name)), min_value=0,
                                      min_clamped=True)
            dpg.add_text("More than one task in total runs SPECULA with --mpi "
                         "(objects distributed with target_rank).", color=hint)
            dpg.add_input_text(label="Modules", tag=t("modules"), multiline=True,
                               width=w, height=60, default_value="\n".join(opts.modules))
            dpg.add_input_text(label="Environment setup", tag=t("setup"), multiline=True,
                               width=w, height=50, default_value=opts.setup,
                               hint="e.g. source $WORK/venv/bin/activate")
            dpg.add_input_text(label="Extra #SBATCH options", tag=t("extra_sbatch"),
                               multiline=True, width=w, height=36,
                               default_value=opts.extra_sbatch, hint="--mem=100G")
            with dpg.group(horizontal=True):
                dpg.add_checkbox(label="CPU only (--cpu)", tag=t("cpu"),
                                 default_value=opts.cpu)
                dpg.add_combo(["0", "1"], label="Precision", tag=t("precision"), width=50,
                              default_value=str(opts.precision))
                dpg.add_input_int(label="N-simul", tag=t("nsimul"), width=90,
                                  default_value=int(opts.nsimul), min_value=1,
                                  min_clamped=True)
                dpg.add_checkbox(label="Diagram", tag=t("diagram"),
                                 default_value=opts.diagram)
            dpg.add_input_text(label="Extra specula arguments", tag=t("extra_args"),
                               width=w, default_value=opts.extra_args)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Generate script", callback=self.generate_script)
                dpg.add_button(label="Load script...", callback=self._browse_script)
                dpg.add_button(label="Save script...", callback=self._browse_save_script)
                dpg.add_text("The script can be edited before submitting.", color=hint)
            dpg.add_input_text(tag=t("script"), multiline=True, width=-1, height=220,
                               tab_input=True)

            # ── results / submit ─────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Results", color=head)
            dpg.add_checkbox(label="Download results when the job has finished",
                             tag=t("download"), default_value=prof["download"])
            dpg.add_input_text(label="Local results directory", tag=t("download_dir"),
                               width=w, default_value=prof["download_dir"])
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="SUBMIT JOB", width=160, height=32,
                               callback=self._on_submit)
                dpg.add_button(label="Save settings", callback=self._on_save_profile)
                dpg.add_button(label="Jobs...",
                               callback=lambda: self.command({"type": "show_jobs"}))
            dpg.add_input_text(tag=t("log"), multiline=True, readonly=True, width=-1,
                               height=140)
        self.draw_files()
        self.update_root_dir()
        self.generate_script()

    # ── simulation files ──────────────────────────────────────────────────────

    def sim_name(self) -> str:
        if self.base_file:
            return slurm.safe_name(pathlib.Path(self.base_file).stem)
        return slurm.safe_name(self.sim_info.get("name") or "untitled")

    def run_files(self) -> list[str]:
        """File names in the specula command of the job."""
        return slurm.run_file_names(self.sim_name(), self.override_files)

    def set_files(self, base: str, overrides, custom: bool = True) -> None:
        self.base_file = base
        self.override_files = [str(p) for p in overrides]
        self.files_custom = custom
        self.draw_files()
        self._files_changed()

    def use_editor_files(self, *_) -> None:
        self.set_files("", self.sim_info.get("overrides") or [], custom=False)

    def draw_files(self) -> None:
        """(Re)create the rows of the simulation files."""
        parent = self.t("files")
        dpg.delete_item(parent, children_only=True)
        self._rows += 1
        editor = self.sim_info.get("name") or "unsaved simulation"
        self._file_row(parent, "Base", self.base_file, None,
                       hint=f"empty = the simulation open in the editor ({editor})")
        for i, path in enumerate(self.override_files):
            self._file_row(parent, f"Override {i + 1}", path, i)
        self._show_file_status()

    def _row_tag(self, index, what: str) -> str:
        return self.t(f"file{self._rows}_{'base' if index is None else index}_{what}")

    def _file_row(self, parent, label: str, path: str, index, hint: str = "") -> None:
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text(f"{label:<11}")
            dpg.add_input_text(tag=self._row_tag(index, "path"), default_value=path,
                               width=520, hint=hint, user_data=index,
                               callback=self._on_file_edited)
            dpg.add_button(label="...", user_data=index,
                           callback=lambda s, a, u: self._browse_yaml(u))
            if index is None:
                dpg.add_button(label="Editor", callback=lambda: self._set_base(""))
            else:
                dpg.add_button(label="x", user_data=index,
                               callback=lambda s, a, u: self._remove_override(u))
            dpg.add_text("", tag=self._row_tag(index, "status"))

    def _on_file_edited(self, sender, value, index) -> None:
        value = value.strip()
        if index is None:
            self.base_file = value
        else:
            self.override_files[index] = value
        self.files_custom = True
        self._show_file_status()
        self._files_changed()

    def _set_base(self, path: str) -> None:
        self.base_file = path
        self.files_custom = True
        dpg.set_value(self._row_tag(None, "path"), path)
        self._show_file_status()
        self._files_changed()

    def _remove_override(self, index: int) -> None:
        if 0 <= index < len(self.override_files):
            del self.override_files[index]
            self.set_files(self.base_file, self.override_files)

    def _add_override(self, *_) -> None:
        self._browse_yaml("add")

    def _browse_yaml(self, index) -> None:
        """File dialog for the base (None), override *index*, or a new override ("add")."""
        tag = self.t("yaml_dialog")
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)
        current = self.base_file if index is None else (
            self.override_files[index] if isinstance(index, int) else "")

        def chosen(sender, app_data):
            path = app_data.get("file_path_name", "")
            if not path:
                return
            if index is None:
                self._set_base(path)
            elif index == "add":
                self.set_files(self.base_file, [*self.override_files, path])
            else:
                self.override_files[index] = path
                self.set_files(self.base_file, self.override_files)

        start = os.path.dirname(current) if current else (
            os.path.dirname(self.override_files[-1]) if self.override_files else os.getcwd())
        with dpg.file_dialog(show=True, tag=tag, width=760, height=420, callback=chosen,
                             default_path=start if os.path.isdir(start) else os.getcwd()):
            dpg.add_file_extension(".yml")
            dpg.add_file_extension(".yaml")
            dpg.add_file_extension(".*")

    def _show_file_status(self) -> None:
        ok, bad = self.tm.get_color("text_success"), self.tm.get_color("text_error")
        rows = [(None, self.base_file), *enumerate(self.override_files)]
        for index, path in rows:
            tag = self._row_tag(index, "status")
            if not dpg.does_item_exist(tag):
                continue
            if index is None and not path:
                dpg.set_value(tag, "")
            elif os.path.isfile(path):
                dpg.set_value(tag, "ok")
                dpg.configure_item(tag, color=ok)
            else:
                dpg.set_value(tag, "not found on this machine")
                dpg.configure_item(tag, color=bad)

    def missing_files(self) -> list[str]:
        paths = ([self.base_file] if self.base_file else []) + self.override_files
        return [p for p in paths if not p or not os.path.isfile(p)]

    def _files_changed(self) -> None:
        self._show_root_dir()
        if self.generated_script is None or \
                dpg.get_value(self.t("script")) == self.generated_script:
            self.generate_script()

    # ── loading / saving a job script ─────────────────────────────────────────

    def _browse_script(self, *_) -> None:
        tag = self.t("script_dialog")
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)

        def chosen(sender, app_data):
            path = app_data.get("file_path_name", "")
            if path:
                self.load_script(path)

        with dpg.file_dialog(show=True, tag=tag, width=760, height=420, callback=chosen,
                             default_path=os.getcwd()):
            for ext in (".slurm", ".sh", ".sbatch", ".job", ".*"):
                dpg.add_file_extension(ext)

    def load_script(self, path: str) -> None:
        """Read a job script into the form and the simulation files."""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                parsed = slurm.parse_script(f.read())
        except OSError as exc:
            self.append_log(f"Cannot read {path}: {exc}")
            return
        self._apply_options(parsed.options)
        if parsed.files:
            folder = os.path.dirname(os.path.abspath(path))
            files = [f if os.path.isabs(f) else os.path.normpath(os.path.join(folder, f))
                     for f in parsed.files]
            self.set_files(files[0], files[1:])
        self.generate_script()
        self.append_log(f"Loaded {path}: {len(parsed.files)} simulation file(s)")
        for note in parsed.notes:
            self.append_log(f"  {note}")
        for missing in self.missing_files():
            self.append_log(f"  Not found on this machine: {missing} "
                            "(choose the file with '...')")

    def _apply_options(self, options: dict) -> None:
        t = self.t
        for name, value in options.items():
            if name == "modules":
                dpg.set_value(t("modules"), "\n".join(str(m) for m in value))
            elif name == "precision":
                dpg.set_value(t("precision"), str(value))
            elif name in ("partition", "qos"):
                # combos keep values that are not in their list
                dpg.set_value(t(name), str(value))
            elif dpg.does_item_exist(t(name)):
                dpg.set_value(t(name), value)

    def _browse_save_script(self, *_) -> None:
        tag = self.t("save_dialog")
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)

        def chosen(sender, app_data):
            path = app_data.get("file_path_name", "")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(dpg.get_value(self.t("script")).replace("\r\n", "\n"))
                self.append_log(f"Script saved to {path}")
            except OSError as exc:
                self.append_log(f"Cannot save {path}: {exc}")

        with dpg.file_dialog(show=True, tag=tag, width=760, height=420, callback=chosen,
                             default_path=os.getcwd(),
                             default_filename=f"{self.options().job_name or 'job'}"):
            dpg.add_file_extension(".slurm")
            dpg.add_file_extension(".*")

    # ── root_dir on the cluster ───────────────────────────────────────────────

    def _base_root_dir(self) -> str:
        """root_dir of the base simulation, as written in it."""
        if not self.base_file:
            return self.sim_info.get("root_dir", "")
        try:
            mtime = os.path.getmtime(self.base_file)
        except OSError:
            return ""
        path, stamp, root = self._root_cache
        if path != self.base_file or stamp != mtime:
            data = yaml_io.read_yaml_file(self.base_file) or {}
            root = next((str(obj.get("root_dir") or "") for obj in data.values()
                         if isinstance(obj, dict) and obj.get("class") == "SimulParams"), "")
            self._root_cache = (self.base_file, mtime, root)
        return root

    def cluster_root_dir(self) -> str:
        """root_dir the job will use (cluster prefixes not expanded)."""
        explicit = dpg.get_value(self.t("remote_root_dir")).strip()
        if explicit:
            return explicit
        root = self._base_root_dir()
        mapped = slurm.map_path(root, slurm.parse_path_map(dpg.get_value(self.t("path_map"))))
        return mapped if mapped is not None else root

    def _show_root_dir(self, *_) -> None:
        root = self._base_root_dir()
        cluster = self.cluster_root_dir()
        if not root and not cluster:
            text = "The simulation has no root_dir."
        elif cluster == root:
            text = f"root_dir on the cluster: {root} (not mapped)"
        else:
            text = f"root_dir on the cluster: {cluster}   (in the simulation: {root})"
        dpg.set_value(self.t("root_preview"), text)

    # ── editor events ─────────────────────────────────────────────────────────

    def apply(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "sim_info":
            self.sim_info = {k: v for k, v in msg.items() if k != "type"}
            if not self.files_custom:
                # follow the editor's overrides until the list is changed here
                self.override_files = list(self.sim_info.get("overrides") or [])
            self.draw_files()
            if not dpg.get_value(self.t("calib_local")).strip():
                dpg.set_value(self.t("calib_local"), self.sim_info.get("local_calib", ""))
            self.update_root_dir()
            self._files_changed()            # follows the simulation unless hand-edited
        elif kind == "log":
            self.append_log(msg.get("text", ""))
        elif kind == "conn_status":
            dpg.set_value(self.t("conn_status"), msg.get("text", ""))
            ok = msg.get("ok")
            if ok is not None:
                dpg.configure_item(self.t("conn_status"),
                                   color=self.tm.get_color("text_success" if ok
                                                           else "text_error"))

    def append_log(self, text: str) -> None:
        if dpg.does_item_exist(self.t("log")):
            stamp = time.strftime("%H:%M:%S")
            dpg.set_value(self.t("log"), dpg.get_value(self.t("log")) + f"[{stamp}] {text}\n")

    # ── form ──────────────────────────────────────────────────────────────────

    def update_root_dir(self, *_) -> None:
        t = self.t
        if dpg.get_value(t("calib_upload")):
            local = dpg.get_value(t("calib_local")).strip()
            parent = dpg.get_value(t("remote_calib_parent")).strip().rstrip("/")
            if local and parent:
                dpg.set_value(t("remote_root_dir"),
                              f"{parent}/{os.path.basename(os.path.normpath(local))}")
        self._show_root_dir()

    def _browse_calib(self, *_) -> None:
        tag = self.t("calib_dialog")
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)

        def chosen(sender, app_data):
            path = app_data.get("file_path_name", "")
            if path:
                dpg.set_value(self.t("calib_local"), path)
                self.update_root_dir()

        dpg.add_file_dialog(directory_selector=True, show=True, tag=tag, width=700,
                            height=400, callback=chosen,
                            default_path=dpg.get_value(self.t("calib_local")) or os.getcwd())

    def apply_preset(self, sender, name) -> None:
        t = self.t
        partition, qos, limit = slurm.PRESETS[name]
        dpg.set_value(t("partition"), partition)
        dpg.set_value(t("qos"), qos)
        dpg.set_value(t("time"), limit)
        if partition.startswith("dcgp"):
            dpg.set_value(t("gpus_per_node"), 0)
            dpg.set_value(t("cpu"), True)
        self.generate_script()

    def options(self) -> slurm.SlurmOptions:
        g = dpg.get_value
        t = self.t
        return slurm.SlurmOptions(
            job_name=g(t("job_name")).strip(),
            account=g(t("account")).strip(),
            partition=g(t("partition")),
            qos=g(t("qos")),
            time=g(t("time")).strip(),
            nodes=int(g(t("nodes"))),
            ntasks_per_node=int(g(t("ntasks_per_node"))),
            cpus_per_task=int(g(t("cpus_per_task"))),
            gpus_per_node=int(g(t("gpus_per_node"))),
            modules=[m.strip() for m in g(t("modules")).splitlines() if m.strip()],
            setup=g(t("setup")),
            extra_sbatch=g(t("extra_sbatch")),
            cpu=bool(g(t("cpu"))),
            precision=int(g(t("precision"))),
            nsimul=int(g(t("nsimul"))),
            diagram=bool(g(t("diagram"))),
            extra_args=g(t("extra_args")),
        )

    def generate_script(self, *_) -> None:
        opts = self.options()
        self.generated_script = slurm.render_script(opts, self.run_files())
        dpg.set_value(self.t("script"), self.generated_script)
        for err in slurm.validate(opts):
            self.append_log(f"Check: {err}")

    def profile_from_ui(self) -> dict:
        g = dpg.get_value
        t = self.t
        return {
            "login_host": g(t("login_host")).strip(),
            "transfer_host": g(t("transfer_host")).strip(),
            "remote_base": g(t("remote_base")).strip(),
            "step_email": g(t("step_email")).strip(),
            "calib_upload": bool(g(t("calib_upload"))),
            "calib_local": g(t("calib_local")).strip(),
            "remote_calib_parent": g(t("remote_calib_parent")).strip(),
            "remote_root_dir": g(t("remote_root_dir")).strip(),
            "path_map": g(t("path_map")),
            "download": bool(g(t("download"))),
            "download_dir": g(t("download_dir")).strip(),
            "poll_interval": self.profile.get("poll_interval", 60),
            "slurm": self.options().to_dict(),
        }

    # ── commands ──────────────────────────────────────────────────────────────

    def _on_check_connection(self, *_) -> None:
        dpg.set_value(self.t("conn_status"), "connecting…")
        self.command({"type": "check_connection", "profile": self.profile_from_ui(),
                      "setup": dpg.get_value(self.t("setup"))})

    def _on_step_login(self, *_) -> None:
        email = dpg.get_value(self.t("step_email")).strip()
        if not email:
            self.append_log("Enter the e-mail of your CINECA account first")
            return
        self.command({"type": "step_login", "email": email,
                      "profile": self.profile_from_ui(),
                      "setup": dpg.get_value(self.t("setup"))})

    def _on_submit(self, *_) -> None:
        opts = self.options()
        errors = slurm.validate(opts)
        if errors:
            for err in errors:
                self.append_log(f"Cannot submit: {err}")
            return
        missing = self.missing_files()
        if missing:
            for path in missing:
                self.append_log(f"Cannot submit: simulation file not found: {path or '(empty)'}")
            return
        script = dpg.get_value(self.t("script"))
        if script == self.generated_script:
            script = None          # not edited by hand: generated from the current state
        else:
            self.append_log("Submitting the hand-edited script (its specula line must use "
                            f"the job's file names: {' '.join(self.run_files())})")
        self.command({"type": "submit", "profile": self.profile_from_ui(),
                      "options": opts.to_dict(), "script": script,
                      "files": {"base": self.base_file,
                                "overrides": list(self.override_files)}})

    def _on_save_profile(self, *_) -> None:
        self.command({"type": "save_profile", "profile": self.profile_from_ui()})


class HpcJobsView:
    """Table of the submitted jobs, with their output in a sub-window."""

    def __init__(self, command, theme_manager, jobs=(), prefix: str = "hpc"):
        self.command = command
        self.tm = theme_manager
        self.p = prefix
        self.jobs = [hpc.HpcJob.from_dict(d) for d in jobs]

    def t(self, name: str) -> str:
        return f"{self.p}_{name}"

    def build(self, parent) -> None:
        with dpg.group(parent=parent):
            with dpg.group(horizontal=True):
                dpg.add_button(label="Refresh now",
                               callback=lambda: self.command({"type": "refresh_jobs"}))
                dpg.add_text("States are refreshed every minute while jobs are running.",
                             color=self.tm.get_color("text_hint"))
            dpg.add_child_window(tag=self.t("jobs_list"), border=False)
        self._draw()

    def apply(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "jobs":
            self.jobs = [hpc.HpcJob.from_dict(d) for d in msg.get("jobs", [])]
            self._draw()
        elif kind == "job_output":
            text_tag = self._output_window(str(msg.get("job_id", "")))
            dpg.set_value(text_tag, msg.get("text", ""))

    def _draw(self) -> None:
        parent = self.t("jobs_list")
        dpg.delete_item(parent, children_only=True)
        if not self.jobs:
            dpg.add_text("No jobs submitted yet.", parent=parent)
            return
        tm = self.tm
        with dpg.table(parent=parent, header_row=True, borders_innerH=True, resizable=True):
            for col in ("Job", "Name", "State", "Elapsed", "Info", "Submitted", ""):
                dpg.add_table_column(label=col)
            for job in self.jobs:
                with dpg.table_row():
                    dpg.add_text(job.job_id)
                    dpg.add_text(job.name)
                    color = ("text_success" if job.state == "COMPLETED"
                             else "text_error" if job.finished
                             else "monitor_status_waiting")
                    dpg.add_text(job.state, color=tm.get_color(color))
                    dpg.add_text(job.elapsed)
                    info = job.last_error or job.reason or (
                        f"exit {job.exit_code}" if job.exit_code else "")
                    if job.downloaded:
                        info = (info + "; " if info else "") + "results downloaded"
                    dpg.add_text(info[:80])
                    dpg.add_text(time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(job.submitted)))
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="Output", user_data=job.job_id,
                                       callback=lambda s, a, u: self.request_output(u))
                        if not job.finished:
                            self._job_button("Cancel", "cancel_job", job.job_id)
                        self._job_button("Download", "download_job", job.job_id)
                        self._job_button("Forget", "forget_job", job.job_id)

    def _job_button(self, label: str, command: str, job_id: str) -> None:
        dpg.add_button(label=label, user_data=job_id,
                       callback=lambda s, a, u: self.command({"type": command, "job_id": u}))

    def request_output(self, job_id: str) -> None:
        """Open the output window of *job_id* and ask the editor for the output."""
        dpg.set_value(self._output_window(job_id), "Fetching the job output…")
        self.command({"type": "job_output", "job_id": job_id})

    def _output_window(self, job_id: str) -> str:
        tag = self.t(f"out_{job_id}")
        if not dpg.does_item_exist(tag):
            with dpg.window(label=f"Job {job_id} output", tag=tag, width=900, height=520):
                dpg.add_button(label="Refresh", callback=lambda: self.request_output(job_id))
                dpg.add_input_text(tag=f"{tag}_text", multiline=True, readonly=True,
                                   width=-1, height=-1)
        dpg.show_item(tag)
        dpg.focus_item(tag)
        return f"{tag}_text"
