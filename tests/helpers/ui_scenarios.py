"""
UI-coupled behaviour scenarios, driven by tests/fakedpg.py in a subprocess.
Prints a JSON dict  {scenario_name: result}.
"""

import os
import tempfile

# Never touch the developer's real settings, even when run by hand.
os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)

import json
import sys
import traceback

from tests import fakedpg

fakedpg.install()

from specula_studio import socketio_client  # noqa: E402

socketio_client.SocketIOClient._connect = lambda self: None

from specula_studio.app import TEMPLATES_DIR  # noqa: E402
from specula_studio.core.templates import load_bundled_templates  # noqa: E402
from specula_studio.file_handler import FileHandler  # noqa: E402
from specula_studio.graph_manager import GraphManager  # noqa: E402
from specula_studio.node_manager import NodeManager  # noqa: E402

FIXTURES = sys.argv[1]


def make():
    data_tpl, proc_tpl = load_bundled_templates()
    templates = {**data_tpl, **proc_tpl}
    graph = GraphManager(templates)
    nm = NodeManager(graph, templates, debug=False)
    nm.init_themes()
    return nm, FileHandler(nm)


def link_id_for(nm, dst_name, dst_attr):
    for lid, (_s, _sa, d, da) in nm.link_registry.items():
        if nm.graph.nodes[d]["name"] == dst_name and da == dst_attr:
            return lid
    return None


def scenario_new_node_required():
    nm, fh = make()
    u = nm.create_node("Modalrec")
    out = fh.export_to_yaml_dict()
    return {"export": dict(out[nm.graph.nodes[u]["name"]]),
            "complete": nm.is_node_complete(u)}


def scenario_list_ref_delink():
    nm, fh = make()
    fh.load_simulation(f"{FIXTURES}/params_scao_sh_test.yml")
    prop = nm.graph.find_by_name("prop")
    before = list(nm.graph.nodes[prop]["values"]["source_dict_ref"])
    nm.delink_callback(None, link_id_for(nm, "prop", "source_dict_ref"))
    after = nm.graph.nodes[prop]["values"].get("source_dict_ref")
    return {"before": before, "after": after,
            "outputs_extra": nm.graph.nodes[prop].get("outputs_extra", [])}


def scenario_unique_names():
    nm, fh = make()
    a = nm.create_node("Integrator")
    nm.rename_node(a, "aIntegrator1")
    b = nm.create_node("Integrator")
    c = nm.create_node("Integrator")
    return sorted(nm.graph.nodes[u]["name"] for u in (a, b, c))


def scenario_rename():
    nm, fh = make()
    fh.load_simulation(f"{FIXTURES}/params_scao_sh_test.yml")
    src = nm.graph.find_by_name("on_axis_source")
    errors = {n: nm.rename_node(src, n) for n in ("a.b", "a-b", "a:b", "", "sh", "a b")}
    ok = nm.rename_node(src, "ngs")
    out = fh.export_to_yaml_dict()
    return {"errors": errors, "ok": ok,
            "prop_sources": out["prop"]["source_dict_ref"],
            "prop_outputs": out["prop"].get("outputs"),
            "sh_in_ef": out["sh"]["inputs"]["in_ef"],
            "psf_in_ef": out["psf"]["inputs"]["in_ef"]}


def scenario_override_preview():
    import shutil
    import tempfile

    import yaml

    from specula_studio.app import SpeculaEditor as Editor

    tmp = tempfile.mkdtemp()
    sim = shutil.copy(f"{FIXTURES}/params_scao_sh_test.yml", tmp)
    ov = shutil.copy(f"{FIXTURES}/params_scao_sh_test_rec.yml", tmp)
    ed = Editor(yaml_folder=str(TEMPLATES_DIR))
    ed._open_simulation(sim)
    g = ed.nm.graph

    def val(name, key):
        return g.nodes[g.find_by_name(name)]["values"].get(key)

    ed.override_manager.load_overrides([ov])
    ed._on_override_toggled(ov)
    res = {"preview_total_time": val("main", "total_time"),
           "preview_has_im_calibrator": g.find_by_name("im_calibrator") is not None}
    g.nodes[g.find_by_name("psf")]["values"]["wavelengthInNm"] = 750
    ed._save_to(sim)
    saved = yaml.safe_load(open(sim))
    res.update(saved_total_time=saved["main"]["total_time"],
               saved_psf_wl=saved["psf"]["wavelengthInNm"],
               saved_has_im_calibrator="im_calibrator" in saved,
               saved_meta=saved.get("_overrides_metadata"))
    ed._on_override_toggled(ov)        # disable
    res.update(off_total_time=val("main", "total_time"), off_psf_wl=val("psf", "wavelengthInNm"),
               off_has_im_calibrator=g.find_by_name("im_calibrator") is not None)
    ed2 = Editor(yaml_folder=str(TEMPLATES_DIR))
    ed2._open_simulation(sim)
    g2 = ed2.nm.graph
    res.update(reopen_total_time=g2.nodes[g2.find_by_name("main")]["values"].get("total_time"),
               reopen_enabled=ed2.override_manager.get_enabled_overrides())
    return res


def scenario_undo_dirty():
    from specula_studio.app import SpeculaEditor as Editor
    from specula_studio.core import clipboard

    ed = Editor(yaml_folder=str(TEMPLATES_DIR))
    ed._open_simulation(f"{FIXTURES}/params_scao_sh_test.yml")
    g = ed.graph
    res = {"dirty_after_open": ed.is_dirty()}
    psf = g.find_by_name("psf")
    g.nodes[psf]["values"]["wavelengthInNm"] = 999
    ed._poll_history(force=True)
    res["dirty_after_edit"] = ed._dirty
    ed.undo()
    res["value_after_undo"] = g.nodes[g.find_by_name("psf")]["values"]["wavelengthInNm"]
    res["dirty_after_undo"] = ed._dirty
    ed.redo()
    res["value_after_redo"] = g.nodes[g.find_by_name("psf")]["values"]["wavelengthInNm"]
    n = len(g.nodes)
    ed._paste(clipboard.copy_nodes(g, [g.find_by_name("dm")]))
    res["added"] = len(g.nodes) - n
    res["canvas_nodes"] = len(ed.nm.uuid_to_dpg) == len(g.nodes)
    ed.undo()
    res["after_undo_paste"] = len(g.nodes) - n
    return res


if __name__ == "__main__":
    results = {}
    for name, fn in list(globals().items()):
        if name.startswith("scenario_"):
            try:
                results[name[9:]] = fn()
            except Exception:
                results[name[9:]] = {"exception": traceback.format_exc()}
    sys.stdout.write("@@JSON@@" + json.dumps(results, default=str))
