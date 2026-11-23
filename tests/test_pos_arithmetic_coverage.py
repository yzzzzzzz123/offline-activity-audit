"""Counterexamples for full-sheet POS arithmetic and source precision."""
from copy import deepcopy
from decimal import Decimal
import unittest

from audit_core.pdf_evidence import audit_decision, calculate, document_map
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from tests.pdf_test_support import flags_for, unknown_checks


def source(count=140, *, split=False):
    facts = ["销售!R1: C1=名称 | C2=69码 | C3=销售数量 | C4=单价（元） | C5=销售金额"]
    for index in range(2, count + 2):
        facts.append(f"销售!R{index}: C1=测试商品 | C2=6900000000000 | C3={index} | C4=0.1 | C5={Decimal(index) / 10}")
    total = sum(range(2, count + 2))
    facts.append(f"销售!R{count+2}: C1=合计： | C3=公式=SUM(C2:C{count+1})；缓存值={total} | C5=公式=SUM(E2:E{count+1})；缓存值={Decimal(total)/10}")
    chunks = [facts[:75], facts[75:]] if split else [facts]
    documents = [{"unit_id": f"excel-{i}", "facts": rows, "roles": ["spreadsheet"], "limitations": []}
                 for i, rows in enumerate(chunks)]
    units = [{"unit_id": d["unit_id"], "file_id": "original-excel", "source_file": "原始POS.xlsx"} for d in documents]
    materials = [{"id": "pos_excel", "state": "present", "source_ids": [d["unit_id"] for d in documents]}]
    return documents, materials, units


def native_formula_source(layout="factor", *, quantities=None, prices=None):
    """Three source layouts; names are synthetic, all formulas are native facts."""
    quantities = quantities or [2, 3, 4]
    prices = prices or [Decimal("19.8")] * len(quantities)
    if layout == "factor":
        header, first, quantity_col, price_col, amount_col, formula_cols = (
            "C1=商品名称 | C5=销售数量 | C6=销售金额 | C7=销售总计", 3, 5, 6, 7, ("E", "F", "G"))
    elif layout == "retail":
        header, first, quantity_col, price_col, amount_col, formula_cols = (
            "C1=商品编码 | C2=商品名称 | C5=销售数量 | C6=单位 | C7=零售价 | C8=销售总计", 4, 5, 7, 8, ("E", "G", "H"))
    else:
        header, first, quantity_col, price_col, amount_col, formula_cols = (
            "C1=日期 | C2=商品名称 | C4=单价 | C5=销售数量 | C6=销售金额", 3, 5, 4, 6, ("E", "D", "F"))
    facts = [f"Sheet1!R{first-1}: {header}"]
    amounts = []
    for index, (quantity, price) in enumerate(zip(quantities, prices), first):
        amount = Decimal(quantity) * Decimal(price)
        amounts.append(amount)
        cells = {1: "测试商品", quantity_col: str(quantity), price_col: str(price),
                 amount_col: f"公式={formula_cols[0]}{index}*{formula_cols[1]}{index}；缓存值={amount}"}
        facts.append(f"Sheet1!R{index}: " + " | ".join(f"C{col}={raw}" for col, raw in sorted(cells.items())))
    last = first + len(quantities) - 1
    facts.append(f"Sheet1!R{last+1}: C1=合计 | C{quantity_col}={sum(quantities)} | "
                 f"C{amount_col}=公式=SUM({formula_cols[2]}{first}:{formula_cols[2]}{last})；缓存值={sum(amounts)}")
    documents, materials, units = source(1)
    documents[0]["facts"] = facts
    return documents, materials, units


def inline_total_source():
    quantities = [553, 486, 367, 598, 466, 467, 671, 560, 632, 735]
    documents, materials, units = source(1)
    facts = ["销售!R1: C1=测试销售数据", "销售!R2: C1=门店 | C2=商品条码 | C3=商品名称 | C4=销售数量 | C5=销售单价 | C6=金额"]
    for row, quantity in enumerate(quantities, 3):
        facts.append(f"销售!R{row}: C1=测试门店{row} | C2=6900000000000 | C3=测试商品 | C4={quantity} | C5=29.9 | C6=公式=D{row}*E{row}；缓存值={Decimal(quantity)*Decimal('29.9')}")
    facts.append("销售!R13: C1=      合计：165496.5元")
    documents[0]["facts"] = facts
    return documents, materials, units


class PosArithmeticCoverageTests(unittest.TestCase):
    def test_partial_ten_row_model_sum_cannot_create_a_false_whole_sheet_difference(self):
        documents, materials, units = source()
        flags = flags_for("personnel_incentive")
        evidence = unknown_checks("personnel_incentive", flags)
        raw = next(c for c in evidence["checks"] if c["rule_id"] == "pos_arithmetic")
        plan = prepare_pos_arithmetic(documents, materials, units)
        partial = deepcopy(next(c for c in plan["comparisons"] if c["value_kind"] == "quantity"))
        partial["right"] = partial["right"][:10]
        raw.update(status="fail", reason="抽取前十行后误报总数不符", source_ids=["excel-0"], comparisons=[partial])
        original = deepcopy(evidence)
        decision = audit_decision("personnel_incentive", flags, evidence, documents, materials, units=units)
        check = next(c for c in decision["checks"] if c["rule_id"] == "pos_arithmetic")
        self.assertEqual(check["status"], "pass")
        self.assertEqual(len(check["calculations"]), 142)
        total = next(c for c in check["calculations"] if c["value_kind"] == "quantity")
        self.assertEqual(len(total["right"]), 140)
        self.assertEqual(total["left_value"], total["right_value"])
        self.assertEqual(evidence, original, "原始模型观察不可被覆盖")

    def test_headers_and_totals_across_chunks_keep_all_rows_and_sources(self):
        documents, materials, units = source(160, split=True)
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"])
        self.assertEqual(len(plan["comparisons"]), 162)
        quantity = next(c for c in plan["comparisons"] if c["value_kind"] == "quantity")
        self.assertEqual(len(quantity["right"]), 160)
        self.assertEqual({v["unit_id"] for v in quantity["right"]}, {"excel-0", "excel-1"})
        self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))

    def test_every_product_row_is_checked_and_raw_decimal_deficit_is_preserved(self):
        documents, materials, units = source()
        documents[0]["facts"][100] = "销售!R101: C1=测试商品 | C2=6900000000000 | C3=68 | C4=29.9 | C5=公式=C101*D101；缓存值=2033.1999999999998"
        plan = prepare_pos_arithmetic(documents, materials, units)
        comparison = next(c for c in plan["comparisons"] if c["label"] == "销售第101行销售金额")
        result = calculate(comparison, document_map(documents))
        self.assertFalse(result["matches"])
        self.assertEqual(result["left_value"], "2033.1999999999998")
        self.assertEqual(result["right_value"], "2033.2")

    def test_uncached_formula_or_missing_value_never_silently_passes(self):
        documents, materials, units = source()
        documents[0]["facts"][100] = "销售!R101: C1=测试商品 | C3=68 | C4=29.9 | C5=公式=C101*D101；缓存值=无缓存结果，无法确认数值"
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertTrue(any("101" in text for text in plan["limitations"]))

    def test_a_partial_sum_formula_is_unknown_instead_of_proving_full_totals(self):
        documents, materials, units = source()
        documents[0]["facts"][-1] = "销售!R142: C1=合计 | C3=公式=SUM(C2:C11)；缓存值=65 | C5=公式=SUM(E2:E11)；缓存值=6.5"
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertFalse(any(c["operation"] == "sum" for c in plan["comparisons"]))

    def test_same_sheet_names_in_different_files_cannot_merge(self):
        documents, materials, units = source(5, split=False)
        extra = deepcopy(documents[0]); extra["unit_id"] = "other"
        documents.append(extra)
        units.append({"unit_id": "other", "file_id": "other-file", "source_file": "另一个POS.xlsx"})
        materials[0]["source_ids"].append("other")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"])
        self.assertEqual(len(plan["tables"]), 2)
        for comparison in plan["comparisons"]:
            self.assertEqual({v["unit_id"] for v in [comparison["left"], *comparison["right"]]}, {comparison["left"]["unit_id"]})

    def test_stamped_copy_cannot_fill_a_missing_excel_amount(self):
        documents, materials, units = source(1)
        documents[0]["facts"][1] = "销售!R2: C1=测试商品 | C3=2 | C4=0.1"
        stamped = deepcopy(documents[0]); stamped["unit_id"] = "stamped"
        stamped["facts"][1] += " | C5=0.2"
        documents.append(stamped)
        materials.append({"id": "pos", "state": "present", "source_ids": ["stamped"]})
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertNotIn("stamped", plan["source_ids"])

    def test_explicit_retail_price_and_amount_headers_keep_all_rows(self):
        documents, materials, units = source(8)
        documents[0]["facts"][0] = documents[0]["facts"][0].replace(
            "单价（元）", "零售价（元）").replace("销售金额", "合计金额")
        original = deepcopy(documents)
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"])
        self.assertEqual(len(plan["comparisons"]), 10)
        self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))
        self.assertEqual(documents, original)

    def test_pivot_sum_labels_map_only_quantity_and_amount(self):
        for separator in (":", "："):
            with self.subTest(separator=separator):
                documents, materials, units = source(6)
                documents[0]["facts"][0] = documents[0]["facts"][0].replace(
                    "销售数量", "求和项" + separator + "销售数量").replace(
                    "单价（元）", "零售价").replace("销售金额", "求和项" + separator + "销售金额")
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertTrue(plan["complete"])
                self.assertEqual(len(plan["comparisons"]), 8)
                self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))

    def test_average_quantity_or_summed_price_is_not_a_sale_column(self):
        for before, after in (("销售数量", "平均值项:销售数量"),
                              ("单价（元）", "求和项:销售单价")):
            with self.subTest(header=after):
                documents, materials, units = source(3)
                documents[0]["facts"][0] = documents[0]["facts"][0].replace(before, after)
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertEqual(plan["comparisons"], [])

    def test_newlines_in_header_and_product_cell_do_not_drop_a_row(self):
        documents, materials, units = source(4)
        documents[0]["facts"][0] = documents[0]["facts"][0].replace(
            "销售数量", "销售\n数量").replace("单价（元）", "零售\n价（元）")
        documents[0]["facts"][1] = documents[0]["facts"][1].replace("测试商品", "测试\n商品")
        original = deepcopy(documents)
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"])
        self.assertEqual(plan["tables"][0]["detail_rows"], 4)
        self.assertEqual(len(plan["comparisons"]), 6)
        self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))
        self.assertEqual(documents, original)

    def test_unrecognized_second_file_or_sheet_cannot_be_silently_complete(self):
        for second_file in (False, True):
            with self.subTest(second_file=second_file):
                documents, materials, units = source(3)
                other = deepcopy(documents[0]); other["unit_id"] = "other"
                other["facts"] = [f.replace("销售!", "另一工作表!") for f in other["facts"]]
                other["facts"][0] = other["facts"][0].replace("单价（元）", "无法对应的价格")
                documents.append(other)
                units.append({"unit_id": "other", "file_id": "other-file" if second_file else "original-excel",
                              "source_file": "另一个POS.xlsx" if second_file else "原始POS.xlsx"})
                materials[0]["source_ids"].append("other")
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertEqual(len(plan["tables"]), 1)
                self.assertTrue(any("另一工作表" in text for text in plan["limitations"]))

    def test_partly_unreadable_native_source_cannot_be_silently_complete(self):
        documents, materials, units = source(3)
        documents[0]["facts"].append("无法保留原始行列的记录")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertTrue(any("原始POS.xlsx" in text for text in plan["limitations"]))

    def test_two_explicit_sheets_are_checked_without_inventing_cross_sheet_totals(self):
        documents, materials, units = source(6)
        documents[0]["facts"] = [f.replace("销售!", "Sheet4!") for f in documents[0]["facts"]]
        documents[0]["facts"][0] = documents[0]["facts"][0].replace(
            "销售数量", "求和项:销售数量").replace("单价（元）", "零售价").replace(
            "销售金额", "求和项:销售金额")
        detail_documents, _, _ = source(40)
        other = detail_documents[0]; other["unit_id"] = "details"
        other["facts"] = [f.replace("销售!", "Sheet1!") for f in other["facts"][:-1]]
        other["facts"][0] = other["facts"][0].replace("单价（元）", "零售价")
        documents.append(other)
        units.append({"unit_id": "details", "file_id": "original-excel", "source_file": "原始POS.xlsx"})
        materials[0]["source_ids"].append("details")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"], "另一表的合计不能自动充当本表合计")
        self.assertEqual([t["detail_rows"] for t in plan["tables"]], [6, 40])
        self.assertEqual(len(plan["comparisons"]), 48)
        self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))
        self.assertEqual(len(plan["limitations"]), 1)
        self.assertIn("Sheet1未找到", plan["limitations"][0])

    def test_older_non_native_evidence_keeps_existing_fallback(self):
        documents, materials, units = source(1)
        documents[0]["facts"] = ["销售数量2，单价0.1，销售金额0.2"]
        self.assertIsNone(prepare_pos_arithmetic(documents, materials, units))

    def test_native_sales_total_layouts_keep_every_row_and_source_operand(self):
        for layout in ("factor", "retail", "explicit"):
            with self.subTest(layout=layout):
                documents, materials, units = native_formula_source(layout, quantities=list(range(1, 37)))
                before = deepcopy((documents, materials, units))
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertTrue(plan["complete"], plan["limitations"])
                self.assertEqual(plan["tables"][0]["detail_rows"], 36)
                self.assertEqual(len(plan["comparisons"]), 38)
                self.assertTrue(all(calculate(c, document_map(documents))["matches"] for c in plan["comparisons"]))
                self.assertEqual((documents, materials, units), before)

    def test_sales_amount_header_alone_never_proves_a_unit_price(self):
        documents, materials, units = native_formula_source()
        documents[0]["facts"] = [line.replace("公式=E3*F3；缓存值=39.6", "39.6")
                                 for line in documents[0]["facts"]]
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["comparisons"], [])

    def test_formula_mapping_requires_every_row_same_columns_and_exact_cache(self):
        substitutions = (
            ("E4*F4", "E3*F4"),
            ("E4*F4", "E4*D4"),
            ("E4*F4", "E4*F4+0"),
            ("E4*F4", "Sheet2!E4*F4"),
            ("缓存值=59.4", "缓存值=59.4000000000000000000000000001"),
            ("缓存值=59.4", "缓存值=无缓存结果，无法确认数值"),
            (" | C5=3", ""),
            (" | C6=19.8", ""),
            (" | C7=公式=E4*F4；缓存值=59.4", ""),
        )
        for before, after in substitutions:
            with self.subTest(before=before, after=after):
                documents, materials, units = native_formula_source()
                self.assertIn(before, documents[0]["facts"][2])
                documents[0]["facts"][2] = documents[0]["facts"][2].replace(before, after)
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertEqual(plan["comparisons"], [], "其他两行公式正确不能代替全表证明")

    def test_formula_mapping_cannot_borrow_another_file_or_ignore_duplicate_headers(self):
        for changed in ("C6=销售金额 | C8=销售金额", "C6=销售金额 | C8=销售单价"):
            with self.subTest(changed=changed):
                documents, materials, units = native_formula_source()
                documents[0]["facts"][0] = documents[0]["facts"][0].replace("C6=销售金额", changed)
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertEqual(plan["comparisons"], [])
        documents, materials, units = native_formula_source()
        other = deepcopy(documents[0]); other["unit_id"] = "other"
        other["facts"][2] = other["facts"][2].replace("公式=E4*F4；缓存值=59.4", "59.4")
        documents.append(other)
        units.append({"unit_id": "other", "file_id": "other-file", "source_file": "其他POS.xlsx"})
        materials[0]["source_ids"].append("other")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertEqual(len(plan["tables"]), 1)
        self.assertTrue(all(c["left"]["unit_id"] == "excel-0" for c in plan["comparisons"]))

    def test_lone_dot_after_total_is_not_a_detail_but_source_facts_stay_intact(self):
        documents, materials, units = native_formula_source("explicit", quantities=list(range(1, 22)))
        documents[0]["facts"].append("Sheet1!R25: C6=.")
        before = deepcopy(documents)
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"], plan["limitations"])
        self.assertEqual(plan["tables"][0]["detail_rows"], 21)
        self.assertEqual(len(plan["comparisons"]), 23)
        self.assertEqual(documents, before)

    def test_product_or_number_after_total_is_still_incomplete(self):
        for content in ("C1=测试商品 | C6=.", "C6=0", "C4=19.8 | C5=1 | C6=19.8"):
            with self.subTest(content=content):
                documents, materials, units = native_formula_source("explicit")
                documents[0]["facts"].append("Sheet1!R7: " + content)
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertTrue(any("合计之后" in text for text in plan["limitations"]))

    def test_dot_inside_detail_range_and_partial_sum_remain_unknown(self):
        documents, materials, units = native_formula_source("explicit")
        documents[0]["facts"][2] = "Sheet1!R4: C6=."
        self.assertFalse(prepare_pos_arithmetic(documents, materials, units)["complete"])
        documents, materials, units = native_formula_source()
        documents[0]["facts"][-1] = documents[0]["facts"][-1].replace("SUM(G3:G5)", "SUM(G3:G4)")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertFalse(any(c["operation"] == "sum" and c["value_kind"] == "amount" for c in plan["comparisons"]))

    def test_actual_quantity_609_versus_declared_826_still_fails_with_correct_amount(self):
        documents, materials, units = native_formula_source(
            "retail", quantities=[1] * 34 + [574, 1],
            prices=[Decimal("10")] * 34 + [Decimal("17.3"), Decimal("275.4")])
        documents[0]["facts"][-1] = documents[0]["facts"][-1].replace("C5=609", "C5=826")
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertTrue(plan["complete"])
        results = [calculate(c, document_map(documents)) for c in plan["comparisons"]]
        failed = [c for c in results if not c["matches"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["value_kind"], failed[0]["left_value"], failed[0]["right_value"]),
                         ("quantity", "826", "609"))
        amount = next(c for c in results if c["operation"] == "sum" and c["value_kind"] == "amount")
        self.assertEqual((amount["left_value"], amount["right_value"]), ("10545.6", "10545.6"))
        flags = flags_for("personnel_incentive")
        decision = audit_decision("personnel_incentive", flags,
                                  unknown_checks("personnel_incentive", flags), documents, materials, units=units)
        check = next(c for c in decision["checks"] if c["rule_id"] == "pos_arithmetic")
        self.assertEqual(check["status"], "fail")
        self.assertIn("826", check["reason"])
        self.assertIn("609", check["reason"])

    def test_single_final_currency_total_checks_all_rows_but_keeps_missing_quantity(self):
        documents, materials, units = inline_total_source()
        before = deepcopy((documents, materials, units))
        plan = prepare_pos_arithmetic(documents, materials, units)
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["tables"][0]["total_rows"], [13])
        self.assertEqual(plan["tables"][0]["detail_rows"], 10)
        sums = [c for c in plan["comparisons"] if c["operation"] == "sum"]
        self.assertEqual(len(sums), 1)
        self.assertEqual(sums[0]["left"]["quote"], "C1=      合计：165496.5元")
        self.assertEqual(len(sums[0]["right"]), 10)
        self.assertEqual({v["unit_id"] for v in [sums[0]["left"], *sums[0]["right"]]}, {"excel-0"})
        result = calculate(sums[0], document_map(documents))
        self.assertEqual((result["left_value"], result["right_value"]), ("165496.5", "165496.5"))
        self.assertTrue(result["matches"])
        self.assertEqual(plan["limitations"], ["销售未找到销售数量合计，无法完成数量合计复算。"])
        flags = flags_for("personnel_incentive")
        decision = audit_decision("personnel_incentive", flags, unknown_checks("personnel_incentive", flags),
                                  documents, materials, units=units)
        check = next(c for c in decision["checks"] if c["rule_id"] == "pos_arithmetic")
        self.assertEqual(check["status"], "unknown")
        self.assertFalse(check["coverage"]["complete"])
        self.assertEqual((documents, materials, units), before)

    def test_inline_total_amount_deficit_is_a_real_failure_even_without_quantity_total(self):
        documents, materials, units = inline_total_source()
        documents[0]["facts"][-1] = "销售!R13: C1=合计：165496.4元"
        flags = flags_for("personnel_incentive")
        decision = audit_decision("personnel_incentive", flags, unknown_checks("personnel_incentive", flags),
                                  documents, materials, units=units)
        check = next(c for c in decision["checks"] if c["rule_id"] == "pos_arithmetic")
        self.assertEqual(check["status"], "fail")
        self.assertIn("165496.4", check["reason"])
        self.assertIn("165496.5", check["reason"])
        self.assertIn("未找到销售数量合计", check["reason"])

    def test_inline_total_rejects_prose_subtotals_multiple_numbers_and_extra_cells(self):
        for value in ("小计：165496.5元", "合计：165496.5元（仅部分门店）", "合计：165496.5元，另有10元",
                      "合计：165496.5", "销售单价合计：165496.5元", "合计：165496.5元\n说明：未含退货",
                      "合计：165496.5元 | C3=另一商品"):
            with self.subTest(value=value):
                documents, materials, units = inline_total_source()
                documents[0]["facts"][-1] = "销售!R13: C1=" + value
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertFalse(any(c["operation"] == "sum" for c in plan["comparisons"]))

    def test_inline_total_requires_sole_last_row_and_contiguous_complete_detail_block(self):
        for mode in ("footer", "later_product", "two_inline", "other_total", "gap", "later_header", "huge_index"):
            with self.subTest(mode=mode):
                documents, materials, units = inline_total_source()
                facts = documents[0]["facts"]
                if mode == "footer":
                    facts.append("销售!R14: C1=说明")
                elif mode == "later_product":
                    facts.append("销售!R14: C1=另一商品 | C4=1 | C5=29.9 | C6=29.9")
                elif mode == "two_inline":
                    facts.append("销售!R14: C1=合计：165496.5元")
                elif mode == "other_total":
                    facts.insert(-1, "销售!R12: C1=合计 | C4=5535 | C6=165496.5")
                elif mode == "gap":
                    facts[6] = "销售!R7: C1=此行无法确认"
                elif mode == "later_header":
                    facts.append("销售!R14: C1=门店 | C4=销售数量 | C5=销售单价 | C6=金额")
                else:
                    facts[-1] = "销售!R1000000000000000000: C1=合计：165496.5元"
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertFalse(any(c["operation"] == "sum" and c["left"]["quote"].endswith("元")
                                     for c in plan["comparisons"]))

    def test_inline_total_cannot_borrow_details_from_another_sheet_or_file(self):
        for same_file in (True, False):
            with self.subTest(same_file=same_file):
                documents, materials, units = inline_total_source()
                total = documents[0]["facts"].pop()
                documents.append({"unit_id": "other", "facts": [total.replace("销售!", "另一表!") if same_file else total],
                                  "roles": ["spreadsheet"], "limitations": []})
                materials[0]["source_ids"].append("other")
                units.append({"unit_id": "other", "file_id": "original-excel" if same_file else "other-file",
                              "source_file": "原始POS.xlsx" if same_file else "其他POS.xlsx"})
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertFalse(any(c["operation"] == "sum" for c in plan["comparisons"]))

    def test_inline_total_never_supplies_a_missing_row_amount_or_price(self):
        for absent in (" | C6=公式=D3*E3；缓存值=16534.7", " | C5=29.9"):
            with self.subTest(absent=absent):
                documents, materials, units = inline_total_source()
                documents[0]["facts"][2] = documents[0]["facts"][2].replace(absent, "")
                plan = prepare_pos_arithmetic(documents, materials, units)
                self.assertFalse(plan["complete"])
                self.assertTrue(any("第3行" in reason for reason in plan["limitations"]))
                if "C6" in absent:
                    self.assertFalse(any(c["operation"] == "sum" for c in plan["comparisons"]))


if __name__ == "__main__":
    unittest.main()
