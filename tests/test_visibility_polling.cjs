const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const base=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const scope=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(base.slice(0,base.indexOf('const forbidden='))+';globalThis.makeFixture=fixture;',scope);
const tests=[];
function test(name,fn){tests.push({name,fn});}
test('Hidden console performs no periodic status requests and resumes one fresh read',async()=>{
 const h=scope.makeFixture();await h.connect();h.calls.length=0;await h.setHidden(true);await h.advance(60000);
 assert.equal(h.calls.filter(c=>c.url==='/api/status').length,0);
 await h.setHidden(false);assert.equal(h.calls.filter(c=>c.url==='/api/status').length,1);
 await h.advance(10000);assert.equal(h.calls.filter(c=>c.url==='/api/status').length,2);await h.close();
});
test('Visibility changes coalesce with an existing request and preserve selection drafts',async()=>{
 const h=scope.makeFixture();await h.connect();let resolve;const waiting=new Promise(r=>resolve=r);
 h.handlers.set('/api/status',()=>waiting);const read=h.component.loadStatus();await Promise.resolve();h.calls.length=0;
 h.component.contacts.value=[{entry_id:'draft',display_name:'合成草稿',selected:true}];
 await h.setHidden(true);await h.setHidden(false);await h.setHidden(false);
 assert.equal(h.calls.filter(c=>c.url==='/api/status').length,0);
 resolve({data:{background_tasks_enabled:false}});await read;
 assert.equal(h.component.contacts.value[0].selected,true);await h.close();
});
test('Unmount removes visibility handler and cannot restart polling',async()=>{
 const h=scope.makeFixture();await h.connect();assert(h.docListeners.has('visibilitychange'));await h.close();
 assert.equal(h.docListeners.has('visibilitychange'),false);h.calls.length=0;await h.setHidden(false);await h.advance(60000);
 assert.equal(h.calls.length,0);
});
(async()=>{for(const t of tests){await t.fn();console.log('PASS '+t.name);}console.log(JSON.stringify({passed:tests.length,failed:0}));})().catch(e=>{console.error(e);process.exitCode=1;});
