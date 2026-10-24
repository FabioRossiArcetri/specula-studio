"""
Automatic layouts of the graph: pure functions, node sizes in, positions out.

Three strategies share one analysis of the graph:

* :func:`left_to_right` — the signal flows from left to right, one column per
  stage;
* :func:`compact`       — the same, wrapped into bands no wider than the
  window (cut where the fewest links cross);
* :func:`data_objects_on_top` — the data objects (simulation parameters,
  sources, pupil stops...) in a row above the object that uses them, the
  processing objects below, from left to right.

Analysis
--------
* Data connections give the stages.  Feedback connections (delay -1) are
  left out; a cycle without one is broken where a depth-first search meets
  it again.  Each node goes one stage after its latest input (longest path).
* Objects without data inputs move next to the first stage that uses them,
  through a data connection or a reference.
* References only matter for placing such objects: an object referenced by
  many (the simulation parameters) would pull everything towards itself.
* Inside a stage, nodes are ordered by the mean position of their neighbours
  (barycenter sweeps), which removes most crossings; then each node is placed
  level with the mean of its neighbours, without overlaps, so links run as
  straight as possible.

:func:`grid` places a group of new objects (Add Multiple Objects).

Positions are top-left corners in canvas pixels (a node's ``gui_pos``).
"""

from __future__ import annotations

from collections import defaultdict

Edge = tuple[str, str]
Size = tuple[float, float]

SWEEPS = 4              # barycenter sweeps (down + up)
SHARED_REF = 2          # objects referenced by more than this do not attract


def graph_edges(graph) -> tuple[list[Edge], list[Edge]]:
    """(data edges, reference edges) of a :class:`~.model.Graph`, as
    (source uuid, destination uuid); feedback connections are left out."""
    data, refs = [], []
    for conn in graph.connections:
        src, src_attr, dst, dst_attr = conn
        if src == dst:
            continue
        if dst_attr.endswith("_ref") or dst_attr == "layer_list":
            refs.append((src, dst))
            continue
        delay = graph.connection_properties.get(conn, {}).get("delay", 0)
        if delay == -1 or ":-" in str(src_attr):
            continue
        data.append((src, dst))
    return _unique(data), _unique(refs)


def _unique(edges) -> list:
    return list(dict.fromkeys(edges))


# ── analysis ─────────────────────────────────────────────────────────────────

def _acyclic(nodes: list, edges: list[Edge]) -> list[Edge]:
    """*edges* without the ones closing a cycle (depth-first back edges)."""
    succ = defaultdict(list)
    for a, b in edges:
        succ[a].append(b)
    state, back = {}, set()
    for root in nodes:
        if root in state:
            continue
        stack = [(root, iter(succ[root]))]
        state[root] = 1                              # on the stack
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[node] = 2                      # done
                stack.pop()
            elif state.get(child) == 1:
                back.add((node, child))
            elif child not in state:
                state[child] = 1
                stack.append((child, iter(succ[child])))
    return [e for e in edges if e not in back]


def layers(nodes: list, edges: list[Edge], refs: list[Edge] = ()) -> dict:
    """Stage of every node: longest path over the data *edges*; nodes without
    data inputs right before their first consumer (data or reference)."""
    edges = _acyclic(nodes, [e for e in edges if e[0] in nodes and e[1] in nodes])
    preds, succ = defaultdict(list), defaultdict(list)
    for a, b in edges:
        preds[b].append(a)
        succ[a].append(b)
    layer: dict = {}
    remaining = {n: len(preds[n]) for n in nodes}
    ready = [n for n in nodes if remaining[n] == 0]
    while ready:
        n = ready.pop(0)
        layer[n] = max((layer[p] + 1 for p in preds[n]), default=0)
        for s in succ[n]:
            remaining[s] -= 1
            if remaining[s] == 0:
                ready.append(s)
    consumers = defaultdict(set)
    for a, b in list(edges) + [r for r in refs if r[0] in nodes and r[1] in nodes]:
        consumers[a].add(b)
    sources = [n for n in nodes if not preds[n]]
    for _ in range(len(sources) + 1):                # until stable
        changed = False
        for n in sources:
            if consumers[n]:
                wanted = max(0, min(layer[c] for c in consumers[n]) - 1)
                if wanted != layer[n]:
                    layer[n], changed = wanted, True
        if not changed:
            break
    return layer


def _neighbours(nodes, edges, refs) -> list[Edge]:
    """Edges that attract nodes to each other: data edges, and references to
    objects used by few nodes."""
    uses = defaultdict(int)
    for a, _ in refs:
        uses[a] += 1
    attract = [r for r in refs if uses[r[0]] <= SHARED_REF]
    return [e for e in _unique(list(edges) + attract) if e[0] in nodes and e[1] in nodes]


def orders(layer: dict, links: list[Edge]) -> list[list]:
    """Nodes of each stage, ordered to reduce crossings (barycenter sweeps)."""
    count = max(layer.values(), default=-1) + 1
    rows = [[n for n in layer if layer[n] == k] for k in range(count)]
    adj = defaultdict(list)
    for a, b in links:
        adj[a].append(b)
        adj[b].append(a)

    def rank():
        return {n: (i + 0.5) / len(row) for row in rows for i, n in enumerate(row)}

    for sweep in range(SWEEPS):
        down = sweep % 2 == 0
        for k in (range(1, count) if down else range(count - 2, -1, -1)):
            r = rank()
            side = [m for m in r if (layer[m] < k if down else layer[m] > k)]
            sides = set(side)

            def key(n, r=r, sides=sides):
                near = [r[m] for m in adj[n] if m in sides]
                return sum(near) / len(near) if near else r[n]
            rows[k].sort(key=key)
    return rows


def _place(order: list, desired: dict, extent: dict, gap: float, start: float) -> dict:
    """Positions along one stage, in *order*, without overlaps and never
    before *start*, each node as close to its *desired* position as possible:
    overlapping nodes merge into blocks centred on the mean of their wishes."""
    blocks: list = []          # [nodes, wishes as block positions, size, position]
    for n in order:
        wish = desired.get(n)
        blocks.append([[n], [] if wish is None else [wish], extent[n], 0.0])
        while True:
            nodes, wishes, size, _ = blocks[-1]
            prev_end = blocks[-2][3] + blocks[-2][2] + gap if len(blocks) > 1 else start
            pos = sum(wishes) / len(wishes) if wishes else prev_end
            blocks[-1][3] = max(pos, start)
            if len(blocks) > 1 and blocks[-1][3] < prev_end:
                a = blocks.pop(-2)
                shift = a[2] + gap                    # members of b start after a
                b = blocks.pop()
                blocks.append([a[0] + b[0], a[1] + [w - shift for w in b[1]],
                               a[2] + gap + b[2], 0.0])
                continue
            break
    pos = {}
    for nodes, _, _, position in blocks:
        for n in nodes:
            pos[n] = position
            position += extent[n] + gap
    return pos


def _flow(nodes, sizes, layer, links, gap_major, gap_minor, origin) -> dict:
    """Stages as columns from *origin*; each node level with its neighbours."""
    rows = orders(layer, links)
    adj = defaultdict(list)
    for a, b in links:
        adj[a].append(b)
        adj[b].append(a)
    x, col_x = origin[0], []
    for row in rows:
        col_x.append(x)
        x += max((sizes[n][0] for n in row), default=0) + gap_major
    height = {n: sizes[n][1] for n in nodes}
    y: dict = {}

    def centre(m):
        return y[m] + height[m] / 2

    # forward: level with the nodes before; backward: nodes that start a
    # chain (no neighbour before) level with the nodes after them
    for row in rows:
        k = layer[row[0]] if row else 0
        desired = {}
        for n in row:
            before = [centre(m) for m in adj[n] if m in y and layer[m] < k]
            if before:
                desired[n] = sum(before) / len(before) - height[n] / 2
        y.update(_place(row, desired, height, gap_minor, origin[1]))
    for row in reversed(rows):
        k = layer[row[0]] if row else 0
        desired = {}
        for n in row:
            before = [m for m in adj[n] if layer[m] < k]
            after = [centre(m) for m in adj[n] if layer[m] > k]
            if not before and after:
                desired[n] = sum(after) / len(after) - height[n] / 2
            else:
                desired[n] = y[n]
        y.update(_place(sorted(row, key=lambda n: desired[n]), desired, height,
                        gap_minor, origin[1]))
    return {n: (col_x[layer[n]], y[n]) for n in nodes}


# ── strategies ───────────────────────────────────────────────────────────────

def left_to_right(nodes: list, sizes: dict, edges: list[Edge], refs: list[Edge] = (),
                  gap_x: float = 60, gap_y: float = 60, origin=(50, 50)) -> dict:
    """The signal flow from left to right, one column per stage."""
    layer = layers(nodes, edges, refs)
    return _flow(nodes, sizes, layer, _neighbours(nodes, edges, refs), gap_x, gap_y, origin)


def compact(nodes: list, sizes: dict, edges: list[Edge], refs: list[Edge] = (),
            max_width: float = 1800, gap_x: float = 60, gap_y: float = 60,
            origin=(50, 50)) -> dict:
    """Left to right, wrapped into bands no wider than *max_width*."""
    layer = layers(nodes, edges, refs)
    links = _neighbours(nodes, edges, refs)
    count = max(layer.values(), default=-1) + 1
    width = [max((sizes[n][0] for n in nodes if layer[n] == k), default=0) for k in range(count)]
    bands = _bands(width, [(layer[a], layer[b]) for a, b in links], max_width - origin[0], gap_x)
    positions, top = {}, origin[1]
    for band in bands:
        members = [n for n in nodes if layer[n] in band]
        local = {n: layer[n] - band[0] for n in members}
        placed = _flow(members, sizes, local, [e for e in links if e[0] in local and e[1] in local],
                       gap_x, gap_y, (origin[0], top))
        positions.update(placed)
        top = max(placed[n][1] + sizes[n][1] for n in members) + 2 * gap_y
    return positions


def _bands(width: list, spans: list, max_width: float, gap: float) -> list[list[int]]:
    """Consecutive stages grouped into bands no wider than *max_width*: as
    few bands as possible, cut where the fewest links cross, of balanced
    widths (dynamic programming over the cut positions)."""
    count = len(width)

    def band_width(i, j):                        # stages i..j-1
        return sum(width[i:j]) + gap * (j - i - 1)

    def cut(k):                                  # links crossing between k-1 and k
        return sum(1 for a, b in spans if min(a, b) < k <= max(a, b))

    best = {0: (0, 0, 0.0, [])}                  # stage -> (bands, cuts, widest, starts)
    for j in range(1, count + 1):
        options = []
        for i in range(j):
            if i not in best or (band_width(i, j) > max_width and j - i > 1):
                continue
            n, c, w, starts = best[i]
            options.append((n + 1, c + (cut(i) if i else 0),
                            max(w, band_width(i, j)), starts + [i]))
        if options:
            best[j] = min(options, key=lambda o: o[:3])
    starts = best[count][3] + [count]
    return [list(range(a, b)) for a, b in zip(starts, starts[1:], strict=False)]


def data_objects_on_top(nodes: list, sizes: dict, edges: list[Edge], refs: list[Edge] = (),
                        data_objects=(), gap_x: float = 60, gap_y: float = 60,
                        origin=(50, 50)) -> dict:
    """The *data_objects* (simulation parameters, sources, pupil stops...)
    in a row on top, each above the first object that uses it; the
    processing objects below, from left to right."""
    edges = [e for e in edges if e[0] in nodes and e[1] in nodes]
    top_row = [n for n in nodes if n in set(data_objects)]
    flow = [n for n in nodes if n not in set(data_objects)]
    if not flow or not top_row:                      # only one kind: no row on top
        return left_to_right(nodes, sizes, edges, refs, gap_x, gap_y, origin)
    feeds = {a for a, _ in edges}
    row_h = max(sizes[n][1] for n in top_row)
    below = (origin[0], origin[1] + row_h + 2 * gap_y)
    flow_edges = [e for e in edges if e[0] in flow and e[1] in flow]
    flow_refs = [r for r in refs if r[0] in flow and r[1] in flow]
    positions = left_to_right(flow, sizes, flow_edges, flow_refs, gap_x, gap_y, below)

    # each top object above the left edge of its first consumer
    consumers = defaultdict(list)
    for a, b in list(edges) + [r for r in refs if r[0] in nodes and r[1] in nodes]:
        if b in positions:
            consumers[a].append(b)
    wish = {}
    for n in top_row:
        xs = [positions[c][0] for c in consumers[n]]
        wish[n] = min(xs) if xs else origin[0]
    # objects that feed data first, then the ones only referenced
    order = sorted(top_row, key=lambda n: (wish[n], n not in feeds))
    widths = {n: sizes[n][0] for n in top_row}
    xs = _place(order, wish, widths, gap_x, origin[0])
    positions.update({n: (xs[n], origin[1]) for n in top_row})
    return positions


def grid(nodes: list, sizes: dict, gap_x: float = 40, gap_y: float = 40,
         centre=(0.0, 0.0)) -> dict:
    """*nodes* in a grid, row by row, centred on *centre*: the number of
    columns giving the most nearly square block (in pixels, not in count:
    nodes are wider than tall)."""
    if not nodes:
        return {}

    def shape(cols):
        rows = [nodes[i:i + cols] for i in range(0, len(nodes), cols)]
        col_w = [max(sizes[row[c]][0] for row in rows if c < len(row)) for c in range(cols)]
        row_h = [max(sizes[n][1] for n in row) for row in rows]
        return rows, col_w, row_h

    def squareness(cols):
        _, col_w, row_h = shape(cols)
        w = sum(col_w) + gap_x * (len(col_w) - 1)
        h = sum(row_h) + gap_y * (len(row_h) - 1)
        return max(w, h) / max(min(w, h), 1)

    cols = min(range(1, len(nodes) + 1), key=squareness)
    rows, col_w, row_h = shape(cols)
    width = sum(col_w) + gap_x * (cols - 1)
    height = sum(row_h) + gap_y * (len(rows) - 1)
    positions, y = {}, centre[1] - height / 2
    for r, row in enumerate(rows):
        x = centre[0] - width / 2
        for c, n in enumerate(row):
            positions[n] = (x, y)
            x += col_w[c] + gap_x
        y += row_h[r] + gap_y
    return positions


STRATEGIES = {
    "left_to_right": left_to_right,
    "compact": compact,
    "data_objects_on_top": data_objects_on_top,
}
