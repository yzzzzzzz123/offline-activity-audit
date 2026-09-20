"""Resource paths used only by old processor/renderer regression tests.

The maintenance Skill owns these resources, outside the Skills granted to
current audit models. New audits use scenario_registry and PdfWorkflow and
must never call these processors.
"""
from .paths import PROJECT_ROOT
from pathlib import Path

LEGACY_ROOT = PROJECT_ROOT / "skills/create-offline-audit-scenario/references/legacy"
LEGACY_SKILL_BY_SCENARIO = {
    scenario: LEGACY_ROOT / scenario for scenario in (
        "personnel_incentive", "promotional_display", "poster_material",
        "other_expense", "maintenance_fee", "giveaway_promotion",
        "price_difference_support", "pos_target_incentive", "entry_fee",
        "self_procured_gift_material",
    )
}
