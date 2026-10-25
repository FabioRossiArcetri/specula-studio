"""Class catalog generation, caching and fallback."""

import textwrap

import pytest

from specula_studio import help_provider
from specula_studio.core import catalog

FAKE_SPECULA = {
    "base_processing_obj.py": """
        class BaseProcessingObj:
            def __init__(self, target_device_idx: int = None, precision: int = None):
                pass
        """,
    "base_data_obj.py": """
        class BaseDataObj:
            def __init__(self, target_device_idx: int = None):
                pass
        """,
    "data_objects/recmat.py": """
        from specula.base_data_obj import BaseDataObj
        class Recmat(BaseDataObj):
            def __init__(self, recmat, norm_factor: float = 0.0):
                pass
        """,
    "processing_objects/modalrec.py": """
        from specula.base_processing_obj import BaseProcessingObj
        class Modalrec(BaseProcessingObj):
            def __init__(self, recmat: Recmat, nmodes: int = None, gains: list = None):
                self.inputs['in_slopes'] = InputValue(type=Slopes, optional=True)
                self.outputs['out_modes'] = self.modes
        """,
    "processing_objects/template_processing_obj.py": """
        from specula.base_processing_obj import BaseProcessingObj
        class ProcessingObjName(BaseProcessingObj):
            def __init__(self, a: int = 1):
                pass
        """,
}


@pytest.fixture()
def fake_root(tmp_path):
    root = tmp_path / "specula"
    for rel, code in FAKE_SPECULA.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(code))
    return root


def test_build_catalog_from_sources(fake_root):
    data, proc = catalog.build_catalog(fake_root, runtime=False)
    assert "Recmat" in data and data["Recmat"]["is_data_obj"]
    rec = proc["Modalrec"]
    assert rec["parameters"]["recmat"]["kind"] == "reference"
    assert rec["parameters"]["recmat"]["default"] == "REQUIRED"
    assert rec["parameters"]["gains"]["kind"] == "data"
    assert "in_slopes" in rec["inputs"] and "out_modes" in rec["outputs"]
    assert "ProcessingObjName" not in proc            # template example skipped


def test_cache_roundtrip_and_key_changes(fake_root, tmp_path, monkeypatch):
    monkeypatch.setenv("SPECULA_STUDIO_CACHE", str(tmp_path / "cache"))
    data, proc = catalog.build_catalog(fake_root, runtime=False)
    path = catalog.cache_path(fake_root)
    catalog.write_cache(path, data, proc, fake_root)
    assert catalog.read_cache(path) == (data, proc)
    (fake_root / "processing_objects" / "modalrec.py").write_text("# changed\n")
    assert catalog.cache_path(fake_root) != path


def test_load_catalog_prefers_cache_and_falls_back(fake_root, tmp_path, monkeypatch):
    monkeypatch.setenv("SPECULA_STUDIO_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(catalog, "specula_root", lambda: fake_root)
    data, proc, info, pending = catalog.load_catalog()
    assert info.source == "bundled" and pending == fake_root
    assert "Modalrec" in proc                       # bundled templates
    d2, p2 = catalog.build_catalog(fake_root, runtime=False)
    catalog.write_cache(catalog.cache_path(fake_root), d2, p2, fake_root)
    data, proc, info, pending = catalog.load_catalog()
    assert info.source == "installed" and pending is None
    assert set(proc) == {"Modalrec"}
    _, _, info, pending = catalog.load_catalog(prefer_installed=False)
    assert info.source == "bundled" and pending is None


def test_optional_inputs_come_from_the_catalog():
    templates = catalog.annotate_optional_inputs({
        "X": {"inputs": {"a": {"desc": "Input (optional)"}, "b": {"desc": "Input"}}},
    })
    help_provider.set_catalog(templates)
    try:
        assert help_provider.is_input_optional("X", "a") is True
        assert help_provider.is_input_optional("X", "b") is False
    finally:
        help_provider.set_catalog({})


def test_installed_specula_sources_parse():
    root = catalog.specula_root()
    if root is None:
        pytest.skip("SPECULA not installed")
    data, proc = catalog.build_catalog(root, runtime=False)
    assert "SimulParams" in data
    assert {"Modalrec", "DM", "AtmoPropagation"} <= set(proc)


def test_arguments_supplied_by_simul_are_not_required():
    # Simul gives every class its data_dir (the class's folder under root_dir)
    # and the internal getters: a calibrator without them is complete
    tpl = {"ImCalibrator": {"parameters": {
        "nmodes": {"default": "REQUIRED"}, "data_dir": {"default": "REQUIRED"},
        "params_dict": {"default": "REQUIRED"}, "input_ref_getter": {"default": "REQUIRED"}}}}
    catalog.apply_simul_supplied(tpl)
    params = tpl["ImCalibrator"]["parameters"]
    assert params["data_dir"]["default"] is None and params["nmodes"]["default"] == "REQUIRED"
    assert "params_dict" not in params and "input_ref_getter" not in params
    data, proc, _, _ = catalog.load_catalog(prefer_installed=False)
    assert proc["ImCalibrator"]["parameters"]["data_dir"]["default"] is None
