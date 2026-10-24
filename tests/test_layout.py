"""Automatic layouts (core.layout): no overlaps, left-to-right flow, bounds."""

import pytest

from specula_studio.core import layout, yaml_io
from specula_studio.core.templates import load_bundled_templates
from tests.conftest import FIXTURES

STRATEGIES = sorted(layout.STRATEGIES)


def _scao():
    """The SCAO fixture: uuids, rough node sizes, data and reference edges."""
    data, proc = load_bundled_templates()
    graph = yaml_io.load_graph(yaml_io.read_yaml_file(FIXTURES / "params_scao.yml"), {**data, **proc})
    nodes = list(graph.nodes)
    sizes = {u: (200 + 10 * len(graph.nodes[u]["name"]), 100 + 30 * (len(graph.nodes[u]["type"]) % 5))
             for u in nodes}
    edges, refs = layout.graph_edges(graph)
    names = {u: graph.nodes[u]["name"] for u in nodes}
    return graph, nodes, sizes, edges, refs, names


def _overlaps(positions, sizes):
    boxes = [(u, x, y, x + sizes[u][0], y + sizes[u][1]) for u, (x, y) in positions.items()]
    return [(a[0], b[0]) for i, a in enumerate(boxes) for b in boxes[i + 1:]
            if a[1] < b[3] and b[1] < a[3] and a[2] < b[4] and b[2] < a[4]]


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_every_node_placed_without_overlaps(strategy):
    _, nodes, sizes, edges, refs, _ = _scao()
    positions = layout.STRATEGIES[strategy](nodes, sizes, edges, refs)
    assert set(positions) == set(nodes)
    assert not _overlaps(positions, sizes)
    assert all(x >= 50 and y >= 50 for x, y in positions.values())


def test_graph_edges_leave_out_feedback_and_split_references():
    graph, _, _, edges, refs, names = _scao()
    named = {(names[a], names[b]) for a, b in edges}
    assert ("atmo", "prop") in named and ("control", "dm") in named
    assert ("dm", "prop") not in named                       # dm.out_layer:-1
    assert ("on_axis_source", "prop") in {(names[a], names[b]) for a, b in refs}


def test_left_to_right_flows_left_to_right():
    _, nodes, sizes, edges, refs, names = _scao()
    pos = layout.left_to_right(nodes, sizes, edges, refs)
    for a, b in edges:
        assert pos[a][0] + sizes[a][0] < pos[b][0], (names[a], names[b])
    by_name = {names[u]: p for u, p in pos.items()}
    # the source sits just before the propagation, not in the first column
    assert by_name["on_axis_source"][0] > by_name["seeing"][0]


def test_compact_stays_within_the_width():
    _, nodes, sizes, edges, refs, _ = _scao()
    for width in (1200, 1800):
        pos = layout.compact(nodes, sizes, edges, refs, max_width=width)
        widest = max(x + sizes[u][0] for u, (x, y) in pos.items())
        assert widest <= width or len({x for x, _ in pos.values()}) == 1


def test_data_objects_on_top():
    graph, nodes, sizes, edges, refs, names = _scao()
    data_types = {"SimulParams", "Source", "Pupilstop"}
    data = {u for u in nodes if graph.nodes[u]["type"] in data_types}
    pos = layout.data_objects_on_top(nodes, sizes, edges, refs, data_objects=data)
    assert {names[u] for u in data} == {"main", "on_axis_source", "pupilstop"}
    lowest = max(pos[u][1] + sizes[u][1] for u in data)
    assert all(pos[u][1] > lowest for u in nodes if u not in data)     # generators too
    assert not _overlaps(pos, sizes)


def test_cycles_without_feedback_and_isolated_nodes():
    nodes = ["a", "b", "c", "lonely"]
    sizes = {n: (100, 50) for n in nodes}
    edges = [("a", "b"), ("b", "c"), ("c", "a")]             # no explicit delay
    for strategy in STRATEGIES:
        pos = layout.STRATEGIES[strategy](nodes, sizes, edges)
        assert set(pos) == set(nodes) and not _overlaps(pos, sizes)


def test_bands_cut_where_fewest_links_cross():
    # five stages of width 100; two fit per band of 250: the cut must avoid
    # the stage pair 1-2, crossed by three links
    spans = [(0, 1), (1, 2), (1, 2), (1, 2), (2, 3), (3, 4)]
    bands = layout._bands([100] * 5, spans, 340, 20)
    assert [b[0] for b in bands][1:] != [2]
    assert all(len(b) * 100 + (len(b) - 1) * 20 <= 340 for b in bands)


def test_grid_is_square_centred_and_without_overlaps():
    nodes = [f"n{i}" for i in range(7)]
    sizes = {n: (220, 100 + 20 * (i % 3)) for i, n in enumerate(nodes)}
    pos = layout.grid(nodes, sizes, 40, 40, centre=(500, 400))
    assert set(pos) == set(nodes) and not _overlaps(pos, sizes)
    left = min(x for x, _ in pos.values())
    right = max(x + sizes[n][0] for n, (x, _) in pos.items())
    top = min(y for _, y in pos.values())
    bottom = max(y + sizes[n][1] for n, (_, y) in pos.items())
    assert abs((left + right) / 2 - 500) < 1 and abs((top + bottom) / 2 - 400) < 1
    # wide nodes: fewer columns than rows, and a block close to square
    columns = len({x for x, _ in pos.values()})
    assert columns < 4 and max(right - left, bottom - top) / min(right - left, bottom - top) < 1.6
    assert layout.grid([], {}) == {}
