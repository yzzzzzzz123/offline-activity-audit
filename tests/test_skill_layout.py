"""Bundled commands must use this checkout, independent of cwd or installed copies."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import audit_core
from audit_core import paths
from audit_core.html_report import TEMPLATE_ASSET_PATH
from audit_core.legacy_resources import LEGACY_ROOT
from audit_core.pdf_materials import ORCHESTRATOR_SKILL
from audit_core.orchestrator import DEFAULT_INPUT_DIR
from audit_core.scenario_registry import SCENARIO_SPECS
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_store import DEFAULT_WORKTREES_ROOT


class SkillLayoutTests(unittest.TestCase):
    def test_runtime_resources_and_business_directories_belong_to_this_checkout(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(paths.PROJECT_ROOT, root)
        self.assertEqual(Path(audit_core.__file__).parent, paths.RUNTIME_ROOT / "audit_core")
        self.assertEqual(ROOT_HTML, paths.WORKBENCH_TEMPLATE)
        self.assertTrue(ROOT_HTML.is_file())
        self.assertTrue(TEMPLATE_ASSET_PATH.is_file())
        self.assertNotEqual(TEMPLATE_ASSET_PATH.parent, ROOT_HTML.parent)
        self.assertTrue((paths.CONTRACTS_ROOT / "pdf-policy-audit.schema.json").is_file())
        self.assertTrue((paths.POLICY_ROOT / "catalogue.json").is_file())
        self.assertEqual(DEFAULT_INPUT_DIR, root / "input")
        self.assertEqual(DEFAULT_WORKTREES_ROOT, root / "worktrees")
        self.assertTrue(LEGACY_ROOT.is_dir())
        self.assertFalse(LEGACY_ROOT.is_relative_to(ORCHESTRATOR_SKILL))
        for spec in SCENARIO_SPECS:
            self.assertFalse(LEGACY_ROOT.is_relative_to(spec.skill_dir))
            self.assertTrue(spec.evidence_schema.is_file())
            self.assertTrue((spec.skill_dir / "SKILL.md").is_file())
        self.assertFalse(any((root / "skills").rglob("用于AI核销测试*.xlsx")))

    def test_formal_entrypoints_resolve_bundled_package_from_an_unrelated_cwd(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import json,runpy,sys; from pathlib import Path; "
            "runpy.run_path(sys.argv[1]); import audit_core; from audit_core.paths import PROJECT_ROOT; "
            "print(json.dumps([str(Path(audit_core.__file__).resolve()),str(PROJECT_ROOT)]))"
        )
        with tempfile.TemporaryDirectory() as cwd:
            for name in ("run.py", "serve.py", "clean_temporary.py"):
                with self.subTest(entrypoint=name):
                    result = subprocess.run(
                        [sys.executable, "-I", "-B", "-c", code, str(paths.RUNTIME_ROOT / name)],
                        cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True,
                    )
                    module, project = json.loads(result.stdout)
                    self.assertEqual(Path(module), paths.RUNTIME_ROOT / "audit_core/__init__.py")
                    self.assertEqual(Path(project), root)
