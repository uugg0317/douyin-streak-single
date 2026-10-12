const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m => !/\bsrc\s*=/.test(m[1]) && m[2].includes('const { createApp'))[2];
function mount() {
  let component, response = {}, ledgerResponse = {entries:[]}, failure = false;
  const backgroundPauses = [];
  const hooks = [], timers = new Map(); let timerId = 0;
  const api = {async post(url) {assert.equal(url, '/api/auth/login');return {data:{ok:true}};},interceptors:{response:{use(){}}}, async get(url) {
    if (url === '/api/status') {if (failure) throw Error('offline'); return {data:response};}
    if (url === '/api/ledger') return {data:ledgerResponse};
    assert(['/api/config','/api/logs','/api/email-config'].includes(url)); return {data:{}};
  }};
  const Vue = {ref:value=>({value}), reactive:value=>value, computed:fn=>({get value(){return fn();}}), watch(){}, nextTick:()=>Promise.resolve(), onMounted(){}, onUnmounted:fn=>hooks.push(fn), onBeforeUnmount:fn=>hooks.push(fn), createApp(options){component=options.setup();return {use(){return this;},mount(){}};}};
  const window = {innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){}};
  window.SparkSilkBackground={getState:()=>({enabled:true,available:true,reducedMotion:false,running:true}),setInputPaused:value=>backgroundPauses.push(value)};
  vm.runInNewContext(script,{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{},axios:{create:()=>api},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},setTimeout(){return 1;},clearTimeout(){},setInterval(fn){const id=++timerId;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);}}, {filename:'actual-console-v25.js'});
  return {c:component,backgroundPauses,setResponse(value){response=value;},setLedger(value){ledgerResponse=value;},fail(){failure=true;},cleanup(){hooks.forEach(fn=>fn());}};
}
const tests = [];
async function test(name, fn) {await fn();tests.push(name);}
(async()=>{
 await test('No status response cannot claim a valid login or running schedule',()=>{const h=mount();assert.equal(h.c.credentialDisplay.value.label,'状态待同步');assert.equal(h.c.taskDisplay.value.label,'等待状态同步');h.cleanup();});
 await test('Saved file alone is explicitly unverified',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'unknown'});assert.equal(h.c.credentialDisplay.value.label,'已保存，待检查');h.cleanup();});
 await test('Expired session overrides presence of a valid JSON file',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'expired'});assert.equal(h.c.credentialDisplay.value.label,'已失效，需扫码');assert.equal(h.c.credentialDisplay.value.tone,'bad');h.cleanup();});
 await test('Previous ok without timestamp is not a verified current credential',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:null});assert.equal(h.c.credentialDisplay.value.label,'已保存，待检查');h.cleanup();});
 await test('Confirmed session has a dated last-check label',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:1700000000});assert.equal(h.c.credentialDisplay.value.label,'上次检查有效');assert(h.c.sessionCheckedLabel.value.includes('2023'));h.cleanup();});
 await test('Process-level pause overrides enabled schedule setting',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:false});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'后台已暂停');h.cleanup();});
 await test('Manual active task remains visible while background work is paused',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:false,running:true});assert.equal(h.c.taskDisplay.value.label,'正在执行');h.cleanup();});
 await test('No registered next run is not called scheduled-ready',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:true,next_run:null});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'等待调度');h.c.status.next_run='2030-01-01T00:00:00+08:00';assert.equal(h.c.taskDisplay.value.label,'等待定时执行');h.cleanup();});
 await test('Circuit breaker has precedence over ready schedule',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:true,auto_paused_until:Date.now()/1000+600,next_run:'2030-01-01T00:00:00+08:00'});assert.equal(h.c.taskDisplay.value.label,'异常冷却中');h.cleanup();});
 await test('Real loadStatus consumes verification and process state from API',async()=>{const h=mount();h.setResponse({version:'2.5',state_file_exists:true,session_status:'expired',session_checked_at:1700000000,background_tasks_enabled:false,next_run:null});assert.equal(await h.c.loadStatus(),true);assert.equal(h.c.status.session_status,'expired');assert.equal(h.c.status.session_checked_at,1700000000);assert.equal(h.c.taskDisplay.value.label,'后台已暂停');assert.equal(h.c.credentialDisplay.value.label,'已失效，需扫码');h.cleanup();});
 await test('Failed status polling removes stale green confidence',async()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:1700000000,background_tasks_enabled:true});h.fail();assert.equal(await h.c.loadStatus(),false);assert.equal(h.c.credentialDisplay.value.label,'状态待同步');assert.equal(h.c.taskDisplay.value.label,'等待状态同步');h.cleanup();});
 await test('QR recovery controls and progress are wired without a popup',()=>{const h=mount();for(const name of ['retryQrSync','retryQrImage','retryQrCleanup']){assert.equal(typeof h.c[name],'function');assert(html.includes('@click="'+name+'"'));}assert(html.includes('qr-countdown'));assert(html.includes('qr-steps'));assert.equal(h.c.appVersion.value,'2.5');h.cleanup();});
 await test('Successful Enter login clears input-focus pause even without a blur event',async()=>{const h=mount();h.c.setFluidPaused(true);h.c.loginResult.value='isolated-login-fixture';await h.c.doLogin();assert.equal(h.c.authed.value,true);assert.deepEqual(h.backgroundPauses,[true,false]);h.cleanup();});
 await test('Recent dry-run results are read from status and do not claim messages were sent',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:true,ok:['演练成功对象'],failed:Array.from({length:8},(_,i)=>({name:'未找到对象'+i,reason:'模拟未找到'})),deferred:[],skipped:[]}});await h.c.loadStatus();
  assert.match(h.c.lastRunInfo.summary,/模拟演练/);assert.match(h.c.lastRunInfo.summary,/(?:通过|成功)\s*1/);assert.match(h.c.lastRunInfo.summary,/失败\s*8/);assert.match(h.c.lastRunInfo.summary,/待处理\s*0/);assert(!/已发送|消息已发送/.test(h.c.lastRunInfo.summary));assert(h.c.lastRunInfo.time);assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('A browser-level interruption keeps earlier success and counts real pending names only',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:true,ok:['保留成功'],failed:[{name:'_system',code:'browser_session_closed',reason:'自动化浏览器会话已关闭，本轮已停止；请重新运行模拟演练检查。'}],deferred:[...Array.from({length:9},(_,i)=>({name:'待处理'+i})),{name:'_system',reason:'系统中断'}],skipped:[]}});await h.c.loadStatus();
  assert.match(h.c.lastRunInfo.summary,/(?:通过|成功)\s*1/);assert.match(h.c.lastRunInfo.summary,/失败\s*0/);assert.match(h.c.lastRunInfo.summary,/待处理\s*9/);assert.match(h.c.lastRunInfo.summary,/已中断.*浏览器.*关闭/);assert.equal(h.c.lastRunInfo.success,false);assert(!h.c.lastRunInfo.summary.includes('_system'));assert(!h.c.lastRunInfo.summary.includes('保留成功'));h.cleanup();
 });
 await test('Real sends are labelled separately from simulations',async()=>{
  const h=mount();h.setResponse({last_run:{at:1791777600,dry_run:false,ok:['已确认对象'],failed:[],deferred:[],skipped:[]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/实际发送/);assert.match(h.c.lastRunInfo.summary,/成功\s*1/);assert(!h.c.lastRunInfo.summary.includes('模拟演练'));assert.equal(h.c.lastRunInfo.success,true);h.cleanup();
 });
 await test('Deduplicated names cannot appear in several outcome counts',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['成功甲',' 成功甲 ',{name:'成功甲'}],failed:[{name:'成功甲'},{name:'失败乙'},{name:'失败乙'}],deferred:[{name:'失败乙'},'待处理丙',{name:'待处理丙'}],skipped:['待处理丙',{name:'跳过丁'},'跳过丁',{name:'_system',reason:'本轮预算不足，剩余对象待处理'}]}});await h.c.loadStatus();
  const summary=h.c.lastRunInfo.summary;assert.match(summary,/成功\s*1/);assert.match(summary,/失败\s*1/);assert.match(summary,/待处理\s*1/);assert.match(summary,/跳过\s*1/);assert(!summary.includes('已中断'));assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('Missing and malformed records safely clear an old visible result',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['甲'],failed:[]}});await h.c.loadStatus();
  for(const last_run of [undefined,null,[],{},'unexpected-value']){h.setResponse({last_run});await h.c.loadStatus();assert.equal(h.c.lastRunInfo.summary,'');assert.equal(h.c.lastRunInfo.time,'');assert.equal(h.c.lastRunInfo.success,false);}
  h.setResponse({last_run:{at:'private-invalid-date',ok:'not-an-array',failed:{private:'hidden'}}});await h.c.loadStatus();assert.equal(h.c.lastRunInfo.success,false);assert(!h.c.lastRunInfo.time.includes('private'));assert(!h.c.lastRunInfo.summary.includes('hidden'));h.cleanup();
 });
 await test('UTF names and arbitrary system reasons are not echoed into summary text',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:true,ok:['隐私名😀','隐私名😀'],failed:[{name:'_system',reason:'http://private.example/token=do-not-display C:/private/账户秘密<script>'}],deferred:[{name:'另一隐私名'}],skipped:[]}});await h.c.loadStatus();const summary=h.c.lastRunInfo.summary;
  assert.match(summary,/已中断/);assert.match(summary,/待处理\s*1/);for(const privateText of ['隐私名','private.example','token=','C:/private','账户秘密','<script>'])assert(!summary.includes(privateText));h.cleanup();
 });
 await test('Ledger loading cannot overwrite the authoritative status result',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:true,ok:['甲'],failed:[],deferred:[]}});h.setLedger({entries:[],last_run:{time:'wrong old source',summary:'wrong old source',success:false}});await h.c.loadAll();assert.match(h.c.lastRunInfo.summary,/模拟演练/);assert(!h.c.lastRunInfo.time.includes('wrong'));assert(!h.c.lastRunInfo.summary.includes('wrong'));h.cleanup();
 });
 await test('A legacy record without a mode flag does not pretend to be a real send',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',ok:['甲'],failed:[],deferred:[]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/模式未标明/);assert(!h.c.lastRunInfo.summary.includes('实际发送'));assert(!h.c.lastRunInfo.summary.includes('已发送'));h.cleanup();
 });
 await test('Malformed outcome entries cannot turn an uncertain result green',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['甲'],failed:[null,8,{reason:'private-invalid-entry'}],deferred:[]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/成功\s*1/);assert.match(h.c.lastRunInfo.summary,/记录不完整/);assert.equal(h.c.lastRunInfo.success,false);assert(!h.c.lastRunInfo.summary.includes('private'));h.cleanup();
 });
 await test('Unconfirmed delivery is counted separately and removed from ordinary pending or skipped outcomes',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['Alpha'],failed:[],deferred:[{name:'Beta'},{name:'Gamma'}],skipped:[{name:'Beta',code:'delivery_unconfirmed',reason:'本次发送结果无法确认，请人工核对'}],delivery_unconfirmed:true}});await h.c.loadStatus();const summary=h.c.lastRunInfo.summary;
  assert.match(summary,/实际发送.*成功\s*1/);assert.match(summary,/待处理\s*1/);assert.match(summary,/待核对\s*1/);assert.match(summary,/请在抖音核对.*勿直接重发/);assert(!/跳过\s*1/.test(summary));assert(!summary.includes('Alpha'));assert(!summary.includes('Beta'));assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('Duplicate unconfirmed entries do not double-count and a flag alone stays a warning',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['Alpha'],failed:[{name:'Beta'}],deferred:[{name:'Beta'}],skipped:[{name:'Beta',code:'delivery_unconfirmed'},{name:'Beta',code:'delivery_unconfirmed'},{name:'Alpha',code:'delivery_unconfirmed'}],delivery_unconfirmed:true}});await h.c.loadStatus();let summary=h.c.lastRunInfo.summary;
  assert.match(summary,/成功\s*1/);assert.match(summary,/失败\s*0/);assert.match(summary,/待处理\s*0/);assert.match(summary,/待核对\s*1/);assert.equal(h.c.lastRunInfo.success,false);
  h.setResponse({last_run:{at:'2026-10-12T12:00:00+08:00',dry_run:false,ok:['Alpha'],failed:[],delivery_unconfirmed:true}});await h.c.loadStatus();summary=h.c.lastRunInfo.summary;assert.match(summary,/待核对.*勿直接重发/);assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 console.log(JSON.stringify({passed:tests.length,tests},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
