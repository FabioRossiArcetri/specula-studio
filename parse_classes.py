import os
import ast
import yaml
import sys
from pathlib import Path

def represent_tuple(dumper, data):
    return dumper.represent_sequence('tag:yaml.org,2002:seq', data)

yaml.add_representer(tuple, represent_tuple)

class SpeculaMetadataParser(ast.NodeVisitor):
    def __init__(self):
        self.found_classes = {}
        # Base classes that define a "Specula Object"
        self.data_base_classes = {'BaseDataObj', 'Layer', 'ElectricField'}
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
                except:
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
        try: return ast.unparse(node).strip("'").strip('"')
        except: return str(node)

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
        Assign 'kind' to parameters based on type and hardcoded rules:
        - Data Objects in reference_data_objects set -> 'reference'
        - Other Data Objects -> 'object'
        - Keywords + not in block list -> 'reference'
        - Everything else -> 'value'
        """
        ref_keywords = ['telescope', 'atmo', 'target', 'wfs', 'dm', 'source']
        
        for param, meta in info["parameters"].items():
            p_type = meta.get("type")
            is_ref_keyword = any(k in param.lower() for k in ref_keywords)
            
            # First check block list
            if param in self.ref_block_list:
                meta["kind"] = "value"
            
            # Check if type is a known Data Object class
            elif p_type in self.found_classes:
                # Check if this Data Object is in the reference set
                if p_type in self.reference_data_objects:
                    meta["kind"] = "reference"
                else:
                    # For other Data Objects, check their category
                    type_info = self.found_classes.get(p_type, {})
                    if any(base in self.data_base_classes for base in type_info.get("bases", [])):
                        meta["kind"] = "object"  # Data Object that's not a reference
                    else:
                        meta["kind"] = "value"  # Not a Data Object
            
            # Check keyword-based references (not in block list)
            elif is_ref_keyword and param not in self.ref_block_list:
                meta["kind"] = "reference"
            
            # Default to value
            else:
                meta["kind"] = "value"


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

    # Create directories
    base_path = Path(output_folder)
    subfolders = {
        "data_objects": base_path / "data_objects", 
        "processing_objects": base_path / "processing_objects", 
        "other": base_path / "other"
    }
    for folder in subfolders.values(): 
        folder.mkdir(parents=True, exist_ok=True)

    # Save to YAML
    count = 0
    for class_name, data in parser.found_classes.items():
        # Check if class is valid Specula node
        is_target = any(b in parser.target_bases for b in data["bases"])
        has_io = bool(data["inputs"] or data["outputs"])
        
        if not (is_target or has_io):
            continue

        category = data.pop("category", "other")
        target_dir = subfolders.get(category, subfolders["other"])
        
        with open(target_dir / f"{class_name}.yml", "w", encoding="utf-8") as yf:
            yaml.dump({class_name: data}, yf, sort_keys=False, default_flow_style=False)
        count += 1

    print(f"Successfully generated {count} YAML templates.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python parse_classes.py <input_folder1> <input_folder2> ... <output_folder>")
    else:
        run_parser(sys.argv[1:-1], sys.argv[-1])