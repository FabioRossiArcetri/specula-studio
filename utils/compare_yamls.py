import yaml
import sys
from deepdiff import DeepDiff
from pprint import pprint

def load_yaml(filepath):
    try:
        with open(filepath, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        sys.exit(1)

def compare_simulations(file1, file2):
    data1 = load_yaml(file1)
    data2 = load_yaml(file2)

    # DeepDiff finds all differences
    # ignore_order=True is helpful if list elements moved around
    diff = DeepDiff(data1, data2, ignore_order=True)

    if not diff:
        print(f"Success: {file1} and {file2} are identical.")
    else:
        print(f"Differences found between {file1} and {file2}:")
        
        # Categorize the output
        if 'dictionary_item_added' in diff:
            print("\n[Items added in the second file]:")
            pprint(diff['dictionary_item_added'])

        if 'dictionary_item_removed' in diff:
            print("\n[Items missing in the second file]:")
            pprint(diff['dictionary_item_removed'])

        if 'values_changed' in diff:
            print("\n[Values that are different]:")
            for path, change in diff['values_changed'].items():
                print(f"  - {path}:")
                print(f"    Old: {change['old_value']}")
                print(f"    New: {change['new_value']}")

        if 'type_changes' in diff:
            print("\n[Type mismatches]:")
            pprint(diff['type_changes'])

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python compare_yamls.py <file1.yml> <file2.yml>")
    else:
        compare_simulations(sys.argv[1], sys.argv[2])