from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.contract_pipeline import merge_contract, validate_inventory, validate_rows


class ContractMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layouts = []
        self.fragments = []
        for page, kind, count in ((1, "stores", 2), (2, "sales_attachment", 2), (3, "sales_attachment", 1)):
            self.layouts.append({"source_file": "contract.pdf", "source_page": page, "tables": [{
                "table_no": 1, "kind": kind, "row_count": count, "clockwise_rotation": 0,
                "body_bounds": {"left": 0, "top": .1, "right": 1, "bottom": .9},
            }]})
            rows = []
            for number in range(1, count + 1):
                row = {"page_local_row_no": number}
                if kind == "stores":
                    row.update(store_name=f"Store {number}", address=None, stack_count=None)
                else:
                    # Duplicate-looking transactions on one page and across pages are valid.
                    row.update(source_page=page, product_code="same-code", quantity=1, total_amount=10)
                rows.append(row)
            self.fragments.append({"source_file": "contract.pdf", "source_page": page,
                                   "table_no": 1, "kind": kind, "records": rows, "extraction_notes": []})
        self.core = {
            "schema_version": "2.5", "scenario": "promotional_display",
            "contract": {"source_file": "contract.pdf", "sales_attachment": {
                "present": True, "source_pages": [2, 3], "total_quantity": None, "total_amount": 123.45,
            }}, "extraction_notes": [],
            "page_inventory": [{"source_page": page["source_page"], "tables": [{
                key: table[key] for key in ("table_no", "kind", "row_count")
            } for table in page["tables"]]} for page in self.layouts],
        }

    def test_cross_page_row_number_restarts_and_identical_transactions_are_preserved(self) -> None:
        before = deepcopy((self.core, self.layouts, self.fragments))
        result = merge_contract(self.core, self.layouts, self.fragments)
        sales = result["contract"]["sales_attachment"]
        self.assertEqual([(row["line_no"], row["source_page"]) for row in sales["records"]], [(1, 2), (2, 2), (3, 3)])
        self.assertEqual([row["product_code"] for row in sales["records"]], ["same-code"] * 3)
        self.assertEqual([row["line_no"] for row in result["contract"]["stores"]], [1, 2])
        self.assertEqual((self.core, self.layouts, self.fragments), before)

    def test_printed_total_is_preserved_without_summing_or_filling_missing_total(self) -> None:
        sales = merge_contract(self.core, self.layouts, self.fragments)["contract"]["sales_attachment"]
        self.assertEqual(sales["total_amount"], 123.45)
        self.assertIsNone(sales["total_quantity"])
        self.assertEqual(sales["source_pages"], [2, 3])

    def test_missing_overlap_reordering_and_wrong_source_are_rejected(self) -> None:
        for fault in ("missing", "overlap", "reorder", "file", "page", "table", "kind"):
            with self.subTest(fault=fault):
                fragments = deepcopy(self.fragments)
                if fault == "missing":
                    fragments.pop()
                elif fault == "overlap":
                    fragments.append(deepcopy(fragments[-1]))
                elif fault == "reorder":
                    fragments[1:] = reversed(fragments[1:])
                elif fault == "file":
                    fragments[1]["source_file"] = "another.pdf"
                elif fault == "page":
                    fragments[1]["records"][0]["source_page"] = 3
                elif fault == "table":
                    fragments[1]["table_no"] = 2
                else:
                    fragments[1]["kind"] = "stores"
                with self.assertRaises(AuditError):
                    merge_contract(self.core, self.layouts, fragments)

    def test_summary_cannot_hide_an_independently_found_sales_page(self) -> None:
        self.core["contract"]["sales_attachment"]["source_pages"] = [2]
        with self.assertRaisesRegex(AuditError, "附件范围"):
            merge_contract(self.core, self.layouts, self.fragments)

    def test_even_pages_without_tables_must_have_a_coverage_receipt(self) -> None:
        self.core["page_inventory"].append({"source_page": 4, "tables": []})
        with self.assertRaises(AuditError):
            merge_contract(self.core, self.layouts, self.fragments)
        self.layouts.append({"source_file": "contract.pdf", "source_page": 4, "tables": []})
        self.assertEqual(len(merge_contract(self.core, self.layouts, self.fragments)["contract"]["stores"]), 2)

    def test_reordered_pages_and_duplicate_table_ids_are_rejected(self) -> None:
        with self.assertRaises(AuditError):
            validate_inventory(list(reversed(self.core["page_inventory"])), 3)
        self.core["page_inventory"][0]["tables"].append(deepcopy(self.core["page_inventory"][0]["tables"][0]))
        with self.assertRaises(AuditError):
            validate_inventory(self.core["page_inventory"], 3)

    def test_requested_rows_are_exact_and_printed_barcode_must_be_valid(self) -> None:
        value = {"records": [{"page_local_row_no": 2, "barcode_69": "6970356167341"}]}
        validate_rows(value, [2])
        with self.assertRaises(AuditError):
            validate_rows(value, [1])
        value["records"][0]["barcode_69"] = "6970356167342"
        with self.assertRaisesRegex(AuditError, "EAN-13"):
            validate_rows(value, [2])


if __name__ == "__main__":
    unittest.main()
