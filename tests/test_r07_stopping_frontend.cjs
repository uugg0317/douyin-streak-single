const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m => !/\bsrc\s*=/.test(m[1]) && m[2].includes('const { createApp'))[2];
function mount() {
  let component, response = {}, failed = false;
  const posted = [], messages = [], hooks = [], timers = new Map(); let timer = 0;
  const api = {interceptors:{request:{use(){}},response:{use(){}}}, async get(url) {
    if (url === '/api/status') {if (failed) throw Error('synthetic offline');return {data:response};}
    if (url === '/api/ledger') return {data:{ledger_version:'synthetic-v1',entries:[{entry_id:'synthetic-a',display_name:'合成好友',selected:true}]}};
    return {data:{}};
  }, async post(url,data) {posted.push({url,data});return {data:{ok:true}};}};
  const Vue = {ref:value=>({value}),reactive:value=>value,computed:fn=>({get value(){return fn();}}),watch(){},nextTick:()=>Promise.resolve(),onMounted(){},onUnmounted:fn=>hooks.push(fn),onBeforeUnmount:fn=>hooks.push(fn),createApp(options){component=options.setup();return {use(){return this;},mount(){}};}};
  const window = {innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){}};
  const ElMessage = Object.fromEntries(['success','error','warning','info'].map(kind=>[kind,text=>messages.push({kind,text})]));
  vm.runInNewContext(script,{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{ElMessage},axios:{create:()=>api},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},setTimeout(){return 1;},clearTimeout(){},setInterval(fn){const id=++timer;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);}}, {filename:'r07-actual-console.js'});
  return {c:component,posted,messages,setStatus(value){response=value;},fail(){failed=true;},cleanup(){hooks.forEach(fn=>fn());}};
}
const tests=[];function test(name,fn){tests.push({name,fn});}
test('超时等待停止状态显示待核对，不误称任务空闲或后台暂停',async()=>{
  const h=mount();h.setStatus({running:true,stopping:true,background_tasks_enabled:false});await h.c.loadAll();
  assert.equal(h.c.status.running,true);assert.equal(h.c.status.stopping,true);assert.equal(h.c.taskDisplay.value.label,'正在停止／结果待核对');
  assert.match(h.c.taskDisplay.value.detail,/后台任务尚未结束.*勿重复启动/);assert.match(h.c.taskStatusAnnouncement.value,/正在停止.*待核对.*勿重复启动/);assert(!h.c.taskStatusAnnouncement.value.includes('当前没有发送任务'));h.cleanup();
});
test('停止中真实发送、演练和继续未完成均不能启动，也无自动退出或重启请求',async()=>{
  const h=mount();h.c.authed.value=true;h.setStatus({running:true,stopping:true,retry_status:'manual',pending_retry_count:2});await h.c.loadAll();
  assert.equal(h.c.canContinuePending.value,false);await h.c.runStreak(false);await h.c.runStreak(true);await h.c.continuePending();assert.equal(h.posted.length,0);h.cleanup();
});
test('矛盾状态 running=false 但 stopping=true 时仍保守阻止重复启动',async()=>{
  const h=mount();h.c.authed.value=true;h.setStatus({running:false,stopping:true,retry_status:'manual',pending_retry_count:2});await h.c.loadAll();
  assert.equal(h.c.status.running,true);assert.equal(h.c.canContinuePending.value,false);await h.c.runStreak(false);assert.equal(h.posted.length,0);h.cleanup();
});
test('停止中轮询断线不会清空保护或放行重复发送',async()=>{
  const h=mount();h.setStatus({running:true,stopping:true});await h.c.loadAll();h.fail();await h.c.loadStatus();
  assert.equal(h.c.status.stopping,true);assert.equal(h.c.status.running,true);assert.match(h.c.taskDisplay.value.label,/正在停止/);await h.c.runStreak(false);assert.equal(h.posted.length,0);h.cleanup();
});
test('后台明确结束后恢复既有状态，页面不自动续发',async()=>{
  const h=mount();h.setStatus({running:true,stopping:true});await h.c.loadAll();h.setStatus({running:false,stopping:false,background_tasks_enabled:false});await h.c.loadStatus();
  assert.equal(h.c.status.stopping,false);assert.equal(h.c.status.running,false);assert.equal(h.c.taskDisplay.value.label,'后台已暂停');assert.equal(h.posted.length,0);
  await h.c.runStreak(false);assert.equal(h.posted.length,1);assert.equal(h.posted[0].url,'/api/run');h.cleanup();
});
test('普通运行仍显示原有正在执行，缺省字段保持旧接口兼容',async()=>{
  const h=mount();h.setStatus({running:true});await h.c.loadStatus();assert.equal(h.c.status.stopping,false);assert.equal(h.c.taskDisplay.value.label,'正在执行');h.cleanup();
});
test('停止中提示接入现有可读状态区，执行按钮禁用且不显示普通发送进度',()=>{
  assert(html.includes('status.stopping'));assert(html.includes("status.stopping ? '正在停止／结果待核对'"));
  assert.match(html,/:disabled="status.running \|\| status.stopping/);
  assert.match(html,/v-if="status.running && !status.stopping && status.send_progress"/);
  assert.match(html,/role="status" aria-live="polite" aria-atomic="true">\{\{ taskStatusAnnouncement \}\}/);
});
(async()=>{let failed=0;for(const {name,fn} of tests){try{await fn();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length}));process.exitCode=failed?1:0;})();
