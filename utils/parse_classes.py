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
        # Base classes that define a "Specula Object"
        self.data_base_classes = {'BaseDataObj', 'Layer', 'ElectricField', 'SimulParams'}
        self.proc_base_classes = {
            'BaseProcessingObj', 'BaseOperation', 'BaseSlopec', 'IirFilter', 'Slopec',
            'BaseCalibrator', 'BaseWFS', 'BaseWavefront', 'BaseGenerator'
        }
        self.variadic_input_classes = {
            "DataStore",
            "DataBuffer",
        }
        self.target_bases = self.data_base_classes.union(self.proc_base_classes)
        
        # Define which Data Objects are "reference" vs "object"
        # These are special classes that output reference pins (squares)
        self.reference_data_objects = {
            'SimulParams',
            'Pupilstop', 
            'Source',
        }
        
        # Explicitly define parameters that should NEVER be treated as references
        self.ref_block_list = {'target_device_idx', 'precision'}

    def visit_ClassDef(self, node):
        base_names = [ast.unparse(b) for b in node.bases]
        
        # Categorize based on inheritance
        category = "other"
        if any(base in self.data_base_classes for base in base_names):
            category = "data_objects"
        elif any(base in self.proc_base_classes for base in base_names):
            category = "processing_objects"

        class_info = {
            "class_name": node.name,
            "bases": base_names,
            "category": category,
            "parameters": {},
            "inputs": {},
            "outputs": [],
        }

        # Parse __init__ for parameters and references
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
            
            # Save as a standard parameter
            info["parameters"][name] = { "type": param_type, "default": default_val }

        # Handle Specula-specific traits (inputs/outputs)
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
        Pass 2: Strict Whitelist Enrichment.
        Base classes ONLY provide metadata for parameters already found in child.
        """
        for _ in range(3):  # Multi-pass for multi-level inheritance
            for class_name, info in self.found_classes.items():
                
                # --- Step A: Variadic Logic (Force specific inputs) ---
                if class_name in self.variadic_input_classes:
                    if "input_list" not in info["inputs"]:
                        info["inputs"]["input_list"] = {"type": "Any", "kind": "variadic"}
                    info["inputs"]["input_list"]["kind"] = "variadic"

                # --- Step B: Strict Enrichment from Bases ---
                for base in info["bases"]:
                    if base in self.found_classes:
                        base_data = self.found_classes[base]
                        
                        # 1. ENRICHMENT ONLY:
                        for p_name in list(info["parameters"].keys()):
                            if p_name in base_data["parameters"]:
                                base_meta = base_data["parameters"][p_name]
                                child_meta = info["parameters"][p_name]
                                
                                # Only fill in metadata if child is missing it
                                if child_meta.get("type") in [None, "Any"]:
                                    child_meta["type"] = base_meta.get("type", "Any")
                                
                                # Use base default ONLY if child is REQUIRED or None
                                if child_meta.get("default") in [None, "REQUIRED"]:
                                    if "default" in base_meta:
                                        child_meta["default"] = base_meta["default"]

                        # 2. INPUTS/OUTPUTS (Framework Level)
                        for inp, meta in base_data.get("inputs", {}).items():
                            if inp not in info["inputs"]:
                                info["inputs"][inp] = meta.copy()
                        
                        for out in base_data.get("outputs", []):
                            if out not in info["outputs"]:
                                info["outputs"].append(out)

                # --- Step C: Kind Assignment ---
                self._assign_parameter_kinds(info)


   
    
    def _assign_parameter_kinds(self, info):
        """
        Assign 'kind' to parameters based on type:
        - If type is a known class that inherits from Data Object bases -> 'reference'
        - If type is a list/dict of Data Objects -> 'reference'
        - Otherwise -> 'value'
        """
        for param, meta in info["parameters"].items():
            p_type = meta.get("type")
            
            # First check block list
            if param in self.ref_block_list:
                meta["kind"] = "value"
                continue                         
            
            # Determine the kind
            if self.is_data_object_type(p_type) or self.is_generic_of_data_object(p_type):
                # ALL Data Objects and generics of Data Objects should be references
                meta["kind"] = "reference"
            else:
                meta["kind"] = "value"
            
            # Debug
            print(f"[KIND_DEBUG] {info.get('class_name', 'unknown')}.{param}: type={p_type}, kind={meta['kind']}")

    # Helper function to check if a type is a Data Object
    def is_data_object_type(self, type_str):
        """Check if a type string represents a Data Object."""
        if not type_str:
            return False
            
        # First check if it's a data base class
        if type_str in self.data_base_classes:
            return True
            
        # Check if it's a known class
        if type_str in self.found_classes:
            class_info = self.found_classes[type_str]
            bases = class_info.get("bases", [])
            
            # Check direct inheritance
            for base in bases:
                # Get base name without module prefix for comparison
                base_name = base.split('.')[-1]
                if base_name in self.data_base_classes or self.is_data_object_type(base_name):
                    return True
        return False
    
    # Helper function to check if it's a generic type (list/dict) of Data Objects
    def is_generic_of_data_object(self, type_str):
        """Check if type is list[T] or dict[K, V] where T/V is a Data Object."""
        if not type_str:
            return False
            
        # Check for list[T]                
        # Match patterns like list[T], List[T], dict[K, V], Dict[K, V]
        list_pattern = r'^(list|List)\[([^]]+)\]$'
        dict_pattern = r'^(dict|Dict)\[([^]]+),([^]]+)\]$'
        
        list_match = re.match(list_pattern, type_str)
        if list_match:
            inner_type = list_match.group(2).strip()
            return self.is_data_object_type(inner_type)
        
        dict_match = re.match(dict_pattern, type_str)
        if dict_match:
            # For dict, check the value type (second group)
            value_type = dict_match.group(3).strip()
            return self.is_data_object_type(value_type)
        
        return False
    
    # Helper function to extract inner type from generic annotations
    def extract_inner_type(self, type_str):
        """Extract inner type from generic annotations like list[T], dict[K, V]."""
        if not type_str:
            return None
            
        # Match patterns like list[T], List[T], dict[K, V], Dict[K, V]
        list_pattern = r'^(list|List)\[([^]]+)\]$'
        dict_pattern = r'^(dict|Dict)\[([^]]+),([^]]+)\]$'
        
        list_match = re.match(list_pattern, type_str)
        if list_match:
            inner_type = list_match.group(2).strip()
            return inner_type
        
        dict_match = re.match(dict_pattern, type_str)
        if dict_match:
            # For dict, check the value type (third group)
            value_type = dict_match.group(3).strip()
            return value_type
        
        return None

    @staticmethod
    def _type_name(descriptor):
        """Return the string name of a runtime descriptor's .type attribute."""
        t = descriptor.type
        return t.__name__ if hasattr(t, '__name__') else str(t)

    def enrich_from_runtime(self):
        """
        Pass 3: For classes that define input_names() and/or output_names()
        classmethods, call them at runtime and REPLACE the AST-parsed
        inputs/outputs with the authoritative runtime data.

        specula is assumed to always be installed.
        """
        import inspect
        import specula
        import specula.processing_objects as proc_pkg
        import specula.data_objects as data_pkg

        specula.init(0)  # CPU-only mode

        # Build map of class_name -> actual class
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
        for class_name, info in self.found_classes.items():
            if class_name not in runtime_classes:
                continue
            klass = runtime_classes[class_name]

            # Replace inputs from input_names() if available
            if hasattr(klass, 'input_names') and callable(klass.input_names):
                try:
                    input_dict = klass.input_names()
                    if isinstance(input_dict, dict) and len(input_dict) > 0:
                        new_inputs = {}
                        for inp_name, inp_desc in input_dict.items():
                            inp_type_name = self._type_name(inp_desc)
                            inp_description = inp_desc.desc if hasattr(inp_desc, 'desc') else ''
                            existing_kind = info["inputs"].get(inp_name, {}).get("kind", "single")
                            if class_name in self.variadic_input_classes and inp_name == "input_list":
                                existing_kind = "variadic"
                            new_inputs[inp_name] = {
                                "type": inp_type_name,
                                "kind": existing_kind,
                                "desc": inp_description,
                            }
                        info["inputs"] = new_inputs
                        print(f"[RUNTIME] {class_name}: replaced inputs from input_names() -> {list(new_inputs.keys())}")
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.input_names() failed: {e}")

            # Replace outputs from output_names() if available
            if hasattr(klass, 'output_names') and callable(klass.output_names):
                try:
                    output_dict = klass.output_names()
                    if isinstance(output_dict, dict) and len(output_dict) > 0:
                        new_outputs = []
                        new_output_details = {}
                        for out_name, out_desc in output_dict.items():
                            out_type_name = self._type_name(out_desc)
                            out_description = out_desc.desc if hasattr(out_desc, 'desc') else ''
                            new_outputs.append(out_name)
                            new_output_details[out_name] = {
                                "type": out_type_name,
                                "desc": out_description,
                            }
                        info["outputs"] = new_outputs
                        info["output_details"] = new_output_details
                        print(f"[RUNTIME] {class_name}: replaced outputs from output_names() -> {new_outputs}")
                        enriched_count += 1
                except Exception as e:
                    print(f"[RUNTIME] {class_name}.output_names() failed: {e}")

        print(f"[RUNTIME] Enriched {enriched_count} class(es) from runtime input_names/output_names.")


def run_parser(input_folders, output_folder):
    parser = SpeculaMetadataParser()

    # Pass 1: Scan files
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

    # Pass 2: Merge data
    parser.resolve_inheritance()

    # Pass 3: Runtime enrichment from input_names() / output_names()
    parser.enrich_from_runtime()

    # Debug: Print all found classes
    print(f"\n[DEBUG] Found {len(parser.found_classes)} classes:")
    for class_name, data in parser.found_classes.items():
        print(f"  - {class_name}: bases={data.get('bases', [])}, has_io={bool(data.get('inputs') or data.get('outputs'))}")

    # Create directories
    base_path = Path(output_folder)
    subfolders = {
        "data_objects": base_path / "data_objects", 
        "processing_objects": base_path / "processing_objects", 
        "other": base_path / "other"
    }
    for folder in subfolders.values(): 
        folder.mkdir(parents=True, exist_ok=True)

    # Track which classes are referenced as parameter types
    referenced_classes = set()
    for class_name, class_info in parser.found_classes.items():
        for param_name, param_info in class_info.get("parameters", {}).items():
            param_type = param_info.get("type")
            if param_type and param_type in parser.found_classes:
                referenced_classes.add(param_type)
                print(f"[DEBUG] Class {class_name} references {param_type} in parameter {param_name}")

    # Save to YAML - be more inclusive
    count = 0
    saved_classes = set()
    for class_name, data in parser.found_classes.items():
        # Check if class is valid Specula node OR is referenced as a parameter type
        is_target = any(b in parser.target_bases for b in data["bases"])
        has_io = bool(data["inputs"] or data["outputs"])
        is_referenced = class_name in referenced_classes
        
        # ALWAYS include classes that are in reference_data_objects
        is_special_ref = class_name in parser.reference_data_objects
        
        if not (is_target or has_io or is_referenced or is_special_ref):
            continue
        
        category = data.pop("category", "other")
        target_dir = subfolders.get(category, subfolders["other"])
        
        # Debug: Check parameter kinds
        print(f"\n[DEBUG] Saving class {class_name} (category: {category})")
        print(f"[DEBUG] Parameters:")
        for param_name, param_info in data.get("parameters", {}).items():
            print(f"  - {param_name}: type={param_info.get('type')}, kind={param_info.get('kind')}, default={param_info.get('default')}")
        
        with open(target_dir / f"{class_name}.yml", "w", encoding="utf-8") as yf:
            yaml.dump({class_name: data}, yf, sort_keys=False, default_flow_style=False)
        count += 1
        saved_classes.add(class_name)

    # Debug: Print which classes weren't saved
    unsaved = set(parser.found_classes.keys()) - saved_classes
    if unsaved:
        print(f"\n[DEBUG] Classes NOT saved to templates ({len(unsaved)}):")
        for class_name in sorted(unsaved):
            data = parser.found_classes[class_name]
            print(f"  - {class_name}: bases={data.get('bases', [])}")

    print(f"\nSuccessfully generated {count} YAML templates.")

    # Also create a merged template file for debugging
    merged_data = {}
    for class_name in saved_classes:
        merged_data[class_name] = parser.found_classes[class_name]
    
    merged_path = base_path / "all_templates_merged.yml"
    with open(merged_path, "w", encoding="utf-8") as yf:
        yaml.dump(merged_data, yf, sort_keys=True, default_flow_style=False)
    print(f"Created merged template file: {merged_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python parse_classes.py <input_folder1> <input_folder2> ... <output_folder>")
    else:
        run_parser(sys.argv[1:-1], sys.argv[-1])