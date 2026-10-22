const assert = require('node:assert/strict');
const vm = require('node:vm');
const {loadConsoleSource} = require('./console_source.cjs');
const {html, modules, source} = loadConsoleSource();
const tests = [];
function test(name, fn) {tests.push({name, fn});}

test('HTML references the actual results and controller once in order', () => {
  assert.deepEqual(modules.map(item => item.pathname), ['/static/console-results.js', '/static/console-app.js']);
  assert(!html.includes('const { createApp'));
  assert(!html.includes('function summarizeLastRun'));
  assert(!html.includes('function describeLastRun'));
  assert(html.includes('/static/qr-login.js'));
  assert(html.includes('/static/silk-background.js'));
});
test('Loader composes actual source and both modules parse independently', () => {
  assert.equal(source, modules.map(item => item.source).join('\n;\n'));
  for (const item of modules) new vm.Script(item.source, {filename:item.pathname});
  assert.match(modules[1].source, /const \{describeLastRun, summarizeLastRun\} = window\.SparkConsoleResults/);
  assert(!modules[1].source.includes('function summarizeLastRun'));
});
test('Missing, reordered, duplicated and asynchronous module references fail', () => {
  const tags = [...html.matchAll(/<script data-console-module[^>]*><\/script>/g)].map(match => match[0]);
  assert.equal(tags.length, 2);
  assert.throws(() => loadConsoleSource(html.replace(tags[0], '')));
  assert.throws(() => loadConsoleSource(html.replace(tags[0], '__FIRST_MODULE__').replace(tags[1], tags[0]).replace('__FIRST_MODULE__', tags[1])));
  assert.throws(() => loadConsoleSource(html.replace(tags[1], tags[0])));
  assert.throws(() => loadConsoleSource(html.replace(tags[0], tags[0].replace('<script ', '<script async '))));
});
test('Loader rejects external or unexpected module paths', () => {
  assert.throws(() => loadConsoleSource(html.replace('/static/console-app.js', 'https://fixture.invalid/console-app.js')));
  assert.throws(() => loadConsoleSource(html.replace('/static/console-app.js', '/static/unexpected.js')));
});
test('Result formatters run without Vue, DOM, HTTP or timers and retain unknown protection', () => {
  const window = {};
  vm.runInNewContext(modules[0].source, {window}, {filename:modules[0].pathname});
  const formatter = window.SparkConsoleResults;
  assert(Object.isFrozen(formatter));
  assert.deepEqual(Object.keys(formatter).sort(), ['describeLastRun', 'summarizeLastRun']);
  const run = {dry_run:false, at:'2026-10-23T08:00:00+08:00', ok:[], failed:[], deferred:[],
    skipped:[{name:'synthetic friend', code:'delivery_unconfirmed'}]};
  const before = JSON.stringify(run);
  const summary = formatter.summarizeLastRun(run);
  assert.equal(summary.success, false);
  assert.match(summary.summary, /待核对 1.*勿直接重发/);
  assert.match(formatter.describeLastRun(run)[0].detail, /不会自动重发/);
  assert.equal(JSON.stringify(run), before);
});
test('Independent result formatters preserve successful dry-run and malformed-result meanings', () => {
  const window = {};
  vm.runInNewContext(modules[0].source, {window});
  const {summarizeLastRun, describeLastRun} = window.SparkConsoleResults;
  const result = summarizeLastRun({dry_run:true, ok:['synthetic friend'], failed:[], deferred:[], skipped:[]});
  assert.equal(result.success, true);
  assert.match(result.summary, /模拟演练（未发送消息）.*演练通过 1/);
  assert.equal(summarizeLastRun({dry_run:false, ok:'malformed'}).success, false);
  assert.equal(describeLastRun(null).length, 0);
});
test('Unknown and prototype keys only produce the fixed explanatory fallback', () => {
  const window = {};
  vm.runInNewContext(modules[0].source, {window});
  for (const key of ['constructor', '__proto__', 'toString', 'synthetic unknown reason']) {
    const run = {dry_run:true, failed:[{name:'synthetic friend', code:key}],
      delivery_checks:[{name:'synthetic check', reason:key}]};
    const details = window.SparkConsoleResults.describeLastRun(run);
    assert.equal(details[0].detail, '未完成，具体情况请查看日志');
    assert.equal(details[1].detail, '演练诊断：确认依据尚未验证');
    assert(details.every(item => typeof item.detail === 'string'));
  }
});

let failed = 0;
for (const {name, fn} of tests) {
  try {fn(); console.log('PASS ' + name);}
  catch (error) {failed++; console.error('FAIL ' + name + '\n' + error.stack);}
}
console.log(JSON.stringify({passed:tests.length-failed, failed, total:tests.length}));
process.exitCode = failed ? 1 : 0;
