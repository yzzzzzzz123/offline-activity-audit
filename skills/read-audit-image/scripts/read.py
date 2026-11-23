"""Image-only front door to the normal upload reader; no model calls."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'orchestrate-offline-audit' / 'scripts'))
from audit_core.material_reader import main

if __name__ == '__main__':
    raise SystemExit(main(family='image'))
