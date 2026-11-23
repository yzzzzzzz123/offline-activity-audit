"""The delivered contract expands to every original constraint and source ID."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from audit_core.model_settings import api_selection
from audit_core.pdf_evidence import audit_schema, classification_schema, photo_review_schema, obj, TEXT
from audit_core.pdf_materials import PdfEvidenceProvider, ORCHESTRATOR_SKILL, _prompt_documents, _prompt_pos_identities
from audit_core.pdf_policy import MATERIALS
from audit_core.prompt_delivery import share_schema_definitions
from audit_core.prompts import bound_prompt
from tests.test_pos_prompt_compaction import large_verification


def expand(schema):
    definitions = schema.get('$defs', {})
    def read(node):
        if isinstance(node, dict):
            if set(node) == {'$ref'}:
                return read(definitions[node['$ref'].split('/')[-1]])
            return {k: read(v) for k, v in node.items() if k != '$defs'}
        return [read(v) for v in node] if isinstance(node, list) else node
    return read(schema)


def repeated_schema():
    source = obj({'unit_id': {'type': 'string', 'enum': ['source-a', 'source-b']},
                  'quote': TEXT, 'number': TEXT})
    return obj({'rows': {'type': 'array', 'minItems': 1, 'items': obj({
        'left': deepcopy(source), 'right': deepcopy(source),
        'alternatives': {'type': 'array', 'items': deepcopy(source)}})}})


class SchemaDeliveryTests(unittest.TestCase):
    def test_every_scenario_and_photo_contract_expands_exactly_to_original(self):
        materials = [{'id': 'promotion_contract', 'state': 'present', 'source_ids': ['contract-a', 'contract-b']},
                     {'id': 'settlement', 'state': 'present', 'source_ids': ['settlement-a']}]
        schemas = [classification_schema(s) for s in MATERIALS]
        schemas += [audit_schema(s, materials=materials) for s in MATERIALS]
        schemas += [photo_review_schema(['photo-a', 'photo-b'], ['contract-a', 'photo-a', 'photo-b'], ['contract-a'])]
        for original in schemas:
            before = deepcopy(original)
            projected = share_schema_definitions(original)
            self.assertEqual(original, before)
            self.assertEqual(expand(projected), original)
            jsonschema.Draft202012Validator.check_schema(projected)

    def test_valid_and_invalid_values_have_identical_schema_decisions(self):
        original = repeated_schema(); projected = share_schema_definitions(original)
        self.assertIn('$defs', projected)
        source = {'unit_id': 'source-a', 'quote': '原文1.005', 'number': '1.005'}
        valid = {'rows': [{'left': source, 'right': source, 'alternatives': [source]}]}
        invalid = [None, {}, {'rows': []}, {**valid, 'extra': True}]
        for field in ('unit_id', 'quote', 'number'):
            value = deepcopy(valid);value['rows'][0]['left'].pop(field);invalid.append(value)
        for value in ('other-source', None, 0):
            broken = deepcopy(valid);broken['rows'][0]['right']['unit_id'] = value;invalid.append(broken)
        broken = deepcopy(valid);broken['rows'][0]['alternatives'][0]['quote'] = '';invalid.append(broken)
        for value in [valid] + invalid:
            decisions = [jsonschema.Draft202012Validator(s).is_valid(value) for s in (original, projected)]
            self.assertEqual(decisions[0], decisions[1])
        self.assertTrue(jsonschema.Draft202012Validator(projected).is_valid(valid))
        self.assertTrue(all(not jsonschema.Draft202012Validator(projected).is_valid(v) for v in invalid))

    def test_existing_reference_scopes_are_left_unchanged(self):
        for key, value in (('$id', 'urn:original'), ('$ref', '#/definitions/a'),
                           ('$defs', {'a': TEXT}), ('$dynamicAnchor', 'scope')):
            original = {**repeated_schema(), key: value}
            self.assertIs(share_schema_definitions(original), original)
        self.assertIs(share_schema_definitions(TEXT), TEXT)

    def test_literal_business_values_are_never_treated_as_schema_references(self):
        original = repeated_schema()
        literal = {'$ref': '#/$defs/not-a-real-definition', 'properties': {'type': '客户原值'}}
        original['properties']['literal'] = {'const': literal}
        original['required'].append('literal')
        projected = share_schema_definitions(original)
        self.assertEqual(projected['properties']['literal'], {'const': literal})
        source = {'unit_id': 'source-a', 'quote': '原文1', 'number': '1'}
        value = {'rows': [{'left': source, 'right': source, 'alternatives': []}], 'literal': literal}
        self.assertTrue(jsonschema.Draft202012Validator(projected).is_valid(value))

    def test_api_receives_full_equivalent_schema_and_original_validator(self):
        original = repeated_schema()
        source = {'unit_id': 'source-a', 'quote': '原文1', 'number': '1'}
        value = {'rows': [{'left': source, 'right': source, 'alternatives': []}]}
        seen = []
        def complete(selection, content, *, system, **kwargs):
            delivered = json.loads(system.split('本批 JSON Schema：\n', 1)[1])
            from audit_core.model_authority import transport_schema
            self.assertEqual(expand(delivered), transport_schema(original))
            self.assertIn('全部审核要求原文', system)
            seen.append(content)
            return json.dumps(value, ensure_ascii=False), {'input_tokens': 0, 'output_tokens': 0}
        def validate(value):
            jsonschema.validate(value, original)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);skill = root/'skill';skill.mkdir()
            (skill/'SKILL.md').write_text('全部审核要求原文', encoding='utf-8')
            selection = api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_only'})
            with patch('audit_core.qwen_model.complete_json', side_effect=complete):
                result = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection, stage_summary_reviewer=isolated_stage_summary)._call(
                    root/'call', skill, original, bound_prompt('{facts}', facts='完整原始凭证内容'),
                    images=[], label='等价Schema隔离校验', validator=validate)
        self.assertEqual(result, value);self.assertEqual(len(seen), 1)
        self.assertIn('完整原始凭证内容', json.dumps(seen, ensure_ascii=False))

    def test_transcription_schema_and_serialization_keep_existing_cache_contract(self):
        original = repeated_schema()
        selection = api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_only'})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def extract(**kwargs):
                self.assertEqual(kwargs['schema_path'].read_text(encoding='utf-8'), json.dumps(original, ensure_ascii=False))
                return {'unchanged': True}
            with patch('audit_core.qwen_model.extract_qwen_json', side_effect=extract):
                provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection, stage_summary_reviewer=isolated_stage_summary)
                provider._call(root/'reading', ORCHESTRATOR_SKILL, original, bound_prompt('原读取请求'),
                               images=[], label='原转录契约', validator=None, transcription=True)
                provider._call(root/'explicit-reading', root/'other-skill', original, bound_prompt('原读取请求'),
                               images=[], label='原转录契约', validator=None, transcription=True)


class ModeratePosDeliveryTests(unittest.TestCase):
    def test_medium_native_table_restores_all_original_rows_and_metadata(self):
        documents = [{'unit_id': 'native-pos', 'roles': ['spreadsheet'], 'limitations': ['原始限制保留'],
            'facts': [f'原工作表!R{i}: C1=参半原始商品 | C2=06970356168997.0 | C3=1.005 | C4=原始批次备注'
                      for i in range(1, 801)]}]
        before = deepcopy(documents)
        plain_bytes = len(json.dumps(documents, ensure_ascii=False).encode())
        self.assertGreater(plain_bytes, 64_000);self.assertLess(plain_bytes, 600_000)
        text = _prompt_documents(documents);packed = json.loads(text)
        self.assertIn('fragments', packed)
        fragments = packed['fragments'];restored = deepcopy(packed['documents'])
        for document in restored:
            document.update(packed['document_metadata'][document.pop('metadata')])
            document['facts'] = [f if isinstance(f, str) else fragments[f[0]] + str(f[1]) + ': ' +
                                 ' | '.join(fragments[i] for i in f[2]) for f in document['facts']]
        self.assertEqual(restored, before);self.assertEqual(documents, before)
        self.assertLess(len(text.encode()), plain_bytes // 2)

    def test_medium_identity_results_keep_every_source_and_complete_outcome(self):
        original = large_verification();original['rows'] = original['rows'][:120]
        original['rows'][-1].update(status='unknown', reason_code='catalog_product_missing')
        size = len(json.dumps(original, ensure_ascii=False).encode())
        self.assertGreater(size, 64_000);self.assertLess(size, 600_000)
        before = deepcopy(original);packed = json.loads(_prompt_pos_identities(original))
        actual = [(g['result'], uid, indexes) for g in packed['groups']
                  for uid, positions in g['sources'].items() for indexes in positions]
        expected = [({k: v for k, v in r.items() if k not in {'unit_id', 'source'}},
                     r['unit_id'], r['source']['fact_indexes']) for r in original['rows']]
        self.assertCountEqual(actual, expected)
        self.assertEqual(packed['row_count'], 120);self.assertEqual(original, before)
        self.assertEqual(packed['limitations'], original['limitations'])


if __name__ == '__main__':
    unittest.main()
