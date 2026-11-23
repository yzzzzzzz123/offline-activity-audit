"""Stable CLI wrapper; models call this rather than authoring file readers."""
from audit_core.material_reader import main

if __name__ == '__main__':
    raise SystemExit(main())
