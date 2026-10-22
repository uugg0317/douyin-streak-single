// Regression scenarios execute the actual page setup, Axios interceptors and QR SDK.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const baseline=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const context=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(baseline.slice(0,baseline.indexOf('const cases=[];'))+'; globalThis.reviewFixture=fixture;globalThis.reviewFlush=flush;',context);
const fixture=context.reviewFixture,flush=context.reviewFlush,BASE='/api/credentials/qr';
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
const tests=[];async function test(name,run){await run();tests.push(name);}
(async()=>{
 await test('Late 401 from an old connection epoch cannot invalidate a reconnected console or new QR job',async()=>{
  const h=fixture();await h.connect();const pending=defer();let first=true;
  h.handlers.set('/api/status',()=>{if(first){first=false;return pending.promise;}return{data:{version:'2.8',running:false}};});
  const old=h.api.get('/api/status').catch(()=>{});await flush();const oldConfig=h.calls.at(-1).config;
  await h.component.resetConsole();await h.connect();assert.equal(h.component.authed.value,true);
  assert.equal(await h.component.startQrLogin(),true);await flush();assert.equal(h.component.qrLogin.hasJob,true);
  const newConfig=h.calls.find(c=>c.url===BASE+'/start').config;assert.notEqual(oldConfig.sparkManagementEpoch,newConfig.sparkManagementEpoch);
  pending.reject({response:{status:401,data:{detail:'old connection rejected'}}});await old;await flush();
  assert.equal(h.component.authed.value,true);assert.equal(h.component.status.status_connected,true);assert.equal(h.component.qrLogin.hasJob,true);assert.equal(h.disposed,0);assert.equal(h.component.connectionError.value,'');await h.close();
 });
 await test('A current 401 reports an unconfirmed connection without mounting a password gate',async()=>{
  const h=fixture();await h.connect();h.handlers.set('/api/status',()=>Promise.reject({response:{status:401,data:{detail:'rejected'}}}));assert.equal(await h.component.loadStatus(),false);assert.equal(h.component.authed.value,true);assert.equal(h.component.status.status_connected,false);assert.match(h.component.connectionError.value,/连接未确认/);await h.close();
 });
 await test('Reset clears browser-only private snapshots and cancels local reads without server logout',async()=>{
  const h=fixture();await h.connect();h.component.contacts.value=[{entry_id:'entry-private'}];h.component.logsContent.value='synthetic-private-log';h.component.emailCfg.smtp_pass='synthetic-private-value';
  assert.equal(await h.component.resetConsole(),true);assert.equal(h.component.authed.value,true);assert.equal(h.component.contacts.value.length,0);assert.equal(h.component.logsContent.value,'');assert.equal(h.component.emailCfg.smtp_pass,'');assert.equal(h.component.status.status_connected,false);assert.match(h.component.connectionError.value,/连接已重置/);assert(!h.calls.some(c=>c.url.startsWith('/api/auth/')));await h.close();
 });
 await test('Obsolete logout intent cannot block public console initialization',async()=>{
  const storage=new Map([['dcs_logout_pending','1']]);const h=fixture({storage});await flush();for(const mount of h.mounted)await mount();await flush();assert.equal(h.component.authed.value,true);assert.equal(h.component.status.status_connected,true);assert.equal(h.storage.has('dcs_logout_pending'),false);assert(h.calls.some(c=>c.url==='/api/status'));assert(!h.calls.some(c=>c.url.startsWith('/api/auth/')));await h.close();
 });
 await test('Offline first load remains visible and can reconnect without any credential prompt',async()=>{const h=fixture();h.handlers.set('/api/status',()=>Promise.reject(Error('offline')));for(const mount of h.mounted)await mount();await flush();assert.equal(h.component.authed.value,true);assert.equal(h.component.status.status_connected,false);h.handlers.delete('/api/status');await h.component.connectConsole();assert.equal(h.component.status.status_connected,true);assert.equal(h.component.connectionError.value,'');assert(!h.calls.some(c=>c.url.startsWith('/api/auth/')));await h.close();});
 await test('A lost start response preserves its UUID; only explicit recovery retrieves the same server job',async()=>{
  const h=fixture();await h.connect();let registered=null;const requests=[];
  h.handlers.set(BASE+'/start',({data})=>{requests.push(data.request_id);if(!registered){registered=data.request_id;return Promise.reject(Error('response lost after server reservation'));}assert.equal(data.request_id,registered);return{data:h.waiting()};});
  assert.equal(await h.component.startQrLogin(),false);await flush();assert.equal(h.component.qrLogin.startUnconfirmed,true);assert.equal(h.component.qrLogin.canRetryStart,true);assert.equal(h.component.qrLogin.running,false);assert.equal(h.component.qrLogin.hasJob,false);assert.match(h.component.qrLogin.message,/结果尚未确认/);
  assert.equal(await h.component.startQrLogin(),false);await h.advance(20000);assert.equal(requests.length,1);assert(!h.calls.some(c=>c.url.endsWith('/cancel')));assert(!h.calls.some(c=>c.url.endsWith('/recover-cleanup')));
  assert.equal(await h.component.retryQrStart(),true);await flush();assert.equal(requests.length,2);assert.equal(requests[0],requests[1]);assert.equal(h.component.qrLogin.hasJob,true);assert.equal(h.component.qrLogin.startUnconfirmed,false);assert.equal(h.component.qrLogin.canRetryStart,false);assert(!h.calls.some(c=>c.url.endsWith('/cancel')));assert(!JSON.stringify(h.component.qrLogin).includes('synthetic-public-capability'));await h.close();
 });
 await test('Repeated unknown start responses retain one UUID and never automatically start or cancel',async()=>{
  const h=fixture();await h.connect();const requests=[];h.handlers.set(BASE+'/start',({data})=>{requests.push(data.request_id);return Promise.reject({response:{status:503,data:{detail:'synthetic uncertain server error'}}});});
  assert.equal(await h.component.startQrLogin(),false);assert.equal(await h.component.retryQrStart(),false);assert.equal(await h.component.retryQrStart(),false);await h.advance(30000);assert.equal(new Set(requests).size,1);assert.equal(requests.length,3);assert.equal(h.component.qrLogin.canRetryStart,true);assert(!h.calls.some(c=>c.url.endsWith('/cancel')));await h.close();
 });
 await test('Definite 403 start rejection does not retain a phantom task or disable normal restart',async()=>{
  const h=fixture();await h.connect();h.handlers.set(BASE+'/start',()=>Promise.reject({response:{status:403,data:{detail:'bad origin'}}}));assert.equal(await h.component.startQrLogin(),false);assert.equal(h.component.qrLogin.startUnconfirmed,false);assert.equal(h.component.qrLogin.canRetryStart,false);assert.equal(h.component.authed.value,true);h.handlers.delete(BASE+'/start');assert.equal(await h.component.startQrLogin(),true);await h.close();
 });
 await test('Deleting one stable identity never removes another contact with the same display name',async()=>{
  const h=fixture();await h.connect();h.handlers.set('/api/ledger',()=>({data:{contacts:[{entry_id:'same-a',display_name:'same-name',selected:true},{entry_id:'same-b',display_name:'same-name',selected:false}]}}));await h.component.loadAll();assert.equal(h.component.contacts.value.length,2);const row=h.component.contacts.value[0];assert.notEqual(h.component.contactKey(row),h.component.contactKey(h.component.contacts.value[1]));assert.equal(row.identity_ambiguous,true);assert.equal(h.component.toggleContactSelection(row),false);
  await h.component.deleteFriend(row);const deleted=h.calls.find(c=>c.url==='/api/ledger/delete');assert.deepEqual(JSON.parse(JSON.stringify(deleted.data)),{entry_id:'same-a'});assert.equal(h.component.contacts.value.length,1);assert.equal(h.component.contacts.value[0].entry_id,'same-b');await h.close();
 });
 await test('Selection and batch deletion use stable IDs, preserving unrelated rows',async()=>{
  const h=fixture();await h.connect();h.component.contacts.value=[{entry_id:'entry-a',display_name:'name-a',selected:true},{entry_id:'entry-b',display_name:'name-b',selected:false}];h.handlers.set('/api/ledger',()=>({data:{contacts:h.component.contacts.value}}));await h.component.saveLedgerSelection();const selection=h.calls.find(c=>c.url==='/api/ledger/selection');assert.deepEqual(JSON.parse(JSON.stringify(selection.data)),{selected_ids:['entry-a']});h.handlers.set('/api/ledger/batch-delete',()=>({data:{ok:true,deleted:1}}));await h.component.batchDeleteSelected();const batch=h.calls.find(c=>c.url==='/api/ledger/batch-delete');assert.deepEqual(JSON.parse(JSON.stringify(batch.data)),{entry_ids:['entry-a']});assert.equal(h.component.contacts.value.length,1);assert.equal(h.component.contacts.value[0].entry_id,'entry-b');await h.close();
 });
 await test('Legacy duplicate names are blocked instead of deleting or selecting both contacts',async()=>{
  const h=fixture();await h.connect();h.component.contacts.value=[{display_name:'duplicate',selected:true},{display_name:'duplicate',selected:false}];await h.component.deleteFriend(h.component.contacts.value[0]);await h.component.saveLedgerSelection();assert(!h.calls.some(c=>c.url==='/api/ledger/delete'||c.url==='/api/ledger/selection'));assert.equal(h.component.contacts.value.length,2);assert.notEqual(h.component.contactKey(h.component.contacts.value[0]),h.component.contactKey(h.component.contacts.value[1]));await h.close();
 });
 await test('Legacy unique names retain safe compatibility while both responsive lists key identities',async()=>{
  const h=fixture();await h.connect();h.component.contacts.value=[{display_name:'unique',selected:true}];await h.component.deleteFriend(h.component.contacts.value[0]);const call=h.calls.find(c=>c.url==='/api/ledger/delete');assert.deepEqual(JSON.parse(JSON.stringify(call.data)),{display_name:'unique'});assert(html.includes(':key="contactKey(row)"'));assert(html.includes(':key="contactKey(item)"'));assert(html.includes('同名会话'));assert(html.includes('待核对'));assert(html.includes('已拒绝'));await h.close();
 });
 await test('Manual continuation submits only the server queue and requires an eligible current status',async()=>{
  const h=fixture();await h.connect();Object.assign(h.component.status,{status_connected:true,running:false,retry_status:'manual',pending_retry_count:2});assert.equal(h.component.canContinuePending.value,true);assert.equal(await h.component.continuePending(),true);const call=h.calls.find(c=>c.url==='/api/run/pending');assert.deepEqual(JSON.parse(JSON.stringify(call.data)),{dry:false});assert.equal(h.component.pendingRetryBusy.value,false);Object.assign(h.component.status,{retry_status:'scheduled',pending_retry_count:2,status_connected:true});assert.equal(h.component.canContinuePending.value,false);assert.equal(await h.component.continuePending(),false);assert.equal(h.calls.filter(c=>c.url==='/api/run/pending').length,1);h.component.status.retry_status='manual';h.component.status.running=true;assert.equal(h.component.canContinuePending.value,false);await h.close();
 });
 console.log(`Public connection, QR and identity recovery: ${tests.length} isolated scenarios passed`);tests.forEach(name=>console.log('  PASS '+name));
})().catch(error=>{console.error(error);process.exitCode=1;});