"""
HPC menu: submit the current simulation as a SLURM batch job and follow it.

Mixin of :class:`specula_studio.app.SpeculaEditor`.  All ssh/scp work runs
in background threads; the jobs table is redrawn on the GUI thread
(``_poll_hpc_ui`` is called every frame and redraws only on changes).

Batch jobs run without a DisplayServer: monitors are not available for them;
results are downloaded when the job has finished.
"""

import logging
import os
import pathlib
import subprocess
import threading
import time

import dearpygui.dearpygui as dpg

from .. import hpc, sshutil
from ..core import slurm, ssh_config

log = logging.getLogger(__name__)

_WIN = "hpc_window"
_JOBS_WIN = "hpc_jobs_window"
_DEFAULT_PROFILE = {
    "login_host": "login.leonardo.cineca.it",
    "transfer_host": "",
    "remote_base": "$WORK/specula_studio",
    "step_email": "",
    "calib_upload": False,
    "calib_local": "",
    "remote_calib_parent": "$WORK/specula_studio/calib",
    "remote_root_dir": "",
    "download": True,
    "download_dir": str(pathlib.Path.home() / "specula_hpc_results"),
    "poll_interval": 60,
    "slurm": slurm.SlurmOptions().to_dict(),
}


def _t(name: str) -> str:
    return f"hpc_{name}"


class HpcMixin:
    # ── state ─────────────────────────────────────────────────────────────────

    def _init_hpc_state(self):
        self._hpc_jobs = [hpc.HpcJob.from_dict(d)
                          for d in self.preferences.get("hpc_jobs", []) or []]
        self._hpc_lock = threading.Lock()
        self._hpc_jobs_version = 0
        self._hpc_drawn_version = -1
        self._hpc_poll_stop = threading.Event()
        self._hpc_poll_thread = None
        self._hpc_busy = False
        self._hpc_env: dict = {}
        if any(not j.finished or (j.download_to and not j.downloaded)
               for j in self._hpc_jobs):
            self._start_hpc_poller()

    def _hpc_profile(self) -> dict:
        prof = dict(_DEFAULT_PROFILE)
        prof.update(self.preferences.get("hpc", {}) or {})
        prof["slurm"] = {**_DEFAULT_PROFILE["slurm"], **(prof.get("slurm") or {})}
        return prof

    def _save_hpc_jobs(self):
        with self._hpc_lock:
            self.preferences["hpc_jobs"] = [j.to_dict() for j in self._hpc_jobs]
            self._hpc_jobs_version += 1
        self._save_settings()

    # ── HPC submit window ─────────────────────────────────────────────────────

    def _show_hpc_window(self, *_):
        if dpg.does_item_exist(_WIN):
            dpg.show_item(_WIN)
            dpg.focus_item(_WIN)
            self._hpc_refresh_sim_info()
            return
        prof = self._hpc_profile()
        opts = slurm.SlurmOptions.from_dict(prof["slurm"])
        tm = self.theme_manager
        hint = tm.get_color("text_hint")
        head = tm.get_color("control_subsection_header")
        hosts = [h.alias for h in ssh_config.list_ssh_hosts()]
        w = -220

        with dpg.window(label="HPC Batch Job (SLURM)", tag=_WIN, width=860, height=900):
            dpg.add_text("", tag=_t("sim_info"))
            dpg.add_text("Batch jobs run without a display server: live monitors are "
                         "not available; results are downloaded when the job ends.",
                         color=hint, wrap=820)

            # ── cluster ──────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Cluster", color=head)
            if hosts:
                dpg.add_combo(hosts, label="SSH config hosts", width=w,
                              callback=lambda s, a: dpg.set_value(_t("login_host"), a))
            dpg.add_input_text(label="Login host", tag=_t("login_host"), width=w,
                               default_value=prof["login_host"],
                               hint="user@login.leonardo.cineca.it or an ssh alias")
            dpg.add_input_text(label="Transfer host (optional)", tag=_t("transfer_host"),
                               width=w, default_value=prof["transfer_host"],
                               hint="e.g. user@data.leonardo.cineca.it for large data")
            with dpg.group(horizontal=True):
                dpg.add_button(label="Check connection", callback=self._hpc_check_connection)
                dpg.add_text("not checked", tag=_t("conn_status"), color=hint)
            with dpg.group(horizontal=True):
                dpg.add_input_text(tag=_t("step_email"), width=260,
                                   default_value=prof["step_email"],
                                   hint="CINECA account e-mail")
                dpg.add_button(label="Log in (2FA, step)", callback=self._hpc_step_login)
            dpg.add_text(f"Leonardo needs a 12-hour certificate: step ssh login <email> "
                         f"--provisioner {hpc.STEP_PROVISIONER} (opens the browser).",
                         color=hint, wrap=820)
            dpg.add_input_text(label="Remote working directory", tag=_t("remote_base"),
                               width=w, default_value=prof["remote_base"])

            # ── data ─────────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Calibration data", color=head)
            dpg.add_checkbox(label="Upload calibration data before submitting",
                             tag=_t("calib_upload"), default_value=prof["calib_upload"],
                             callback=self._hpc_update_root_dir)
            with dpg.group(horizontal=True):
                dpg.add_input_text(label="", tag=_t("calib_local"), width=w + 120,
                                   default_value=prof["calib_local"] or self._hpc_local_calib(),
                                   callback=self._hpc_update_root_dir)
                dpg.add_text("Local directory")
                dpg.add_button(label="...", callback=self._hpc_browse_calib)
            dpg.add_input_text(label="Remote parent directory", tag=_t("remote_calib_parent"),
                               width=w, default_value=prof["remote_calib_parent"],
                               callback=self._hpc_update_root_dir)
            dpg.add_input_text(label="root_dir on the cluster", tag=_t("remote_root_dir"),
                               width=w, default_value=prof["remote_root_dir"],
                               hint="empty = keep the simulation's root_dir")
            dpg.add_text("SimulParams.root_dir is set to this directory for the job only.",
                         color=hint)

            # ── SLURM ────────────────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("SLURM job", color=head)
            dpg.add_combo(list(slurm.PRESETS), label="Preset", width=w,
                          callback=self._hpc_apply_preset)
            dpg.add_input_text(label="Job name", tag=_t("job_name"), width=w,
                               default_value=opts.job_name)
            dpg.add_input_text(label="Account (--account)", tag=_t("account"), width=w,
                               default_value=opts.account, hint="e.g. try25_rossi")
            dpg.add_combo(slurm.PARTITIONS, label="Partition", tag=_t("partition"), width=w,
                          default_value=opts.partition)
            dpg.add_combo(slurm.QOS_CHOICES, label="QOS", tag=_t("qos"), width=w,
                          default_value=opts.qos)
            dpg.add_input_text(label="Time limit", tag=_t("time"), width=w,
                               default_value=opts.time, hint="HH:MM:SS or D-HH:MM:SS")
            with dpg.group(horizontal=True):
                for name, label in (("nodes", "Nodes"), ("ntasks_per_node", "Tasks/node"),
                                    ("cpus_per_task", "CPUs/task"),
                                    ("gpus_per_node", "GPUs/node")):
                    dpg.add_input_int(label=label, tag=_t(name), width=90,
                                      default_value=int(getattr(opts, name)), min_value=0,
                                      min_clamped=True)
            dpg.add_text("More than one task in total runs SPECULA with --mpi "
                         "(objects distributed with target_rank).", color=hint)
            dpg.add_input_text(label="Modules", tag=_t("modules"), multiline=True,
                               width=w, height=60, default_value="\n".join(opts.modules))
            dpg.add_input_text(label="Environment setup", tag=_t("setup"), multiline=True,
                               width=w, height=50, default_value=opts.setup,
                               hint="e.g. source $WORK/venv/bin/activate")
            dpg.add_input_text(label="Extra #SBATCH options", tag=_t("extra_sbatch"),
                               multiline=True, width=w, height=36,
                               default_value=opts.extra_sbatch, hint="--mem=100G")
            with dpg.group(horizontal=True):
                dpg.add_checkbox(label="CPU only (--cpu)", tag=_t("cpu"),
                                 default_value=opts.cpu)
                dpg.add_combo(["0", "1"], label="Precision", tag=_t("precision"), width=50,
                              default_value=str(opts.precision))
                dpg.add_input_int(label="N-simul", tag=_t("nsimul"), width=90,
                                  default_value=int(opts.nsimul), min_value=1,
                                  min_clamped=True)
                dpg.add_checkbox(label="Diagram", tag=_t("diagram"),
                                 default_value=opts.diagram)
            dpg.add_input_text(label="Extra specula arguments", tag=_t("extra_args"),
                               width=w, default_value=opts.extra_args)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Generate script", callback=self._hpc_generate_script)
                dpg.add_text("The script can be edited before submitting.", color=hint)
            dpg.add_input_text(tag=_t("script"), multiline=True, width=-1, height=220,
                               tab_input=True)

            # ── results / submit ─────────────────────────────────────────────
            dpg.add_separator()
            dpg.add_text("Results", color=head)
            dpg.add_checkbox(label="Download results when the job has finished",
                             tag=_t("download"), default_value=prof["download"])
            dpg.add_input_text(label="Local results directory", tag=_t("download_dir"),
                               width=w, default_value=prof["download_dir"])
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="SUBMIT JOB", width=160, height=32,
                               callback=self._hpc_submit)
                dpg.add_button(label="Save settings", callback=self._hpc_save_profile)
                dpg.add_button(label="Jobs...", callback=self._show_hpc_jobs_window)
            dpg.add_input_text(tag=_t("log"), multiline=True, readonly=True, width=-1,
                               height=140)
        self._hpc_refresh_sim_info()
        self._hpc_update_root_dir()
        self._hpc_generate_script()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _hpc_log(self, text: str):
        log.info("HPC: %s", text)
        if dpg.does_item_exist(_t("log")):
            stamp = time.strftime("%H:%M:%S")
            dpg.set_value(_t("log"), dpg.get_value(_t("log")) + f"[{stamp}] {text}\n")

    def _hpc_refresh_sim_info(self):
        if not dpg.does_item_exist(_t("sim_info")):
            return
        name = self.current_simulation_name or "(unsaved simulation)"
        om = self.override_manager
        n_ov = len(om.get_enabled_overrides()) if om.any_enabled() else 0
        dpg.set_value(_t("sim_info"), f"Simulation: {name}"
                      + (f"   ({n_ov} override file(s) will be passed too)" if n_ov else ""))

    def _hpc_local_calib(self) -> str:
        """SimulParams.root_dir of the simulation, resolved on this machine."""
        for node in self.graph.nodes.values():
            if node.get("type") == "SimulParams":
                root = node.get("values", {}).get("root_dir")
                if root:
                    base = (os.path.dirname(self.current_simulation_path)
                            if self.current_simulation_path else os.getcwd())
                    path = root if os.path.isabs(root) else os.path.join(base, root)
                    return os.path.normpath(path)
        return ""

    def _hpc_update_root_dir(self, *_):
        if not dpg.does_item_exist(_t("remote_root_dir")):
            return
        if dpg.get_value(_t("calib_upload")):
            local = dpg.get_value(_t("calib_local")).strip()
            parent = dpg.get_value(_t("remote_calib_parent")).strip().rstrip("/")
            if local and parent:
                dpg.set_value(_t("remote_root_dir"),
                              f"{parent}/{os.path.basename(os.path.normpath(local))}")

    def _hpc_browse_calib(self, *_):
        tag = _t("calib_dialog")
        if dpg.does_item_exist(tag):
            dpg.delete_item(tag)

        def chosen(sender, app_data):
            path = app_data.get("file_path_name", "")
            if path:
                dpg.set_value(_t("calib_local"), path)
                self._hpc_update_root_dir()

        dpg.add_file_dialog(directory_selector=True, show=True, tag=tag, width=700,
                            height=400, callback=chosen,
                            default_path=dpg.get_value(_t("calib_local")) or os.getcwd())

    def _hpc_apply_preset(self, sender, name):
        partition, qos, limit = slurm.PRESETS[name]
        dpg.set_value(_t("partition"), partition)
        dpg.set_value(_t("qos"), qos)
        dpg.set_value(_t("time"), limit)
        if partition.startswith("dcgp"):
            dpg.set_value(_t("gpus_per_node"), 0)
            dpg.set_value(_t("cpu"), True)
        self._hpc_generate_script()

    def _hpc_options(self) -> slurm.SlurmOptions:
        g = dpg.get_value
        return slurm.SlurmOptions(
            job_name=g(_t("job_name")).strip(),
            account=g(_t("account")).strip(),
            partition=g(_t("partition")),
            qos=g(_t("qos")),
            time=g(_t("time")).strip(),
            nodes=int(g(_t("nodes"))),
            ntasks_per_node=int(g(_t("ntasks_per_node"))),
            cpus_per_task=int(g(_t("cpus_per_task"))),
            gpus_per_node=int(g(_t("gpus_per_node"))),
            modules=[m.strip() for m in g(_t("modules")).splitlines() if m.strip()],
            setup=g(_t("setup")),
            extra_sbatch=g(_t("extra_sbatch")),
            cpu=bool(g(_t("cpu"))),
            precision=int(g(_t("precision"))),
            nsimul=int(g(_t("nsimul"))),
            diagram=bool(g(_t("diagram"))),
            extra_args=g(_t("extra_args")),
        )

    def _hpc_run_files(self) -> list[str]:
        """Names of the files passed to specula (as in prepare_job_files)."""
        name = self.current_simulation_name or "untitled"
        om = self.override_manager
        overrides = om.get_enabled_overrides() if om.any_enabled() else []
        files = [f"{name}_simul.yml"]
        files += [f"override{i}_{os.path.basename(p)}" for i, p in enumerate(overrides, 1)]
        return [*files, slurm.HPC_OVERRIDE_NAME]

    def _hpc_generate_script(self, *_):
        if not dpg.does_item_exist(_t("script")):
            return
        opts = self._hpc_options()
        self._hpc_generated_script = slurm.render_script(opts, self._hpc_run_files())
        dpg.set_value(_t("script"), self._hpc_generated_script)
        for err in slurm.validate(opts):
            self._hpc_log(f"Check: {err}")

    def _hpc_profile_from_ui(self) -> dict:
        g = dpg.get_value
        return {
            "login_host": g(_t("login_host")).strip(),
            "transfer_host": g(_t("transfer_host")).strip(),
            "remote_base": g(_t("remote_base")).strip(),
            "step_email": g(_t("step_email")).strip(),
            "calib_upload": bool(g(_t("calib_upload"))),
            "calib_local": g(_t("calib_local")).strip(),
            "remote_calib_parent": g(_t("remote_calib_parent")).strip(),
            "remote_root_dir": g(_t("remote_root_dir")).strip(),
            "download": bool(g(_t("download"))),
            "download_dir": g(_t("download_dir")).strip(),
            "poll_interval": self._hpc_profile().get("poll_interval", 60),
            "slurm": self._hpc_options().to_dict(),
        }

    def _hpc_save_profile(self, *_):
        self.preferences["hpc"] = self._hpc_profile_from_ui()
        self._save_settings()
        self._hpc_log("Settings saved")

    def _hpc_background(self, target, *args):
        if self._hpc_busy:
            self._hpc_log("Busy: wait for the current operation to finish")
            return
        self._hpc_busy = True

        def run():
            try:
                target(*args)
            except Exception as exc:          # report, never kill the GUI
                log.exception("HPC operation failed")
                self._hpc_log(f"Error: {exc}")
            finally:
                self._hpc_busy = False

        threading.Thread(target=run, daemon=True, name="hpc-worker").start()

    # ── connection / login ────────────────────────────────────────────────────

    def _hpc_check_connection(self, *_):
        prof = self._hpc_profile_from_ui()
        setup = dpg.get_value(_t("setup"))

        def work():
            dpg.set_value(_t("conn_status"), "connecting…")
            client = hpc.HpcClient(prof["login_host"], prof["transfer_host"])
            info = client.check_connection(setup)
            color = self.theme_manager.get_color("text_success" if info["ok"] else "text_error")
            dpg.set_value(_t("conn_status"), "OK" if info["ok"] else "FAILED")
            dpg.configure_item(_t("conn_status"), color=color)
            self._hpc_log(info["message"])
            if info["ok"]:
                self._hpc_env = info["env"]
                self._hpc_log(f"specula on the cluster: {info['specula']}" if info["specula"]
                              else "specula NOT found in the job environment: add the "
                                   "environment setup lines (venv / conda activation)")
                if prof["transfer_host"] != prof["login_host"] and prof["transfer_host"]:
                    res = client._run(["ssh", *sshutil.SSH_OPTIONS, client.transfer_host,
                                       "true"], timeout=60)
                    # data movers have no shell: a refused command still proves login
                    msg = "reachable" if res.ok or "permission denied" not in \
                        res.stderr.lower() else hpc.explain_ssh_failure(res)
                    self._hpc_log(f"Transfer host {client.transfer_host}: {msg}")

        self._hpc_background(work)

    def _hpc_step_login(self, *_):
        email = dpg.get_value(_t("step_email")).strip()
        if not email:
            self._hpc_log("Enter the e-mail of your CINECA account first")
            return
        if not hpc.step_available():
            self._hpc_log("The smallstep client 'step' is not installed "
                          "(Windows: winget install Smallstep.step; see the CINECA docs)")
            return

        def work():
            self._hpc_log("Starting 'step ssh login' — complete the login in the browser…")
            res = sshutil.run_command(hpc.step_login_command(email), timeout=600)
            if res.ok:
                self._hpc_log("Certificate obtained; checking the connection…")
                self._hpc_busy = False
                self._hpc_check_connection()
            else:
                self._hpc_log(f"step login failed: {(res.stderr or res.stdout).strip()}")

        self._hpc_background(work)

    # ── submission ────────────────────────────────────────────────────────────

    def _hpc_submit(self, *_):
        prof = self._hpc_profile_from_ui()
        opts = self._hpc_options()
        errors = slurm.validate(opts)
        if errors:
            for err in errors:
                self._hpc_log(f"Cannot submit: {err}")
            return
        base, overrides = self.sim_control._run_inputs()
        if not base:
            self._hpc_log("The simulation is empty")
            return
        script = dpg.get_value(_t("script"))
        if script == getattr(self, "_hpc_generated_script", None):
            script = None          # not edited by hand: generate from the current state
        else:
            self._hpc_log("Submitting the hand-edited script")
        req = hpc.SubmitRequest(
            login_host=prof["login_host"],
            transfer_host=prof["transfer_host"],
            remote_base=prof["remote_base"] or "$WORK/specula_studio",
            sim_name=self.current_simulation_name or "untitled",
            base=base,
            override_paths=overrides,
            options=opts,
            calib_local_dir=prof["calib_local"] if prof["calib_upload"] else "",
            remote_root_dir=prof["remote_root_dir"],
            download_to=prof["download_dir"] if prof["download"] else "",
            script_text=script,
        )
        self.preferences["hpc"] = prof
        self._save_settings()

        def work():
            job = hpc.submit(req, progress=self._hpc_log)
            with self._hpc_lock:
                self._hpc_jobs.insert(0, job)
            self._save_hpc_jobs()
            self._hpc_log(f"Job {job.job_id} in {job.job_dir}")
            self._start_hpc_poller()

        self._hpc_background(work)

    # ── job monitoring ────────────────────────────────────────────────────────

    def _start_hpc_poller(self):
        if self._hpc_poll_thread is not None and self._hpc_poll_thread.is_alive():
            return
        self._hpc_poll_stop.clear()
        self._hpc_poll_thread = threading.Thread(target=self._hpc_poll_loop, daemon=True,
                                                 name="hpc-poller")
        self._hpc_poll_thread.start()

    def _hpc_poll_loop(self):
        interval = max(15, int(self._hpc_profile().get("poll_interval", 60)))
        while not self._hpc_poll_stop.is_set():
            pending = self._hpc_poll_once()
            if not pending:
                return                       # nothing left to follow
            self._hpc_poll_stop.wait(interval)

    def _hpc_poll_once(self) -> bool:
        """Refresh unfinished jobs, download finished ones; True if work remains."""
        with self._hpc_lock:
            jobs = list(self._hpc_jobs)
        remaining = False
        for job in jobs:
            if not job.finished:
                hpc.update_job(job)
                if job.finished:
                    log.info("HPC job %s finished: %s", job.job_id, job.state)
            if job.finished and job.download_to and not job.downloaded:
                try:
                    target = hpc.download_results(job, job.download_to)
                    log.info("HPC job %s results downloaded to %s", job.job_id, target)
                except hpc.HpcError as exc:
                    job.last_error = str(exc)
            remaining |= (not job.finished) or bool(job.download_to and not job.downloaded
                                                    and not job.last_error)
        self._save_hpc_jobs()
        return remaining

    def stop_hpc_poller(self):
        self._hpc_poll_stop.set()

    # ── jobs window ───────────────────────────────────────────────────────────

    def _show_hpc_jobs_window(self, *_):
        if not dpg.does_item_exist(_JOBS_WIN):
            with dpg.window(label="HPC Jobs", tag=_JOBS_WIN, width=980, height=360):
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Refresh now", callback=self._hpc_refresh_now)
                    dpg.add_text("States are refreshed every minute while jobs are "
                                 "running.", color=self.theme_manager.get_color("text_hint"))
                dpg.add_child_window(tag=_t("jobs_list"), border=False)
        dpg.show_item(_JOBS_WIN)
        dpg.focus_item(_JOBS_WIN)
        self._hpc_drawn_version = -1

    def _hpc_refresh_now(self, *_):
        self._hpc_background(self._hpc_poll_once)

    def _poll_hpc_ui(self):
        """Redraw the jobs table when the job list changed (GUI thread)."""
        if self._hpc_drawn_version == self._hpc_jobs_version:
            return
        if not dpg.does_item_exist(_t("jobs_list")):
            return
        self._hpc_drawn_version = self._hpc_jobs_version
        dpg.delete_item(_t("jobs_list"), children_only=True)
        with self._hpc_lock:
            jobs = list(self._hpc_jobs)
        if not jobs:
            dpg.add_text("No jobs submitted yet.", parent=_t("jobs_list"))
            return
        tm = self.theme_manager
        with dpg.table(parent=_t("jobs_list"), header_row=True, borders_innerH=True,
                       resizable=True):
            for col in ("Job", "Name", "State", "Elapsed", "Info", "Submitted", ""):
                dpg.add_table_column(label=col)
            for job in jobs:
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
                                       callback=lambda s, a, u: self._hpc_show_output(u))
                        if not job.finished:
                            dpg.add_button(label="Cancel", user_data=job.job_id,
                                           callback=lambda s, a, u: self._hpc_cancel(u))
                        dpg.add_button(label="Download", user_data=job.job_id,
                                       callback=lambda s, a, u: self._hpc_download(u))
                        dpg.add_button(label="Forget", user_data=job.job_id,
                                       callback=lambda s, a, u: self._hpc_forget(u))

    def _hpc_job(self, job_id: str):
        with self._hpc_lock:
            return next((j for j in self._hpc_jobs if j.job_id == job_id), None)

    def _hpc_show_output(self, job_id: str):
        job = self._hpc_job(job_id)
        if job is None:
            return
        tag = _t(f"out_{job_id}")
        if not dpg.does_item_exist(tag):
            with dpg.window(label=f"Job {job_id} output", tag=tag, width=900, height=520):
                dpg.add_button(label="Refresh", callback=lambda: fetch())
                dpg.add_input_text(tag=f"{tag}_text", multiline=True, readonly=True,
                                   width=-1, height=-1)

        def fetch():
            def work():
                client = job.client()
                parts = []
                for suffix in ("out", "err"):
                    path = f"{job.job_dir}/slurm-{job.job_id}.{suffix}"
                    try:
                        parts.append(f"==> {path} <==\n{client.tail(path)}")
                    except hpc.HpcError as exc:
                        parts.append(f"==> {path} <==\n{exc}")
                if dpg.does_item_exist(f"{tag}_text"):
                    dpg.set_value(f"{tag}_text", "\n".join(parts))
            threading.Thread(target=work, daemon=True).start()

        dpg.show_item(tag)
        fetch()

    def _hpc_cancel(self, job_id: str):
        job = self._hpc_job(job_id)
        if job is None:
            return

        def work():
            job.client().cancel(job.job_id)
            hpc.update_job(job)
            self._save_hpc_jobs()

        threading.Thread(target=self._hpc_safe, args=(work,), daemon=True).start()

    def _hpc_download(self, job_id: str):
        job = self._hpc_job(job_id)
        if job is None:
            return
        local = job.download_to or self._hpc_profile()["download_dir"]

        def work():
            target = hpc.download_results(job, local)
            log.info("Results of job %s downloaded to %s", job.job_id, target)
            self._save_hpc_jobs()
            if os.name == "nt":
                subprocess.Popen(["explorer", target])

        threading.Thread(target=self._hpc_safe, args=(work,), daemon=True).start()

    def _hpc_forget(self, job_id: str):
        with self._hpc_lock:
            self._hpc_jobs = [j for j in self._hpc_jobs if j.job_id != job_id]
        self._save_hpc_jobs()

    def _hpc_safe(self, fn):
        try:
            fn()
        except Exception as exc:
            log.error("HPC: %s", exc)
            with self._hpc_lock:
                self._hpc_jobs_version += 1
