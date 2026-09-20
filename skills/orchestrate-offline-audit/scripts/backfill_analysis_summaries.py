from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
if str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts') not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts'))

from audit_core.analysis_summary_backfill import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
