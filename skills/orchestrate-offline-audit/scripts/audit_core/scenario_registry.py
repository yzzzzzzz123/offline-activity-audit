from __future__ import annotations

from .paths import PROJECT_ROOT

from dataclasses import dataclass
from pathlib import Path




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
        "陈列堆头",
        "堆头核销",
        "audit-promotional-display",
        ("堆头", "陈列"),
        "display",
    ),
    ScenarioSpec(
        "poster_material",
        "KT板等物料制作",
        "海报物料核销",
        "audit-poster-material",
        ("展示道具", "物料制作", "海报"),
        "poster",
    ),
    ScenarioSpec(
        "giveaway_promotion",
        "搭赠",
        "额外搭赠核销",
        "audit-giveaway-promotion",
        ("额外搭赠", "搭赠"),
        "generic",
    ),
    ScenarioSpec(
        "price_difference_support",
        "补差",
        "价格补差核销",
        "audit-price-difference-support",
        ("补差", "价格补差"),
        "generic",
    ),
    ScenarioSpec(
        "entry_fee",
        "条码费",
        "条码费核销",
        "audit-entry-fee",
        ("条码费", "进场费"),
        "generic",
    ),
    ScenarioSpec(
        "self_procured_gift_material",
        "外采赠品",
        "自采赠品物料核销",
        "audit-self-procured-gift-material",
        ("自采赠品物料", "自采赠品", "自采物料"),
        "generic",
    ),
    ScenarioSpec(
        "pos_target_incentive",
        "POS达标激励",
        "POS达标激励核销",
        "audit-pos-target-incentive",
        ("POS达标激励",),
        "generic",
    ),
)

SCENARIO_BY_ID = {item.scenario: item for item in SCENARIO_SPECS}
SCENARIO_ORDER = tuple(item.scenario for item in SCENARIO_SPECS)
GENERIC_SCENARIOS = frozenset(
    item.scenario for item in SCENARIO_SPECS if item.runtime == "generic"
)
SCENARIO_LABELS = {item.scenario: item.label for item in SCENARIO_SPECS}
BIZ_TYPE_SCENARIOS = {item.label: item.scenario for item in SCENARIO_SPECS}


def scenario_for_biz_type(value: object) -> str | None:
    """Match only the eight agreed business values, without aliases or inference."""
    return BIZ_TYPE_SCENARIOS.get(value) if isinstance(value, str) else None


SCENARIO_SHEETS = {item.scenario: item.sheet_name for item in SCENARIO_SPECS}
SHEET_SCENARIOS = {item.sheet_name: item.scenario for item in SCENARIO_SPECS}
SHEET_SCENARIOS["进场费核销"] = "entry_fee"  # Read-only compatibility with old workbooks.
SKILL_BY_SCENARIO = {item.scenario: item.skill_dir for item in SCENARIO_SPECS}
EVIDENCE_SCHEMA_BY_SCENARIO = {
    item.scenario: item.evidence_schema for item in SCENARIO_SPECS
}


def scenario_spec(scenario: str) -> ScenarioSpec:
    try:
        return SCENARIO_BY_ID[scenario]
    except KeyError as exc:
        raise KeyError(f"Unsupported audit scenario: {scenario}") from exc
