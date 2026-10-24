"""Monitor payload conversion and plot dispatch (numpy only)."""

import numpy as np

from specula_studio import monitor_data as md


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_payload_roundtrip_shapes():
    for arr in (np.float32(2.5), np.arange(5, dtype=np.float32),
                np.arange(12, dtype=np.float32).reshape(3, 4)):
        out = md.payload_to_array(md.make_payload(np.asarray(arr)))
        assert np.array_equal(out, np.asarray(arr).reshape(out.shape))
    lst = {"type": "2d_array", "data": [1, 2, 3, 4], "shape": [2, 2]}
    assert md.payload_to_array(lst).shape == (2, 2)
    multi = {"type": "multi_data", "data": [[1, 2, 3, 4]], "shapes": [[2, 2]]}
    assert md.payload_to_array(multi).shape == (2, 2)
    assert md.payload_to_array({"type": "error", "data": "x"}) is None


def test_to_cpu_array_copies_and_handles_complex():
    src = np.ones(3, dtype=np.float32)
    out = md.to_cpu_array(src)
    out[0] = 5
    assert src[0] == 1                       # never aliases the simulation buffer
    assert np.allclose(md.to_cpu_array(np.array([3 + 4j])), [5.0])


def test_extract_display_array_preference_order():
    obj = _Obj(array_for_display=lambda: np.array([1.0]), get_value=lambda: np.array([2.0]))
    assert md.extract_display_array(obj)[0] == 1.0
    obj = _Obj(get_value=lambda: np.array([2.0]))
    assert md.extract_display_array(obj)[0] == 2.0
    obj = _Obj(slopes=np.array([3.0]))
    assert md.extract_display_array(obj, "out_slopes")[0] == 3.0


def test_plot_dispatch():
    calls = []

    class Plotter:
        def plot_history(self, v): calls.append(("history", v)); return True
        def plot_vector(self, a): calls.append(("vector", a.shape)); return True
        def plot_scatter(self, a): calls.append(("scatter", a.shape)); return True
        def plot_2d_image_clean(self, a): calls.append(("image", a.shape)); return True

    p = Plotter()
    md.plot_array(p, np.array([4.0]))
    md.plot_array(p, np.arange(3.0))
    md.plot_array(p, np.zeros((4, 5)))
    md.plot_array(p, np.zeros((2, 4, 5)))
    assert [c[0] for c in calls] == ["history", "vector", "image", "image"]
    assert md.min_update_interval(np.zeros((2000, 2000))) == 0.5
