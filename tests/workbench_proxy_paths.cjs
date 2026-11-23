// Exercise the actual client URL resolver without opening a browser or calling an API.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../skills/orchestrate-offline-audit/assets/offline-activity-audit.html'), 'utf8');
const source = html.slice(html.indexOf('const applicationBaseUrl'), html.indexOf('const apiJson ='));
assert(source.includes('const applicationApiUrl'));
for (const [href, request, expected] of [
  ['http://127.0.0.1:8080/', '/api/model-settings', 'http://127.0.0.1:8080/api/model-settings'],
  ['https://offline-verify.example:8828/ai/', '/api/model-settings', 'https://offline-verify.example:8828/ai/api/model-settings'],
  ['https://offline-verify.example:8828/ai/?run=case-1', '/api/runs', 'https://offline-verify.example:8828/ai/api/runs'],
  ['https://offline-verify.example:8828/ai/offline-activity-audit.html', '/api/intake/jobs?completed=0', 'https://offline-verify.example:8828/ai/api/intake/jobs?completed=0'],
  ['https://offline-verify.example:8828/nested/ai/', '/api/runs/case-1/log', 'https://offline-verify.example:8828/nested/ai/api/runs/case-1/log'],
]) {
  const context = vm.createContext({URL,window:{location:{href}}});
  const actual = vm.runInContext(source + '\napplicationApiUrl(' + JSON.stringify(request) + ');', context);
  assert.equal(actual, expected);
}
assert.equal((html.match(/fetch\(applicationApiUrl\(/g) || []).length, 3);
assert(!html.includes("fetch('/api/"));
console.log('PASS: root, proxy prefix, record queries, canonical HTML, nested paths and all live fetch call sites');
