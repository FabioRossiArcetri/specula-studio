import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "fixtures" / "specula"
GOLDEN = REPO / "tests" / "golden"

# Never read or write the developer's real settings file from tests (this
# also applies to subprocesses, which inherit the environment).
os.environ.setdefault(
    "SPECULA_STUDIO_SETTINGS",
    os.path.join(tempfile.mkdtemp(prefix="specula_studio_test_"), "settings.json"),
)
