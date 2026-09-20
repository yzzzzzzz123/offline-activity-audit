from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts') not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts'))

from audit_core.maintenance import main

if __name__ == "__main__":
    raise SystemExit(main())
