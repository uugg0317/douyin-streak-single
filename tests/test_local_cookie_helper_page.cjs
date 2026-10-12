'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'local-cookie-helper.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const origin = 'https://panel.example.invalid';
const requestId = 'request-offline-0001';
const jobId = 'a'.repeat(32);

async function harness() {
  const messages = [], requests = [], handlers = {}, elements = {};
  let resolveStart;
  const startResponse = new Promise(resolve => { resolveStart = resolve; });
  const opener = { postMessage(message, target) { messages.push({message, target}); } };
  const document = { getElementById(id) { return elements[id] ||= {textContent:'',disabled:false,hidden:false,addEventListener(type, handler){this[type] = handler;}}; } };
  const snapshot = {request_id:requestId,job_id:jobId,status:'starting',count:0,running:true,saved_path:'C:/private/result.json'};
  const context = {document,location:{hash:'#origin='+encodeURIComponent(origin)},URLSearchParams,
    window:{opener,addEventListener(type,handler){handlers[type]=handler;}},
    setInterval(){return 1;},clearInterval(){},setTimeout,
    async fetch(url, options={}) {
      requests.push({url,options});
      if(url==='/helper/config') return {ok:true,json:async()=>({trusted_origin:origin,page_nonce:'offline-nonce',status:{status:'idle',running:false}})};
      if(url==='/helper/extract') { await startResponse; return {ok:true,json:async()=>snapshot}; }
      if(url==='/helper/cancel') return {ok:true,json:async()=>({...snapshot,status:'cancelled',running:false,error:'cancelled'})};
      throw new Error('unexpected local request');
    }};
  vm.runInNewContext(source, context);
  for(let index=0;index<6;index++) await Promise.resolve();
  return {messages,requests,handlers,elements,opener,resolveStart,snapshot};
}
function event(h,type,payload,options={}) { return {origin,source:h.opener,data:{type,payload},...options}; }
function startPayload(){return {origin,request_id:requestId,deadline:Date.now()/1000+300};}

(async()=>{
  let count=0;
  {
    const h=await harness();
    assert.equal(h.messages[0].message.type,'sparkkeeper-local-cookie-ready');
    const starting=h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload()));
    await h.handlers.message(event(h,'sparkkeeper-local-cookie-cancel',{request_id:requestId,job_id:null}));
    assert.match(h.elements.status.textContent,/取消/);
    h.resolveStart(); await starting;
    const cancel=h.requests.find(item=>item.url==='/helper/cancel');
    assert.ok(cancel,'early cancellation must be sent immediately after START resolves');
    assert.equal(JSON.parse(cancel.options.body).job_id,jobId);
    assert.equal(cancel.options.headers['X-Helper-Nonce'],'offline-nonce');
    assert.deepEqual(h.messages.filter(item=>item.message.type==='sparkkeeper-local-cookie-progress').map(item=>item.message.payload.status),['cancelled']);
    const ack=h.messages.find(item=>item.message.type==='sparkkeeper-local-cookie-ack');
    assert.equal(ack.message.payload.request_id,requestId);assert.equal(ack.message.payload.job_id,jobId);
    assert.ok(!JSON.stringify(h.messages).includes('private'));
    await h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload()));
    assert.equal(h.requests.filter(item=>item.url==='/helper/extract').length,1);
    count++;
  }
  {
    const h=await harness();
    const starting=h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload()));
    await h.handlers.message(event(h,'sparkkeeper-local-cookie-cancel',{request_id:'different-request-id',job_id:null}));
    h.resolveStart();await starting;
    assert.equal(h.requests.filter(item=>item.url==='/helper/cancel').length,0);
    assert.match(h.elements.status.textContent,/浏览器/);count++;
  }
  {
    const h=await harness();
    await h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload(),{origin:'https://stranger.example.invalid'}));
    await h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload(),{source:{}}));
    assert.equal(h.requests.filter(item=>item.url==='/helper/extract').length,0);count++;
  }
  {
    const h=await harness();
    const starting=h.handlers.message(event(h,'sparkkeeper-local-cookie-start',startPayload()));
    await h.elements.cancel.click();
    h.resolveStart();await starting;
    assert.equal(h.requests.filter(item=>item.url==='/helper/cancel').length,1);count++;
  }
  process.stdout.write(`local helper page: ${count} async regressions passed\n`);
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
