"""Visible execution names remain Chinese across old/new and future event types."""
import ast
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html'
SCRIPTS = ROOT / 'skills/orchestrate-offline-audit/scripts'
NODE = shutil.which('node')


def event_types():
    names = {'run.created', 'run.resumed', 'run.revalidation_started', 'intake.completed'}
    for path in SCRIPTS.rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            name = call.func.id if isinstance(call.func, ast.Name) else call.func.attr if isinstance(call.func, ast.Attribute) else ''
            if name not in {'observe', 'record_step', 'emit', 'append_event'}:
                continue
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
    return sorted(names)


@unittest.skipUnless(NODE, 'Node.js is required for display projection checks')
class ExecutionChineseFrontendTests(unittest.TestCase):
    def render(self, expressions):
        text = ASSET.read_text(encoding='utf-8')
        functions = text[text.index('      const executionEventLabels = '):text.index('      const runTone = ')]
        script = "const scenarioLabels={pos_target_incentive:'POS达标激励'};\n" + functions
        script += '\nconsole.log(JSON.stringify(' + expressions + '));'
        result = subprocess.run([NODE, '-'], input=script, text=True, encoding='utf-8',
                                capture_output=True, check=True, timeout=20)
        return json.loads(result.stdout)

    def test_every_current_producer_event_has_an_explicit_chinese_name(self):
        names = event_types()
        result = self.render(json.dumps(names) + '.map(type=>({type,label:executionEventName({type}),known:Object.hasOwn(executionEventLabels,type)}))')
        self.assertGreater(len(result), 80)
        for row in result:
            with self.subTest(event=row['type']):
                self.assertTrue(row['known'], 'Add a display name when adding a producer event')
                self.assertRegex(row['label'], '[\u3400-\u9fff]')
                self.assertNotIn(row['type'], row['label'])

    def test_legacy_alternate_field_workflow_prefix_and_future_events(self):
        result = self.render("""[
          executionEventName({event:'run.created'}),
          executionEventName({type:'workflow.scenario.started'}),
          executionEventName({type:'qince.future_action',label:'qince.future_action'}),
          executionEventName({type:'future.unknown',label:'unknown'}),
          executionEventName({type:'constructor'}),
          executionEventName({}),
          executionEventName({type:'ai.request',label:'读取第2批现场照片'},true)
        ]""")
        self.assertEqual(result[:2], ['已建立本次核销任务', '开始本类业务核对'])
        self.assertEqual(result[2:6], ['勤策业务资料记录', '核销处理记录', '核销处理记录', '核销处理记录'])
        self.assertEqual(result[-1], '读取第2批现场照片')

    def test_states_errors_and_unknown_values_cannot_leak_machine_codes(self):
        result = self.render("""['failed','deferred','timeout','future_state','constructor',null]
          .map(value=>[executionStateName(value),executionErrorName(value)])""")
        for row in result:
            for label in row:
                self.assertRegex(label, '[\u3400-\u9fff]')
                self.assertNotIn('future_state', label)
                self.assertNotIn('constructor', label)
        self.assertEqual(result[0][0], '执行失败')
        self.assertEqual(result[2][1], '模型请求超时')

    def test_attachment_titles_translate_roles_without_changing_source_names(self):
        result = self.render("""[
          executionResourceName({path:'analysis/model-observations/call-progress-0123.json',label:'已保存分析记录 · call-progress-0123'}),
          executionResourceName({path:'analysis/results/pos_target_incentive.json',label:'pos_target_incentive 确定性核销结果'}),
          executionResourceName({path:'analysis/future.json',label:'future.internal_record'}),
          executionResourceName({path:'analysis/future.json',label:'已保存分析记录 · future_internal'})
        ]""")
        self.assertEqual(result, ['模型调用进度与用量','POS达标激励 确定性核销结果','已保存的分析依据','已保存的分析依据'])


if __name__ == '__main__':
    unittest.main()
