"""
Regenerate the bundled class templates (``specula_yaml_docs``) from SPECULA.

    specula-studio-parse [SPECULA_PACKAGE_DIR] OUTPUT_DIR

Without SPECULA_PACKAGE_DIR the installed ``specula`` package is used.
"""

import pathlib
import sys

import yaml

from ..core import catalog
from ..core.yaml_io import _CleanDumper


def write_templates(data: dict, proc: dict, out_dir: pathlib.Path) -> int:
    count = 0
    for sub, templates in (("data_objects", data), ("processing_objects", proc)):
        folder = out_dir / sub
        folder.mkdir(parents=True, exist_ok=True)
        for name, tpl in templates.items():
            tpl = {k: v for k, v in tpl.items() if k != "help"}
            with open(folder / f"{name}.yml", "w", encoding="utf-8") as f:
                yaml.dump({name: tpl}, f, Dumper=_CleanDumper, sort_keys=False,
                          default_flow_style=False)
            count += 1
    return count


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    root = pathlib.Path(argv[0]) if len(argv) == 2 else catalog.specula_root()
    if root is None:
        print("SPECULA is not installed; pass its package directory", file=sys.stderr)
        return 1
    data, proc = catalog.build_catalog(root, runtime=True, with_help=False)
    n = write_templates(data, proc, pathlib.Path(argv[-1]))
    print(f"Wrote {n} templates to {argv[-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
