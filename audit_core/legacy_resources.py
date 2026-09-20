"""Resource paths used only by old processor/renderer regression tests.

These are deliberately outside the discoverable skills directory. New audits
use scenario_registry and PdfWorkflow and must never call these processors.
"""
from pathlib import Path

LEGACY_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "legacy"
LEGACY_SKILL_BY_SCENARIO = {
    scenario: LEGACY_ROOT / scenario for scenario in (
        "personnel_incentive", "promotional_display", "poster_material",
        "other_expense", "maintenance_fee", "giveaway_promotion",
        "price_difference_support", "pos_target_incentive", "entry_fee",
        "self_procured_gift_material",
    )
}
