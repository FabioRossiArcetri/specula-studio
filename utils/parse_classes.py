import ast
import importlib
import math
import pkgutil
import re
import sys
import yaml
from pathlib import Path


def represent_tuple(dumper, data):
    return dumper.represent_sequence('tag:yaml.org,2002:seq', data)

yaml.add_representer(tuple, represent_tuple)


# ── Array-type detection ──────────────────────────────────────────────────────
_ARRAY_BASE_TYPES = frozenset([
    "list", "List",
    "ndarray", "np.ndarray", "numpy.ndarray",
    "array",
    "tuple", "Tuple",
    "sequence", "Sequence",
])

def _is_array_type(type_str: str) -> bool:
    """Return True if *type_str* describes an array / sequence parameter."""
    if not type_str:
        return False
    s = type_str.strip()
    opt_match = re.match(r'^Optional\[(.+)\]$', s)
    if opt_match:
        s = opt_match.group(1).strip()
    bare = s.split('[')[0].strip()
    return bare in _ARRAY_BASE_TYPES


def _try_eval_inf(node):
    """
    Evaluate AST nodes representing float ±infinity that ast.literal_eval
    cannot handle (float('inf'), math.inf, np.inf, unary minus variants).
    Returns the float value or None if not recognised.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "float"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        s = node.args[0].value.strip().lower()
        if s in ("inf", "infinity", "+inf", "+infinity"):
            return math.inf
        if s in ("-inf", "-infinity"):
            return -math.inf

    if (
        isinstance(node, ast.Attribute)
        and node.attr == "inf"
        and isinstance(node.value, ast.Name)
    ):
        return math.inf

    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
    ):
        inner = _try_eval_inf(node.operand)
        if inner is not None:
            return -inner

    return None


class SpeculaMetadataParser(ast.NodeVisitor):
    def __init__(self):
        self.found_classes = {}

        # Classes that declare a variadic input_list pin
        self.variadic_input_classes = {
            "DataStore",
            "DataBuffer",
            "AtmoPropagation",
        }
        self.variadic_input_names = {
            "input_list",
            "common_layer_list",
        }

        # Parameters that must never be treated as object references
        self.ref_block_list = {"target_device_idx", "precision"}

        # NOTE: the former `get_as_data` set (Recmat, PupData, IFunc, M2C, …)
        # has been removed.  All data-object typed parameters now receive
        # kind: "reference" uniformly, which activates the _ref ↔ _object
        # toggle in the GUI for every one of them.  There is no longer a
        # hard-coded list of "always load from disk" types; the user chooses
        # at edit time.

    # =========================================================================
    # AST visitor
    # =========================================================================

    def visit_ClassDef(self, node):
        base_names = [ast.unparse(b) for b in node.bases]
        class_info = {
            "class_name": node.name,
            "bases": base_names,
            "category": "other",
            "parameters": {},
            "inputs": {},
            "outputs": [],
        }
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                self._parse_init(item, class_info)
        self.found_classes[node.name] = class_info

    def _parse_init(self, node, info):
        args     = node.args.args[1:]
        defaults = node.args.defaults
        diff     = len(args) - len(defaults)

        for i, arg in enumerate(args):
            name       = arg.arg
            param_type = "Any"
            if arg.annotation:
                param_type = ast.unparse(arg.annotation)

            default_val = "REQUIRED"
            if i >= diff:
                default_node = defaults[i - diff]
                try:
                    default_val = ast.literal_eval(default_node)
                except Exception:
                    inf_val = _try_eval_inf(default_node)
                    if inf_val is not None:
                        default_val = inf_val
                    else:
                        default_val = ast.unparse(default_node)
                        print(
                            f"[INIT_PARSE] Could not evaluate default for "
                            f"{name} in {info['class_name']}: "
                            f"stored as string '{default_val}'"
                        )

            info["parameters"][name] = {"type": param_type, "default": default_val}

        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                    ):
                        if target.value.attr == "inputs":
                            key = self._get_key(target.slice)
                            if isinstance(stmt.value, ast.Call):
                                for kw in stmt.value.keywords:
                                    if kw.arg == "type":
                                        info["inputs"][key] = {
                                            "type": ast.unparse(kw.value),
                                            "kind": (
                                                "variadic"
                                                if (
                                                    key in self.variadic_input_names
                                                    and info.get("class_name")
                                                    in self.variadic_input_classes
                                                )
                                                else "single"
                                            ),
                                        }
                        elif target.value.attr == "outputs":
                            key = self._get_key(target.slice)
                            if key not in info["outputs"]:
                                info["outputs"].append(key)

    def _get_key(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        try:
            return ast.unparse(node).strip("'").strip('"')
        except Exception as e:
            print(f"[KEY_PARSE] Error parsing key from node: {e}")
            return str(node)

    # =========================================================================
    # Pass 2 – inheritance resolution and kind assignment
    # =========================================================================

    def resolve_inheritance(self):
        for _ in range(3):
            for class_name, info in list(self.found_classes.items()):
                if class_name in self.variadic_input_classes:
                    if "input_list" not in info["inputs"]:
                        info["inputs"]["input_list"] = {"type": "Any", "kind": "variadic"}
                    info["inputs"]["input_list"]["kind"] = "variadic"

                for base in info["bases"]:
                    base_short = base.split(".")[-1]
                    if base_short in self.found_classes:
                        base_data = self.found_classes[base_short]
                        for p_name in list(info["parameters"].keys()):
                            if p_name in base_data.get("parameters", {}):
                                base_meta  = base_data["parameters"][p_name]
                                child_meta = info["parameters"][p_name]
                                if child_meta.get("type") in [None, "Any"]:
                                    child_meta["type"] = base_meta.get("type", "Any")
                                if child_meta.get("default") in [None, "REQUIRED"]:
                                    if "default" in base_meta:
                                        child_meta["default"] = base_meta["default"]
                        for inp, meta in base_data.get("inputs", {}).items():
                            if inp not in info["inputs"]:
                                info["inputs"][inp] = meta.copy()
                        for out in base_data.get("outputs", []):
                            if out not in info["outputs"]:
                                info["outputs"].append(out)

        for class_name, info in list(self.found_classes.items()):
            info["category"] = self._determine_category_from_bases(class_name)

        for class_name, info in list(self.found_classes.items()):
            self._assign_parameter_kinds(info)

    def _determine_category_from_bases(self, class_name, visited=None):
        if visited is None:
            visited = set()
        if class_name in visited:
            return "other"
        visited.add(class_name)

        info = self.found_classes.get(class_name)
        if not info:
            return "other"

        for base in info.get("bases", []):
            base_short = base.split(".")[-1]
            if base_short == "BaseProcessingObj":
                return "processing_objects"
            if base_short == "BaseDataObj":
                return "data_objects"

        for base in info.get("bases", []):
            base_short = base.split(".")[-1]
            if base_short in self.found_classes:
                cat = self._determine_category_from_bases(base_short, visited)
                if cat != "other":
                    return cat

        return "other"

    def _assign_parameter_kinds(self, info):
        """
        Assign 'kind' to each parameter:

        kind: "reference"
            The parameter holds a data-object — either wired as a live _ref
            link or loaded from disk via the _object suffix.  This covers ALL
            data-object typed parameters without exception; the GUI shows a
            _ref ↔ _object toggle for every one of them.

            (The former distinction between "reference" and "object" has been
            removed.  There is no longer a hard-coded list of "always-from-disk"
            types.  If a parameter's type is a data-object class, it gets
            kind: "reference" and the user chooses the mode in the GUI.)

        kind: "data"
            Array / sequence parameter.  Supports inline values and the _data
            file suffix (e.g. vect_amplitude_data: 'filename').

        kind: "tag"
            CalibManager filename tag (bare 'tag' or any *_tag parameter).
            GUI adds a browse button.

        kind: "value"
            Plain scalar / string / bool.
        """
        for param, meta in info["parameters"].items():
            p_type = meta.get("type", "Any")

            # ── hard exclusions ───────────────────────────────────────────────
            if param in self.ref_block_list:
                meta["kind"] = "value"
                print(f"[KIND_DEBUG] {info.get('class_name','?')}.{param}: type={p_type}, kind=value (blocklist)")
                continue

            # ── tag parameters ────────────────────────────────────────────────
            if param == "tag" or param.endswith("_tag"):
                meta["kind"] = "tag"
                print(f"[KIND_DEBUG] {info.get('class_name','?')}.{param}: type={p_type}, kind=tag")
                continue

            # ── data-object reference ─────────────────────────────────────────
            # ALL parameters typed as a data-object class (whether they were
            # previously "object" or "reference") become kind: "reference".
            if (
                self.is_data_object_type(p_type)
                or self.is_generic_of_data_object(p_type)
                or p_type == "dict"
            ):
                meta["kind"] = "reference"
                print(f"[KIND_DEBUG] {info.get('class_name','?')}.{param}: type={p_type}, kind=reference")
                continue

            # ── array / sequence ──────────────────────────────────────────────
            if _is_array_type(p_type):
                meta["kind"] = "data"
                print(f"[KIND_DEBUG] {info.get('class_name','?')}.{param}: type={p_type}, kind=data")
                continue

            # ── plain scalar ──────────────────────────────────────────────────
            meta["kind"] = "value"
            print(f"[KIND_DEBUG] {info.get('class_name','?')}.{param}: type={p_type}, kind=value")

    # =========================================================================
    # Type-classification helpers
    # =========================================================================

    def is_data_object_type(self, type_str):
        if not type_str:
            return False
        candidate = type_str.split(".")[-1]
        if candidate in self.found_classes:
            return self.found_classes[candidate].get("category") == "data_objects"
        return False

    def is_generic_of_data_object(self, type_str):
        if not type_str:
            return False
        list_match = re.match(r'^(list|List)\[([^\]]+)\]$', type_str)
        if list_match:
            return self.is_data_object_type(list_match.group(2).strip())
        dict_match = re.match(r'^(dict|Dict)\[([^,]+),([^\]]+)\]$', type_str)
        if dict_match:
            return self.is_data_object_type(dict_match.group(3).strip())
        return False

    @staticmethod
    def _type_name(descriptor):
        t = descriptor.type
        return t.__name__ if hasattr(t, "__name__") else str(t)

    # =========================================================================
    # Pass 3 – optional runtime enrichment
    # =========================================================================

    def enrich_from_runtime(self):
        """Replace AST-derived inputs/outputs with authoritative runtime data."""
        import inspect
        import specula
        import specula.processing_objects as proc_pkg
        import specula.data_objects as data_pkg

        specula.init(0)

        runtime_classes = {}
        for pkg in [proc_pkg, data_pkg]:
            for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
                full_name = f"{pkg.__name__}.{module_name}"
                try:
                    module = importlib.import_module(full_name)
                except Exception as e:
                    print(f"[RUNTIME] Could not import {full_name}: {e}")
                    continue
                for cname, klass in inspect.getmembers(module, inspect.isclass):
                    if klass.__module__ == module.__name__:
                        runtime_classes[cname] = klass

        enriched_count = 0
        for class_name, info in list(self.found_classes.items()):
            if class_name not in runtime_classes:
                continue
            klass = runtime_classes[class_name]

            if hasattr(klass, "input_names") and callable(klass.input_names):
                try:
                    input_dict = klass.input_names()
                    if isinstance(input_dict, dict) and input_dict:
                        new_inputs = {}
                        for inp_name, inp_desc in input_dict.items():
                            try:
                                inp_type_name = self._type_name(inp_desc)
                            except Exception:
                                inp_type_name = str(getattr(inp_desc, "type", inp_desc))
                            inp_description = getattr(inp_desc, "desc",
                                getattr(inp_desc, "description", "") or "")
                            existing_kind = info["inputs"].get(inp_name, {}).get("kind", "single")
                            if (
                                class_name in self.variadic_input_classes
                                and inp_name == "input_list"
                            ):
                                existing_kind = "variadic"
                            new_inputs[inp_name] = {
                                "type": inp_type_name,
                                "kind": existing_kind,
                                "desc": inp_description,
                            }
                        info["inputs"] = new_inputs
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.input_names() failed: {e}")

            if hasattr(klass, "output_names") and callable(klass.output_names):
                try:
                    output_dict = klass.output_names()
                    if isinstance(output_dict, dict) and output_dict:
                        new_outputs       = []
                        output_names_list = []
                        for out_name, out_desc in output_dict.items():
                            try:
                                out_type_name = self._type_name(out_desc)
                            except Exception:
                                out_type_name = str(getattr(out_desc, "type", out_desc))
                            out_description = getattr(out_desc, "desc",
                                getattr(out_desc, "description", "") or "")
                            new_outputs.append({
                                "name": out_name,
                                "type": out_type_name,
                                "desc": out_description,
                            })
                            output_names_list.append(out_name)
                        info["outputs"]      = new_outputs
                        info["output_names"] = output_names_list
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.output_names() failed: {e}")

        print(f"[RUNTIME] Enriched {enriched_count} classes (inputs/outputs).")


# =============================================================================
# Entry point
# =============================================================================

def run_parser(input_folders, output_folder):
    parser = SpeculaMetadataParser()

    for folder in input_folders:
        path = Path(folder)
        if not path.exists():
            continue
        for py_file in path.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read())
                    parser.visit(tree)
                except Exception as e:
                    print(f"Skipping {py_file} due to error: {e}")

    parser.resolve_inheritance()

    try:
        parser.enrich_from_runtime()
    except Exception as e:
        print(f"[RUNTIME] enrichment skipped due to error: {e}")

    print(f"\n[DEBUG] Found {len(parser.found_classes)} classes:")
    for class_name, data in parser.found_classes.items():
        has_io = bool(data.get("inputs") or data.get("outputs"))
        print(
            f"  - {class_name}: bases={data.get('bases', [])}, "
            f"has_io={has_io}, category={data.get('category')}"
        )

    base_path  = Path(output_folder)
    subfolders = {
        "data_objects":       base_path / "data_objects",
        "processing_objects": base_path / "processing_objects",
        "other":              base_path / "other",
    }
    for folder in subfolders.values():
        folder.mkdir(parents=True, exist_ok=True)

    referenced_classes = set()
    for class_name, class_info in parser.found_classes.items():
        for param_name, param_info in class_info.get("parameters", {}).items():
            param_type = param_info.get("type")
            if param_type:
                candidate = param_type.split(".")[-1]
                if candidate in parser.found_classes:
                    referenced_classes.add(candidate)
                    print(f"[DEBUG] Class {class_name} references {candidate} in parameter {param_name}")

    count         = 0
    saved_classes = set()

    for class_name, data in parser.found_classes.items():
        is_target     = data.get("category") in ("data_objects", "processing_objects")
        has_io        = bool(data.get("inputs") or data.get("outputs"))
        is_referenced = class_name in referenced_classes

        if not (is_target or has_io or is_referenced):
            continue

        category   = data.pop("category", "other")
        target_dir = subfolders.get(category, subfolders["other"])

        # is_data_obj flag lets NodeManager._is_data_obj_node() work for all
        # inheritance depths without walking the bases list at runtime.
        data["is_data_obj"] = (category == "data_objects")

        print(f"\n[DEBUG] Saving class {class_name} (category: {category})")
        print(f"[DEBUG] Parameters:")
        for param_name, param_info in data.get("parameters", {}).items():
            print(
                f"  - {param_name}: type={param_info.get('type')}, "
                f"kind={param_info.get('kind')}, default={param_info.get('default')}"
            )

        with open(target_dir / f"{class_name}.yml", "w", encoding="utf-8") as yf:
            yaml.dump(
                {class_name: data}, yf,
                sort_keys=False,
                default_flow_style=False,
            )
        count += 1
        saved_classes.add(class_name)

    unsaved = set(parser.found_classes.keys()) - saved_classes
    if unsaved:
        print(f"\n[DEBUG] Classes NOT saved to templates ({len(unsaved)}):")
        for class_name in sorted(unsaved):
            d = parser.found_classes[class_name]
            print(f"  - {class_name}: bases={d.get('bases', [])}")

    print(f"\nSuccessfully generated {count} YAML templates.")

    merged_data = {cn: parser.found_classes[cn] for cn in saved_classes}
    merged_path = base_path / "all_templates_merged.yml"
    with open(merged_path, "w", encoding="utf-8") as yf:
        yaml.dump(merged_data, yf, sort_keys=True, default_flow_style=False)
    print(f"Created merged template file: {merged_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python parse_classes.py "
            "<input_folder1> [input_folder2 …] <output_folder>"
        )
    else:
        run_parser(sys.argv[1:-1], sys.argv[-1])

