"""
socketio_client.py
==================
Owns the Socket.IO connection to the Specula simulation server.

Responsibilities
----------------
- Create and configure the socketio.Client instance.
- Manage the connect / disconnect lifecycle.
- Maintain the set of subscribed outputs and request new data frames.
- Map local node UUIDs to server node names.
- Route raw server events to owner-supplied callbacks.

The owner (NodeManager) provides four callback hooks at construction time so
that this class has *no* dependency on DearPyGui, the graph model, or monitors.
"""

import os
import traceback

import socketio as sio_module

from constants import SOCKETIO_SERVER, MONITOR_QUEUE_SIZE


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
        """
        Parameters
        ----------
        server_url       : URL of the Socket.IO server.
        on_connect       : callable()  – called after a successful connection.
        on_disconnect    : callable()  – called on disconnection.
        on_connect_error : callable(data) – called on connection error.
        on_params        : callable(data) – called when the server sends its
                           node parameter map (``params`` event).
        on_data_update   : callable(name, raw_data) – called when the server
                           pushes a data frame for a subscribed output.
        debug            : enable verbose logging.
        """
        self.server_url = server_url
        self.connected = False
        self.enabled = True
        self.debug = debug

        # Server state ---------------------------------------------------------
        self.server_params: dict = {}        # raw params dict from server
        self.server_nodes: dict = {}         # alias for server_params
        self.uuid_to_server_name: dict = {}  # local uuid -> server node name
        self.subscribed_outputs: set = set() # outputs we are subscribed to

        # Owner callbacks (all optional) ---------------------------------------
        self._on_connect_cb = on_connect
        self._on_disconnect_cb = on_disconnect
        self._on_connect_error_cb = on_connect_error
        self._on_params_cb = on_params
        self._on_data_update_cb = on_data_update

        # Build the socketio.Client --------------------------------------------
        if os.name == "nt":  # Windows needs explicit transport options
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(self, message: str):
        if self.debug:
            print(f"[SOCKETIO] {message}")

    def _setup_handlers(self):
        """Register all Socket.IO event handlers."""

        @self.sio.event
        def any_event(event, data):
            if event not in ["ping", "pong"]:
                print(f"[SOCKET.IO] ANY EVENT: {event} -> {type(data)}")
                if isinstance(data, dict):
                    print(f"    Keys: {list(data.keys())}")

        @self.sio.event
        def connect():
            self.connected = True
            print(f"[SOCKET.IO] Connected! SID: {self.sio.sid}")
            try:
                self.sio.emit("get_params")
                print("[SOCKET.IO] Requested params via 'get_params'")
            except Exception as e:
                print(f"[SOCKET.IO] Server should auto-send params on connect: {e}")
            if self._on_connect_cb:
                self._on_connect_cb()

        @self.sio.event
        def params(data):
            print(f"\n[SOCKET.IO] PARAMS EVENT FIRED! ({len(data)} nodes)")
            if not data:
                print("[SOCKET.IO] No data in params event!")
                return
            self.server_params = data
            self.server_nodes = data
            print("Server objects:", sorted(self.server_nodes.keys()))
            for i, (name, info) in enumerate(list(data.items())[:3]):
                print(
                    f"  {i+1}. {name} ({info.get('class', 'Unknown')}): "
                    f"{info.get('outputs', [])}"
                )
            if self._on_params_cb:
                self._on_params_cb(data)

        @self.sio.event
        def data_update(data):
            print(f"\n[SOCKET.IO] DATA_UPDATE: {data.get('name', 'unknown')}")
            try:
                name = data.get("name")
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
        def connect_error(data):
            self.connected = False
            print(f"[SOCKET.IO] Connection error: {data}")
            if self._on_connect_error_cb:
                self._on_connect_error_cb(data)

        @self.sio.event
        def disconnect():
            self.connected = False
            print("[SOCKET.IO] Disconnected")
            if self._on_disconnect_cb:
                self._on_disconnect_cb()

        @self.sio.event
        def speed_report(data):
            print(f"[SOCKET.IO] Speed report: {data}")

        @self.sio.event
        def done(data):
            print(f"[SOCKET.IO] Done event: {data}")
            if self.subscribed_outputs:
                self.request_next_frame()

    def _connect(self):
        """Attempt to connect to the Socket.IO server."""
        if not self.enabled:
            return
        try:
            print(f"[SOCKET.IO] Connecting to {self.server_url}...")
            self.connected = False
            self.sio.connect(self.server_url, namespaces=["/"])
            print(f"[SOCKET.IO] Connected! SID: {self.sio.sid}")
            self.sio.emit("test_connection", {"client": "node_editor"})
        except Exception as e:
            print(f"[SOCKET.IO] Connection failed: {e}")
            traceback.print_exc()
            self.connected = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def reconnect(self):
        """Reconnect to the server (called by monitor windows)."""
        self._connect()

    def disconnect(self):
        """Disconnect gracefully."""
        if self.connected:
            self.sio.disconnect()

    def emit(self, event: str, data=None) -> bool:
        """Emit an event. Returns True on success."""
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

    def request_next_frame(self):
        """Request next data frame from the server for all subscribed outputs."""
        if not self.connected:
            print("[SOCKET.IO] Not connected, cannot request data")
            return
        if not self.subscribed_outputs:
            return
        outputs_list = list(self.subscribed_outputs)
        print(f"[SOCKET.IO] Emitting 'newdata' for: {outputs_list}")
        try:
            self.sio.emit("newdata", outputs_list)
        except Exception as e:
            print(f"[SOCKET.IO] Error emitting 'newdata': {e}")

    def subscribe(self, server_output_name: str):
        """Add *server_output_name* to the subscription set and request data."""
        self.subscribed_outputs.add(server_output_name)
        if self.connected:
            self.request_next_frame()

    def unsubscribe(self, server_output_name: str):
        """Remove *server_output_name* from subscriptions and notify server."""
        self.subscribed_outputs.discard(server_output_name)
        if self.connected:
            try:
                self.sio.emit("unsubscribe", {"output": server_output_name})
            except Exception as e:
                print(f"[SOCKET.IO] Error sending unsubscribe: {e}")

    # ------------------------------------------------------------------
    # Node-to-server mapping helpers
    # ------------------------------------------------------------------

    def bind_nodes_to_server(self, graph_nodes: dict, params: dict):
        """
        Auto-bind local graph nodes to server node names by class type.
        Updates ``node_data['name']`` in-place when a unique match is found.
        """
        server_by_class: dict = {}
        for server_name, meta in params.items():
            cls = meta.get("class")
            if cls:
                server_by_class.setdefault(cls, []).append(server_name)

        for node_uuid, node_data in graph_nodes.items():
            if node_uuid in self.uuid_to_server_name:
                continue
            node_type = node_data.get("type")
            candidates = server_by_class.get(node_type, [])
            if len(candidates) == 1:
                server_name = candidates[0]
                node_data["name"] = server_name
                self.uuid_to_server_name[node_uuid] = server_name
                print(f"[BIND] {node_uuid} ({node_type}) -> {server_name}")
            elif len(candidates) > 1:
                print(
                    f"[BIND] Ambiguous server instances for {node_uuid} "
                    f"({node_type}): {candidates}"
                )
            else:
                print(f"[BIND] No server instance for {node_uuid} ({node_type})")

    def update_uuid_mapping(self, graph_nodes: dict):
        """
        Rebuild the full ``uuid_to_server_name`` mapping by matching node
        names (and falling back to class-based matching).
        """
        print("[MAPPING] Updating UUID -> server name mapping")
        self.uuid_to_server_name.clear()
        mapped_count = 0
        for node_uuid, node_data in graph_nodes.items():
            client_name = node_data.get("name")
            node_type = node_data.get("type", "")
            if not client_name:
                continue
            if client_name in self.server_nodes:
                self.uuid_to_server_name[node_uuid] = client_name
                mapped_count += 1
            else:
                candidates = [
                    sn
                    for sn, si in self.server_nodes.items()
                    if si.get("class") == node_type
                ]
                if len(candidates) == 1:
                    self.uuid_to_server_name[node_uuid] = candidates[0]
                    mapped_count += 1
                elif candidates:
                    print(
                        f"[MAPPING] Multiple candidates for {client_name} "
                        f"({node_type}): {candidates}"
                    )
        print(
            f"[MAPPING] Complete: {mapped_count}/{len(graph_nodes)} nodes mapped"
        )

    def get_server_output_name(
        self, node_uuid: str, output_name: str, graph_nodes: dict
    ) -> str:
        """
        Return the fully-qualified server output name ``<server_node>.<output>``.
        Falls back to auto-detection and then a synthetic name.
        """
        if not output_name:
            raise ValueError("output_name must be provided")

        server_name = self.uuid_to_server_name.get(node_uuid)

        if not server_name:
            node_data = graph_nodes.get(node_uuid, {})
            node_name = node_data.get("name", "<unnamed>")
            node_type = node_data.get("type", "<unknown>")

            candidates = [
                (sn, si.get("class"))
                for sn, si in self.server_nodes.items()
                if sn == node_name or si.get("class") == node_type
            ]
            if candidates:
                server_name = candidates[0][0]
                self.uuid_to_server_name[node_uuid] = server_name
                if len(candidates) > 1:
                    print(
                        f"[MONITOR] Multiple server candidates for {node_name}, "
                        f"using {server_name}"
                    )
                else:
                    print(f"[MONITOR] Auto-mapped {node_name} -> {server_name}")

        if not server_name:
            node_data = graph_nodes.get(node_uuid, {})
            node_name = node_data.get("name", "unknown")
            server_name = f"auto_{node_name}"
            print(f"[MONITOR] Warning: Using fallback server name: {server_name}")

        return f"{server_name}.{output_name}"
