import ast
import importlib
import pkgutil
import re
import sys
import yaml
from pathlib import Path

def represent_tuple(dumper, data):
    return dumper.represent_sequence('tag:yaml.org,2002:seq', data)

yaml.add_representer(tuple, represent_tuple)

class SpeculaMetadataParser(ast.NodeVisitor):
    def __init__(self):
        self.found_classes = {}
        # Variadic inputs that some classes may define
        self.variadic_input_classes = {
            "DataStore",
            "DataBuffer",
        }
        self.get_as_data = {
            "Recmat",
            "PupData",
            "Slopes",
            'IFunc',
            'IFuncInv',
            'M2C',
            'Pupilstop',
        }
        # Explicitly define parameters that should NEVER be treated as references
        self.ref_block_list = {'target_device_idx', 'precision'}

    def visit_ClassDef(self, node):
        base_names = [ast.unparse(b) for b in node.bases]

        # Start with a neutral category; we'll determine it deterministically after inheritance resolution.
        class_info = {
            "class_name": node.name,
            "bases": base_names,
            "category": "other",   # placeholder; real category assigned in resolve_inheritance()
            "parameters": {},
            "inputs": {},
            "outputs": [],
        }

        # Parse __init__ for parameters and AST-declared inputs/outputs
        for item in node.body:
            if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                self._parse_init(item, class_info)

        # Record class for Pass 2 (Inheritance Resolution)
        self.found_classes[node.name] = class_info

    def _parse_init(self, node, info):
        args = node.args.args[1:]  # Skip 'self'
        defaults = node.args.defaults
        diff = len(args) - len(defaults)

        for i, arg in enumerate(args):
            name = arg.arg
            param_type = "Any"
            if arg.annotation:
                param_type = ast.unparse(arg.annotation)

            default_val = "REQUIRED"
            if i >= diff:
                default_node = defaults[i - diff]
                try:
                    default_val = ast.literal_eval(default_node)
                except Exception as e:
                    print(f"[INIT_PARSE] Could not evaluate default for {name} in {info['class_name']}: {e}")
                    default_val = ast.unparse(default_node)

            info["parameters"][name] = { "type": param_type, "default": default_val }

        # Handle AST-assigned inputs/outputs found within __init__ body
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute):
                        if target.value.attr == "inputs":
                            key = self._get_key(target.slice)
                            if isinstance(stmt.value, ast.Call):
                                for kw in stmt.value.keywords:
                                    if kw.arg == "type":
                                        info["inputs"][key] = {
                                            "type": ast.unparse(kw.value),
                                            "kind": "variadic" if (
                                                key == "input_list" and
                                                info.get("class_name") in self.variadic_input_classes
                                            ) else "single"
                                        }
                        elif target.value.attr == "outputs":
                            key = self._get_key(target.slice)
                            if key not in info["outputs"]:
                                info["outputs"].append(key)

    def _get_key(self, node):
        if isinstance(node, ast.Constant): return node.value
        try:
            return ast.unparse(node).strip("'").strip('"')
        except Exception as e:
            print(f"[KEY_PARSE] Error parsing key from node: {e}")
            return str(node)

    def resolve_inheritance(self):
        """
        Pass 2: Enrich from bases (parameters/inputs/outputs), then deterministically
        assign category purely from inheritance: BaseProcessingObj / BaseDataObj / other.

        Rules:
        - If (directly or indirectly) any ancestor base name (name suffix) is 'BaseProcessingObj' -> processing_objects
        - Else if any ancestor base name is 'BaseDataObj' -> data_objects
        - Else -> other
        """
        # First: multi-pass enrichment (existing behavior) so child parameters/inputs/outputs
        # can inherit metadata from base classes found in the AST.
        for _ in range(3):  # multi-pass to resolve multi-level inheritance
            for class_name, info in list(self.found_classes.items()):
                # Variadic default input
                if class_name in self.variadic_input_classes:
                    if "input_list" not in info["inputs"]:
                        info["inputs"]["input_list"] = {"type": "Any", "kind": "variadic"}
                    info["inputs"]["input_list"]["kind"] = "variadic"

                # Enrich from bases that we discovered in AST
                for base in info["bases"]:
                    base_short = base.split('.')[-1]
                    if base_short in self.found_classes:
                        base_data = self.found_classes[base_short]

                        # Enrich parameters (only fill missing fields)
                        for p_name in list(info["parameters"].keys()):
                            if p_name in base_data.get("parameters", {}):
                                base_meta = base_data["parameters"][p_name]
                                child_meta = info["parameters"][p_name]
                                if child_meta.get("type") in [None, "Any"]:
                                    child_meta["type"] = base_meta.get("type", "Any")
                                if child_meta.get("default") in [None, "REQUIRED"]:
                                    if "default" in base_meta:
                                        child_meta["default"] = base_meta["default"]

                        # Enrich inputs/outputs from base if missing
                        for inp, meta in base_data.get("inputs", {}).items():
                            if inp not in info["inputs"]:
                                info["inputs"][inp] = meta.copy()
                        for out in base_data.get("outputs", []):
                            if out not in info["outputs"]:
                                info["outputs"].append(out)

        # After enrichment, deterministically assign category using only base-class names
        for class_name, info in list(self.found_classes.items()):
            info["category"] = self._determine_category_from_bases(class_name)

        # After category assignment, assign parameter kinds as before
        for class_name, info in list(self.found_classes.items()):
            self._assign_parameter_kinds(info)

    def _determine_category_from_bases(self, class_name, visited=None):
        """
        Determine category ('processing_objects'|'data_objects'|'other') by walking base names.

        We treat a base name matching 'BaseProcessingObj' (suffix) as processing,
        and 'BaseDataObj' as data. The walk is done over AST-known classes as well,
        so indirect inheritance through classes found in the files will be followed.
        """
        if visited is None:
            visited = set()
        if class_name in visited:
            return "other"  # circular guard
        visited.add(class_name)

        info = self.found_classes.get(class_name)
        if not info:
            return "other"

        # Examine direct bases first
        for base in info.get("bases", []):
            base_short = base.split('.')[-1]  # ignore module prefix
            if base_short == "BaseProcessingObj":
                return "processing_objects"
            if base_short == "BaseDataObj":
                return "data_objects"

        # If none direct, follow bases that are themselves in found_classes
        for base in info.get("bases", []):
            base_short = base.split('.')[-1]
            if base_short in self.found_classes:
                cat = self._determine_category_from_bases(base_short, visited)
                if cat != "other":
                    return cat

        # Nothing matched
        return "other"

    def _assign_parameter_kinds(self, info):
        """
        Assign 'kind' to parameters based on type:
        - If type is a known class that (per our deterministic inheritance) is a Data Object -> 'reference'
        - If type is a list/dict of Data Objects -> 'reference'
        - Otherwise -> 'value'
        """
        for param, meta in info["parameters"].items():
            p_type = meta.get("type")

            if param in self.ref_block_list:
                meta["kind"] = "value"
                continue

            if self.is_data_object_type(p_type) or self.is_generic_of_data_object(p_type) or p_type == 'dict':                
                if p_type in self.get_as_data:
                    meta["kind"] = "object"
                else:
                    meta["kind"] = "reference"
            else:
                meta["kind"] = "value"

            # Debug
            print(f"[KIND_DEBUG] {info.get('class_name', 'unknown')}.{param}: type={p_type}, kind={meta['kind']}")

    def is_data_object_type(self, type_str):
        """Check if a type string refers to a class we deterministically classified as data object."""
        if not type_str:
            return False

        # Consider short name if dotted; e.g., 'specula.data_objects.Pupilstop' -> 'Pupilstop'
        candidate = type_str.split('.')[-1]
        # If candidate is one of our found classes, check its category
        if candidate in self.found_classes:
            return self.found_classes[candidate].get("category") == "data_objects"

        # No match -> False
        return False

    def is_generic_of_data_object(self, type_str):
        """Check if a generic type contains a Data Object inner type (list[T], dict[K,V])."""
        if not type_str:
            return False

        list_pattern = r'^(list|List)\[([^]]+)\]$'
        dict_pattern = r'^(dict|Dict)\[([^]]+),([^]]+)\]$'

        list_match = re.match(list_pattern, type_str)
        if list_match:
            inner_type = list_match.group(2).strip()
            return self.is_data_object_type(inner_type)

        dict_match = re.match(dict_pattern, type_str)
        if dict_match:
            value_type = dict_match.group(3).strip()
            return self.is_data_object_type(value_type)

        return False

    @staticmethod
    def _type_name(descriptor):
        """Return the string name of a runtime descriptor's .type attribute."""
        t = descriptor.type
        return t.__name__ if hasattr(t, '__name__') else str(t)

    def enrich_from_runtime(self):
        """
        Pass 3: Optional runtime enrichment for inputs/outputs (unchanged).
        This method no longer affects category classification — that's done purely via inheritance.
        """
        import inspect
        import specula
        import specula.processing_objects as proc_pkg
        import specula.data_objects as data_pkg

        specula.init(0)  # CPU-only mode

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

            # Inputs authoritative replacement
            if hasattr(klass, 'input_names') and callable(klass.input_names):
                try:
                    input_dict = klass.input_names()
                    if isinstance(input_dict, dict) and input_dict:
                        new_inputs = {}
                        for inp_name, inp_desc in input_dict.items():
                            try:
                                inp_type_name = self._type_name(inp_desc)
                            except Exception:
                                inp_type_name = str(getattr(inp_desc, "type", inp_desc))
                            inp_description = getattr(inp_desc, "desc", getattr(inp_desc, "description", "") or "")
                            existing_kind = info["inputs"].get(inp_name, {}).get("kind", "single")
                            if class_name in self.variadic_input_classes and inp_name == "input_list":
                                existing_kind = "variadic"
                            new_inputs[inp_name] = {"type": inp_type_name, "kind": existing_kind, "desc": inp_description}
                        info["inputs"] = new_inputs
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.input_names() failed: {e}")

            # Outputs authoritative replacement (kept as typed list)
            if hasattr(klass, 'output_names') and callable(klass.output_names):
                try:
                    output_dict = klass.output_names()
                    if isinstance(output_dict, dict) and output_dict:
                        new_outputs = []
                        output_names_list = []
                        for out_name, out_desc in output_dict.items():
                            try:
                                out_type_name = self._type_name(out_desc)
                            except Exception:
                                out_type_name = str(getattr(out_desc, "type", out_desc))
                            out_description = getattr(out_desc, "desc", getattr(out_desc, "description", "") or "")
                            new_outputs.append({"name": out_name, "type": out_type_name, "desc": out_description})
                            output_names_list.append(out_name)
                        info["outputs"] = new_outputs
                        info["output_names"] = output_names_list
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.output_names() failed: {e}")

        print(f"[RUNTIME] Enriched {enriched_count} classes (inputs/outputs).")

def run_parser(input_folders, output_folder):
    parser = SpeculaMetadataParser()

    # Pass 1: Scan files (AST)
    for folder in input_folders:
        path = Path(folder)
        if not path.exists(): continue
        for py_file in path.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read())
                    parser.visit(tree)
                except Exception as e:
                    print(f"Skipping {py_file} due to error: {e}")

    # Pass 2: Merge data and deterministically classify by inheritance
    parser.resolve_inheritance()

    # Pass 3: Optional runtime enrichment of inputs/outputs (this won't change classification)
    try:
        parser.enrich_from_runtime()
    except Exception as e:
        # Make runtime enrichment optional and non-fatal
        print(f"[RUNTIME] enrichment skipped due to error: {e}")

    # Debug: Print found classes and categories
    print(f"\n[DEBUG] Found {len(parser.found_classes)} classes:")
    for class_name, data in parser.found_classes.items():
        has_io = bool(data.get('inputs') or data.get('outputs'))
        print(f"  - {class_name}: bases={data.get('bases', [])}, has_io={has_io}, category={data.get('category')}")

    # Create directories
    base_path = Path(output_folder)
    subfolders = {
        "data_objects": base_path / "data_objects",
        "processing_objects": base_path / "processing_objects",
        "other": base_path / "other"
    }
    for folder in subfolders.values():
        folder.mkdir(parents=True, exist_ok=True)

    # Track classes referenced as parameter types
    referenced_classes = set()
    for class_name, class_info in parser.found_classes.items():
        for param_name, param_info in class_info.get("parameters", {}).items():
            param_type = param_info.get("type")
            if param_type:
                candidate = param_type.split('.')[-1]
                if candidate in parser.found_classes:
                    referenced_classes.add(candidate)
                    print(f"[DEBUG] Class {class_name} references {candidate} in parameter {param_name}")

    # Save to YAML templates
    count = 0
    saved_classes = set()
    for class_name, data in parser.found_classes.items():
        is_target = data.get("category") in ("data_objects", "processing_objects")
        has_io = bool(data.get("inputs") or data.get("outputs"))
        is_referenced = class_name in referenced_classes

        if not (is_target or has_io or is_referenced):
            continue

        category = data.pop("category", "other")
        target_dir = subfolders.get(category, subfolders["other"])

        print(f"\n[DEBUG] Saving class {class_name} (category: {category})")
        print(f"[DEBUG] Parameters:")
        for param_name, param_info in data.get("parameters", {}).items():
            print(f"  - {param_name}: type={param_info.get('type')}, kind={param_info.get('kind')}, default={param_info.get('default')}")

        with open(target_dir / f"{class_name}.yml", "w", encoding="utf-8") as yf:
            yaml.dump({class_name: data}, yf, sort_keys=False, default_flow_style=False)
        count += 1
        saved_classes.add(class_name)

    unsaved = set(parser.found_classes.keys()) - saved_classes
    if unsaved:
        print(f"\n[DEBUG] Classes NOT saved to templates ({len(unsaved)}):")
        for class_name in sorted(unsaved):
            data = parser.found_classes[class_name]
            print(f"  - {class_name}: bases={data.get('bases', [])}")

    print(f"\nSuccessfully generated {count} YAML templates.")

    merged_data = {cn: parser.found_classes[cn] for cn in saved_classes}
    merged_path = base_path / "all_templates_merged.yml"
    with open(merged_path, "w", encoding="utf-8") as yf:
        yaml.dump(merged_data, yf, sort_keys=True, default_flow_style=False)
    print(f"Created merged template file: {merged_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python parse_classes.py <input_folder1> <input_folder2> ... <output_folder>")
    else:
        run_parser(sys.argv[1:-1], sys.argv[-1])