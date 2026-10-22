"""
Widgets of the simulation control panel.

The same panel is shown either in its own OS window (a separate process,
:mod:`specula_studio.control_window`, talking to the editor over
:mod:`specula_studio.control_link`) or as a window inside the editor.  The
view never runs anything itself: Start / Step / Abort / Test SSH are passed
to an *actions* object with the run options as a plain dict.
"""

from __future__ import annotations

import collections
import logging
import threading

import dearpygui.dearpygui as dpg

from ..core import ssh_config
from ..core.run_prep import DEFAULT_DISPLAY_SERVER_PORT

log = logging.getLogger(__name__)

TERMINAL_MAX_CHUNKS = 2000

DEFAULT_OPTIONS = {
    "backend": "In-Process",        # "In-Process" | "Remote"
    "host": "localhost",
    "user": "",
    "ds_port": DEFAULT_DISPLAY_SERVER_PORT,
    "nsimul": 1,
    "cpu": False,
    "target": 0,
    "precision": 1,
    "log_level": "INFO",
    "stepping": True,
    "profile": False,
    "diagram": False,
    "steps": 100,
}


def host_label(h: dict) -> str:
    return f"{h['user']}@{h['host']}" if h.get("user") else h.get("host", "")


class ControlPanelView:
    """
    *actions* must provide ``start(options)``, ``step(n)``, ``abort()``,
    ``test_ssh(target)`` and ``clear_terminal()``.  Update methods
    (``append_terminal``, ``set_status`` …) may be called from any thread.
    """

    def __init__(self, actions, theme_manager, recent_hosts=(), prefix: str = "sim"):
        self.actions = actions
        self.tm = theme_manager
        self.recent_hosts = list(recent_hosts)
        self.p = prefix
        self._terminal = collections.deque(maxlen=TERMINAL_MAX_CHUNKS)
        self._terminal_lock = threading.Lock()
        self._ssh_hosts: dict = {}

    def t(self, name: str) -> str:
        return f"{self.p}_{name}"

    # ── building ──────────────────────────────────────────────────────────────

    def build(self, parent) -> None:
        tm = self.tm
        t = self.t
        hint = tm.get_color("text_hint")
        sub = tm.get_color("control_subsection_header")
        last = self.recent_hosts[0] if self.recent_hosts else {"host": "localhost", "user": ""}

        with dpg.group(horizontal=True, parent=parent):
            with dpg.child_window(width=520):
                dpg.add_text("Backend Mode", color=tm.get_color("control_section_header"))
                dpg.add_combo(label="Execution Mode", items=["Remote", "In-Process"],
                              tag=t("backend"), default_value="In-Process",
                              callback=self.update_remote_enabled)
                dpg.add_text(
                    "In-Process: runs inside the editor, monitors fed directly.\n"
                    "Remote: runs the 'specula' command, locally (localhost)\n"
                    "or over SSH; monitors connect to its DisplayServer.",
                    color=hint)
                dpg.add_text("Remote Server Configuration", color=sub)
                with dpg.group(horizontal=True):
                    dpg.add_combo(label="", tag=t("ssh_hosts"), items=[], width=-180,
                                  callback=self._on_ssh_config_host)
                    dpg.add_text("SSH config hosts")
                    dpg.add_button(label="Reload", tag=t("ssh_reload"),
                                   callback=self.refresh_ssh_hosts)
                dpg.add_text("", tag=t("ssh_host_info"), color=hint)
                if self.recent_hosts:
                    dpg.add_combo(label="Recent hosts", tag=t("recent_hosts"),
                                  items=[host_label(h) for h in self.recent_hosts],
                                  default_value=host_label(last), width=-140,
                                  callback=self._on_recent_host)
                dpg.add_input_text(label="Server IP / Hostname", tag=t("remote_ip"),
                                   default_value=last.get("host", "localhost"),
                                   hint="localhost, 127.0.0.1, or remote host", width=-140)
                dpg.add_input_text(label="SSH Username", tag=t("remote_user"),
                                   default_value=last.get("user", ""),
                                   hint="empty = current user", width=-140)
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Test SSH", tag=t("test_ssh"),
                                   callback=self._on_test_ssh)
                    dpg.add_text("logins must work without prompts (keys / agent)",
                                 tag=t("ssh_test_status"), color=hint, wrap=380)
                dpg.add_input_int(label="Display server port", tag=t("ds_port"),
                                  default_value=DEFAULT_DISPLAY_SERVER_PORT,
                                  min_value=0, max_value=65535, width=-140)
                dpg.add_text("0 = pick a free port (requires a recent SPECULA)", color=hint)
                dpg.add_separator()
                dpg.add_text("Simulation Arguments", color=sub)
                dpg.add_input_int(label="N-Simul", tag=t("nsimul"), default_value=1,
                                  min_value=1)
                dpg.add_checkbox(label="Use CPU", tag=t("cpu"))
                dpg.add_input_int(label="GPU ID", tag=t("target"), default_value=0)
                dpg.add_combo(label="Precision", items=["0", "1"], tag=t("precision"),
                              default_value="1")
                dpg.add_combo(label="Log Level", items=["DEBUG", "INFO", "WARNING"],
                              tag=t("log"), default_value="INFO")
                dpg.add_checkbox(label="Stepping Mode", tag=t("stepping"), default_value=True)
                with dpg.group(horizontal=True):
                    dpg.add_checkbox(label="Profile", tag=t("profile"))
                    dpg.add_checkbox(label="SPECULA diagram (PNG)", tag=t("diagram"))
                dpg.add_text("(Profile / diagram: local and SSH runs only)", color=hint)
                dpg.add_separator()
                dpg.add_button(label="START SIMULATION", width=-1, height=35,
                               callback=lambda: self.actions.start(self.options()))
                dpg.add_button(label="Advance Step", width=-1, height=25,
                               callback=lambda: self.actions.step(1))
                with dpg.group(horizontal=True):
                    dpg.add_input_int(label="Steps", tag=t("advance_n_steps"),
                                      default_value=100, min_value=1, width=140)
                    dpg.add_button(label="Advance N Steps", width=-1, height=25,
                                   callback=self._on_step_n)
                dpg.add_button(label="Abort Simulation", width=-1, height=25,
                               callback=lambda: self.actions.abort())
                dpg.add_text("Status: idle", tag=t("status_text"), color=hint)

            with dpg.child_window(width=-1, tag=t("terminal_child"), border=True):
                with dpg.group(horizontal=True):
                    dpg.add_text("Terminal Output", color=tm.get_color("control_terminal_title"))
                    dpg.add_button(label="Clear", callback=self._on_clear)
                    dpg.add_button(label="Save...", callback=self._on_save)
                dpg.add_input_text(tag=t("terminal"), multiline=True, readonly=True,
                                   width=-1, height=-1)
        self.refresh_ssh_hosts()
        self.update_remote_enabled()
        self._show_terminal()

    # Widgets that only matter for Remote runs (greyed out for In-Process).
    _REMOTE_WIDGETS = ("ssh_hosts", "ssh_reload", "recent_hosts", "remote_ip",
                       "remote_user", "test_ssh", "ds_port")

    def update_remote_enabled(self, *_):
        backend = self.t("backend")
        remote = (dpg.get_value(backend) == "Remote") if dpg.does_item_exist(backend) else True
        for name in self._REMOTE_WIDGETS:
            if dpg.does_item_exist(self.t(name)):
                dpg.configure_item(self.t(name), enabled=remote)

    # ── options ───────────────────────────────────────────────────────────────

    def options(self) -> dict:
        t = self.t

        def get(name, default):
            return dpg.get_value(t(name)) if dpg.does_item_exist(t(name)) else default

        return {
            "backend": get("backend", DEFAULT_OPTIONS["backend"]),
            "host": (get("remote_ip", "localhost") or "localhost").strip(),
            "user": (get("remote_user", "") or "").strip(),
            "ds_port": int(get("ds_port", DEFAULT_DISPLAY_SERVER_PORT)),
            "nsimul": int(get("nsimul", 1)),
            "cpu": bool(get("cpu", False)),
            "target": int(get("target", 0)),
            "precision": int(get("precision", "1")),
            "log_level": get("log", "INFO"),
            "stepping": bool(get("stepping", True)),
            "profile": bool(get("profile", False)),
            "diagram": bool(get("diagram", False)),
            "steps": int(get("advance_n_steps", 100)),
        }

    def ssh_target(self) -> str:
        opts = self.options()
        return f"{opts['user']}@{opts['host']}" if opts["user"] else opts["host"]

    # ── callbacks ─────────────────────────────────────────────────────────────

    def refresh_ssh_hosts(self, *_):
        path = ssh_config.default_config_path()
        self._ssh_hosts = {h.alias: h for h in ssh_config.list_ssh_hosts(path)}
        if not dpg.does_item_exist(self.t("ssh_hosts")):
            return
        dpg.configure_item(self.t("ssh_hosts"), items=list(self._ssh_hosts))
        if self._ssh_hosts:
            info = f"{len(self._ssh_hosts)} host(s) from {path}"
        elif path.exists():
            info = f"No concrete Host entries in {path}"
        else:
            info = f"No ssh config found ({path})"
        dpg.set_value(self.t("ssh_host_info"), info)

    def _on_ssh_config_host(self, sender, alias):
        host = self._ssh_hosts.get(alias)
        if host is None:
            return
        # Pass the alias itself to ssh/scp, so every option of the config
        # entry applies (HostName, User, Port, IdentityFile, ProxyJump, ...).
        dpg.set_value(self.t("remote_ip"), alias)
        dpg.set_value(self.t("remote_user"), "")
        dpg.set_value(self.t("backend"), "Remote")
        self.update_remote_enabled()
        dpg.set_value(self.t("ssh_host_info"), f"{alias} -> {host.describe()}  ({host.source})")

    def _on_recent_host(self, sender, value):
        user, _, host = value.rpartition("@")
        dpg.set_value(self.t("remote_ip"), host)
        dpg.set_value(self.t("remote_user"), user)
        dpg.set_value(self.t("backend"), "Remote")
        self.update_remote_enabled()

    def _on_test_ssh(self, *_):
        target = self.ssh_target()
        if target in ("localhost", "127.0.0.1", ""):
            self.set_ssh_test("localhost: no ssh needed", True)
            return
        self.set_ssh_test(f"testing {target}…", None)
        self.actions.test_ssh(target)

    def _on_step_n(self, *_):
        self.actions.step(max(1, int(dpg.get_value(self.t("advance_n_steps")))))

    def _on_clear(self, *_):
        with self._terminal_lock:
            self._terminal.clear()
        self._show_terminal()
        self.actions.clear_terminal()

    def _on_save(self, *_):
        def write(sender, app_data):
            path = app_data.get("file_path_name")
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.terminal_text())
            log.info("Terminal output saved to %s", path)

        with dpg.file_dialog(label="Save terminal output", callback=write, width=700,
                             height=400, default_filename="specula_run.log"):
            dpg.add_file_extension(".log")
            dpg.add_file_extension(".*")

    # ── updates (any thread) ──────────────────────────────────────────────────

    def terminal_text(self) -> str:
        with self._terminal_lock:
            return "".join(self._terminal)

    def append_terminal(self, text: str) -> None:
        with self._terminal_lock:
            self._terminal.append(text)
        self._show_terminal()

    def reset_terminal(self, text: str) -> None:
        with self._terminal_lock:
            self._terminal.clear()
            if text:
                self._terminal.append(text)
        self._show_terminal()

    def _show_terminal(self) -> None:
        tag = self.t("terminal")
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, self.terminal_text())
            try:
                dpg.set_y_scroll(self.t("terminal_child"), -1.0)
            except Exception:
                pass

    def set_status(self, text: str) -> None:
        if dpg.does_item_exist(self.t("status_text")):
            dpg.set_value(self.t("status_text"), f"Status: {text}")

    def set_ssh_test(self, message: str, ok: bool | None) -> None:
        tag = self.t("ssh_test_status")
        if not dpg.does_item_exist(tag):
            return
        dpg.set_value(tag, message)
        key = "text_hint" if ok is None else ("text_success" if ok else "text_error")
        dpg.configure_item(tag, color=self.tm.get_color(key))
