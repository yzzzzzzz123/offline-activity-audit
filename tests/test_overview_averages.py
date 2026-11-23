"""Per-completed-run averages preserve unknown, partial and real zero values."""
import json
import subprocess
import unittest

from audit_core.workbench_runtime import ROOT_HTML


class OverviewAveragesTests(unittest.TestCase):
    def average_script(self):
        html = ROOT_HTML.read_text(encoding='utf-8')
        model_id = next(line.strip() for line in html.splitlines() if 'const modelDisplayId = ' in line)
        helpers = 'const usageModelName = ' + html.split('      const usageModelName = ', 1)[1].split('      const modelLabel = ', 1)[0]
        function = 'const completedRunAverages = ' + html.split('      const completedRunAverages = ', 1)[1].split('      const renderPrimaryMetrics = ', 1)[0]
        return "const terminalRun = run => ['completed','failed'].includes(run?.status);\n" + model_id + '\n' + helpers + '\n' + function

    def test_completed_samples_unknowns_zeros_and_per_run_weighting(self):
        function = self.average_script()
        runs = [
            {'status': 'completed', 'token_usage': {'status': 'recorded', 'total_tokens': 1200}, 'audit_timing': {'active_seconds': 120}},
            {'status': 'completed', 'token_usage': {'status': 'recorded', 'total_tokens': 600}, 'audit_timing': {'active_seconds': 240}},
            {'status': 'completed', 'token_usage': {'status': 'recorded', 'total_tokens': 0}, 'audit_timing': {'active_seconds': 0}},
            {'status': 'completed', 'token_usage': {'status': 'partial', 'total_tokens': 9999}, 'audit_timing': {'active_seconds': None}},
            {'status': 'completed'},
            {'status': 'failed', 'token_usage': {'status': 'recorded', 'total_tokens': 999999}, 'audit_timing': {'active_seconds': 99999}},
            {'status': 'running', 'token_usage': {'status': 'recorded', 'total_tokens': 888888}, 'audit_timing': {'active_seconds': 88888}},
        ]
        code = function + '\nconst runs = ' + json.dumps(runs) + ';console.log(JSON.stringify([completedRunAverages(runs),completedRunAverages(runs.slice(0,2)),completedRunAverages([]),completedRunAverages([{status:"completed",token_usage:{status:"recorded",total_tokens:-1},audit_timing:{active_seconds:NaN}},{status:"completed",audit_timing:{active_seconds:Infinity}}])]))'
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, encoding='utf-8', check=True)
        all_runs, selected, empty, invalid = json.loads(result.stdout)
        empty_models = [{'model': model, 'value': None, 'count': 0, 'available': 0} for model in ('DeepSeek', 'Qwen')]
        self.assertEqual(all_runs.pop('models'), empty_models)
        self.assertEqual(selected.pop('models'), empty_models)
        self.assertEqual(all_runs, {'completed': 5, 'tokens': {'value': 600, 'count': 1, 'available': 3}, 'duration': {'value': 120, 'count': 1, 'available': 3}})
        self.assertEqual(selected, {'completed': 2, 'tokens': {'value': None, 'count': 0, 'available': 2}, 'duration': {'value': None, 'count': 0, 'available': 2}})
        for output in [empty, invalid]:
            self.assertEqual(output['tokens'], {'value': None, 'count': 0, 'available': 0})
            self.assertEqual(output['duration'], {'value': None, 'count': 0, 'available': 0})

    def test_each_extreme_is_excluded_once_even_when_values_tie(self):
        function = self.average_script()
        code = function + '''
const run = value => ({status:'completed', token_usage:{status:'recorded',total_tokens:value},audit_timing:{active_seconds:value}});
console.log(JSON.stringify([[0,100,100,100,10000],[0,0,0],[1,1,2,2],[7]].map(values=>completedRunAverages(values.map(run)))));
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, encoding='utf-8', check=True)
        for output, mean, retained in zip(json.loads(result.stdout), [100, 0, 1.5, None], [3, 1, 2, 0]):
            self.assertEqual(output['tokens']['value'], mean)
            self.assertEqual(output['tokens']['count'], retained)
            self.assertEqual(output['duration']['value'], mean)

    def test_each_model_uses_its_own_complete_run_samples(self):
        code = self.average_script() + '''
const runs = [1,2,3,4].map(n=>({status:'completed',token_usage:{status:n===4?'partial':'recorded',total_tokens:n*110,
  by_model:[{model:'deepseek-v4-1-flash',status:'recorded',total_tokens:n*100},
    {model:'qwen3.8-max',status:n===4?'partial':'recorded',total_tokens:n*10}]}}));
console.log(JSON.stringify(completedRunAverages(runs)));
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True, encoding='utf-8', check=True)
        models = {row['model']: row for row in json.loads(result.stdout)['models']}
        self.assertEqual(models['DeepSeek'], {'model': 'DeepSeek', 'value': 250, 'count': 2, 'available': 4})
        self.assertEqual(models['Qwen'], {'model': 'Qwen', 'value': 20, 'count': 1, 'available': 3})


if __name__ == '__main__':
    unittest.main()
