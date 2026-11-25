import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import build_fixtures  # noqa: E402

FIXTURES = build_fixtures.build()


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="nexus-test-"))
