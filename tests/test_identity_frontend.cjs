const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const scope=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(base.slice(0,base.indexOf('const forbidden='))+';globalThis.makeFixture=fixture;',scope);
const tests=[];const test=(name,fn)=>tests.push({name,fn});
const old='a'.repeat(32),fresh='b'.repeat(32);
const rows=()=>[{entry_id:old,display_name:'旧名',selected:true,observation_status:'not_observed',last_capture_complete:false},
 {entry_id:fresh,display_name:'新名',selected:false,streak_days:12,streak_status:'confirmed',observation_status:'seen',identity_review_pending:true,
  rename_candidates:[{old_entry_id:old,old_name:'旧名',new_name:'新名',evidence:['same_avatar'],unresolved_send:true}]}];
async function setup(){const h=scope.makeFixture();await h.connect();h.handlers.set('/api/ledger',()=>({data:{entries:rows(),ledger_version:'v1',contacts_capture:{complete:false,reason:'round_limit'}}}));await h.component.loadAll();return h;}
const defer=()=>{let resolve;const promise=new Promise(fn=>resolve=fn);return{promise,resolve};};
test('页面移除身份确认面板和关联操作',async()=>{
 const h=await setup();const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
 assert(!html.includes('好友身份待确认'));assert(!html.includes('确认同一人并关联'));assert(!html.includes('确认不是同一人'));
 assert.equal(h.component.reviewIdentity,undefined);assert.equal(h.component.renameCandidates,undefined);
 assert.match(h.component.captureMessage.value,/部分|尚未确认/);await h.close();
});
test('普通旧候选不再阻止勾选有火花好友',async()=>{
 const h=await setup();const data=rows();data[1].rename_candidates[0].unresolved_send=false;
 h.handlers.set('/api/ledger',()=>({data:{entries:data,ledger_version:'v2'}}));await h.component.loadAll();
 h.component.autoSelectSparkFriends();assert.equal(h.component.contacts.value[1].selected,true);
 assert.equal(h.component.contacts.value[0].selected,true); // 保留原有勾选，不擅自清空历史选择。
 assert(!h.calls.some(c=>c.url.includes('identity-decision')));await h.close();
});
test('历史发送未知仍受保护，且不再要求确认是否同一人',async()=>{
 const h=await setup();h.component.autoSelectSparkFriends();assert(!h.component.contacts.value[1].selected);
 assert.match(h.component.identityDescription(h.component.contacts.value[1]),/发送结果待核对/);
 assert(!h.component.identityDescription(h.component.contacts.value[1]).includes('身份'));
 await h.close();
});
(async()=>{let failed=0;for(const t of tests){try{await t.fn();console.log('PASS '+t.name);}catch(e){failed++;console.error(e.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed}));process.exitCode=failed?1:0;})();
