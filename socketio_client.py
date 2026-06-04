"""
socketio_client.py
==================
Owns the Socket.IO connection to the Specula simulation server.

Responsibilities
----------------
- Create and configure the socketio.Client instance.
- Manage the connect / disconnect lifecycle (with a connection lock so that
  concurrent reconnect calls cannot race).
- Maintain the set of subscribed outputs (protected by a threading.RLock) and
  request new data frames.
- Detect stalled streams: if a 'done' event is not received within
  STREAM_STALL_TIMEOUT seconds after emitting 'newdata', the pull cycle is
  re-armed automatically.
- Map local node UUIDs to server node names (exact-name preferred; ambiguous
  class-based matches are logged as warnings and skipped instead of silently
  picking the wrong node).
- Route raw server events to owner-supplied callbacks.
"""

import os
import threading
import traceback

import socketio as sio_module

from constants import (
    SOCKETIO_SERVER,
    MONITOR_QUEUE_SIZE,
    STREAM_STALL_TIMEOUT,
)


class SocketIOClient:
    """Manages the Socket.IO connection and pub/sub with the Specula server."""

    def __init__(
        self,
        server_url: str = SOCKETIO_SERVER,
        on_connect=None,
        on_disconnect=None,
        on_connect_error=None,
        on_params=None,
        on_data_update=None,
        debug: bool = True,
    ):
        self.server_url = server_url
        self.connected  = False
        self.enabled    = True
        self.debug      = debug

        # Server state ─────────────────────────────────────────────────────────
        self.server_params:       dict = {}
        self.server_nodes:        dict = {}
        self.uuid_to_server_name: dict = {}

        # Session id advertised by the server in the 'params' event.
        # Used to verify that the studio is talking to the expected simulation run.
        self.server_session_id: str | None = None

        # Subscribed outputs ── protected by _subs_lock (Issue 8) ─────────────
        self._subs_lock:        threading.RLock = threading.RLock()
        self._subscribed_outputs: set           = set()

        # Owner callbacks ──────────────────────────────────────────────────────
        self._on_connect_cb       = on_connect
        self._on_disconnect_cb    = on_disconnect
        self._on_connect_error_cb = on_connect_error
        self._on_params_cb        = on_params
        self._on_data_update_cb   = on_data_update

        # Connection guard (Issue 7) ─────────────────────────────────────────
        # Prevents concurrent _connect_worker threads from racing on sio.connect.
        self._connect_lock: threading.Lock = threading.Lock()

        # Stream-stall watchdog (Issue 2) ─────────────────────────────────────
        self._stall_timer: threading.Timer | None = None
        self._stall_lock:  threading.Lock          = threading.Lock()

        # Build the socketio.Client ────────────────────────────────────────────
        if os.name == "nt":
            self.sio = sio_module.Client(
                logger=True,
                engineio_logger=True,
                reconnection=True,
                reconnection_attempts=5,
                reconnection_delay=1,
                reconnection_delay_max=5,
                randomization_factor=0.5,
            )
        else:
            self.sio = sio_module.Client(logger=True, engineio_logger=False)

        self._setup_handlers()
        self._connect()

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _log(self, message: str):
        if self.debug:
            print(f"[SOCKETIO] {message}")

    # ── Subscriptions (thread-safe) ────────────────────────────────────────────

    @property
    def subscribed_outputs(self) -> frozenset:
        """Read-only snapshot of the current subscription set."""
        with self._subs_lock:
            return frozenset(self._subscribed_outputs)

    def _subs_add(self, name: str) -> None:
        with self._subs_lock:
            self._subscribed_outputs.add(name)

    def _subs_discard(self, name: str) -> None:
        with self._subs_lock:
            self._subscribed_outputs.discard(name)

    def _subs_any(self) -> bool:
        with self._subs_lock:
            return bool(self._subscribed_outputs)

    def _subs_list(self) -> list:
        with self._subs_lock:
            return list(self._subscribed_outputs)

    # ── Stall watchdog ─────────────────────────────────────────────────────────

    def _arm_stall_watchdog(self):
        """Arm (or re-arm) the stall watchdog timer."""
        if not self._subs_any():
            return
        with self._stall_lock:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
            self._stall_timer = threading.Timer(
                STREAM_STALL_TIMEOUT, self._on_stall_detected
            )
            self._stall_timer.daemon = True
            self._stall_timer.start()

    def _disarm_stall_watchdog(self):
        with self._stall_lock:
            if self._stall_timer is not None:
                self._stall_timer.cancel()
                self._stall_timer = None

    def _on_stall_detected(self):
        """Called by the watchdog timer when no 'done' is received in time."""
        if not self.connected or not self._subs_any():
            return
        self._log(
            f"Stream stall detected (no 'done' in {STREAM_STALL_TIMEOUT}s). "
            "Re-arming pull cycle."
        )
        self.request_next_frame()

    # ──────────────────────────────────────────────────────────────────────────
    # Event handler setup
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_handlers(self):

        @self.sio.event
        def connect():
            self.connected = True
            print(f"[SOCKET.IO] Connected! SID: {self.sio.sid}")
            try:
                self.sio.emit("get_params")
                print("[SOCKET.IO] Requested params via 'get_params'")
            except Exception as e:
                print(f"[SOCKET.IO] get_params emit error: {e}")
            if self._subs_any():
                self.request_next_frame()
            if self._on_connect_cb:
                self._on_connect_cb()

        @self.sio.event
        def params(data):
            if not data:
                print("[SOCKET.IO] No data in params event!")
                return
            # Extract and remove studio-private keys before forwarding
            self.server_session_id = data.pop("_session_id", None)
            protocol_ver           = data.pop("_protocol_version", 1)
            self._log(
                f"PARAMS event: {len(data)} nodes, "
                f"session={self.server_session_id}, protocol_ver={protocol_ver}"
            )
            self.server_params = data
            self.server_nodes  = data
            if self._on_params_cb:
                self._on_params_cb(data)

        @self.sio.event
        def data_update(data):
            try:
                name     = data.get("name")
                raw_data = data.get("data")
                if not name or raw_data is None:
                    print("[SOCKET.IO] Missing name or data in update")
                    return
                if self._on_data_update_cb:
                    self._on_data_update_cb(name, raw_data)
            except Exception as e:
                print(f"[SOCKET.IO] Error in data_update handler: {e}")
                traceback.print_exc()

        @self.sio.event
        def done(data):
            # 'done' signals the end of one display cycle — disarm watchdog and
            # arm it again for the NEXT cycle that will start in request_next_frame.
            self._disarm_stall_watchdog()
            if self._subs_any():
                self.request_next_frame()

        @self.sio.event
        def heartbeat(data):
            """Server-side keepalive.  Confirms the connection is alive."""
            server_sid = data.get("session_id") if isinstance(data, dict) else None
            if server_sid and server_sid != self.server_session_id:
                self._log(
                    f"Heartbeat from unexpected session {server_sid} "
                    f"(expected {self.server_session_id}) — reconnecting."
                )
                # The server has restarted under the same URL; request fresh params.
                try:
                    self.sio.emit("get_params")
                except Exception:
                    pass

        @self.sio.event
        def connect_error(data):
            self.connected = False
            self._disarm_stall_watchdog()
            print(f"[SOCKET.IO] Connection error: {data}")
            if self._on_connect_error_cb:
                self._on_connect_error_cb(data)

        @self.sio.event
        def disconnect():
            self.connected = False
            self._disarm_stall_watchdog()
            print("[SOCKET.IO] Disconnected")
            if self._on_disconnect_cb:
                self._on_disconnect_cb()

        @self.sio.event
        def speed_report(data):
            pass  # informational only

    # ──────────────────────────────────────────────────────────────────────────
    # Connection management
    # ──────────────────────────────────────────────────────────────────────────

    def _connect_worker(self):
        """Worker that runs in ONE background thread at a time (guarded by lock)."""
        if not self._connect_lock.acquire(blocking=False):
            self._log("Connection attempt already in progress — skipping duplicate.")
            return
        try:
            print(f"[SOCKET.IO] Connecting to {self.server_url} …")
            self.connected = False
            self.sio.connect(self.server_url, namespaces=["/"])
            print(f"[SOCKET.IO] Connection established. SID: {self.sio.sid}")
        except Exception as e:
            print(f"[SOCKET.IO] Connection failed: {e}")
            self.connected = False
        finally:
            self._connect_lock.release()

    def _connect(self):
        if not self.enabled:
            return
        t = threading.Thread(target=self._connect_worker, daemon=True)
        t.start()

    def reconnect(self):
        """Reconnect to the server (called by monitor windows or SimulationControl)."""
        self._connect()

    def disconnect(self):
        self._disarm_stall_watchdog()
        if self.connected:
            try:
                self.sio.disconnect()
            except Exception:
                pass

    def emit(self, event: str, data=None) -> bool:
        if not self.connected:
            return False
        try:
            if data is None:
                self.sio.emit(event)
            else:
                self.sio.emit(event, data)
            return True
        except Exception as e:
            print(f"[SOCKET.IO] Error emitting '{event}': {e}")
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Pub/sub
    # ──────────────────────────────────────────────────────────────────────────

    def request_next_frame(self):
        """Emit 'newdata' for all subscribed outputs and arm the stall watchdog."""
        if not self.connected:
            return
        outputs_list = self._subs_list()
        if not outputs_list:
            return
        self._log(f"Emitting 'newdata' for: {outputs_list}")
        try:
            self.sio.emit("newdata", outputs_list)
            self._arm_stall_watchdog()
        except Exception as e:
            print(f"[SOCKET.IO] Error emitting 'newdata': {e}")

    def subscribe(self, server_output_name: str):
        self._subs_add(server_output_name)
        if self.connected:
            self.request_next_frame()

    def unsubscribe(self, server_output_name: str):
        self._subs_discard(server_output_name)
        if not self._subs_any():
            self._disarm_stall_watchdog()
        if self.connected:
            try:
                self.sio.emit("unsubscribe", {"output": server_output_name})
            except Exception as e:
                print(f"[SOCKET.IO] Error sending unsubscribe: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Node-to-server name mapping  (Issue 1)
    # ──────────────────────────────────────────────────────────────────────────

    def bind_nodes_to_server(self, graph_nodes: dict, params: dict):
        """
        Map local graph UUIDs to server node names.

        Strategy (in priority order):
        1. Exact name match: node_data["name"] == server node name.
           This should always work when the studio exported the YAML that SPECULA
           is currently running — both use the same node names.
        2. Class-type fallback: only when no exact match AND exactly ONE server
           node has the same class. Logs a warning. Does NOT mutate
           node_data["name"] so the graph model is never corrupted.
        3. Ambiguous class match: logs a warning and skips. The monitor will show
           "Waiting for data" until the user resolves the naming.
        """
        server_names_set   = set(params.keys())
        server_by_class: dict = {}
        for server_name, meta in params.items():
            cls = meta.get("class")
            if cls:
                server_by_class.setdefault(cls, []).append(server_name)

        for node_uuid, node_data in graph_nodes.items():
            if node_uuid in self.uuid_to_server_name:
                continue
            node_name = node_data.get("name", "")
            node_type = node_data.get("type", "")

            # ── 1. Exact name match ─────────────────────────────────────────
            if node_name in server_names_set:
                self.uuid_to_server_name[node_uuid] = node_name
                self._log(f"[BIND] {node_uuid} ({node_type}) → '{node_name}' (exact match)")
                continue

            # ── 2/3. Class-type fallback ────────────────────────────────────
            candidates = server_by_class.get(node_type, [])
            if len(candidates) == 1:
                server_name = candidates[0]
                self.uuid_to_server_name[node_uuid] = server_name
                self._log(
                    f"[BIND] {node_uuid} ({node_type}) → '{server_name}' "
                    f"(class fallback, node name '{node_name}' not found on server)"
                )
            elif len(candidates) > 1:
                print(
                    f"[BIND] WARNING: Ambiguous server instances for node "
                    f"'{node_name}' ({node_type}): {candidates}. "
                    "No mapping set — rename the node to match the server name."
                )
            else:
                self._log(
                    f"[BIND] No server instance found for '{node_name}' ({node_type})"
                )

    def update_uuid_mapping(self, graph_nodes: dict):
        """
        Rebuild uuid_to_server_name by calling bind_nodes_to_server from scratch.
        Previous mappings are cleared so stale entries don't accumulate across runs.
        """
        self._log("Updating UUID → server name mapping")
        self.uuid_to_server_name.clear()
        self.bind_nodes_to_server(graph_nodes, self.server_nodes)
        mapped = len(self.uuid_to_server_name)
        self._log(f"Mapping complete: {mapped}/{len(graph_nodes)} nodes resolved")

    def get_server_output_name(
        self, node_uuid: str, output_name: str, graph_nodes: dict
    ) -> str:
        """
        Return the fully-qualified server output name ``<server_node>.<output>``.

        Raises ValueError if no server name can be resolved, so callers can
        decide whether to defer or show a meaningful error.
        """
        if not output_name:
            raise ValueError("output_name must be provided")

        server_name = self.uuid_to_server_name.get(node_uuid)

        if not server_name:
            # Try to resolve on the fly using the current server_nodes snapshot.
            node_data = graph_nodes.get(node_uuid, {})
            node_name = node_data.get("name", "")
            node_type = node_data.get("type", "")

            if node_name and node_name in self.server_nodes:
                server_name = node_name
                self.uuid_to_server_name[node_uuid] = server_name
                self._log(f"[MONITOR] Late-mapped '{node_name}' (exact name)")
            else:
                candidates = [
                    sn for sn, si in self.server_nodes.items()
                    if si.get("class") == node_type
                ]
                if len(candidates) == 1:
                    server_name = candidates[0]
                    self.uuid_to_server_name[node_uuid] = server_name
                    self._log(
                        f"[MONITOR] Late-mapped '{node_name}' → '{server_name}' "
                        "(class fallback)"
                    )
                elif len(candidates) > 1:
                    raise ValueError(
                        f"Ambiguous server candidates for '{node_name}' ({node_type}): "
                        f"{candidates}. Rename the node to match the server name."
                    )

        if not server_name:
            raise ValueError(
                f"Cannot resolve server name for node UUID {node_uuid}. "
                "Server params not yet received or node name mismatch."
            )

        return f"{server_name}.{output_name}"