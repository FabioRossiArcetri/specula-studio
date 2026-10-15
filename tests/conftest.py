import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "fixtures" / "specula"
GOLDEN = REPO / "tests" / "golden"
