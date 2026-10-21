const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const scope=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(base.slice(0,base.indexOf('const forbidden='))+';globalThis.makeFixture=fixture;',scope);
const key='d'.repeat(32),tests=[];const test=(name,fn)=>tests.push({name,fn});
const row=()=>({entry_id:key,display_name:'合成火花好友',selected:false,streak_days:81,streak_status:'reset_pending',streak_suspect_value:2,streak_suspect_count:2,streak_suspect_since:'2026-10-18',streak_confirmed_at:'2026-10-17T08:00:00+08:00'});
async function setup(){const h=scope.makeFixture();await h.connect();h.handlers.set('/api/ledger',()=>({data:{entries:[row()],ledger_version:'reset-v1'}}));await h.component.loadAll();return h;}
test('重置候选显示新旧值但不算当前火花，也不能一键勾选',async()=>{
 const h=await setup();assert.equal(h.component.streakResetCandidates.value.length,1);
 assert.match(h.component.streakDisplay(h.component.contacts.value[0]),/可能已重新开始.*81.*2/);
 assert.equal(h.component.streakBadge(h.component.contacts.value[0]),'重置待确认');
 assert.equal(h.component.sparkFriendCount.value,0);h.component.autoSelectSparkFriends();assert.equal(h.component.contacts.value[0].selected,false);await h.close();
});
test('用户接受后携带当前版本及ID，并使用后台确认的值',async()=>{
 const h=await setup();let sent;h.handlers.set('/api/ledger/streak-decision',call=>{sent=call.data;return{data:{ok:true,entries:[{...row(),streak_status:'confirmed',streak_days:2}],ledger_version:'reset-v2'}};});
 assert.equal(await h.component.reviewStreak(h.component.streakResetCandidates.value[0],'accept'),true);
 assert.equal(sent.entry_id,key);assert.equal(sent.action,'accept');assert.equal(sent.expected_ledger_version,'reset-v1');
 assert.equal(h.component.contacts.value[0].streak_days,2);assert.equal(h.component.streakResetCandidates.value.length,0);await h.close();
});
test('保持旧值的结果保留历史天数并移除当前提示',async()=>{
 const h=await setup();h.handlers.set('/api/ledger/streak-decision',()=>({data:{ok:true,entries:[{...row(),streak_status:'suspect'}],ledger_version:'kept'}}));
 assert.equal(await h.component.reviewStreak(h.component.streakResetCandidates.value[0],'keep'),true);
 assert.equal(h.component.contacts.value[0].streak_days,81);assert.equal(h.component.streakResetCandidates.value.length,0);await h.close();
});
test('未保存草稿阻止确认；退出后迟到响应不回填',async()=>{
 const h=await setup();h.component.contacts.value[0].selected=true;const count=h.calls.length;
 assert.equal(await h.component.reviewStreak(row(),'accept'),false);assert.equal(h.calls.length,count);
 h.component.contacts.value[0].selected=false;let resolve;h.handlers.set('/api/ledger/streak-decision',()=>new Promise(fn=>resolve=fn));
 const pending=h.component.reviewStreak(row(),'accept');assert.equal(h.component.ledgerWriteBusy.value,true);
 await h.component.resetConsole();resolve({data:{ok:true,entries:[row()],ledger_version:'late'}});await pending;
 assert.equal(h.component.contacts.value.length,0);assert.equal(h.component.streakResetCandidates.value.length,0);await h.close();
});
test('旧版本409保留原读数并提示刷新',async()=>{
 const h=await setup();h.handlers.set('/api/ledger/streak-decision',()=>Promise.reject({response:{status:409,data:{detail:'请刷新'}}}));
 assert.equal(await h.component.reviewStreak(row(),'accept'),false);assert.equal(h.component.contacts.value[0].streak_days,81);await h.close();
});
(async()=>{let failed=0;for(const t of tests){try{await t.fn();console.log('PASS '+t.name);}catch(e){failed++;console.error(e.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed}));process.exitCode=failed?1:0;})();
