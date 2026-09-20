"""只读评估真实核销收据；金标始终在模型执行结束后独立使用。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .codex_environment import reported_material_access_failure


USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"
)
REVIEW_PHASES = ("first_pass_reviews", "focused_reviews", "calibrated_reviews")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def _read_object(path: Path, issues: list[str]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issues.append(f"无法读取 {path.name}：{type(exc).__name__}")
        return {}
    if not isinstance(value, dict):
        issues.append(f"{path.name} 顶层不是对象")
        return {}
    return value


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        return None
    return int(value) if integer else value


def _nullable_sum(values: list[Any], *, integer: bool = False) -> dict[str, Any]:
    known = [number for value in values if (number := _number(value, integer=integer)) is not None]
    complete = bool(values) and len(known) == len(values)
    return {
        "total": sum(known) if complete else None,
        "known_subtotal": sum(known) if known else None,
        "known_attempts": len(known),
        "unknown_attempts": len(values) - len(known),
        "complete": complete,
    }


def _metrics(path: Path, issues: list[str]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    invalid_lines = 0
    try:
        with path.open(encoding="utf-8-sig") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("attempt 必须是对象")
                    attempts.append(value)
                except (ValueError, json.JSONDecodeError):
                    invalid_lines += 1
                    issues.append(f"model-metrics.jsonl 第 {line_number} 行无效")
    except (OSError, UnicodeError) as exc:
        issues.append(f"无法读取 model-metrics.jsonl：{type(exc).__name__}")

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        # 不补零：未返回 usage 的超时/失败调用仍使整体真实用量未知。
        usage = {
            field: _nullable_sum([
                (row.get("usage") or {}).get(field)
                if isinstance(row.get("usage"), dict) else None
                for row in rows
            ], integer=True)
            for field in USAGE_FIELDS
        }
        retry_indices = [_number(row.get("attempt"), integer=True) for row in rows]
        retries_known = bool(rows) and all(value is not None and value >= 1 for value in retry_indices)
        return {
            "attempt_count": len(rows),
            "status_counts": dict(sorted(Counter(str(row.get("status") or "unknown") for row in rows).items())),
            "reasoning_effort_counts": dict(sorted(Counter(str(row.get("reasoning_effort") or "unknown") for row in rows).items())),
            "retry_attempt_count": sum(value > 1 for value in retry_indices if value is not None) if retries_known else None,
            "elapsed_seconds": _nullable_sum([row.get("elapsed_seconds") for row in rows]),
            "usage": usage,
            "delegated_agents_count": _nullable_sum([row.get("delegated_agents_count") for row in rows], integer=True),
            "image_count": _nullable_sum([row.get("image_count") for row in rows], integer=True),
            "prompt_bytes": _nullable_sum([row.get("prompt_bytes") for row in rows], integer=True),
        }

    result = summarize(attempts)
    result["invalid_lines"] = invalid_lines
    result["receipt_complete"] = bool(attempts) and invalid_lines == 0
    labels = sorted({str(row.get("label") or "unknown") for row in attempts})
    result["by_label"] = {
        label: summarize([row for row in attempts if str(row.get("label") or "unknown") == label])
        for label in labels
    }
    if invalid_lines:
        # 部分损坏的收据不能把其余行之和冒充完整运行总量。
        for summary in [result, *result["by_label"].values()]:
            for field in ["elapsed_seconds", "delegated_agents_count", "image_count", "prompt_bytes"]:
                summary[field]["total"] = None
                summary[field]["complete"] = False
            for aggregate in summary["usage"].values():
                aggregate["total"] = None
                aggregate["complete"] = False
    return result


def _load_labels(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    registry = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(registry, dict) or registry.get("schema_version") != "1.0":
        raise ValueError("评测校准登记表 schema_version 必须为 1.0")
    labels = registry.get("calibrations")
    if not isinstance(labels, list) or not labels:
        raise ValueError("评测校准登记表必须包含非空 calibrations")
    signatures: set[tuple[str, tuple[str, ...]]] = set()
    ids: set[str] = set()
    for item in labels:
        if not isinstance(item, dict):
            raise ValueError("评测校准项必须是对象")
        name, identifier = item.get("contract_store_name"), item.get("calibration_id")
        hashes, observation = item.get("photo_sha256"), item.get("display_observation")
        if not isinstance(name, str) or not name.strip() or not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("评测校准项缺少门店名称或 ID")
        if not isinstance(hashes, list) or not hashes or any(not isinstance(value, str) or not _SHA256.fullmatch(value) for value in hashes):
            raise ValueError("评测校准项照片 SHA-256 无效")
        if not isinstance(observation, dict) or observation.get("standard_evidence") not in {"meets", "does_not_meet", "unclear"}:
            raise ValueError("评测校准项陈列状态无效")
        count = observation.get("vertical_facing_count")
        if count is not None and _number(count, integer=True) is None:
            raise ValueError("评测校准项列数无效")
        signature = (name.strip(), tuple(hashes))
        if signature in signatures or identifier in ids:
            raise ValueError("评测校准项 ID 或有序来源签名重复")
        signatures.add(signature)
        ids.add(identifier)
    return labels, hashlib.sha256(raw).hexdigest()


def _route(review: dict[str, Any]) -> tuple[Any, str, tuple[str, ...]]:
    files = review.get("photo_files")
    return (
        review.get("store_line_no"),
        str(review.get("contract_store_name") or "").strip(),
        tuple(files) if isinstance(files, list) and all(isinstance(name, str) for name in files) else (),
    )


def _phase_score(
    reviews: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    inventory: dict[str, str],
    stores: list[dict[str, Any]],
) -> dict[str, Any]:
    known_hashes = set(inventory.values())
    store_names = {str(store.get("store_name") or "").strip() for store in stores}
    rows: list[dict[str, Any]] = []
    for label in labels:
        expected_hashes = tuple(label["photo_sha256"])
        applicable = label["contract_store_name"].strip() in store_names and all(value in known_hashes for value in expected_hashes)
        candidates = []
        if applicable:
            for review in reviews:
                _, name, files = _route(review)
                if name == label["contract_store_name"].strip() and files and all(name in inventory for name in files):
                    if tuple(inventory[name] for name in files) == expected_hashes:
                        candidates.append(review)
        matched = len(candidates) == 1
        actual = candidates[0].get("display_observation") if matched else None
        actual = actual if isinstance(actual, dict) else {}
        expected = label["display_observation"]
        valid_state = actual.get("standard_evidence") in {"meets", "does_not_meet", "unclear"}
        count = actual.get("vertical_facing_count")
        valid_count = "vertical_facing_count" in actual and (count is None or _number(count, integer=True) is not None)
        comparable = matched and bool(actual) and valid_state and valid_count
        row = {
            "calibration_id": label["calibration_id"],
            "contract_store_name": label["contract_store_name"],
            "source_available": applicable,
            "ordered_hash_route_matched": matched,
            "comparable": comparable,
            "expected_state": expected["standard_evidence"],
            "expected_count": expected.get("vertical_facing_count"),
            "observed_state": actual.get("standard_evidence") if matched else None,
            "observed_count": count if matched else None,
            "state_correct": actual.get("standard_evidence") == expected["standard_evidence"] if comparable else None,
            "count_correct": count == expected.get("vertical_facing_count") if comparable else None,
            "false_positive": expected["standard_evidence"] != "meets" and actual.get("standard_evidence") == "meets" if comparable else None,
            "false_negative": expected["standard_evidence"] == "meets" and actual.get("standard_evidence") != "meets" if comparable else None,
        }
        rows.append(row)
    applicable_count = sum(row["source_available"] for row in rows)
    comparable_count = sum(row["comparable"] for row in rows)
    state_correct = sum(row["state_correct"] is True for row in rows)
    count_correct = sum(row["count_correct"] is True for row in rows)
    return {
        "registry_label_count": len(labels),
        "applicable_label_count": applicable_count,
        "comparable_label_count": comparable_count,
        "complete": applicable_count > 0 and comparable_count == applicable_count,
        "state_correct_count": state_correct,
        "count_correct_count": count_correct,
        "state_accuracy_on_labeled_subset": state_correct / comparable_count if comparable_count else None,
        "count_accuracy_on_labeled_subset": count_correct / comparable_count if comparable_count else None,
        "false_positive_count": sum(row["false_positive"] is True for row in rows),
        "false_negative_count": sum(row["false_negative"] is True for row in rows),
        "details": rows,
    }


def _wall_seconds(manifest: dict[str, Any]) -> float | None:
    try:
        start = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(manifest["completed_at"]).replace("Z", "+00:00"))
        return _number((end - start).total_seconds())
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def score_display_run(workspace: Path, calibrations_path: Path) -> dict[str, Any]:
    """读取一次正式运行，分离已确认标签准确率与无金标完整性。不会改写收据。"""
    workspace = Path(workspace).resolve()
    labels, registry_sha = _load_labels(Path(calibrations_path))
    issues: list[str] = []
    manifest = _read_object(workspace / "manifest.json", issues)
    snapshot = _read_object(workspace / "snapshot.json", issues)
    observation = _read_object(workspace / "analysis/model-observations/promotional-display.json", issues)
    inventory: dict[str, str] = {}
    raw_inventory = observation.get("photo_inventory")
    inventory_valid = isinstance(raw_inventory, list)
    for photo in raw_inventory if inventory_valid else []:
        if not isinstance(photo, dict):
            inventory_valid = False
            continue
        name, digest = photo.get("file_name"), photo.get("sha256")
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in inventory or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            inventory_valid = False
            continue
        inventory[name] = digest
    contract = observation.get("contract") or {}
    contract = contract if isinstance(contract, dict) else {}
    stores = contract.get("stores") or []
    stores_valid = isinstance(stores, list) and bool(stores) and all(isinstance(store, dict) for store in stores)
    stores = stores if stores_valid else []
    expected_store_routes = [(store.get("line_no"), str(store.get("store_name") or "").strip()) for store in stores]
    first_reviews = observation.get("first_pass_reviews")
    first_reviews = first_reviews if isinstance(first_reviews, list) else []
    first_routes = [_route(review) for review in first_reviews if isinstance(review, dict)]
    phases: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    for phase in REVIEW_PHASES:
        reviews = observation.get(phase)
        valid = isinstance(reviews, list) and all(isinstance(review, dict) for review in reviews)
        reviews = reviews if valid else []
        routes = [_route(review) for review in reviews]
        used = [name for _, _, files in routes for name in files]
        expected_files = set(inventory)
        coverage[phase] = {
            "reviews_valid": valid,
            "reviewed_store_count": len(reviews),
            "store_order_matches": bool(stores) and [(line, name) for line, name, _ in routes] == expected_store_routes,
            "covered_photo_count": len(set(used) & expected_files),
            "missing_photo_files": sorted(expected_files - set(used)),
            "unknown_photo_files": sorted(set(used) - expected_files),
            "reused_photo_file_count": sum(count > 1 for count in Counter(used).values()),
            "routing_unchanged_from_first_pass": phase == "first_pass_reviews" or routes == first_routes,
        }
        phases[phase] = _phase_score(reviews, labels, inventory, stores)
    snapshot_run = snapshot.get("run") or {}
    snapshot_run = snapshot_run if isinstance(snapshot_run, dict) else {}
    snapshot_sha = None
    try:
        snapshot_sha = hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest()
    except OSError:
        pass
    checks = {
        "manifest_completed": manifest.get("status") == "completed",
        "snapshot_completed": snapshot_run.get("status") == "completed",
        "workspace_ids_match": bool(manifest.get("workspace_id")) and manifest.get("workspace_id") == snapshot_run.get("workspace_id") == workspace.name,
        "snapshot_hash_matches": bool(snapshot_sha) and manifest.get("snapshot_sha256") == snapshot_sha,
        "static_archive_present": (workspace / "offline-activity-audit.html").is_file(),
        "verification_record_present": bool(snapshot.get("verification")),
        "observation_version_supported": observation.get("version") == 1,
        "photo_inventory_valid": inventory_valid and bool(inventory),
        "contract_stores_valid": stores_valid,
        "material_access_not_reported_blocked": not reported_material_access_failure(observation),
    }
    if not checks["material_access_not_reported_blocked"]:
        issues.append("模型观察报告必需文件或原图访问受阻，该运行不能用于档位能力比较")
    for phase, data in coverage.items():
        checks[f"{phase}_complete"] = data["reviews_valid"] and data["store_order_matches"] and not data["missing_photo_files"] and not data["unknown_photo_files"] and data["routing_unchanged_from_first_pass"]
    metrics = _metrics(workspace / "analysis/model-metrics.jsonl", issues)
    focused = phases["focused_reviews"]
    return {
        "schema_version": "1.0",
        "workspace": str(workspace),
        "workspace_id": manifest.get("workspace_id", workspace.name),
        "model": manifest.get("audit_model"),
        "reasoning_effort": manifest.get("reasoning_effort"),
        "status": manifest.get("status", "unknown"),
        "eligible": all(checks.values()) and focused["complete"],
        "integrity": {"passed": all(checks.values()), "passed_check_count": sum(checks.values()), "check_count": len(checks), "checks": checks},
        "comparability": {
            "photo_inventory_sha256": _digest(sorted(inventory.items())),
            "contract_store_routes_sha256": _digest(expected_store_routes),
            "calibration_registry_sha256": registry_sha,
            "applicable_label_ids": [row["calibration_id"] for row in focused["details"] if row["source_available"]],
        },
        "quality": focused,
        "phase_scores": phases,
        "unlabeled_completeness": {
            "not_an_accuracy_measure": True,
            "contract_store_count": len(stores),
            "source_photo_count": len(inventory),
            "stores_without_applicable_gold_label": max(0, len(stores) - focused["applicable_label_count"]),
            "phases": coverage,
        },
        "efficiency": {"wall_seconds": _wall_seconds(manifest), "attempts": metrics},
        "issues": issues,
        "limitations": [
            "准确率仅限有完全一致门店名称与有序照片 SHA-256 的已确认陈列标签，不表示其余门店、合同、商品或整体核销准确率。",
            "核心评分采用聚焦复核后、确定性校准前的观察；校准后命中不能证明模型自身正确。",
            "完整性检查只核对已保存收据，不独立复验图片内容、HTML 自包含性或临时文件清理。",
            "usage 为进程回执口径；未知用量保留 null，子代理用量是否已计入需由执行记录另行确认。",
        ],
    }


def rank_display_runs(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同来源、同标签运行按效果优先排序；不修改输入，也不推断统计显著性。"""
    comparable = [score.get("comparability") for score in scores if score.get("eligible")]
    if comparable and any(value != comparable[0] for value in comparable[1:]):
        raise ValueError("不能直接排序来源、合同门店路由或金标集合不同的合格运行")

    def key(score: dict[str, Any]) -> tuple[Any, ...]:
        quality = score.get("quality") or {}
        integrity = score.get("integrity") or {}
        elapsed = _number((score.get("efficiency") or {}).get("wall_seconds"))
        return (
            not bool(score.get("eligible")),
            quality.get("false_positive_count", math.inf),
            -quality.get("state_correct_count", 0),
            -quality.get("count_correct_count", 0),
            -integrity.get("passed_check_count", 0),
            elapsed if elapsed is not None else math.inf,
        )

    return sorted(scores, key=key)
