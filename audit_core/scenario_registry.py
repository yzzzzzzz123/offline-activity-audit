from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    scenario: str
    label: str
    sheet_name: str
    skill_name: str
    archive_markers: tuple[str, ...]
    runtime: str

    @property
    def skill_dir(self) -> Path:
        return PROJECT_ROOT / "skills" / self.skill_name

    @property
    def evidence_schema(self) -> Path:
        return self.skill_dir / "references" / "evidence.schema.json"


SCENARIO_SPECS = (
    ScenarioSpec(
        "personnel_incentive",
        "人员激励",
        "人员激励核销",
        "audit-personnel-incentive",
        ("人员激励",),
        "personnel",
    ),
    ScenarioSpec(
        "promotional_display",
        "堆头/陈列",
        "堆头核销",
        "audit-promotional-display",
        ("堆头", "陈列"),
        "display",
    ),
    ScenarioSpec(
        "poster_material",
        "海报/展示道具",
        "海报物料核销",
        "audit-poster-material",
        ("展示道具", "物料制作", "海报"),
        "poster",
    ),
    ScenarioSpec(
        "other_expense",
        "其他费用",
        "其他费用核销",
        "audit-other-expense",
        ("其他",),
        "other",
    ),
    ScenarioSpec(
        "maintenance_fee",
        "维护费用",
        "维护费用核销",
        "audit-maintenance-fee",
        ("维护费用", "维护费"),
        "generic",
    ),
    ScenarioSpec(
        "giveaway_promotion",
        "搭赠",
        "搭赠核销",
        "audit-giveaway-promotion",
        ("搭赠",),
        "generic",
    ),
    ScenarioSpec(
        "price_difference_support",
        "补差",
        "补差核销",
        "audit-price-difference-support",
        ("补差", "价格补差"),
        "generic",
    ),
    ScenarioSpec(
        "pos_target_incentive",
        "POS达标激励",
        "POS达标激励核销",
        "audit-pos-target-incentive",
        ("pos激励达标", "pos达标激励", "pos激励"),
        "generic",
    ),
    ScenarioSpec(
        "entry_fee",
        "进场费",
        "进场费核销",
        "audit-entry-fee",
        ("进场费", "条码费"),
        "generic",
    ),
)

SCENARIO_BY_ID = {item.scenario: item for item in SCENARIO_SPECS}
SCENARIO_ORDER = tuple(item.scenario for item in SCENARIO_SPECS)
GENERIC_SCENARIOS = frozenset(
    item.scenario for item in SCENARIO_SPECS if item.runtime == "generic"
)
SCENARIO_LABELS = {item.scenario: item.label for item in SCENARIO_SPECS}
SCENARIO_SHEETS = {item.scenario: item.sheet_name for item in SCENARIO_SPECS}
SHEET_SCENARIOS = {item.sheet_name: item.scenario for item in SCENARIO_SPECS}
SKILL_BY_SCENARIO = {item.scenario: item.skill_dir for item in SCENARIO_SPECS}
EVIDENCE_SCHEMA_BY_SCENARIO = {
    item.scenario: item.evidence_schema for item in SCENARIO_SPECS
}


def scenario_spec(scenario: str) -> ScenarioSpec:
    try:
        return SCENARIO_BY_ID[scenario]
    except KeyError as exc:
        raise KeyError(f"Unsupported audit scenario: {scenario}") from exc
