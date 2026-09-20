"""通过唯一正式 runner 串行评测 GPT-6 堆头核销，不生成替代业务输出。"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts') not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / 'skills/orchestrate-offline-audit/scripts'))

from audit_core.effort_benchmark import rank_display_runs, score_display_run
from audit_core.product_oss import reference_fingerprint
from audit_core.product_database import load_product_catalog


EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
MODEL = "gpt-6-astra"
SHANGHAI = timezone(timedelta(hours=8))
_SOURCE_EXTENSIONS = {".py", ".md", ".json", ".html", ".js", ".css", ".toml", ".txt"}


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _file_digest(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"生成指纹时文件发生变化：{path.name}")
    return {"size": after.st_size, "sha256": digest.hexdigest()}


def source_fingerprint(project_root: Path, input_dir: Path, codex_binary: Path | None = None) -> dict[str, Any]:
    """冻结原始ZIP、代码规则、数据库快照和 OSS 清单/图片字节。"""
    root, inputs = project_root.resolve(), input_dir.resolve()
    files: dict[str, Path] = {}
    zips = sorted(path for path in inputs.iterdir() if path.is_file() and path.suffix.lower() == ".zip")
    if not zips:
        raise ValueError("输入目录没有 ZIP，不能开始真实档位评测")
    for path in zips:
        files["input/" + path.name] = path
    for name in ("AGENTS.md",):
        path = root / name
        if path.is_file():
            files["source/" + name] = path
    for directory in (
        "skills/orchestrate-offline-audit", "skills/audit-promotional-display",
        "skills/create-offline-audit-scenario/references/legacy",
    ):
        for path in (root / directory).rglob("*"):
            if path.is_file() and path.suffix.lower() in _SOURCE_EXTENSIONS and "__pycache__" not in path.parts:
                files["source/" + path.relative_to(root).as_posix()] = path
    driver = root / "skills/audit-promotional-display/scripts/benchmark_display_effort.py"
    if driver.is_file():
        files["source/skills/audit-promotional-display/scripts/benchmark_display_effort.py"] = driver
    knowledge_rules = root / "shared/product-database/audit-knowledge.md"
    if knowledge_rules.is_file():
        files["source/shared/product-database/audit-knowledge.md"] = knowledge_rules
    if codex_binary is not None:
        files["runtime/codex"] = codex_binary.resolve()
    signatures = {name: _file_digest(path) for name, path in sorted(files.items())}
    catalog = load_product_catalog()
    database = catalog["data_source"]
    signatures.update(reference_fingerprint(catalog))
    after = load_product_catalog()["data_source"]
    for key in ("sha256", "image_manifest_keys_sha256"):
        if after[key] != database[key]:
            raise ValueError("生成指纹期间数据库商品身份或 OSS 关联改变")
    signatures["database/product_catalog.products"] = {
        "row_count": database["row_count"], "sha256": database["sha256"],
        "image_manifest_keys_sha256": database["image_manifest_keys_sha256"],
    }
    encoded = json.dumps(signatures, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "input_zip_count": len(zips),
        "rag_view_count": sum(key.startswith("oss-images/") for key in signatures),
        "product_database": database,
        "files": signatures,
        "captured_at": _now(),
    }


def fingerprint_changes(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    old, new = baseline["files"], current["files"]
    return [name for name in sorted(set(old) | set(new)) if old.get(name) != new.get(name)]


def build_command(
    project_root: Path,
    run_id: str,
    effort: str,
    receipt: Path,
    *,
    input_dir: Path | None = None,
    worktrees: Path | None = None,
) -> list[str]:
    if effort not in EFFORTS:
        raise ValueError(f"未知档位：{effort}")
    command = [
        sys.executable, "-B", str(project_root / "skills/orchestrate-offline-audit/scripts/run.py"),
        "--run-id", run_id,
        "--producer-model", "codex",
        "--model", MODEL,
        "--reasoning-effort", effort,
        "--scenario", "promotional_display",
        "--result-json", str(receipt),
    ]
    # 未显式覆盖时，正式 runner 继续使用自身默认 input/ 与 worktrees/。
    if input_dir is not None:
        command.extend(["--input-dir", str(input_dir)])
    if worktrees is not None:
        command.extend(["--worktrees", str(worktrees)])
    return command


def _safe_progress(line: str) -> str | None:
    value = line.strip()
    if not value.startswith(("AI 正在识别 ", "AI 完成 ")):
        return None
    # 不转发失败细节或 Codex JSON 事件，不保存完整模型 stdout/stderr。
    if any(character in value for character in ("{", "}", "\x1b")) or len(value) > 500:
        return None
    return value


def _stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def execute_formal_runner(command: list[str], project_root: Path, codex_binary: Path) -> dict[str, Any]:
    environment = dict(os.environ)
    environment.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "OFFLINE_AUDIT_CODEX": str(codex_binary)})
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=project_root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **options)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            progress = _safe_progress(line)
            if progress:
                print(progress, flush=True)
        code = process.wait()
    except BaseException:
        _stop_child(process)
        raise
    finally:
        if process.stdout:
            process.stdout.close()
    return {"exit_code": code, "runner_wall_seconds": round(time.monotonic() - started, 3)}


def find_workspace(worktrees: Path, run_id: str, effort: str, receipt: Path) -> Path | None:
    """成功优先使用正式回执；失败只按本批唯一 run-id 找其自有诊断目录。"""
    candidates: set[Path] = set()
    root = worktrees.resolve()
    if receipt.is_file():
        value = json.loads(receipt.read_text(encoding="utf-8-sig"))
        candidate = Path(str(value.get("worktree") or "")).resolve()
        if candidate.parent != root:
            raise ValueError("正式回执 worktree 不属于本批指定目录")
        candidates.add(candidate)
    if root.is_dir():
        candidates.update(path for path in root.iterdir() if path.is_dir() and not path.name.startswith("."))
    matched = []
    for candidate in candidates:
        manifest = candidate / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("run_id") == run_id and value.get("reasoning_effort") == effort and value.get("audit_model") == MODEL and value.get("producer_model") == "codex":
            matched.append(candidate)
    if len(matched) > 1:
        raise ValueError("同一本批 run-id 对应多个 worktree，拒绝猜测")
    return matched[0] if matched else None


def _number_text(value: Any, suffix: str = "") -> str:
    if value is None:
        return "未知"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def _configuration_failed(workspace: Path, score: dict[str, Any]) -> bool:
    if (score.get("integrity") or {}).get("checks", {}).get("material_access_not_reported_blocked") is False:
        return True
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8-sig"))
    failure = manifest.get("failure") or {}
    return manifest.get("status") == "failed" and (
        failure.get("type") == "CodexRequestConfigurationError"
        or any(marker in str(failure.get("message") or "") for marker in ("（configuration）", "（auth）"))
    )


def build_report(state: dict[str, Any]) -> dict[str, Any]:
    records = state.get("runs") or []
    full_matrix = state["requested_efforts"] == list(EFFORTS) and len(records) == len(EFFORTS) and all(row.get("finished_at") and row.get("formal_runner_attempted") for row in records)
    fingerprints_match = not state.get("fingerprint_changes") and not state.get("fingerprint_errors")
    scores = [row["score"] for row in records if isinstance(row.get("score"), dict)]
    ranking, comparison_error = [], None
    try:
        ranking = rank_display_runs(scores)
    except ValueError as exc:
        comparison_error = str(exc)
    execution_issues = state.get("execution_issues") or []
    comparable = fingerprints_match and comparison_error is None and not execution_issues
    qualified = [score for score in ranking if score.get("eligible")]
    recommendation = None
    reasons: list[str] = []
    if state.get("status") != "completed":
        reasons.append("本批尚未完整结束")
    if not full_matrix:
        reasons.append("未完成 low 至 ultra 全六档；子集只用于诊断")
    if not fingerprints_match:
        reasons.append("输入、代码、规则、schema、RAG 或 CLI 指纹发生变化或无法核验，本批不可比")
    if comparison_error:
        reasons.append(comparison_error)
    reasons.extend(execution_issues)
    if not qualified:
        reasons.append("没有通过完整性硬门槛并具备完整适用金标的运行")
    if not reasons:
        best = qualified[0]
        recommendation = {
            "reasoning_effort": best["reasoning_effort"],
            "workspace_id": best["workspace_id"],
            "workspace": best["workspace"],
            "basis": "先通过正式完成与完整性硬门槛，再按错误放行数、金标状态正确数、实体列数正确数、完整性、耗时排序。",
            "scope": "本批每档一次、同一堆头材料包及已确认陈列标签子集，不代表全部场景或全项目准确率。",
        }
    return {
        **state,
        "full_six_effort_matrix": full_matrix,
        "comparable": comparable,
        "ranking_workspace_ids": [score["workspace_id"] for score in ranking],
        "recommendation": recommendation,
        "no_recommendation_reasons": reasons,
        "evaluation_notes": [
            "只从正式 runner 新执行产物评分；历史输出与校准答案不提供给模型。",
            "核心准确率仅来自聚焦复核后、确定性校准前的陈列金标子集；其余门店只报告完整性。",
            "每个档位只有本批一次样本，不能宣称统计显著、稳定生产优胜或全项目准确率。",
            "ultra 是本机 Codex advertised 的最大推理加自动委派；其有效行为及子任务用量以实际收据为准。",
            "文字预检等实际子步骤档位、未知 token 和委派计数以 metrics 为准，不将 null 补成零。",
            "地图查询是外部可变服务，地点结论差异不能直接归因为推理强度。",
        ],
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# GPT-6 堆头核销推理档位评测", "",
        f"业务运行标识：`{report['requested_run_id']}`；批次：`{report['batch_id']}`。",
        f"状态：{report['status']}；开始：{report['started_at']}；更新：{report['updated_at']}。",
        f"模型固定 `{MODEL}`，生产来源固定 `codex`；执行顺序：{' → '.join(report['requested_efforts'])}。",
        f"原始输入目录：`{report['input_dir']}`；正式 worktrees：`{report['worktrees']}`。",
        "",
    ]
    recommendation = report.get("recommendation")
    if recommendation:
        lines.extend([f"本批推荐：**{recommendation['reasoning_effort']}**。{recommendation['basis']}", recommendation["scope"], ""])
    else:
        lines.extend(["本批暂不推荐生产默认档位。", *[f"- {reason}" for reason in report["no_recommendation_reasons"]], ""])
    lines.extend([
        "## 各档真实结果", "",
        "| 档位 | 正式状态 / 可入选 | 校准前错放 | 状态正确 / 可评分标签 | 列数正确 / 可评分标签 | 正式耗时 | 调用尝试数 | 输入 / 输出 token |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    for record in report["runs"]:
        score = record.get("score") or {}
        quality = score.get("quality") or {}
        efficiency = score.get("efficiency") or {}
        metrics = efficiency.get("attempts") or {}
        usage = metrics.get("usage") or {}
        count = quality.get("comparable_label_count")
        lines.append(
            f"| {record['effort']} | {score.get('status', record.get('status', '未完成'))} / {'是' if score.get('eligible') else '否'} "
            f"| {_number_text(quality.get('false_positive_count'))} "
            f"| {_number_text(quality.get('state_correct_count'))} / {_number_text(count)} "
            f"| {_number_text(quality.get('count_correct_count'))} / {_number_text(count)} "
            f"| {_number_text(efficiency.get('wall_seconds'), '秒')} "
            f"| {_number_text(metrics.get('attempt_count'))} "
            f"| {_number_text((usage.get('input_tokens') or {}).get('total'))} / {_number_text((usage.get('output_tokens') or {}).get('total'))} |"
        )
    lines.extend(["", "未知 token 表示收据没有完整提供，不能当作零。重试、超时和失败调用仍计入尝试及已知耗时。", "", "## 完整性与校准影响", ""])
    for record in report["runs"]:
        score = record.get("score") or {}
        lines.extend([f"### {record['effort']}", "", f"专属 run-id：`{record['run_id']}`；正式退出码：{record.get('exit_code', '尚未结束')}。"])
        if record.get("workspace"):
            lines.append(f"正式运行目录：`{record['workspace']}`。")
        if record.get("error"):
            lines.append(f"执行或评分诊断：{record['error']}。")
        if score:
            checks = score.get("integrity") or {}
            counts = score.get("unlabeled_completeness") or {}
            lines.append(f"收据完整性通过 {checks.get('passed_check_count')}/{checks.get('check_count')}；合同门店 {counts.get('contract_store_count')} 家，原始照片 {counts.get('source_photo_count')} 张，无适用金标门店 {counts.get('stores_without_applicable_gold_label')} 家。上述覆盖数字不是准确率。")
            phase_scores = score.get("phase_scores") or {}
            for name, label in (("first_pass_reviews", "首次照片提取"), ("focused_reviews", "聚焦复核、校准前"), ("calibrated_reviews", "确定性校准后")):
                phase = phase_scores.get(name) or {}
                lines.append(f"- {label}：状态正确 {phase.get('state_correct_count')}/{phase.get('comparable_label_count')}，列数正确 {phase.get('count_correct_count')}/{phase.get('comparable_label_count')}，错放 {phase.get('false_positive_count')}。")
            failed_checks = [name for name, passed in (checks.get("checks") or {}).items() if not passed]
            if failed_checks:
                lines.append("未通过的收据检查：" + "、".join(failed_checks) + "。")
            metrics = (score.get("efficiency") or {}).get("attempts") or {}
            lines.append("实际子步骤推理档位尝试分布：`" + json.dumps(metrics.get("reasoning_effort_counts") or {}, ensure_ascii=False) + "`。")
            for issue in score.get("issues") or []:
                lines.append(f"- 收据诊断：{issue}")
        lines.append("")
    lines.extend(["## 可比性与结论范围", "", f"全六档已执行：{'是' if report['full_six_effort_matrix'] else '否'}；输入与执行条件可比：{'是' if report['comparable'] else '否'}。"])
    if report.get("fingerprint_changes"):
        lines.append("本批检测到以下文件指纹变化，禁止据此推荐：")
        lines.extend(f"- `{name}`" for name in report["fingerprint_changes"])
    lines.extend(f"- {note}" for note in report["evaluation_notes"])
    lines.extend(["", "完整逐标签分数、全部尝试用量及首尾文件指纹见同目录 `report.json` 和 `fingerprint-*.json`。正式 HTML 仍由各次 bundled runner 生成在对应 worktree，本评测驱动不生成或修改 HTML。", ""])
    return "\n".join(lines)


def _publish(batch: Path, state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = _now()
    report = build_report(state)
    _atomic_json(batch / "progress.json", {"batch_id": state["batch_id"], "status": state["status"], "updated_at": state["updated_at"], "requested_efforts": state["requested_efforts"], "finished_efforts": [row["effort"] for row in state["runs"] if row.get("finished_at")], "active_effort": next((row["effort"] for row in state["runs"] if not row.get("finished_at")), None), "report": str(batch / "report.json")})
    _atomic_json(batch / "report.json", report)
    _atomic_text(batch / "report.md", render_report_markdown(report))
    return report


def run_benchmark(args: argparse.Namespace, *, project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    from audit_core.codex_runner import _find_codex
    from audit_core.orchestrator import output_date_from_run_id

    output_date_from_run_id(args.run_id)
    efforts = list(args.efforts)
    if not efforts or efforts != [effort for effort in EFFORTS if effort in efforts]:
        raise ValueError("--efforts 必须为 low 至 ultra 顺序中的不重复子序列")
    root = project_root.resolve()
    inputs = (Path(args.input_dir) if args.input_dir else root / "input").resolve()
    worktrees = (Path(args.worktrees) if args.worktrees else root / "worktrees").resolve()
    codex_binary = Path(_find_codex()).resolve()
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d_%H%M_%S_%f")
    batch = root / "artifacts" / ("effort-benchmark-" + stamp)
    batch.mkdir(parents=True, exist_ok=False)
    (batch / "receipts").mkdir()
    state: dict[str, Any] = {
        "schema_version": "1.0", "batch_id": batch.name, "artifact_directory": str(batch),
        "requested_run_id": args.run_id, "requested_efforts": efforts,
        "model": MODEL, "producer_model": "codex", "input_dir": str(inputs), "worktrees": str(worktrees),
        "python": sys.version.split()[0], "python_executable": sys.executable, "codex_binary": str(codex_binary),
        "started_at": _now(), "updated_at": _now(), "status": "running", "runs": [],
        "fingerprint_changes": [], "fingerprint_errors": [],
    }
    _publish(batch, state)
    print(f"评测批次已建立：{batch}", flush=True)
    try:
        baseline = source_fingerprint(root, inputs, codex_binary)
        _atomic_json(batch / "fingerprint-start.json", baseline)
        state["baseline_sha256"] = baseline["sha256"]
        calibrations = root / "skills/audit-promotional-display/references/display-standard-calibrations.json"
        for effort in efforts:
            run_id = f"{args.run_id[:44]}-b{stamp}-{effort}"
            receipt = batch / "receipts" / f"{effort}-formal-result.json"
            record: dict[str, Any] = {"effort": effort, "run_id": run_id, "result_receipt": str(receipt), "started_at": _now(), "status": "running"}
            state["runs"].append(record)
            _publish(batch, state)
            print(f"开始 GPT-6 {effort} 正式堆头核销（{len(state['runs'])}/{len(efforts)}）", flush=True)
            try:
                before = source_fingerprint(root, inputs, codex_binary)
                differences = fingerprint_changes(baseline, before)
                state["fingerprint_changes"] = sorted(set(state["fingerprint_changes"]) | set(differences))
                record["fingerprint_before_sha256"] = before["sha256"]
                command = build_command(root, run_id, effort, receipt, input_dir=inputs if args.input_dir else None, worktrees=worktrees if args.worktrees else None)
                record["formal_runner_attempted"] = True
                record.update(execute_formal_runner(command, root, codex_binary))
                record["status"] = "completed" if record["exit_code"] == 0 else "failed"
                workspace = find_workspace(worktrees, run_id, effort, receipt)
                if workspace is not None:
                    record["workspace"] = str(workspace)
                    record["score"] = score_display_run(workspace, calibrations)
                    if _configuration_failed(workspace, record["score"]):
                        record["execution_issue"] = f"{effort} 的运行配置或材料读取预检失败；停止后续档位，修复环境后重建完整批次。"
                        state.setdefault("execution_issues", []).append(record["execution_issue"])
                else:
                    record["error"] = "正式 runner 未产生可精确关联的 worktree，无法评分"
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = f"{type(exc).__name__}：{exc}"
            finally:
                record["finished_at"] = _now()
                try:
                    after = source_fingerprint(root, inputs, codex_binary)
                    _atomic_json(batch / f"fingerprint-after-{effort}.json", after)
                    record["fingerprint_after_sha256"] = after["sha256"]
                    state["fingerprint_changes"] = sorted(set(state["fingerprint_changes"]) | set(fingerprint_changes(baseline, after)))
                except Exception as exc:
                    state["fingerprint_errors"].append(f"{effort} 后无法核验指纹：{type(exc).__name__}：{exc}")
                _publish(batch, state)
            print(f"GPT-6 {effort} 已结束：{record['status']}；评测收据已保存。", flush=True)
            if record.get("execution_issue"):
                break
        final = source_fingerprint(root, inputs, codex_binary)
        _atomic_json(batch / "fingerprint-end.json", final)
        state["fingerprint_changes"] = sorted(set(state["fingerprint_changes"]) | set(fingerprint_changes(baseline, final)))
        state["final_sha256"] = final["sha256"]
        state["status"] = "failed" if state.get("execution_issues") else "completed"
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        state["fingerprint_errors"].append("用户中断批次，未完成全部首尾核验")
        raise
    except Exception as exc:
        state["status"] = "failed"
        state["fingerprint_errors"].append(f"批次执行或首尾核验失败：{type(exc).__name__}：{exc}")
    finally:
        state["finished_at"] = _now()
        report = _publish(batch, state)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过正式 runner 顺序评测 GPT-6 low 至 ultra 的完整堆头流程，生成独立 JSON/Markdown 评测收据")
    parser.add_argument("--run-id", required=True, help="以 YYYYMMDD 业务日期开头；每档会附加本批唯一后缀，原标识完整保存在报告")
    parser.add_argument("--efforts", nargs="+", choices=EFFORTS, default=list(EFFORTS), help="仅用于显式诊断子集，必须保持从 low 到 ultra 的顺序；子集不推荐生产默认档位")
    parser.add_argument("--input-dir", help="显式指定时才覆盖正式 runner 默认 input 目录")
    parser.add_argument("--worktrees", help="显式指定时才覆盖正式 runner 默认 worktrees 目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_benchmark(args)
    except KeyboardInterrupt:
        print("评测已中断，已有正式运行与进度收据保留。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"无法启动评测：{type(exc).__name__}：{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "artifact_directory": report["artifact_directory"], "recommendation": report["recommendation"], "comparable": report["comparable"]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "completed" and report["comparable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
