from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import AuditError, json_number, money, normalize_text, now_utc


KNOWN_EXPENSE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("进场费", ("进场费", "条码费", "上架费")),
    ("POS达标激励", ("pos达标激励", "pos激励", "满减返还", "达标奖励")),
    ("人员激励", ("人员报酬", "促销员工资", "理货员报酬", "销售提成", "培训费用", "人员激励")),
    ("海报/物料制作", ("物料制作", "广告物料", "海报制作", "海报张贴", "宣传单页", "展示道具", "自采赠品物料")),
    ("陈列堆头", ("场地使用费", "场地活动费", "中庭场地", "地堆", "堆头", "陈列", "专架", "端架")),
    ("维护费用", ("维护费用", "维护费", "渠道维护", "直营维护", "经销商维护")),
    ("补差", ("价格补差", "补差", "差额补偿")),
    ("搭赠", ("额外搭赠", "搭赠")),
    ("赠品", ("公司形象品", "促销品卡", "小样", "赠品")),
)


def _issue(
    code: str,
    title: str,
    source_files: list[str],
    observed: str,
    expected: str,
    impact: str,
    resubmission: str,
    *,
    severity: str = "high",
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "source_files": source_files,
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": severity,
        "confidence": confidence,
    }


def _classify_description(value: Any) -> str | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    for label, markers in KNOWN_EXPENSE_MARKERS:
        if any(normalize_text(marker) in normalized for marker in markers):
            return label
    return None


def _money_or_none(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _document_by_role(
    documents: list[dict[str, Any]],
    role: str,
) -> list[dict[str, Any]]:
    return [item for item in documents if str(item.get("role")) == role]


def _visible_approval(document: dict[str, Any]) -> bool:
    approval = document.get("approval")
    if not isinstance(approval, dict):
        return False
    return bool(
        str(approval.get("new_expense_type") or "").strip()
        and str(approval.get("approval_authority") or "").strip()
        and str(approval.get("approval_statement") or "").strip()
        and str(approval.get("approval_date") or "").strip()
        and approval.get("approval_mark_visible") == "visible"
    )


def audit_other_expense_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "other_expense":
        raise AuditError("其他费用审核收到错误的场景类型")

    documents = list(evidence.get("documents") or [])
    issues: list[dict[str, Any]] = []
    known_matches: list[dict[str, Any]] = []
    for document in documents:
        for line in document.get("expense_lines") or []:
            description = str(line.get("description") or "").strip()
            category = _classify_description(description)
            if category is None:
                continue
            known_matches.append(
                {
                    "source_file": str(document["source_file"]),
                    "description": description,
                    "existing_type": category,
                    "amount": line.get("amount"),
                }
            )

    if known_matches:
        sources = list(dict.fromkeys(item["source_file"] for item in known_matches))
        observed = "；".join(
            f"{item['source_file']}：{item['description']} → {item['existing_type']}"
            for item in known_matches
        )
        categories = "、".join(dict.fromkeys(item["existing_type"] for item in known_matches))
        issues.append(
            _issue(
                "expense_type_not_new",
                "类型归类错误",
                sources,
                observed,
                "其他费用只接受无法归入现有分类、且经特殊审批新增的费用类型。",
                f"已识别费用可以归入现有的{categories}，不能用“其他费用”绕过原类型规则。",
                f"将对应费用按{categories}重新归类；同一包包含多种费用时，按类型拆分或分别提交相应资料。",
            )
        )

    approval_documents = _document_by_role(documents, "special_approval")
    valid_approvals = [document for document in approval_documents if _visible_approval(document)]
    if not known_matches and not valid_approvals:
        submitted = [str(document["source_file"]) for document in documents]
        approval_observations = (
            "；".join(
                f"{document['source_file']}：{document.get('visible_summary') or '未识别到批准新增类型的完整信息'}"
                for document in approval_documents
            )
            if approval_documents
            else "本包未发现独立的特殊审批证明"
        )
        issues.append(
            _issue(
                "special_approval_missing",
                "需特殊审批",
                submitted,
                approval_observations,
                "新增费用类型须有独立审批证明，明确新类型、审批主体、批准意见、日期及签字/盖章/系统完成标记。",
                "未建立新增费用类型的授权依据，不能进入其他费用核销。",
                "补交批准新增费用类型的独立特殊审批证明；普通促销合同、结算单和文件名不能替代该审批。",
            )
        )

    contract_documents = _document_by_role(documents, "signed_promotional_contract")
    if len(contract_documents) != 1:
        raise AuditError("其他费用证据必须恰好包含一份签章促销合同")
    contract = contract_documents[0]
    contract_problems: list[str] = []
    if contract.get("signed_visible") != "visible":
        contract_problems.append("签章未清楚显示")
    if len(contract.get("party_names") or []) < 2:
        contract_problems.append("合同双方未完整识别")
    if not contract.get("activity_start") or not contract.get("activity_end"):
        contract_problems.append("活动周期不完整")
    if not contract.get("expense_lines") and contract.get("total_amount") is None:
        contract_problems.append("费用构成和预算均未识别")
    if contract_problems:
        issues.append(
            _issue(
                "promotional_contract_incomplete",
                "促销合同不完整",
                [str(contract["source_file"])],
                f"{contract['source_file']}：" + "；".join(contract_problems),
                "促销合同应清楚显示签订双方、活动周期、费用范围或预算，并已签章。",
                "活动业务基线不完整，不能确认本次费用属于哪个活动及其约定范围。",
                "补交双方、活动周期、费用范围或预算完整且已签章的促销合同。",
            )
        )

    settlement_documents = _document_by_role(documents, "settlement")
    if len(settlement_documents) != 1:
        raise AuditError("其他费用证据必须恰好包含一份结算单")
    settlement = settlement_documents[0]
    settlement_problems: list[str] = []
    if settlement.get("company_template_visible") != "visible":
        settlement_problems.append("未确认公司统一模板")
    if settlement.get("customer_seal_visible") != "visible":
        settlement_problems.append("客户盖章未清楚显示")
    if not settlement.get("customer_name"):
        settlement_problems.append("客户名称未识别")
    if not settlement.get("expense_lines"):
        settlement_problems.append("费用明细或计算依据未识别")
    if settlement.get("total_amount") is None:
        settlement_problems.append("结算合计未识别")
    if settlement_problems:
        issues.append(
            _issue(
                "settlement_invalid",
                "结算单不合格",
                [str(settlement["source_file"])],
                f"{settlement['source_file']}：" + "；".join(settlement_problems),
                "使用公司统一结算单模板，清楚列示客户、费用明细、计算方式、金额和日期，并加盖客户章。",
                "申报金额和客户确认依据不完整。",
                "按公司统一模板补交费用明细完整、金额清楚且有客户盖章的结算单。",
            )
        )

    supporting_roles = {"supporting_document", "invoice_or_receipt"}
    supporting_documents = [
        document for document in documents if str(document.get("role")) in supporting_roles
    ]
    if not supporting_documents:
        issues.append(
            _issue(
                "type_specific_support_missing",
                "具体费用资料不完整",
                [str(contract["source_file"]), str(settlement["source_file"])],
                "除促销合同和结算单外，未识别到独立合同、协议、发票、收据或相关文件。",
                "按审批后的新费用类型提供能证明费用发生和活动执行的独立支持材料。",
                "只有合同和结算总额，不能证明具体费用事实。",
                "按新类型审批要求补交合同、协议、相关文件及活动期照片、POS或其他执行证明。",
            )
        )

    contract_amount = _money_or_none(contract.get("total_amount"), label="促销合同金额")
    settlement_amount = _money_or_none(settlement.get("total_amount"), label="结算单金额")
    if (
        contract_amount is not None
        and settlement_amount is not None
        and contract_amount != settlement_amount
    ):
        issues.append(
            _issue(
                "amount_mismatch",
                "金额不一致",
                [str(contract["source_file"]), str(settlement["source_file"])],
                f"{contract['source_file']}：{contract_amount}元；{settlement['source_file']}：{settlement_amount}元。",
                "同一活动口径下，签章促销合同与客户盖章结算单明确列示的金额应精确一致。",
                "合同预算与本次结算申报未闭合，且不能用总额倒推费用构成。",
                "更正金额冲突的合同或结算单，逐项保留费用描述、计算依据和金额。",
            )
        )

    claim_amount = settlement_amount or Decimal("0.00")
    supported_amount = Decimal("0.00")
    if known_matches:
        decision = "类型归类错误"
    elif not valid_approvals:
        decision = "需特殊审批"
    elif issues:
        decision = "资料需补正"
    else:
        decision = "待人工核定"
        approval = valid_approvals[0]["approval"]
        issues.append(
            _issue(
                "manual_special_review",
                "待人工核定",
                [str(valid_approvals[0]["source_file"]), str(settlement["source_file"])],
                f"特殊审批已显示新增类型“{approval['new_expense_type']}”；结算申报{claim_amount}元。",
                "其他费用的新增类型和金额由有权审批人最终核定，系统不自动建议通过金额。",
                "资料已进入特殊审批人工核定环节，自动建议核销金额保持0元。",
                "无需补交；请由有权审批人确认新增费用类型和最终金额。",
                severity="medium",
            )
        )

    exceptions = [
        {
            "severity": issue["severity"],
            "code": issue["code"],
            "message": issue["title"],
            "impact": issue["impact"],
            "suggestion": issue["resubmission"],
            "source": "、".join(issue["source_files"]),
        }
        for issue in issues
    ]
    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "other_expense",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "其他费用特殊审批核销",
        "summary": {
            "conclusion": "human_review",
            "claimed_amount": json_number(claim_amount),
            "suggested_approved_amount": json_number(supported_amount),
            "temporarily_held_amount": json_number(claim_amount),
            "high_exception_count": sum(issue["severity"] == "high" for issue in issues),
            "medium_exception_count": sum(issue["severity"] == "medium" for issue in issues),
            "error_group_count": len(issues),
            "document_count": len(documents),
            "known_type_match_count": len(known_matches),
            "special_approval_status": "valid" if valid_approvals else "missing_or_invalid",
            "decision_label": decision,
        },
        "other_expense_audit": {
            "documents": documents,
            "known_type_matches": known_matches,
            "special_approval_files": [
                str(document["source_file"]) for document in approval_documents
            ],
            "valid_special_approval_files": [
                str(document["source_file"]) for document in valid_approvals
            ],
            "excluded_files": [Path(path).name for path in case.get("excluded_files") or []],
            "issues": issues,
        },
        "exceptions": exceptions,
    }
