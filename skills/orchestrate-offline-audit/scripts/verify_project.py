"""Run the project regressions against this Skill's bundled runtime."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(RUNTIME_ROOT))

if __name__ == "__main__":
    unittest.main(module=None, argv=[sys.argv[0], "discover", "-s", str(PROJECT_ROOT / "tests"),
                                    "-t", str(PROJECT_ROOT), *sys.argv[1:]])
