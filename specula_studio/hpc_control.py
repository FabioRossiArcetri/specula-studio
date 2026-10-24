"""
HPC menu: submit the current simulation as a SLURM batch job and follow it.

:class:`HpcControl` is the editor side of the two HPC windows (*Submit Batch
Job* and *Batch Jobs*, see :mod:`.ui.hpc_views`).  Each is shown in its own
OS window — a separate process linked by :mod:`.control_link` — or, with the
preference *hpc_windows* = "docked" (or when that fails), as a window inside
the editor.  Either way the views only send commands; everything else stays
here: ssh/scp in background threads, the list of submitted jobs and its
polling (which goes on with the windows closed), and the settings file.

Background threads never touch DearPyGui: they queue events, and
:meth:`HpcControl.poll` (GUI thread, every frame) delivers them to the
windows.

Batch jobs run without a DisplayServer: monitors are not available for them;
results are downloaded when the job has finished.
"""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import subprocess
import threading
import time

import dearpygui.dearpygui as dpg

from . import hpc, sshutil
from .control_link import ControlServer, WindowProcess
from .core import slurm, yaml_io
from .ui.hpc_views import HpcJobsView, HpcSubmitView

log = logging.getLogger(__name__)

DEFAULT_PROFILE = {
    "login_host": "login.leonardo.cineca.it",
    "transfer_host": "",
    "remote_base": "$WORK/specula_studio",
    "step_email": "",
    "calib_upload": False,
    "calib_local": "",
    "remote_calib_parent": "$WORK",
    "remote_root_dir": "",
    "download": True,
    "download_dir": str(pathlib.Path.home() / "specula_hpc_results"),
    "poll_interval": 60,
    # data mirrored on the cluster, below $WORK (see slurm.map_path)
    "path_map": slurm.DEFAULT_PATH_MAP,
    # SPECULA installed in a virtual environment in $WORK
    "slurm": {**slurm.SlurmOptions().to_dict(),
              "setup": "source $WORK/specula/bin/activate"},
}

SUBMIT, JOBS = "submit", "jobs"
# window -> (process kind, title, docked window tag, docked size)
_WINDOWS = {
    SUBMIT: ("hpc_submit", "HPC Batch Job (SLURM)", "hpc_window", (860, 900)),
    JOBS:   ("hpc_jobs", "HPC Jobs", "hpc_jobs_window", (980, 640)),
}
_SIM_INFO_INTERVAL = 0.5        # seconds between checks of the simulation shown
# job states without output to show yet
_WAITING_STATES = ("", "PENDING", "CONFIGURING", "REQUEUED", "SUSPENDED")


class HpcControl:
    def __init__(self, editor):
        self.editor = editor
        self.jobs = [hpc.HpcJob.from_dict(d)
                     for d in editor.preferences.get("hpc_jobs", []) or []]
        self._lock = threading.Lock()
        self._jobs_version = 0
        self._sent_jobs_version = -1
        self._events: queue.Queue = queue.Queue()      # (window, event)
        self._docked: dict = {SUBMIT: None, JOBS: None}  # in-editor views
        self._links: dict = {SUBMIT: None, JOBS: None}   # ControlServer per window
        self._procs: dict = {SUBMIT: None, JOBS: None}   # WindowProcess per window
        self._sim_info: dict = {}
        self._sim_info_checked = 0.0
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._busy = False
        self._outputs: dict = {}               # job id -> last SLURM log text read
        self._selected_job = ""                # job shown in the jobs window's output
        self._open_job = ""                    # set after a submit: show it (GUI thread)
        if any(not j.finished or (j.download_to and not j.downloaded) for j in self.jobs):
            self._start_poller()

    # ── settings ──────────────────────────────────────────────────────────────

    def profile(self) -> dict:
        prof = dict(DEFAULT_PROFILE)
        prof.update(self.editor.preferences.get("hpc", {}) or {})
        prof["slurm"] = {**DEFAULT_PROFILE["slurm"], **(prof.get("slurm") or {})}
        return prof

    def _save_profile(self, profile: dict) -> None:
        self.editor.preferences["hpc"] = profile
        self.editor._save_settings()

    def _save_jobs(self) -> None:
        with self._lock:
            self.editor.preferences["hpc_jobs"] = [j.to_dict() for j in self.jobs]
            self._jobs_version += 1
        self.editor._save_settings()

    def _jobs_dicts(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self.jobs]

    # ── the simulation shown in the submit window ─────────────────────────────

    def sim_info(self) -> dict:
        ed = self.editor
        om = ed.override_manager
        overrides = om.get_enabled_overrides() if om.any_enabled() else []
        name = ed.current_simulation_name or ""
        simul_params, root = self._simul_params()
        return {"name": name, "n_overrides": len(overrides),
                "overrides": [str(p) for p in overrides],
                "run_files": slurm.run_file_names(name or "untitled", overrides),
                "simul_params": simul_params,
                "root_dir": root, "local_calib": self._local_calib(root)}

    def _simul_params(self) -> tuple[str, str]:
        """(name, root_dir as written) of the simulation's SimulParams object."""
        for node in self.editor.graph.nodes.values():
            if node.get("type") == "SimulParams":
                return (str(node.get("name") or "main"),
                        str(node.get("values", {}).get("root_dir") or ""))
        return "main", ""

    def _local_calib(self, root: str) -> str:
        """*root* (the simulation's root_dir) resolved on this machine."""
        if not root:
            return ""
        ed = self.editor
        base = (os.path.dirname(ed.current_simulation_path)
                if ed.current_simulation_path else os.getcwd())
        path = root if os.path.isabs(root) else os.path.join(base, root)
        return os.path.normpath(path)

    # ── windows ───────────────────────────────────────────────────────────────

    def show_submit_window(self, *_):
        self._show(SUBMIT)

    def show_jobs_window(self, *_):
        self._show(JOBS)

    def _show(self, window: str) -> None:
        """Open *window* in its own OS window (or inside the editor, if so chosen)."""
        if self.editor.preferences.get("hpc_windows", "window") == "docked":
            self.show_docked(window)
            return
        try:
            if self._links[window] is None:
                link = ControlServer(on_connect=lambda w=window: self._on_window_connected(w))
                kind, title, _, _ = _WINDOWS[window]
                self._links[window] = link
                self._procs[window] = WindowProcess(link, kind, f"SPECULA Studio - {title}")
            self._procs[window].launch()
        except Exception as exc:
            log.warning("Cannot open the HPC window in its own window (%s); "
                        "showing it inside the editor", exc)
            self.show_docked(window)

    def _init_event(self, window: str) -> dict:
        if window == SUBMIT:
            self._sim_info = self.sim_info()
            return {"type": "init", "profile": self.profile(), "sim_info": self._sim_info}
        with self._lock:
            outputs = dict(self._outputs)
        return {"type": "init", "jobs": self._jobs_dicts(), "outputs": outputs,
                "selected": self._selected_job}

    def _on_window_connected(self, window: str) -> None:
        link = self._links[window]
        if link is not None:
            link.send(self._init_event(window))

    def show_docked(self, window: str) -> None:
        """Show *window* inside the editor."""
        _, title, tag, (width, height) = _WINDOWS[window]
        if dpg.does_item_exist(tag):
            dpg.show_item(tag)
            dpg.focus_item(tag)
            if window == SUBMIT:
                self._emit(SUBMIT, {"type": "sim_info", **self.sim_info()})
            return
        tm = self.editor.theme_manager
        with dpg.window(label=title, tag=tag, width=width, height=height):
            pass
        init = self._init_event(window)
        view: HpcSubmitView | HpcJobsView
        if window == SUBMIT:
            view = HpcSubmitView(self.handle_command, tm, init["profile"], init["sim_info"])
        else:
            view = HpcJobsView(self.handle_command, tm, init["jobs"], init["outputs"],
                               init["selected"])
        view.build(tag)
        self._docked[window] = view

    def _emit(self, window: str, event: dict) -> None:
        """Queue *event* for *window* (any thread; delivered by :meth:`poll`)."""
        self._events.put((window, event))

    def _deliver(self, window: str, event: dict) -> None:
        view = self._docked[window]
        if view is not None and dpg.does_item_exist(_WINDOWS[window][2]):
            try:
                view.apply(event)
            except Exception:
                log.exception("HPC window update failed")
        link = self._links[window]
        if link is not None:
            link.send(event)

    def poll(self) -> None:
        """GUI thread, every frame: run window commands, deliver events."""
        for link in self._links.values():
            if link is None:
                continue
            while True:
                try:
                    msg = link.commands.get_nowait()
                except queue.Empty:
                    break
                self.handle_command(msg)
        while True:
            try:
                window, event = self._events.get_nowait()
            except queue.Empty:
                break
            self._deliver(window, event)
        if self._sent_jobs_version != self._jobs_version:
            self._sent_jobs_version = self._jobs_version
            self._deliver(JOBS, {"type": "jobs", "jobs": self._jobs_dicts()})
        if self._open_job:
            # a job was just submitted: show it with its output
            self._selected_job, self._open_job = self._open_job, ""
            self._deliver(JOBS, {"type": "select_job", "job_id": self._selected_job})
            self.show_jobs_window()
        now = time.monotonic()
        if now - self._sim_info_checked > _SIM_INFO_INTERVAL:
            self._sim_info_checked = now
            info = self.sim_info()
            if info != self._sim_info:
                self._sim_info = info
                self._deliver(SUBMIT, {"type": "sim_info", **info})

    def shutdown(self) -> None:
        self._poll_stop.set()
        for window in (SUBMIT, JOBS):
            if self._procs[window] is not None:
                self._procs[window].stop()
            if self._links[window] is not None:
                self._links[window].close()
                self._links[window] = None

    # ── commands from the windows ─────────────────────────────────────────────

    def handle_command(self, msg: dict) -> None:
        kind = msg.get("type")
        try:
            if kind == "check_connection":
                self._check_connection(msg["profile"], msg.get("setup", ""))
            elif kind == "step_login":
                self._step_login(msg["email"], msg["profile"], msg.get("setup", ""))
            elif kind == "submit":
                self._submit(msg["profile"], slurm.SlurmOptions.from_dict(msg["options"]),
                             msg.get("script"), msg.get("files"))
            elif kind == "save_profile":
                self._save_profile(msg["profile"])
                self._log("Settings saved")
            elif kind == "show_jobs":
                self.show_jobs_window()
            elif kind == "refresh_jobs":
                self._background(self._poll_once)
            elif kind == "job_output":
                self._selected_job = str(msg["job_id"])
                self._job_output(self._selected_job)
            elif kind == "cancel_job":
                self._cancel(str(msg["job_id"]))
            elif kind == "download_job":
                self._download(str(msg["job_id"]))
            elif kind == "forget_job":
                self._forget(str(msg["job_id"]))
            else:
                log.warning("Unknown HPC command %r", kind)
        except Exception as exc:
            log.exception("HPC command %s failed", kind)
            self._log(f"Error: {exc}")

    def _log(self, text: str) -> None:
        log.info("HPC: %s", text)
        self._emit(SUBMIT, {"type": "log", "text": text})

    def _background(self, target, *args) -> None:
        if self._busy:
            self._log("Busy: wait for the current operation to finish")
            return
        self._busy = True

        def run():
            try:
                target(*args)
            except Exception as exc:          # report, never kill the GUI
                log.exception("HPC operation failed")
                self._log(f"Error: {exc}")
            finally:
                self._busy = False

        threading.Thread(target=run, daemon=True, name="hpc-worker").start()

    # ── connection / login ────────────────────────────────────────────────────

    def _check_connection(self, prof: dict, setup: str) -> None:
        self._emit(SUBMIT, {"type": "conn_status", "text": "connecting…", "ok": None})

        def work():
            client = hpc.HpcClient(prof["login_host"], prof["transfer_host"])
            info = client.check_connection(setup)
            self._emit(SUBMIT, {"type": "conn_status", "ok": bool(info["ok"]),
                                "text": "OK" if info["ok"] else "FAILED"})
            self._log(info["message"])
            if info["ok"]:
                self._log(f"specula on the cluster: {info['specula']}" if info["specula"]
                          else "specula NOT found in the job environment: add the "
                               "environment setup lines (venv / conda activation)")
                if info.get("specula_src"):
                    self._log(f"SPECULA imported from {info['specula_src']}")
                if prof["transfer_host"] != prof["login_host"] and prof["transfer_host"]:
                    res = client._run(["ssh", *sshutil.SSH_OPTIONS, client.transfer_host,
                                       "true"], timeout=60)
                    # data movers have no shell: a refused command still proves login
                    msg = "reachable" if res.ok or "permission denied" not in \
                        res.stderr.lower() else hpc.explain_ssh_failure(res)
                    self._log(f"Transfer host {client.transfer_host}: {msg}")

        self._background(work)

    def _step_login(self, email: str, prof: dict, setup: str) -> None:
        if not hpc.step_available():
            self._log("The smallstep client 'step' is not installed "
                      "(Windows: winget install Smallstep.step; see the CINECA docs)")
            return

        def work():
            self._log("Starting 'step ssh login' — complete the login in the browser…")
            res = sshutil.run_command(hpc.step_login_command(email), timeout=600)
            if res.ok:
                self._log("Certificate obtained; checking the connection…")
                self._busy = False
                self._check_connection(prof, setup)
            else:
                self._log(f"step login failed: {(res.stderr or res.stdout).strip()}")

        self._background(work)

    # ── submission ────────────────────────────────────────────────────────────

    def _job_inputs(self, files: dict | None) -> tuple[dict | None, list, str]:
        """(base simulation, override paths, name) of the job.

        *files* comes from the submit window: ``base`` is a YAML file, or
        empty for the simulation open in the editor; ``overrides`` the
        override files (missing = the editor's enabled ones)."""
        files = files or {}
        base_path = str(files.get("base") or "")
        if base_path:
            if not os.path.isfile(base_path):
                raise hpc.HpcError(f"Simulation file not found: {base_path}")
            base = yaml_io.read_yaml_file(base_path)
            if base is None:
                raise hpc.HpcError(f"Cannot read the simulation file {base_path}")
            overrides: list = []
            name = slurm.safe_name(pathlib.Path(base_path).stem)
        else:
            ed = self.editor
            base, overrides = ed.sim_control._run_inputs()
            name = slurm.safe_name(ed.current_simulation_name or "untitled")
        if "overrides" in files:
            overrides = [str(p) for p in files["overrides"]]
        for path in overrides:
            if not os.path.isfile(path):
                raise hpc.HpcError(f"Override file not found: {path}")
        return base, overrides, name

    def _submit(self, prof: dict, opts: slurm.SlurmOptions, script: str | None,
                files: dict | None = None) -> None:
        errors = slurm.validate(opts)
        if errors:
            for err in errors:
                self._log(f"Cannot submit: {err}")
            return
        try:
            base, overrides, sim_name = self._job_inputs(files)
        except hpc.HpcError as exc:
            self._log(f"Cannot submit: {exc}")
            return
        if not base:
            self._log("The simulation is empty")
            return
        req = hpc.SubmitRequest(
            login_host=prof["login_host"],
            transfer_host=prof["transfer_host"],
            remote_base=prof["remote_base"] or "$WORK/specula_studio",
            sim_name=sim_name,
            base=base,
            override_paths=overrides,
            options=opts,
            calib_local_dir=prof["calib_local"] if prof["calib_upload"] else "",
            remote_root_dir=prof["remote_root_dir"],
            download_to=prof["download_dir"] if prof["download"] else "",
            script_text=script,
            path_map=slurm.parse_path_map(prof.get("path_map", "")),
        )
        self._save_profile(prof)

        def work():
            job = hpc.submit(req, progress=self._log)
            with self._lock:
                self.jobs.insert(0, job)
            self._save_jobs()
            self._log(f"Job {job.job_id} in {job.job_dir}; its output is shown in "
                      "the HPC Jobs window")
            self._start_poller()
            self._open_job = job.job_id          # poll() opens the jobs window on it

        self._background(work)

    # ── job monitoring ────────────────────────────────────────────────────────

    def _start_poller(self) -> None:
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True,
                                             name="hpc-poller")
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        interval = max(15, int(self.profile().get("poll_interval", 60)))
        while not self._poll_stop.is_set():
            if not self._poll_once():
                return                       # nothing left to follow
            self._poll_stop.wait(interval)

    def _poll_once(self) -> bool:
        """Refresh unfinished jobs, download finished ones; True if work remains.

        The logs of running jobs are fetched at every poll, and once more
        when a job ends (so the jobs window shows its last lines)."""
        with self._lock:
            jobs = list(self.jobs)
        remaining = False
        for job in jobs:
            if not job.finished:
                hpc.update_job(job)
                if job.finished:
                    log.info("HPC job %s finished: %s", job.job_id, job.state)
                    self._fetch_logs(job)
                elif job.state not in _WAITING_STATES:
                    self._fetch_logs(job)
            if job.finished and job.download_to and not job.downloaded:
                try:
                    target = hpc.download_results(job, job.download_to)
                    log.info("HPC job %s results downloaded to %s", job.job_id, target)
                except hpc.HpcError as exc:
                    job.last_error = str(exc)
            remaining |= (not job.finished) or bool(job.download_to and not job.downloaded
                                                    and not job.last_error)
        self._save_jobs()
        return remaining

    def _job(self, job_id: str) -> hpc.HpcJob | None:
        with self._lock:
            return next((j for j in self.jobs if j.job_id == job_id), None)

    def _fetch_logs(self, job: hpc.HpcJob) -> None:
        """Read the job's SLURM logs from the cluster and send them to the jobs window."""
        try:
            text = hpc.job_logs(job)
        except hpc.HpcError as exc:
            text = f"Cannot read the output of job {job.job_id}: {exc}"
        stamp = time.strftime("%H:%M:%S")
        text = f"[{job.state or 'unknown'} at {stamp}]\n{text}"
        with self._lock:
            self._outputs[job.job_id] = text
        self._emit(JOBS, {"type": "job_output", "job_id": job.job_id, "text": text})

    def _job_output(self, job_id: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        threading.Thread(target=self._safe, args=(lambda: self._fetch_logs(job),),
                         daemon=True).start()

    def _cancel(self, job_id: str) -> None:
        job = self._job(job_id)
        if job is None:
            return

        def work():
            job.client().cancel(job.job_id)
            hpc.update_job(job)
            self._save_jobs()

        threading.Thread(target=self._safe, args=(work,), daemon=True).start()

    def _download(self, job_id: str) -> None:
        job = self._job(job_id)
        if job is None:
            return
        local = job.download_to or self.profile()["download_dir"]

        def work():
            target = hpc.download_results(job, local)
            log.info("Results of job %s downloaded to %s", job.job_id, target)
            self._save_jobs()
            if os.name == "nt":
                subprocess.Popen(["explorer", target])

        threading.Thread(target=self._safe, args=(work,), daemon=True).start()

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self.jobs = [j for j in self.jobs if j.job_id != job_id]
        self._save_jobs()

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            log.error("HPC: %s", exc)
            with self._lock:
                self._jobs_version += 1
