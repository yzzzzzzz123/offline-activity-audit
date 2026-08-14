from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.orchestrator import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["input", *sys.argv[1:]]))
