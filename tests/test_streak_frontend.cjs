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
(async()=>{let failed=0;for(const t of tests){try{await t.fn();console.log('PASS '+t.name);}catch(e){failed++;console.error(e.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed}));process.exitCode=failed?1:0;})();
