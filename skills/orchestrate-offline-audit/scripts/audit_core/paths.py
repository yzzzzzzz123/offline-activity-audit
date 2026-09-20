"""Locations owned by the bundled orchestration Skill and its host project."""
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = SKILL_ROOT.parents[1]
RUNTIME_ROOT = SKILL_ROOT / "scripts"
CONTRACTS_ROOT = SKILL_ROOT / "references" / "contracts"
POLICY_ROOT = SKILL_ROOT / "references" / "pdf-policy"
WORKBENCH_TEMPLATE = SKILL_ROOT / "assets" / "offline-activity-audit.html"
