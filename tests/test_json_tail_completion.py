"""A JSON answer cut off only by trailing brackets must not fail the claim."""
import json
import tempfile
import unittest
from pathlib import Path

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser, _complete_closing_brackets


class JsonTailCompletionTests(unittest.TestCase):
    def test_missing_trailing_closers_are_completed_without_adding_content(self):
        self.assertEqual(_complete_closing_brackets('{"a": 1'), {"a": 1})
        self.assertEqual(_complete_closing_brackets('{"a": [1, 2'), {"a": [1, 2]})
        self.assertEqual(_complete_closing_brackets('{"a": {"b": "c"'), {"a": {"b": "c"}})

    def test_anything_other_than_a_tail_is_left_to_the_normal_failure(self):
        for text in ('{"a": "unterminated', '{"a": 1]', '[1, 2}', '{"a": }}', "", '[]'):
            with self.subTest(text=text):
                self.assertIsNone(_complete_closing_brackets(text))

    def test_parser_accepts_a_completed_tail_and_still_rejects_broken_json(self):
        with tempfile.TemporaryDirectory() as directory:
            schema = Path(directory) / "schema.json"
            schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            parser = EvidenceOutputParser(schema_path=schema)
            self.assertEqual(parser.parse('{"checks": [{"rule_id": "r"'), {
                "checks": [{"rule_id": "r"}]})
            self.assertEqual(parser.last_diagnostics["validation_code"], "json_syntax_completed")
            with self.assertRaises((AuditError, ValueError)):
                parser.parse('{"checks": [}')


if __name__ == "__main__":
    unittest.main()
