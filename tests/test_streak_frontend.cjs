const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const scope=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(base.slice(0,base.indexOf('const forbidden='))+';globalThis.makeFixture=fixture;',scope);
const tests=[];const test=(name,fn)=>tests.push({name,fn});
test('未知和可疑旧值不参与火花人数或一键勾选，展示历史确认时间',async()=>{
 const h=scope.makeFixture();await h.connect();
 const rows=['confirmed','unknown','suspect','none'].map((state,i)=>({entry_id:String(i),display_name:'synthetic-'+i,streak_days:state==='none'?0:81,streak_status:state,streak_confirmed_at:'2026-10-18T08:00:00+08:00',streak_confirmed_date:'2026-10-18',streak_unconfirmed_days:0,streak_may_be_stale:false}));
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'synthetic'}}));await h.component.loadAll();
 assert.equal(h.component.sparkFriendCount.value,1);h.component.autoSelectSparkFriends();
 assert.deepEqual(Array.from(h.component.contacts.value.filter(x=>x.selected),x=>x.entry_id),['0']);
 assert.match(h.component.streakDisplay(h.component.contacts.value[1]),/未更新.*81/);
 assert.match(h.component.streakDisplay(h.component.contacts.value[2]),/待核对.*81/);
 assert.match(h.component.streakDetail(h.component.contacts.value[1]),/最后确认于 \d{4}-\d{2}-\d{2}/);
 assert.match(h.component.streakDisplay(h.component.contacts.value[3]),/无火花/);
 await h.close();
});
test('旧台账无新字段仍可显示历史数值',async()=>{
 const h=scope.makeFixture();await h.connect();assert.match(h.component.streakDisplay({streak_days:12}),/12/);await h.close();
});
test('历史读数显示最后确认业务日期和年龄，过期更醒目且缺失日期不虚构零天',async()=>{
 const h=scope.makeFixture();await h.connect();
 const row={streak_status:'unknown',streak_confirmed_date:'2026-10-01',streak_unconfirmed_days:17,streak_may_be_stale:true};
 assert.match(h.component.streakDetail(row),/最后确认于 2026-10-01（17 天前）.*可能已过期/);assert.equal(h.component.streakStale(row),true);
 assert.match(h.component.streakDetail({streak_unconfirmed_days:null}),/未记录|异常/);await h.close();
});
test('重燃与即将消失计入火花并可一键勾选，仍保留真实状态',async()=>{
 const h=scope.makeFixture();await h.connect();
 const rows=[
  {entry_id:'normal',display_name:'正常',streak_days:215,streak_status:'confirmed'},
  {entry_id:'rekindle',display_name:'重燃',streak_days:81,streak_status:'unknown',streak_label:'重燃中 2/3'},
  {entry_id:'expire',display_name:'即将消失',streak_days:0,streak_status:'unknown',streak_label:'1 天后消失'},
  {entry_id:'missing',display_name:'未识别',streak_days:99,streak_status:'unknown'},
  {entry_id:'old',display_name:'未观察到',streak_status:'unknown',streak_label:'重燃中 2/3',observation_status:'not_observed'},
  {entry_id:'suspect',display_name:'冲突',streak_status:'suspect',streak_label:'重燃中 2/3'}];
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'special-fixture'}}));
 await h.component.loadAll();assert.equal(h.component.sparkFriendCount.value,3);
 h.component.autoSelectSparkFriends();assert.deepEqual(Array.from(h.component.contacts.value.filter(x=>x.selected),x=>x.entry_id),['normal','rekindle','expire']);
 assert.match(h.component.streakDisplay(h.component.contacts.value[1]),/重燃中 2\/3.*历史 81/);
 assert.match(h.component.streakDisplay(h.component.contacts.value[2]),/1 天后消失/);
 assert.equal(h.component.contacts.value[2].streak_days,0);await h.close();
});
test('特殊火花不绕过实际同名及发送记录保护',async()=>{
 const h=scope.makeFixture();await h.connect();
 const rows=[{entry_id:'a',display_name:'合成A',streak_label:'重燃中 2/3',streak_status:'unknown',identity_ambiguous:true},
 {entry_id:'b',display_name:'合成B',streak_label:'1 天后消失',streak_status:'unknown',send_block_reason:'related_send_unconfirmed'}];
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'protected'}}));await h.component.loadAll();
 assert.equal(h.component.sparkFriendCount.value,2);h.component.autoSelectSparkFriends();assert.equal(h.component.selectedFriendCount.value,0);await h.close();
});
test('历史同名行不再把后台已确认唯一的特殊火花好友误禁选',async()=>{
 const h=scope.makeFixture();await h.connect();const rows=[
 {entry_id:'old',display_name:'同名历史',identity_ambiguous:false,observation_status:'not_observed',selected:false,streak_status:'unknown',streak_label:'重燃中 2/3'},
 {entry_id:'new',display_name:'同名历史',identity_ambiguous:false,observation_status:'seen',selected:true,streak_status:'unknown',streak_label:'重燃中 2/3'}];
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'current-unique'}}));await h.component.loadAll();
 assert.equal(h.component.contacts.value[1].identity_ambiguous,false);assert.equal(h.component.contacts.value[1].selected,true);
 assert.equal(h.component.sparkFriendCount.value,1);h.component.contacts.value[1].selected=false;h.component.autoSelectSparkFriends();
 assert.equal(h.component.contacts.value[1].selected,true);assert.equal(h.component.contacts.value[0].selected,false);await h.close();
});
test('默认只显示当前记录，历史开关可查看且不改变勾选',async()=>{
 const h=scope.makeFixture();await h.connect();h.handlers.set('/api/ledger',()=>({data:{entries:[{entry_id:'old',display_name:'历史',observation_status:'not_observed',selected:true},{entry_id:'now',display_name:'当前',observation_status:'seen',selected:false}],ledger_version:'history-view'}}));await h.component.loadAll();
 assert.equal(h.component.historicalContactCount.value,1);assert.deepEqual(Array.from(h.component.filteredContacts.value,x=>x.entry_id),['now']);
 h.component.showHistoricalContacts.value=true;assert.equal(h.component.filteredContacts.value.length,2);assert.equal(h.component.contacts.value[0].selected,true);await h.close();
});
test('灰色火花与观察时间会出现在详情里，过期提示和核对状态单独展示',async()=>{
 const h=scope.makeFixture();await h.connect();
 const now='2023-11-15T06:13:20+08:00';
 const rows=[
  {entry_id:'gray',display_name:'灰色',streak_days:361,streak_status:'confirmed',streak_at_risk:true,streak_seen_at:now,streak_confirmed_at:now},
  {entry_id:'expire',display_name:'即将消失',streak_days:0,streak_status:'unknown',streak_label:'1 天后消失',streak_seen_at:now},
  {entry_id:'pending',display_name:'待核对',send_verification_status:'pending_review',active_diagnostic_reason:'scope_changed'}];
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'risk'}}));
 await h.component.loadAll();
 assert.match(h.component.streakDisplay(h.component.contacts.value[0]), /361 天.*灰色/);
 assert.match(h.component.streakDetail(h.component.contacts.value[0]), /灰色火花/);
 assert.match(h.component.streakDetail(h.component.contacts.value[0]), /观察于/);
 assert.equal(h.component.streakExpiring(h.component.contacts.value[1]), true);
 assert.match(h.component.streakDetail(h.component.contacts.value[1]), /火花即将消失：1 天后消失/);
 assert.match(h.component.verificationLabel(h.component.contacts.value[2]), /仍待核对/);
 assert.equal(h.component.contacts.value[2].active_diagnostic_reason, 'scope_changed');
 await h.close();
});
test('总览展示火花即将消失警告，且保留最后确认于作为无观察时间时的回退',async()=>{
 const h=scope.makeFixture();await h.connect();
 h.handlers.set('/api/status',()=>({data:{today_summary:{business_date:'2026-11-08',accepted:0,unconfirmed:0,pending:0,selected:1,includes_historical_unconfirmed:false,historical_unreviewed:0,streak_warnings:[{display_name:'空空',streak_label:'6 天后消失',expires_in_days:6},{display_name:'合成即将消失',streak_label:'1 天后消失',expires_in_days:1,observed_at:'2026-11-08T00:12:00+08:00'}]},active_send_diagnostics:[{display_name:'合成待核对',reason:'scope_changed',business_date:'2026-11-08',at:'2026-11-08T00:05:00+08:00'}]}}));
 await h.component.loadStatus();
 assert.equal(h.component.todaySummary.value.streak_warnings.length, 1);
 assert.match(h.component.streakWarningMessage.value, /合成即将消失/);
 assert.equal(h.component.streakWarningMessage.value.includes('空空'), false);
 assert.match(h.component.activeDiagnosticMessage.value, /合成待核对.*scope_changed/);
 assert.match(h.component.streakDetail({streak_status:'unknown',streak_confirmed_date:'2026-10-01',streak_unconfirmed_days:17,streak_may_be_stale:true}), /最后确认于 2026-10-01（17 天前）/);
 await h.close();
});

test('一键勾选只保留当前有火花的好友，并可只看有火花',async()=>{
 const h=scope.makeFixture();await h.connect();
 const rows=[
  {entry_id:'spark',display_name:'有火花',streak_days:218,streak_status:'confirmed',observation_status:'seen',selected:false},
  {entry_id:'gray',display_name:'灰色',streak_days:361,streak_status:'confirmed',streak_at_risk:true,observation_status:'seen',selected:false},
  {entry_id:'extra',display_name:'无火花却勾选',streak_days:0,streak_status:'unknown',observation_status:'seen',selected:true},
  {entry_id:'old',display_name:'历史重复',streak_days:215,streak_status:'unknown',observation_status:'not_observed',selected:true}];
 h.handlers.set('/api/ledger',()=>({data:{entries:rows,ledger_version:'exact-select'}}));
 await h.component.loadAll();
 assert.equal(h.component.sparkFriendCount.value,2);
 h.component.autoSelectSparkFriends();
 assert.deepEqual(Array.from(h.component.contacts.value.filter(x=>x.selected),x=>x.entry_id),['spark','gray']);
 h.component.showOnlySparkFriends.value=true;
 assert.deepEqual(Array.from(h.component.filteredContacts.value,x=>x.entry_id),['spark','gray']);
 await h.close();
});
(async()=>{let failed=0;for(const t of tests){try{await t.fn();console.log('PASS '+t.name);}catch(e){failed++;console.error(e.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed}));process.exitCode=failed?1:0;})();
