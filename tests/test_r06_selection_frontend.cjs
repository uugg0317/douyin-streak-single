const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m => !/\bsrc\s*=/.test(m[1]) && m[2].includes('const { createApp'))[2];
const rows = selected => ['A', 'B', 'C'].map(id => ({entry_id:id,display_name:`合成好友${id}`,selected:selected.includes(id)}));
const ids = value => Array.from(value, row => row.entry_id);
function mount() {
  let component, ledger = {entries:rows(['A']),ledger_version:'v-A'}, saveFailure = false, saveResult = null, runConflict = false, ledgerFailure = false;
  const posted = [], messages = [], hooks = [], timers = new Map(); let timer = 0;
  const api = {interceptors:{request:{use(){}},response:{use(){}}}, async get(url) {
    if (url === '/api/ledger') {if (ledgerFailure) throw Error('synthetic ledger failure');return {data:ledger};}
    return {data:{}};
  }, async post(url,data) {
    posted.push({url,data});
    if (url === '/api/run' && runConflict) throw {response:{status:409,data:{detail:'synthetic stale version'}}};
    if (url === '/api/ledger/selection') {
      if (saveFailure) throw Error('synthetic save failure');
      if (saveResult) return saveResult();
      ledger = {entries:rows(data.selected_ids),ledger_version:'v-B'};
      return {data:{ok:true,...ledger}};
    }
    return {data:{ok:true}};
  }};
  const Vue = {ref:value=>({value}),reactive:value=>value,computed:fn=>({get value(){return fn();}}),watch(){},nextTick:()=>Promise.resolve(),onMounted(){},onUnmounted:fn=>hooks.push(fn),onBeforeUnmount:fn=>hooks.push(fn),createApp(options){component=options.setup();return {use(){return this;},mount(){}};}};
  const window = {innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){}};
  const ElMessage = Object.fromEntries(['success','error','warning','info'].map(kind=>[kind,text=>messages.push({kind,text})]));
  vm.runInNewContext(script,{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{ElMessage},axios:{create:()=>api},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},confirm:()=>true,setTimeout(){return 1;},clearTimeout(){},setInterval(fn){const id=++timer;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);}}, {filename:'r06-actual-console.js'});
  return {c:component,posted,messages,setLedger(value){ledger=value;},failSave(){saveFailure=true;},failLedger(){ledgerFailure=true;},conflictRun(){runConflict=true;},setSave(fn){saveResult=fn;},draft(selected){component.contacts.value.forEach(row=>row.selected=selected.includes(row.entry_id));},cleanup(){hooks.forEach(fn=>fn());}};
}
const tests = [];
function test(name,fn){tests.push({name,fn});}
test('A 已生效而草稿 B 未保存时，实际发送和演练均被拦截且不隐式保存',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);
  await h.c.runStreak(false);await h.c.runStreak(true);
  assert.equal(h.posted.length,0);assert.equal(h.c.selectionDirty.value,true);
  assert.deepEqual(ids(h.c.savedSelection.value),['A']);assert.deepEqual(ids(h.c.draftSelection.value),['B']);
  assert.match(h.c.selectionStatusMessage.value,/先保存.*放弃/);h.cleanup();
});
test('保存失败保留已生效 A 和版本，草稿 B 仍可修正',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);h.failSave();await h.c.saveLedgerSelection();
  assert.deepEqual(ids(h.c.savedSelection.value),['A']);assert.deepEqual(ids(h.c.draftSelection.value),['B']);
  assert.equal(h.c.ledgerVersion.value,'v-A');assert.equal(h.c.selectionDirty.value,true);
  assert.match(h.c.selectionStatusMessage.value,/保存失败.*仍.*1/);await h.c.runStreak(false);
  assert.equal(h.posted.filter(p=>p.url==='/api/run').length,0);h.cleanup();
});
test('只有保存成功 B 才推进已生效快照和版本，网页请求明确提交版本',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);await h.c.saveLedgerSelection();
  assert.deepEqual(ids(h.c.savedSelection.value),['B']);assert.equal(h.c.ledgerVersion.value,'v-B');assert.equal(h.c.selectionDirty.value,false);
  await h.c.runStreak(false);const sent=h.posted.find(p=>p.url==='/api/run');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.data)),{dry_run:false,expected_ledger_version:'v-B'});assert(!Object.hasOwn(sent.data,'force'));h.cleanup();
});
test('刷新已生效 C 时保留未保存的草稿 B，放弃操作恢复最新已生效名单',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);h.setLedger({entries:rows(['C']),ledger_version:'v-C'});await h.c.loadAll();
  assert.deepEqual(ids(h.c.draftSelection.value),['B']);assert.deepEqual(ids(h.c.savedSelection.value),['C']);assert.equal(h.c.selectionDirty.value,true);
  h.c.discardSelectionChanges();assert.deepEqual(ids(h.c.draftSelection.value),['C']);assert.equal(h.c.selectionDirty.value,false);h.cleanup();
});
test('保存期间继续修改草稿时，响应不得把新草稿当成已保存',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);let finish;
  h.setSave(()=>new Promise(resolve=>{finish=resolve;}));const saving=h.c.saveLedgerSelection();h.draft(['C']);
  finish({data:{ok:true,entries:rows(['B']),ledger_version:'v-B'}});await saving;
  assert.deepEqual(ids(h.c.savedSelection.value),['B']);assert.deepEqual(ids(h.c.draftSelection.value),['C']);assert.equal(h.c.selectionDirty.value,true);
  await h.c.runStreak(false);assert.equal(h.posted.filter(p=>p.url==='/api/run').length,0);h.cleanup();
});
test('未取得版本时阻止发送；退出清除已生效名单、草稿和版本',async()=>{
  const h=mount();h.setLedger({entries:rows(['A'])});await h.c.loadAll();await h.c.runStreak(false);
  assert.equal(h.posted.length,0);h.setLedger({entries:rows(['A']),ledger_version:'v-A'});await h.c.loadAll();h.draft(['B']);await h.c.resetConsole();
  assert.equal(h.c.savedSelection.value.length,0);assert.equal(h.c.draftSelection.value.length,0);assert.equal(h.c.ledgerVersion.value,null);assert.equal(h.c.selectionStatusMessage.value,'');h.cleanup();
});
test('概览明确已生效人数和名单，草稿提示与保存反馈有可读状态语义',()=>{
  assert.match(html,/已生效好友/);assert.match(html,/savedFriendCount/);assert.match(html,/savedSelectionLabel/);
  assert.match(html,/role="status"[^>]*aria-live="polite"[^>]*>\s*\{\{ selectionStatusMessage \}\}/);
  assert.match(html,/@click="discardSelectionChanges"/);
});
test('409 版本冲突不重试发送，并要求显式刷新核对',async()=>{
  const h=mount();await h.c.loadAll();h.conflictRun();await h.c.runStreak(false);assert.equal(h.c.ledgerReady.value,false);
  assert.match(h.c.selectionStatusMessage.value,/本次未启动.*刷新.*核对/);await h.c.runStreak(false);
  assert.equal(h.posted.filter(p=>p.url==='/api/run').length,1);assert.deepEqual(ids(h.c.savedSelection.value),['A']);h.cleanup();
});
test('服务器未确认保存成功不会推进已生效快照',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);h.setSave(()=>({data:{ok:false}}));await h.c.saveLedgerSelection();
  assert.deepEqual(ids(h.c.savedSelection.value),['A']);assert.equal(h.c.ledgerVersion.value,'v-A');assert.equal(h.c.selectionDirty.value,true);assert.match(h.c.selectionStatusMessage.value,/保存失败/);h.cleanup();
});
test('刷新失败保留旧已生效名单和草稿，不自动保存',async()=>{
  const h=mount();await h.c.loadAll();h.draft(['B']);h.failLedger();assert.equal(await h.c.refreshLedgerSelection(),false);
  assert.deepEqual(ids(h.c.savedSelection.value),['A']);assert.deepEqual(ids(h.c.draftSelection.value),['B']);assert.match(h.c.selectionStatusMessage.value,/刷新失败/);assert.equal(h.posted.length,0);h.cleanup();
});
(async()=>{let failed=0;for(const {name,fn} of tests){try{await fn();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length}));process.exitCode=failed?1:0;})();
