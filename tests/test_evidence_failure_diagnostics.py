"""Source constraints and diagnostics use synthetic evidence and no remote model."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser, ReadingSourceCoverageError
from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.model_settings import api_selection
from audit_core.pdf_evidence import audit_decision, audit_schema, DOCUMENT_SCHEMA
from audit_core.pdf_policy import MATERIALS
from audit_core.qwen_model import extract_qwen_json
from tests.pdf_test_support import PolicyProvider, flags_for, unknown_checks


class EvidenceFailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.materials = [
            {'id': 'activity_application', 'state': 'present', 'source_ids': ['contract'], 'reason': 'Synthetic contract'},
            {'id': 'settlement', 'state': 'present', 'source_ids': ['claim'], 'reason': 'Synthetic settlement'},
        ]
        self.flags = flags_for('personnel_incentive')
        self.documents = [
            {'unit_id': 'contract', 'facts': ['合同预算14000元，合同奖励单价2元']},
            {'unit_id': 'claim', 'facts': ['申报奖励单价2元，申报数量7049']},
        ]

    def priced_evidence(self):
        value = unknown_checks('personnel_incentive', self.flags)
        check = next(c for c in value['checks'] if c['rule_id'] == 'reward_unit_price')
        check.update(status='pass', reason='Synthetic source-bound comparison', source_ids=['claim', 'contract'],
            comparisons=[{'label': 'Synthetic reward price', 'value_kind': 'unit_price',
                'left': {'unit_id': 'claim', 'quote': '申报奖励单价2元', 'number': '2'},
                'right': [{'unit_id': 'contract', 'quote': '合同奖励单价2元', 'number': '2'}],
                'operation': 'value', 'operator': 'eq'}])
        return value, check

    def reading_fixture(self, count=2, width=64):
        expected = ["image-" + f"{index:0{width}x}" for index in range(count)]
        schema = deepcopy(DOCUMENT_SCHEMA)
        schema["properties"]["documents"].update(minItems=count, maxItems=count)
        properties = schema["properties"]["documents"]["items"]["properties"]
        properties["unit_id"] = {**properties["unit_id"], "enum": expected}
        path = self.root / "reading-schema.json"
        path.write_text(json.dumps(schema), encoding="utf-8")
        value = {"documents": [{"unit_id": uid, "roles": ["photo"], "facts": ["PRIVATE_VISIBLE_FACT"], "limitations": []}
                               for uid in expected]}
        def validator(value):
            actual = [d["unit_id"] for d in value["documents"]]
            if len(actual) != len(expected) or len(set(actual)) != len(expected) or set(actual) != set(expected):
                raise ReadingSourceCoverageError(expected, actual)
        return path, expected, value, validator

    def test_reading_unknown_and_missing_ids_report_only_server_ids_and_safe_paths(self):
        path, expected, valid, validator = self.reading_fixture()
        for error in ("unknown", "missing", "extra", "duplicate"):
            value = deepcopy(valid)
            if error == "unknown":
                value["documents"][0]["unit_id"] = "PRIVATE_KEY_OR_INSTRUCTION"
            elif error == "missing":
                value["documents"].pop(0)
            elif error == "extra":
                value["documents"].append({**deepcopy(value["documents"][0]), "unit_id": "PRIVATE_KEY_OR_INSTRUCTION"})
            else:
                value["documents"][0]["unit_id"] = expected[1]
            before = deepcopy(value)
            parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
            with self.subTest(error=error), self.assertRaises(ReadingSourceCoverageError) as raised:
                parser.parse(json.dumps(value))
            details = parser.last_diagnostics
            self.assertEqual(details["validation_code"], "reading_source_coverage")
            self.assertEqual(details["validation_stage"], "evidence" if error == "duplicate" else "schema")
            context = json.loads(parser.last_retry_context)
            self.assertEqual(context["missing"], [] if error == "extra" else [expected[0]])
            self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 1000)
            self.assertNotIn("PRIVATE_", str(raised.exception) + json.dumps(details) + parser.last_retry_context)
            self.assertEqual(value, before)
            self.assertEqual(parser.parse(json.dumps(valid)), valid)
            self.assertEqual(parser.last_diagnostics, {})
            self.assertEqual(parser.last_retry_context, "")

    def test_short_and_legacy_image_ids_keep_strict_structure_and_coverage_retry_hints(self):
        for width in (24, 64):
            with self.subTest(width=width):
                path, expected, valid, validator = self.reading_fixture(width=width)
                bad = {'units': [{'id': uid, 'roles': [], 'facts': [], 'limitations': []} for uid in expected]}
                parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
                with self.assertRaises(AuditError) as raised:
                    parser.parse(json.dumps(bad))
                self.assertIn('"required_root_field":"documents"', str(raised.exception))
                self.assertTrue(all(uid in str(raised.exception) for uid in expected))
                unknown = deepcopy(valid)
                unknown['documents'][0]['unit_id'] = 'PRIVATE_INVALID_RESPONSE_SOURCE'
                with self.assertRaises(ReadingSourceCoverageError):
                    parser.parse(json.dumps(unknown))
                self.assertEqual(json.loads(parser.last_retry_context)['missing'], [expected[0]])
                self.assertNotIn('PRIVATE_', parser.last_retry_context)
                self.assertEqual(parser.parse(json.dumps(valid)), valid)

    def test_both_api_transports_correct_short_id_structure_without_normalizing_output(self):
        for provider in ('ppio', 'deepseek'):
            with self.subTest(provider=provider):
                path, expected, valid, validator = self.reading_fixture(width=24)
                replies = iter(({'units': valid['documents']}, valid))
                prompts = []
                def model(selection, content, **kwargs):
                    prompts.append(content[0]['text'])
                    return json.dumps(next(replies)), {'input_tokens': 12, 'output_tokens': 5}
                selection = api_selection({'provider': provider,
                    'api_key': 'sk_synthetic_never_sent' if provider == 'ppio' else 'sk-synthetic-never-sent'})
                with redirect_stdout(io.StringIO()), patch('audit_core.qwen_model.complete_json', side_effect=model) as request, \
                        patch('audit_core.qwen_model.time.sleep'), \
                        patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
                    result = extract_qwen_json(connection=selection, root=self.root, skill=self.root,
                        schema_path=path, prompt='Synthetic short-ID source reading', images=[], label='隔离资料读取', validator=validator)
                self.assertEqual(result, valid)
                self.assertEqual(request.call_count, 2)
                self.assertIn('required_root_field', prompts[1])
                self.assertTrue(all(uid in prompts[1] for uid in expected))

    def test_reading_bad_facts_or_reasoning_are_not_reclassified_as_id_errors(self):
        path, expected, valid, validator = self.reading_fixture()
        for error in ("facts", "reasoning", "facts_with_wrong_id"):
            value = deepcopy(valid)
            if error == "reasoning":
                value["reasoning"] = "PRIVATE_CHAIN_OF_THOUGHT"
            else:
                value["documents"][0]["facts"] = {"private": "PRIVATE_FACT"}
            if error == "facts_with_wrong_id":
                value["documents"][0]["unit_id"] = "PRIVATE_UNKNOWN"
            parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
            with self.subTest(error=error), self.assertRaises(AuditError) as raised:
                parser.parse(json.dumps(value))
            self.assertEqual(parser.last_diagnostics["validation_code"], "schema_validation")
            self.assertEqual(parser.last_retry_context, "")
            self.assertNotIn("PRIVATE_", json.dumps(parser.last_diagnostics) + str(raised.exception))

    def test_reading_feedback_is_bounded_even_with_many_unknown_model_values(self):
        path, expected, valid, validator = self.reading_fixture(6)
        value = {"documents": [{"unit_id": "PRIVATE_UNKNOWN_" + "x" * 1000,
                                "roles": [], "facts": [], "limitations": []} for _ in range(200)]}
        parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
        with self.assertRaises(ReadingSourceCoverageError) as raised:
            parser.parse(json.dumps(value))
        self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 1000)
        context = json.loads(parser.last_retry_context)
        self.assertEqual(context["missing"], expected)
        self.assertEqual(context["unknown_count"], 200)
        self.assertTrue(context["omitted_unknown_paths"])
        self.assertLessEqual(len(context["unknown_paths"]), 6)
        self.assertNotIn("PRIVATE_", str(raised.exception) + parser.last_retry_context)

    def test_existing_transport_retries_reading_ids_without_logging_fact_or_unknown_value(self):
        path, expected, valid, validator = self.reading_fixture()
        bad_id = deepcopy(valid)
        bad_id["documents"][0]["unit_id"] = "PRIVATE_UNKNOWN_VALUE"
        duplicate = deepcopy(valid)
        duplicate["documents"][0]["unit_id"] = expected[1]
        replies, prompts = iter((bad_id, duplicate, valid)), []
        def model(selection, content, **kwargs):
            prompts.append(content[0]["text"])
            return json.dumps(next(replies)), {"input_tokens": 12, "output_tokens": 5}
        stream = io.StringIO()
        selection = api_selection({"provider": "ppio", "api_key": "sk_synthetic_not_sent"})
        with collect_model_artifacts(self.root / "analysis"), redirect_stdout(stream), \
                patch("audit_core.qwen_model.complete_json", side_effect=model) as request, \
                patch("audit_core.qwen_model.time.sleep"), \
                patch("httpx.AsyncClient", side_effect=AssertionError("External model forbidden")):
            result = extract_qwen_json(connection=selection, root=self.root, skill=self.root,
                schema_path=path, prompt="Synthetic source reading", images=[], label="隔离资料读取", validator=validator)
        self.assertEqual(result, valid)
        self.assertEqual(request.call_count, 3)
        self.assertIn(expected[0], prompts[1])
        self.assertIn('"unknown_count":1', prompts[1])
        self.assertIn('"duplicate"', prompts[2])
        rows = [json.loads(line) for line in (self.root / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["validation_code"] for row in rows[:2]], ["reading_source_coverage"] * 2)
        self.assertEqual([row["validation_stage"] for row in rows[:2]], ["schema", "evidence"])
        self.assertNotIn("PRIVATE_", stream.getvalue() + json.dumps(rows) + "".join(prompts))

    def test_reading_root_errors_get_exact_bounded_shape_hint_without_accepting_or_echoing_output(self):
        path, expected, valid, validator = self.reading_fixture(6)
        cases = [(valid["documents"], "array"), ("PRIVATE_STRING", "string"),
                 (None, "null"), (True, "boolean"), (42, "number")]
        for value, root_type in cases:
            parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
            before = deepcopy(value)
            with self.subTest(root_type=root_type), self.assertRaises(AuditError) as raised:
                parser.parse(json.dumps(value))
            hint = str(raised.exception)
            self.assertIn('"required_root_field":"documents"', hint)
            self.assertIn('"expected_count":6', hint)
            self.assertTrue(all(uid in hint for uid in expected))
            self.assertLessEqual(len(hint.encode("utf-8")), 1000)
            self.assertNotIn("PRIVATE_", hint + json.dumps(parser.last_diagnostics))
            self.assertEqual(parser.last_diagnostics["validation_code"], "json_object")
            self.assertEqual(parser.last_diagnostics["validation_root_type"], root_type)
            self.assertEqual(parser.last_diagnostics["validation_schema_kinds"], ["type"])
            self.assertEqual(value, before)
            self.assertEqual(parser.parse(json.dumps(valid)), valid)
            self.assertEqual(parser.last_diagnostics, {})

    def test_reading_schema_and_json_errors_only_describe_fixed_contract_and_error_kinds(self):
        path, expected, valid, validator = self.reading_fixture()
        bad_facts = deepcopy(valid); bad_facts["documents"][0]["facts"] = {"PRIVATE_KEY": "PRIVATE_VALUE"}
        cases = [("PRIVATE_INVALID_JSON{", "json_syntax", "invalid_json", []),
                 (json.dumps({"PRIVATE_ROOT_KEY": "PRIVATE_VALUE"}), "schema_validation", "object", ["additionalProperties", "required"]),
                 (json.dumps({**valid, "PRIVATE_REASONING_KEY": "PRIVATE_REASONING"}), "schema_validation", "object", ["additionalProperties"]),
                 (json.dumps(bad_facts), "schema_validation", "object", ["type"])]
        for raw, code, root_type, kinds in cases:
            parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
            with self.subTest(code=code, kinds=kinds), self.assertRaises(AuditError) as raised:
                parser.parse(raw)
            hint = str(raised.exception)
            self.assertIn("documents", hint)
            self.assertIn('"document_fields":["unit_id","roles","facts","limitations"]', hint)
            self.assertTrue(all(uid in hint for uid in expected))
            self.assertEqual(parser.last_diagnostics["validation_code"], code)
            self.assertEqual(parser.last_diagnostics["validation_root_type"], root_type)
            self.assertEqual(set(parser.last_diagnostics["validation_schema_kinds"]), set(kinds))
            self.assertLessEqual(len(hint.encode("utf-8")), 1000)
            self.assertNotIn("PRIVATE_", hint + json.dumps(parser.last_diagnostics))
            self.assertEqual(parser.last_retry_context, "")

    def test_reading_hint_does_not_apply_to_other_or_modified_schemas(self):
        path, expected, valid, validator = self.reading_fixture()
        schema = json.loads(path.read_text(encoding="utf-8"))
        variants = [deepcopy(DOCUMENT_SCHEMA), {**schema, "description": "Other schema"}]
        bad_enum = deepcopy(schema)
        bad_enum["properties"]["documents"]["items"]["properties"]["unit_id"]["enum"][0] = "PRIVATE_SCHEMA_ID"
        variants.append(bad_enum)
        for value in variants:
            path.write_text(json.dumps(value), encoding="utf-8")
            parser = EvidenceOutputParser(schema_path=path)
            with self.assertRaises(AuditError) as raised:
                parser.parse('[]')
            self.assertNotIn("expected_count", str(raised.exception))
            self.assertNotIn("PRIVATE_", str(raised.exception))
            self.assertNotIn("validation_root_type", parser.last_diagnostics)
        self.assertEqual(DOCUMENT_SCHEMA, variants[0])

    def test_longest_allowed_server_ids_stay_in_bounded_hint(self):
        path, _, _, _ = self.reading_fixture(6)
        schema = json.loads(path.read_text(encoding="utf-8"))
        expected = ["image-uncached-" + str(index) + "-" + "9" * 79 for index in range(6)]
        self.assertTrue(all(len(uid) == 96 for uid in expected))
        schema["properties"]["documents"]["items"]["properties"]["unit_id"]["enum"] = expected
        path.write_text(json.dumps(schema), encoding="utf-8")
        parser = EvidenceOutputParser(schema_path=path)
        with self.assertRaises(AuditError) as raised:
            parser.parse('{"PRIVATE_KEY":"PRIVATE_VALUE"}')
        self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 1000)
        self.assertTrue(all(uid in str(raised.exception) for uid in expected))
        self.assertNotIn("PRIVATE_", str(raised.exception))

    def test_existing_transport_retries_root_and_structure_then_valid_without_repairing_facts(self):
        path, expected, valid, validator = self.reading_fixture()
        replies = iter((valid["documents"], {"PRIVATE_WRONG_KEY": "PRIVATE_VALUE"}, valid))
        prompts = []
        def model(selection, content, **kwargs):
            prompts.append(content[0]["text"])
            return json.dumps(next(replies)), {"input_tokens": 12, "output_tokens": 5}
        stream = io.StringIO()
        selection = api_selection({"provider": "ppio", "api_key": "sk_synthetic_not_sent"})
        with collect_model_artifacts(self.root / "analysis"), redirect_stdout(stream), \
                patch("audit_core.qwen_model.complete_json", side_effect=model) as request, \
                patch("audit_core.qwen_model.time.sleep"), \
                patch("httpx.AsyncClient", side_effect=AssertionError("External model forbidden")):
            result = extract_qwen_json(connection=selection, root=self.root, skill=self.root,
                schema_path=path, prompt="Synthetic source reading", images=[], label="隔离资料读取", validator=validator)
        self.assertEqual(result, valid)
        self.assertEqual(request.call_count, 3)
        for hint in prompts[1:]:
            self.assertIn('"required_root_field":"documents"', hint)
            self.assertIn('"expected_count":2', hint)
            self.assertTrue(all(uid in hint for uid in expected))
        rows = [json.loads(line) for line in (self.root / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["validation_code"] for row in rows[:2]], ["json_object", "schema_validation"])
        self.assertEqual([row["validation_root_type"] for row in rows[:2]], ["array", "object"])
        self.assertEqual(rows[0]["validation_schema_kinds"], ["type"])
        self.assertEqual(set(rows[1]["validation_schema_kinds"]), {"required", "additionalProperties"})
        self.assertEqual(sum(row["usage"]["input_tokens"] for row in rows), 36)
        self.assertNotIn("PRIVATE_", stream.getvalue() + json.dumps(rows) + "".join(prompts))

    def test_codex_transport_passes_same_shape_reminder_to_next_attempt(self):
        from audit_core import codex_runner

        path, expected, valid, validator = self.reading_fixture()
        replies = iter((valid["documents"], {"PRIVATE_WRONG_KEY": "PRIVATE_VALUE"}, valid))
        prompts = []
        raw_output = self.root / "raw-output.json"
        def transport(command, **kwargs):
            prompts.append(kwargs["input"])
            raw_output.write_text(json.dumps(next(replies)), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        stream = io.StringIO()
        with redirect_stdout(stream), patch.object(codex_runner, "run_model_process", side_effect=transport) as request, \
                patch.object(codex_runner, "prepare_model_directory"), patch.object(codex_runner, "_model_subprocess_environment", return_value=None), \
                patch.object(codex_runner.time, "sleep"), \
                patch("subprocess.Popen", side_effect=AssertionError("External process forbidden")):
            result = codex_runner._run_codex_json(codex="synthetic-codex", model_root=self.root, skill_dir=self.root,
                schema=path, raw_output=raw_output, prompt="Synthetic source reading", images=[], selected_model="synthetic-model",
                model_catalog=self.root / "unused-catalog.json", label="隔离资料读取", max_attempts=3,
                attempt_timeout_seconds=60, reasoning_effort="medium", post_validate=validator)
        self.assertEqual(result, valid)
        self.assertEqual(request.call_count, 3)
        for hint in prompts[1:]:
            self.assertIn('"required_root_field":"documents"', hint)
            self.assertIn('"expected_count":2', hint)
            self.assertTrue(all(uid in hint for uid in expected))
        self.assertNotIn("PRIVATE_", stream.getvalue() + "".join(prompts))

    def test_contextual_schemas_are_valid_and_preserve_all_eight_checklists(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario):
                schema = audit_schema(scenario, materials=self.materials)
                jsonschema.Draft202012Validator.check_schema(schema)
                jsonschema.validate(unknown_checks(scenario, flags_for(scenario), materials=self.materials), schema)
                self.assertEqual(schema['properties']['checks']['minItems'],
                                 audit_schema(scenario)['properties']['checks']['minItems'])

    def test_price_model_double_binds_every_source_and_keeps_missing_facts_unknown(self):
        scenario = 'price_difference_support'
        flags = flags_for(scenario)
        materials = [*deepcopy(self.materials), {'id':'promotion_contract','state':'present','source_ids':['contract'],'reason':'本次合同'}, {'id': 'price_evidence', 'state': 'present',
                                                'source_ids': ['photo-1', 'photo-2'], 'reason': 'Synthetic images'}]
        documents = [*deepcopy(self.documents), *[{'unit_id': uid, 'facts': ['未提供可核验日期及价格']}
                      for uid in ('photo-1', 'photo-2')]]
        units = [{'unit_id': uid, 'image': self.root / (uid + '.png'), 'source_file': uid + '.png'}
                 for uid in ('photo-1', 'photo-2')]
        case = {'kind': 'pdf_policy_audit', 'archive_id': 'a001', 'scenario': scenario,
                'flags': flags, 'materials': materials, 'documents': documents, 'units': units}
        before = deepcopy(case)
        value = PolicyProvider()(case, self.root)
        jsonschema.validate(value, audit_schema(scenario, materials=materials))
        self.assertEqual([item['unit_id'] for item in value['price_photo_reviews']], ['photo-1', 'photo-2'])
        self.assertTrue(all(item['status'] == 'unknown' for item in value['price_photo_reviews']))
        self.assertEqual(value['activity_dates']['contract']['source_ids'], ['contract'])
        self.assertEqual(value['activity_dates']['settlement']['source_ids'], ['claim'])
        self.assertEqual(value['activity_dates']['contract']['periods'], [])
        self.assertTrue(all(item['quote'] is None for item in value['activity_dates']['photos']))
        result = audit_decision(scenario, flags, value, documents, materials, units=units)
        checks = {item['rule_id']: item for item in result['checks']}
        for rule in ('price_evidence', 'settlement_activity_dates', 'photo_activity_dates'):
            self.assertEqual(checks[rule]['status'], 'unknown')
        self.assertEqual(case, before)

    def test_contract_and_claim_roles_are_required_before_source_validation(self):
        value, check = self.priced_evidence()
        schema = audit_schema('personnel_incentive', materials=self.materials)
        jsonschema.validate(value, schema)
        audit_decision('personnel_incentive', self.flags, value, self.documents, self.materials)
        for side in ('left', 'right'):
            invalid = deepcopy(value)
            comparison = next(c for c in invalid['checks'] if c['rule_id'] == 'reward_unit_price')['comparisons'][0]
            if side == 'right':
                comparison['right'][0].update(unit_id='claim', quote='申报奖励单价2元')
            else:
                comparison['left'].update(unit_id='contract', quote='合同奖励单价2元')
            # The old generic shape accepts these roles; both old business
            # validation and the new generation contract reject them.
            jsonschema.validate(invalid, audit_schema('personnel_incentive'))
            with self.subTest(side=side), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)
            with self.assertRaises(AuditError):
                audit_decision('personnel_incentive', self.flags, invalid, self.documents, self.materials)

    def test_missing_contract_quantity_stays_unknown_without_inventing_a_value(self):
        value, _ = self.priced_evidence()
        check = next(c for c in value['checks'] if c['rule_id'] == 'application_quantity_price')
        check.update(reason='合同没有给出原始约定数量，不能用申报数量或预算除以单价补造。',
                     source_ids=['contract', 'claim'])
        jsonschema.validate(value, audit_schema('personnel_incentive', materials=self.materials))
        result = audit_decision('personnel_incentive', self.flags, value, self.documents, self.materials)
        actual = next(c for c in result['checks'] if c['rule_id'] == 'application_quantity_price')
        self.assertEqual(actual['status'], 'unknown')
        self.assertEqual(actual['comparisons'], [])

    def test_schema_failure_records_exact_contract_field_without_document_values(self):
        value, check = self.priced_evidence()
        check['comparisons'][0]['right'][0].update(unit_id='claim', quote='PRIVATE_DOCUMENT_VALUE')
        schema_path = self.root / 'schema.json'
        schema_path.write_text(json.dumps(audit_schema('personnel_incentive', materials=self.materials)), encoding='utf-8')
        parser = EvidenceOutputParser(schema_path=schema_path)
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(value))
        details = parser.last_diagnostics
        self.assertEqual(details['validation_stage'], 'schema')
        self.assertTrue(any(path.endswith('/comparisons/0/right/0/unit_id') for path in details['validation_paths']))
        self.assertNotIn('PRIVATE_DOCUMENT_VALUE', json.dumps(details))

    def test_reading_reply_without_roles_or_limitations_is_not_a_dead_end(self):
        path, expected, _, validator = self.reading_fixture(1)
        minimal = {"documents": [{"unit_id": expected[0], "facts": ["本图可见经销商名称：甲商贸"]}]}
        parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
        stream = io.StringIO()
        with redirect_stdout(stream):
            parsed = parser.parse(json.dumps(minimal))
        self.assertEqual(parsed["documents"][0]["roles"], [])
        self.assertEqual(parsed["documents"][0]["limitations"], [])
        self.assertEqual(parsed["documents"][0]["facts"], ["本图可见经销商名称：甲商贸"])
        self.assertIn("未声明", stream.getvalue())

    def test_inventory_reading_reply_without_limitations_keeps_its_inventory(self):
        from audit_core.audit_rag import VISIBLE_FIELD_INVENTORY_SCHEMA
        expected = ["image-" + "1" * 24]
        schema = deepcopy(DOCUMENT_SCHEMA)
        schema["properties"]["documents"].update(minItems=1, maxItems=1)
        properties = schema["properties"]["documents"]["items"]["properties"]
        properties.pop("sheet_layout", None)
        properties["unit_id"] = {**properties["unit_id"], "enum": expected}
        schema["properties"]["field_inventory"] = deepcopy(VISIBLE_FIELD_INVENTORY_SCHEMA)
        schema["properties"]["field_inventory"]["items"]["properties"]["unit_id"] = {
            "type": "string", "enum": expected}
        path = self.root / "inventory-reading-schema.json"
        path.write_text(json.dumps(schema), encoding="utf-8")
        reply = {"documents": [{"unit_id": expected[0], "facts": ["本图可见经销商名称：甲商贸"]}],
                 "field_inventory": [{"unit_id": expected[0],
                    "written_dealer_name": {"state": "present", "fact_indexes": [0]},
                    "seal_name": {"state": "unclear", "fact_indexes": []},
                    "activity_period": {"state": "absent", "fact_indexes": []}}]}
        seen = []
        with redirect_stdout(io.StringIO()):
            parsed = EvidenceOutputParser(schema_path=path, post_validate=seen.append).parse(json.dumps(reply))
        self.assertEqual(parsed["documents"][0]["limitations"], [])
        self.assertEqual(seen[0]["field_inventory"], reply["field_inventory"])

    def test_reading_reply_without_facts_still_returns_the_structure_hint(self):
        path, expected, _, validator = self.reading_fixture(1)
        minimal = {"documents": [{"unit_id": expected[0], "roles": ["photo"]}]}
        parser = EvidenceOutputParser(schema_path=path, post_validate=validator)
        with redirect_stdout(io.StringIO()), self.assertRaises(AuditError) as raised:
            parser.parse(json.dumps(minimal))
        self.assertIn("facts", str(raised.exception))

    def test_declared_array_repair_never_touches_other_schemas(self):
        path = self.root / "audit.json"
        path.write_text(json.dumps(audit_schema('personnel_incentive', materials=self.materials)), encoding="utf-8")
        value = {"documents": [{"unit_id": "contract", "facts": ["合同预算14000元"]}]}
        with self.assertRaises(AuditError):
            EvidenceOutputParser(schema_path=path).parse(json.dumps(value))

    def test_json_and_source_failures_are_distinguished_and_success_clears_metadata(self):
        path = self.root / 'simple.json'
        path.write_text(json.dumps({'type': 'object'}), encoding='utf-8')
        parser = EvidenceOutputParser(schema_path=path)
        with self.assertRaises(json.JSONDecodeError):
            parser.parse('PRIVATE_INVALID_JSON')
        self.assertEqual(parser.last_diagnostics['validation_code'], 'json_syntax')
        self.assertEqual(parser.parse('{}'), {})
        self.assertEqual(parser.last_diagnostics, {})
        def reject(value):
            raise AuditError('本类已确认的申请参照值必须引用品牌方本次促销合同，不能以结算单或其他资料代替')
        parser.post_validate = reject
        with self.assertRaises(AuditError):
            parser.parse('{}')
        self.assertEqual(parser.last_diagnostics['validation_code'], 'contract_reference')

    def test_three_retries_keep_usage_and_safe_failure_location_without_publishing_evidence(self):
        schema = self.root / 'simple.json'
        schema.write_text(json.dumps({'type': 'object', 'properties': {'ok': {'const': True}},
                                      'required': ['ok'], 'additionalProperties': False}), encoding='utf-8')
        stream = io.StringIO()
        selection = api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_not_sent'})
        with collect_model_artifacts(self.root / 'analysis'), redirect_stdout(stream), \
                patch('audit_core.qwen_model.complete_json', return_value=(
                    '{"ok":"PRIVATE_MODEL_VALUE"}', {'input_tokens': 12, 'output_tokens': 5})) as model, \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            with self.assertRaises(AuditError) as raised:
                extract_qwen_json(connection=selection, root=self.root, skill=self.root,
                    schema_path=schema, prompt='Synthetic evidence fixture', images=[], label='隔离证据校验', validator=None)
        rows = [json.loads(line) for line in (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(model.call_count, 3)
        self.assertTrue(all(row['validation_stage'] == 'schema' and row['validation_paths'] == ['ok'] for row in rows))
        self.assertEqual(sum(row['usage']['input_tokens'] for row in rows), 36)
        self.assertIn('schema_validation', str(raised.exception))
        self.assertNotIn('PRIVATE_MODEL_VALUE', str(raised.exception) + stream.getvalue() + json.dumps(rows))
        self.assertFalse((self.root / 'snapshot.json').exists())

    def test_unknown_source_exception_body_never_enters_diagnostics(self):
        path = self.root / 'simple.json'
        path.write_text(json.dumps({'type': 'object'}), encoding='utf-8')
        def reject(value):
            raise AuditError('PRIVATE_EXCEPTION_BODY')
        parser = EvidenceOutputParser(schema_path=path, post_validate=reject)
        with self.assertRaises(AuditError):
            parser.parse('{}')
        self.assertEqual(parser.last_diagnostics['validation_code'], 'evidence_validation')
        self.assertNotIn('PRIVATE_', json.dumps(parser.last_diagnostics))

    def test_metric_allowlist_rejects_untrusted_validation_metadata(self):
        with collect_model_artifacts(self.root / 'analysis'):
            record_model_attempt({'validation_stage': {}, 'validation_code': [],
                                  'validation_paths': ['https://secret.invalid', '../../secret', 'bad\nvalue'],
                                  'validation_root_type': 'PRIVATE_ROOT', 'validation_schema_kinds': ['PRIVATE_KEY', {}]})
            record_model_attempt({'validation_stage': 'schema', 'validation_code': 'schema_validation',
                                  'validation_paths': ['checks/2/comparisons/0/right/0/unit_id'],
                                  'validation_root_type': 'object', 'validation_schema_kinds': ['required', 'PRIVATE_KEY', 'type', 'type']})
        rows = [json.loads(line) for line in (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0], {})
        self.assertEqual(rows[1]['validation_paths'], ['checks/2/comparisons/0/right/0/unit_id'])
        self.assertEqual(rows[1]['validation_root_type'], 'object')
        self.assertEqual(rows[1]['validation_schema_kinds'], ['required', 'type'])


if __name__ == '__main__':
    unittest.main()
