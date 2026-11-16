const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
for (const match of source.matchAll(/querySelector\("#([\w-]+)"\)/g)) {
  assert.ok(html.includes(`id="${match[1]}"`), `Missing HTML element: ${match[1]}`);
}
const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    checked: true, disabled: false, value: '', dataset: {}, textContent: '', innerHTML: '',
    events: {}, classList: { toggle() {}, contains() { return false; } },
    addEventListener(name, callback) { this.events[name] = callback; },
  });
  return elements.get(selector);
}
const form = element('#nvr-form');
form.elements = { lookback_minutes: {value:'720'}, id:{value:''}, nvr_time_offset_hours:{value:'-3'} };
form.reset = () => { element('#nvr-use-global').checked = true; };
const context = vm.createContext({
  document: { querySelector: element, querySelectorAll: () => [] },
  console, setTimeout: () => {}, clearTimeout: () => {}, Intl, Date,
});
vm.runInContext(source.replace(/checkLogin\(\);\s*$/, ''), context);
vm.runInContext('updateLookbackInput()',context);
assert.equal(form.elements.lookback_minutes.disabled,true);
element('#nvr-use-global').checked=false;
element('#nvr-use-global').events.change();
assert.equal(form.elements.lookback_minutes.disabled,false);
element('#open-nvr-modal').events.click();
assert.equal(element('#nvr-use-global').checked,true);
assert.equal(form.elements.lookback_minutes.disabled,true);
assert.equal(vm.runInContext('verificationFailed({monitor:{nvr_errors:1}})',context),true);
assert.equal(vm.runInContext('verificationFailed({monitor:{offline:1}})',context),false);
assert.equal(vm.runInContext('cameraHttpUrl("192.0.2.100")',context),'http://192.0.2.100/');
assert.equal(vm.runInContext('cameraHttpUrl("192.0.2.999")',context),null);
assert.equal(vm.runInContext('cameraHttpUrl("example.com/path")',context),null);

(async () => {
  const originalApi = vm.runInContext('api', context);
  context.payload={items:[{id:1,name:'Test',base_url:'http://192.0.2.1',offline_channels:1,
    no_recording_channels:1,pending_offline:0,pending_no_recording:0,lookback_minutes:0}]};
  vm.runInContext('api = async () => payload;', context);
  await vm.runInContext('loadNvrs()',context);
  assert.ok(!element('#nvr-list').innerHTML.includes('badge offline'));
  assert.ok(!element('#nvr-list').innerHTML.includes('badge no_recording'));
  assert.ok(element('#nvr-list').innerHTML.includes('data-lookback-minutes="0"'));
  context.payload.items[0].pending_offline=1;
  await vm.runInContext('loadNvrs()',context);
  assert.ok(!element('#nvr-list').innerHTML.includes('badge offline'));
  context.payload={summary:{alerts:0,offline_channels:1}};
  await vm.runInContext('loadDashboard()',context);
  assert.equal(element('#total-alerts').textContent,0);
  assert.equal(element('#total-offline').textContent,1);
  context.fetch = async () => ({status:404, text:async () => 'arquivo nao encontrado'});
  await assert.rejects(originalApi('/api/channels/tree'), /\/api\/channels\/tree.*HTTP 404.*backend\/app.py/);
  console.log('Frontend: campos, busca global, alertas pendentes e falhas validados.');
})().catch(error => { console.error(error); process.exitCode=1; });
