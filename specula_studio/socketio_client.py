"""
The editor's Socket.IO connection to a run's DisplayServer.

Monitor windows of subprocess runs have their own connections (see
``monitor_window.py``); this one only fetches the server's ``params`` — the
objects of the running simulation — so that graph nodes can be mapped to
server object names (exact name preferred; ambiguous class-based matches are
logged and skipped instead of silently picking the wrong object).
"""

import logging
import os
import threading

import socketio as sio_module

from .constants import SOCKETIO_SERVER

log = logging.getLogger(__name__)


class SocketIOClient:
    """Connection to the DisplayServer and node → server-name mapping."""

    def __init__(self, server_url: str = SOCKETIO_SERVER, on_params=None):
        self.server_url = server_url
        self.connected  = False

        self.server_nodes:        dict = {}
        self.uuid_to_server_name: dict = {}

        # Session id advertised by the server in the 'params' event.
        self.server_session_id: str | None = None

        self._on_params_cb = on_params
        # Called with True / False when the connection goes up / is lost
        # (not when disconnect() is called).  Set by SimulationControl.
        self.on_connection_change = None
        self._closing = False

        # Prevents concurrent _connect_worker threads from racing on sio.connect.
        self._connect_lock: threading.Lock = threading.Lock()

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
        # No connection attempt here: SimulationControl connects once a
        # simulation with a DisplayServer has actually been started.

    # ──────────────────────────────────────────────────────────────────────────
    # Event handler setup
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_handlers(self):

        @self.sio.event
        def connect():
            self.connected = True
            log.info(f"[SOCKET.IO] Connected! SID: {self.sio.sid}")
            try:
                self.sio.emit("get_params")
            except Exception as e:
                log.error(f"[SOCKET.IO] get_params emit error: {e}")
            self._notify(True)

        @self.sio.event
        def params(data):
            if not data:
                log.info("[SOCKET.IO] No data in params event!")
                return
            # Extract and remove studio-private keys before forwarding
            self.server_session_id = data.pop("_session_id", None)
            protocol_ver           = data.pop("_protocol_version", 1)
            log.debug(f"PARAMS event: {len(data)} nodes, "
                      f"session={self.server_session_id}, protocol_ver={protocol_ver}")
            self.server_nodes = data
            if self._on_params_cb:
                self._on_params_cb(data)

        @self.sio.event
        def heartbeat(data):
            """Server keepalive: a new session id means the server restarted."""
            server_sid = data.get("session_id") if isinstance(data, dict) else None
            if (
                server_sid
                and server_sid != self.server_session_id
                and self.server_session_id is not None
            ):
                log.debug(f"Heartbeat from unexpected session {server_sid} "
                          f"(expected {self.server_session_id}) — requesting params.")
                try:
                    self.sio.emit("get_params")
                except Exception:
                    pass

        @self.sio.event
        def connect_error(data):
            self.connected = False
            log.error(f"[SOCKET.IO] Connection error: {data}")

        @self.sio.event
        def disconnect(*_reason):
            self.connected = False
            log.info("[SOCKET.IO] Disconnected")
            if not self._closing:
                self._notify(False)

    def _notify(self, connected: bool) -> None:
        if self.on_connection_change is not None:
            try:
                self.on_connection_change(connected)
            except Exception:
                log.exception("Connection-change callback failed")

    # ──────────────────────────────────────────────────────────────────────────
    # Connection management
    # ──────────────────────────────────────────────────────────────────────────

    def _connect_worker(self):
        """Worker that runs in ONE background thread at a time (guarded by lock)."""
        if not self._connect_lock.acquire(blocking=False):
            log.debug("Connection attempt already in progress — skipping duplicate.")
            return
        try:
            log.info(f"[SOCKET.IO] Connecting to {self.server_url} …")
            self.connected = False
            self.sio.connect(self.server_url, namespaces=["/"])
            log.info(f"[SOCKET.IO] Connection established. SID: {self.sio.sid}")
        except Exception as e:
            log.warning(f"[SOCKET.IO] Connection failed: {e}")
            self.connected = False
        finally:
            self._connect_lock.release()

    def reconnect(self):
        """(Re)connect to ``server_url`` in a background thread."""
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def disconnect(self):
        """Close the connection on purpose (no connection-change callback)."""
        if self.connected:
            self._closing = True
            try:
                self.sio.disconnect()
            except Exception:
                pass
            finally:
                self._closing = False

    # ──────────────────────────────────────────────────────────────────────────
    # Node-to-server name mapping
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
                log.debug(f"[BIND] {node_uuid} ({node_type}) → '{node_name}' (exact match)")
                continue

            # ── 2/3. Class-type fallback ────────────────────────────────────
            candidates = server_by_class.get(node_type, [])
            if len(candidates) == 1:
                server_name = candidates[0]
                self.uuid_to_server_name[node_uuid] = server_name
                log.debug(
                    f"[BIND] {node_uuid} ({node_type}) → '{server_name}' "
                    f"(class fallback, node name '{node_name}' not found on server)"
                )
            elif len(candidates) > 1:
                log.warning(
                    f"[BIND] WARNING: Ambiguous server instances for node "
                    f"'{node_name}' ({node_type}): {candidates}. "
                    "No mapping set — rename the node to match the server name."
                )
            else:
                log.debug(f"[BIND] No server instance found for '{node_name}' ({node_type})")

    def update_uuid_mapping(self, graph_nodes: dict):
        """
        Rebuild uuid_to_server_name by calling bind_nodes_to_server from scratch.
        Previous mappings are cleared so stale entries don't accumulate across runs.
        """
        self.uuid_to_server_name.clear()
        self.bind_nodes_to_server(graph_nodes, self.server_nodes)
        log.debug(f"Mapping complete: {len(self.uuid_to_server_name)}/{len(graph_nodes)} "
                  "nodes resolved")

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
                log.debug(f"[MONITOR] Late-mapped '{node_name}' (exact name)")
            else:
                candidates = [
                    sn for sn, si in self.server_nodes.items()
                    if si.get("class") == node_type
                ]
                if len(candidates) == 1:
                    server_name = candidates[0]
                    self.uuid_to_server_name[node_uuid] = server_name
                    log.debug(f"[MONITOR] Late-mapped '{node_name}' → '{server_name}' "
                              "(class fallback)")
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
