const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const script = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m => !/\bsrc\s*=/.test(m[1]) && m[2].includes('const { createApp'))[2];
function mount() {
  let component, response = {}, ledgerResponse = {entries:[]}, failure = false;
  const backgroundPauses = [], posted = [];
  const hooks = [], timers = new Map(); let timerId = 0;
  const api = {async post(url,data) {assert.equal(url,'/api/run');posted.push({url,data});return {data:{ok:true}};},interceptors:{response:{use(){}}}, async get(url) {
    if (url === '/api/status') {if (failure) throw Error('offline'); return {data:response};}
    if (url === '/api/ledger') return {data:ledgerResponse};
    assert(['/api/config','/api/logs','/api/email-config'].includes(url)); return {data:{}};
  }};
  const Vue = {ref:value=>({value}), reactive:value=>value, computed:fn=>({get value(){return fn();}}), watch(){}, nextTick:()=>Promise.resolve(), onMounted(){}, onUnmounted:fn=>hooks.push(fn), onBeforeUnmount:fn=>hooks.push(fn), createApp(options){component=options.setup();return {use(){return this;},mount(){}};}};
  const window = {innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){}};
  window.SparkSilkBackground={getState:()=>({enabled:true,available:true,reducedMotion:false,running:true}),setInputPaused:value=>backgroundPauses.push(value)};
  vm.runInNewContext(script,{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{},axios:{create:()=>api},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},setTimeout(){return 1;},clearTimeout(){},setInterval(fn){const id=++timerId;timers.set(id,fn);return id;},clearInterval(id){timers.delete(id);}}, {filename:'actual-console-v28.js'});
  return {c:component,backgroundPauses,posted,setResponse(value){response=value;},setLedger(value){ledgerResponse=value;},fail(){failure=true;},cleanup(){hooks.forEach(fn=>fn());}};
}
const tests = [];
async function test(name, fn) {await fn();tests.push(name);}
(async()=>{
 await test('Credential colors distinguish verified, invalid and unverified states without changing their labels',()=>{
  const cases=[
   [{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:1700000000},'上次检查有效','var(--status-valid)'],
   [{status_connected:true,state_file_exists:true,session_status:'expired',session_checked_at:1700000000},'已失效，需扫码','var(--status-invalid)'],
   [{status_connected:true,state_file_exists:false,session_status:'unknown'},'未保存凭证','var(--status-invalid)'],
   [{status_connected:true,state_file_exists:true,session_status:'unknown'},'已保存，待检查','var(--status-pending)'],
   [{status_connected:true,state_file_exists:true,session_status:'failed'},'登录状态待检查','var(--status-pending)'],
   [{status_connected:true,state_file_exists:true,session_status:'partial'},'登录状态待检查','var(--status-pending)'],
   [{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:null},'已保存，待检查','var(--status-pending)'],
   [{status_connected:false,state_file_exists:true,session_status:'ok',session_checked_at:1700000000},'状态待同步','var(--status-pending)'],
  ];
  for(const[state,label,color]of cases){const h=mount();try{Object.assign(h.c.status,state);assert.equal(h.c.credentialDisplay.value.label,label);assert.equal(h.c.credentialDisplay.value.color,color,label);}finally{h.cleanup();}}
 });
 await test('Confirmed background and automatic-send pauses use red',()=>{
  for(const[background,automatic,label]of [[false,true,'后台已暂停'],[true,false,'自动发送已暂停']]){
   const h=mount();try{Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:background,running:false,stopping:false,retry_dispatching:false});h.c.config.auto_run_enabled=automatic;assert.equal(h.c.taskDisplay.value.label,label);assert.equal(h.c.taskDisplay.value.color,'var(--status-invalid)',label);}finally{h.cleanup();}
  }
 });
 await test('Enabled guardian states are green while stopping and unconfirmed states stay distinct',()=>{
  const cases=[
   [{status_connected:true,background_tasks_enabled:false,running:true},'正在执行','valid'],
   [{status_connected:true,background_tasks_enabled:false,running:true,stopping:true},'正在停止／结果待核对','invalid'],
   [{status_connected:false,background_tasks_enabled:false,stopping:true},'正在停止／结果待核对','invalid'],
   [{status_connected:true,background_tasks_enabled:false,running:false,retry_dispatching:true},'正在启动补发','valid'],
   [{status_connected:false,background_tasks_enabled:false,running:false,retry_dispatching:true},'正在启动补发','pending'],
   [{status_connected:false,background_tasks_enabled:false,running:false},'等待状态同步','pending'],
   [{status_connected:true,background_tasks_enabled:null,running:false},'等待状态同步','pending'],
   [{status_connected:true,background_tasks_enabled:true,running:false,next_run:null},'等待调度','valid'],
   [{status_connected:true,background_tasks_enabled:true,running:false,next_run:'2030-01-01T00:00:00+08:00'},'等待定时执行','valid'],
  ];
  for(const[state,label,tone]of cases){const h=mount();try{Object.assign(h.c.status,{stopping:false,retry_dispatching:false,...state});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,label);assert.equal(h.c.taskDisplay.value.color,`var(--status-${tone})`,label);}finally{h.cleanup();}}
 });
 await test('No status response cannot claim a valid login or running schedule',()=>{const h=mount();assert.equal(h.c.credentialDisplay.value.label,'状态待同步');assert.equal(h.c.taskDisplay.value.label,'等待状态同步');h.cleanup();});
 await test('Saved file alone is explicitly unverified',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'unknown'});assert.equal(h.c.credentialDisplay.value.label,'已保存，待检查');h.cleanup();});
 await test('Expired session overrides presence of a valid JSON file',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'expired'});assert.equal(h.c.credentialDisplay.value.label,'已失效，需扫码');assert.equal(h.c.credentialDisplay.value.tone,'bad');h.cleanup();});
 await test('Previous ok without timestamp is not a verified current credential',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:null});assert.equal(h.c.credentialDisplay.value.label,'已保存，待检查');h.cleanup();});
 await test('Confirmed session has a dated last-check label',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:1700000000});assert.equal(h.c.credentialDisplay.value.label,'上次检查有效');assert(h.c.sessionCheckedLabel.value.includes('2023'));h.cleanup();});
 await test('Process-level pause overrides enabled schedule setting',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:false});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'后台已暂停');h.cleanup();});
 await test('Manual active task remains visible while background work is paused',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:false,running:true});assert.equal(h.c.taskDisplay.value.label,'正在执行');h.cleanup();});
 await test('No registered next run is not called scheduled-ready',()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:true,next_run:null});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'等待调度');h.c.status.next_run='2030-01-01T00:00:00+08:00';assert.equal(h.c.taskDisplay.value.label,'等待定时执行');h.cleanup();});
 await test('T02 后端无冷却时连续失败仍展示真实下一次定时执行',()=>{
  const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:true,auto_paused_until:0,consecutive_failures:4,next_run:'2030-01-01T00:00:00+08:00'});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'等待定时执行');assert.match(h.c.taskStatusAnnouncement.value,/上一轮发送失败.*连续失败 4 轮/);assert(!h.c.taskStatusAnnouncement.value.includes('暂停'));h.cleanup();
 });
 await test('T02 历史非零冷却字段不能掩盖运行后台开关和用户设置',()=>{
  const h=mount();Object.assign(h.c.status,{status_connected:true,background_tasks_enabled:true,auto_paused_until:Date.now()/1000+36000,next_run:'2030-01-01T00:00:00+08:00'});h.c.config.auto_run_enabled=true;assert.equal(h.c.taskDisplay.value.label,'等待定时执行');h.c.status.running=true;assert.equal(h.c.taskDisplay.value.label,'正在执行');h.c.status.running=false;h.c.status.background_tasks_enabled=false;assert.equal(h.c.taskDisplay.value.label,'后台已暂停');h.c.status.background_tasks_enabled=true;h.c.config.auto_run_enabled=false;assert.equal(h.c.taskDisplay.value.label,'自动发送已暂停');h.c.config.auto_run_enabled=true;h.c.status.next_run=null;assert.equal(h.c.taskDisplay.value.label,'等待调度');h.cleanup();
 });
 await test('T02 状态加载忽略旧冷却值且异常横幅只提示失败和手动重试',async()=>{
  const h=mount();h.setResponse({background_tasks_enabled:true,auto_paused_until:Date.now()/1000+36000,consecutive_failures:3,next_run:'2030-01-01T00:00:00+08:00'});h.c.config.auto_run_enabled=true;await h.c.loadStatus();assert.equal(h.c.status.auto_paused_until,0);assert.equal(h.c.taskDisplay.value.label,'等待定时执行');assert.match(h.c.taskStatusAnnouncement.value,/连续失败 3 轮/);assert(!/异常冷却中|冷却结束后自动恢复|自动发送暂时暂停|isAutoPaused|autoResumeLabel/.test(html));assert(html.includes('仍可手动点击「立即续火花」重试'));h.cleanup();
 });
 await test('Real loadStatus consumes verification and process state from API',async()=>{const h=mount();h.setResponse({version:'2.8',state_file_exists:true,session_status:'expired',session_checked_at:1700000000,background_tasks_enabled:false,next_run:null});assert.equal(await h.c.loadStatus(),true);assert.equal(h.c.status.session_status,'expired');assert.equal(h.c.status.session_checked_at,1700000000);assert.equal(h.c.taskDisplay.value.label,'后台已暂停');assert.equal(h.c.credentialDisplay.value.label,'已失效，需扫码');h.cleanup();});
 await test('Failed status polling removes stale green confidence',async()=>{const h=mount();Object.assign(h.c.status,{status_connected:true,state_file_exists:true,session_status:'ok',session_checked_at:1700000000,background_tasks_enabled:true});h.fail();assert.equal(await h.c.loadStatus(),false);assert.equal(h.c.credentialDisplay.value.label,'状态待同步');assert.equal(h.c.taskDisplay.value.label,'等待状态同步');h.cleanup();});
 await test('QR recovery controls and progress are wired without a popup',()=>{const h=mount();for(const name of ['retryQrSync','retryQrImage','retryQrCleanup']){assert.equal(typeof h.c[name],'function');assert(html.includes('@click="'+name+'"'));}assert(html.includes('qr-countdown'));assert(html.includes('qr-steps'));assert.equal(h.c.appVersion.value,'2.8');h.cleanup();});
 await test('Connecting the public console clears input-focus pause without a blur event',async()=>{const h=mount();h.c.setFluidPaused(true);await h.c.connectConsole();assert.equal(h.c.authed.value,true);assert.deepEqual(h.backgroundPauses,[true,false]);h.cleanup();});
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
 await test('T01 all already-sent-today results show a healthy skipped summary without claiming new deliveries',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{name:'今日对象甲',code:'already_sent_today'},{name:'今日对象乙',code:'already_sent_today'},{name:' 今日对象甲 ',code:'already_sent_today'}]}});await h.c.loadStatus();
  assert.match(h.c.lastRunInfo.summary,/今天已发 2 人，已自动跳过/);assert.match(h.c.lastRunInfo.summary,/成功\s*0/);assert.match(h.c.lastRunInfo.summary,/失败\s*0/);assert.equal(h.c.lastRunInfo.success,true);assert(!h.c.lastRunInfo.summary.includes('今日对象'));h.cleanup();
 });
 await test('T01 new success and ordinary skips retain separate counts from already-sent-today skips',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:['新成功'],failed:[],deferred:[],skipped:[{name:'今日已发',code:'already_sent_today'},{name:'其它对象',code:'ambiguous_name'}]}});await h.c.loadStatus();const summary=h.c.lastRunInfo.summary;
  assert.match(summary,/成功\s*1/);assert.match(summary,/今天已发 1 人，已自动跳过/);assert.match(summary,/跳过\s*1/);assert.equal(h.c.lastRunInfo.success,true);h.cleanup();
 });
 await test('T01 an unconfirmed-today skip is a warning rather than a failure or a healthy completion',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{name:'已发',code:'already_sent_today'},{name:'待核对',code:'unconfirmed_today'},{name:'待核对',code:'unconfirmed_today'}]}});await h.c.loadStatus();const summary=h.c.lastRunInfo.summary;
  assert.match(summary,/今天已发 1 人，已自动跳过/);assert.match(summary,/待核对\s*1/);assert.match(summary,/勿直接重发/);assert.match(summary,/失败\s*0/);assert.match(summary,/待处理\s*0/);assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('T01 inconsistent failures or incomplete records never become green from an already-sent code',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:[],failed:[{name:'同一对象'}],deferred:[],skipped:[{name:'同一对象',code:'already_sent_today'}]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/失败\s*1/);assert(!h.c.lastRunInfo.summary.includes('今天已发'));assert.equal(h.c.lastRunInfo.success,false);
  h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{name:'已发',code:'already_sent_today'},null]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/记录不完整/);assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('T01 dry runs cannot claim already-sent daily skips as successful real sends',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:true,ok:[],failed:[],deferred:[],skipped:[{name:'对象',code:'already_sent_today'}]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/模拟演练/);assert(!h.c.lastRunInfo.summary.includes('今天已发'));assert.equal(h.c.lastRunInfo.success,false);h.cleanup();
 });
 await test('T01 default immediate send keeps dry_run=false and never submits force',async()=>{
  const h=mount();h.setLedger({entries:[{entry_id:'synthetic-entry',display_name:'合成好友',selected:true}],ledger_version:'synthetic-version'});await h.c.loadAll();await h.c.runStreak(false);const sent=h.posted.find(item=>item.url==='/api/run');assert(sent);assert.deepEqual(JSON.parse(JSON.stringify(sent.data)),{dry_run:false,expected_ledger_version:'synthetic-version'});assert(!Object.hasOwn(sent.data,'force'));h.cleanup();
 });
 await test('T01 already-sent counts use stable entry IDs when two contacts have the same display name',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-14T12:00:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{entry_id:'entry-a',name:'同名好友',code:'already_sent_today'},{entry_id:'entry-b',name:'同名好友',code:'already_sent_today'},{entry_id:'entry-a',name:'同名好友',code:'already_sent_today'}]}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/今天已发 2 人，已自动跳过/);assert.equal(h.c.lastRunInfo.success,true);h.cleanup();
 });
 await test('T07 待核对台账保留旧成功时间时仍显示当前不确定结果而非成功',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-15T09:00:00+08:00',dry_run:false,ok:['合成好友'],failed:[],skipped:[]}});await h.c.loadStatus();assert.equal(h.c.lastRunInfo.success,true);
  h.setLedger({entries:[{entry_id:'t07-unknown-entry',display_name:'合成好友',last_status:'unconfirmed',last_sent_at:'2026-10-14T09:00:00+08:00',last_unconfirmed_at:'2026-10-15T09:30:00+08:00'}]});h.setResponse({last_run:{at:'2026-10-15T09:30:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{entry_id:'t07-unknown-entry',name:'合成好友',code:'delivery_unconfirmed',retryable:false}],delivery_unconfirmed:true}});await h.c.loadAll();
  assert.equal(h.c.contacts.value[0].last_status,'unconfirmed');assert.equal(h.c.contacts.value[0].last_sent_at,'2026-10-14T09:00:00+08:00');assert.match(h.c.lastRunInfo.summary,/待核对\s*1/);assert.match(h.c.lastRunInfo.summary,/失败\s*0/);assert.match(h.c.lastRunInfo.summary,/勿直接重发/);assert.equal(h.c.lastRunInfo.success,false);assert.equal(h.posted.length,0);assert(html.includes("row.last_status === 'unconfirmed'"));assert(html.includes("item.last_status === 'unconfirmed' ? '待核对'"));h.cleanup();
 });
 await test('T07 当日待核对跳过只能提示核对且不会请求自动补发或被新状态刷新成健康',async()=>{
  const h=mount();h.setResponse({last_run:{at:'2026-10-15T10:00:00+08:00',dry_run:false,ok:[],failed:[],deferred:[],skipped:[{entry_id:'t07-pending-review',name:'合成待核对',code:'unconfirmed_today',retryable:false}],delivery_unconfirmed:false}});await h.c.loadStatus();assert.match(h.c.lastRunInfo.summary,/待核对\s*1/);assert.match(h.c.lastRunInfo.summary,/失败\s*0/);assert(!h.c.lastRunInfo.summary.includes('今天已发'));assert.equal(h.c.lastRunInfo.success,false);assert.equal(h.posted.length,0);h.cleanup();
 });
 console.log(JSON.stringify({passed:tests.length,tests},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
