// Real QR SDK + actual page setup, with isolated HTTP/timer/blob/browser fakes.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../static/qr-login.js'),'utf8');
const index=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const TOKEN='synthetic-job-capability-not-real';
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve();};
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject};};
class Surface{constructor(){this.listeners=new Map();}addEventListener(k,v){this.listeners.set(k,v);}removeEventListener(k,v){if(this.listeners.get(k)===v)this.listeners.delete(k);}emit(k){this.listeners.get(k)?.();}}
function harness(options={}){
 let now=1700000000000,id=0,requestId=0,blobId=0,success=0;
 const calls=[],states=[],timers=new Map(),created=[],revoked=[],postHandlers=new Map(),getHandlers=new Map(),statusQueue=[];
 const window=new Surface();window.crypto={randomUUID:()=>`00000000-0000-4000-8000-${String(++requestId).padStart(12,'0')}`};
 window.open=()=>{throw Error('QR flow must not open a window');};
 class Clock extends Date{static now(){return now;}}
 class BlobURL extends URL{static createObjectURL(blob){const value='blob:isolated-'+(++blobId);created.push(value);return value;}static revokeObjectURL(value){revoked.push(value);}}
 const waiting=(extra={})=>({job_id:'job-one',job_token:TOKEN,status:'waiting',deadline:1700000300,qr_revision:1,...extra});
 const verifying=(extra={})=>waiting({status:'verification_required',verification_ready:true,verification_revision:1,verification_width:400,verification_height:200,verification_busy:false,...extra});
 const api={
  post(url,data,config={}){assert.equal(config.headers?.['X-Requested-With'],'sparkkeeper');assert.equal(config.headers?.['X-Spark-QR-Request'],'1');calls.push({method:'POST',url,data,config});const handler=postHandlers.get(url);if(handler)return Promise.resolve().then(()=>handler(data,config));if(url.endsWith('/cancel'))return Promise.resolve({data:{status:'cancelled'}});return Promise.resolve({data:waiting()});},
  get(url,config={}){calls.push({method:'GET',url,config});const handler=getHandlers.get(url);if(handler)return Promise.resolve().then(()=>handler(config));if(url.endsWith('/image')||url.endsWith('/verification-image'))return Promise.resolve({status:200,data:{type:'image/png',size:100}});return Promise.resolve({data:statusQueue.shift()||waiting()});}
 };
 vm.runInNewContext(source,{window,URL:BlobURL,Date:Clock,Uint8Array,AbortController,console:{log(){throw Error('No capability logging');}},
  setTimeout(fn,delay){const key=++id;timers.set(key,{fn,delay,at:now+delay});return key;},clearTimeout(key){timers.delete(key);}}, {filename:'qr-login.js'});
 const controller=window.SparkQrLogin.create({api,onChange:state=>states.push(JSON.parse(JSON.stringify(state))),onSuccess:()=>{success++;},...options});
 return{controller,window,calls,states,timers,created,revoked,postHandlers,getHandlers,statusQueue,waiting,verifying,get successes(){return success;},
  async start(){assert.equal(await controller.start(),true);await flush();},
  async advance(ms){const end=now+ms;let steps=0;while(true){const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;if(++steps>1000)throw Error('timer loop');now=next[1].at;timers.delete(next[0]);next[1].fn();await flush();}now=end;await flush();}
 };
}
async function verificationHarness(){
 const h=harness();h.verificationState={};
 h.postHandlers.set('/api/credentials/qr/start',()=>({data:h.verifying()}));
 h.getHandlers.set('/api/credentials/qr/status',()=>({data:h.verifying(h.verificationState)}));
 h.postHandlers.set('/api/credentials/qr/verification-action',()=>({status:202,data:h.verifying({verification_busy:true})}));
 await h.start();
 const captured=new Set();let rect={left:10,top:20,width:400,height:200};
 const element={complete:true,get src(){return h.controller.snapshot().verificationImageUrl;},get currentSrc(){return this.src;},getBoundingClientRect:()=>({...rect}),setPointerCapture:id=>captured.add(id),hasPointerCapture:id=>captured.has(id),releasePointerCapture:id=>captured.delete(id)};
 h.pointer=(x,y,t,extra={})=>({currentTarget:element,clientX:rect.left+x*rect.width,clientY:rect.top+y*rect.height,timeStamp:t,pointerId:7,isPrimary:true,isTrusted:true,button:0,preventDefault(){},...extra});
 h.setRect=value=>{rect={...rect,...value};};h.captured=captured;
 h.actions=()=>h.calls.filter(c=>c.url.endsWith('/verification-action'));
 h.verificationImages=()=>h.calls.filter(c=>c.url.endsWith('/verification-image'));
 return h;
}
const results=[];async function test(name,fn){await fn();results.push(name);}
(async()=>{
 await test('Starts once with UUID, same-origin API and mutation marker; no popup',async()=>{const h=harness();const p=h.controller.start();assert.equal(await h.controller.start(),false);await p;await flush();const start=h.calls.find(c=>c.url.endsWith('/start'));assert.deepEqual(Object.keys(start.data),['request_id']);assert.match(start.data.request_id,/^[0-9a-f-]{36}$/);assert.equal(start.config.headers['X-Spark-QR-Request'],'1');assert.equal(h.calls.filter(c=>c.url.endsWith('/start')).length,1);assert(h.controller.snapshot().imageUrl.startsWith('blob:'));h.controller.dispose();});
 await test('Job capability appears only in headers, never URL, params, body or public state',async()=>{const h=harness();await h.start();await h.advance(1500);for(const call of h.calls){assert(!call.url.includes(TOKEN));assert(!JSON.stringify(call.data||{}).includes(TOKEN));assert(!JSON.stringify(call.config.params||{}).includes(TOKEN));if(call.method==='GET')assert.equal(call.config.headers['X-QR-Job-Token'],TOKEN);}assert(!JSON.stringify(h.states).includes(TOKEN));h.controller.dispose();});
 await test('Status polls at 1.5 seconds, image refreshes at ten seconds or revision change',async()=>{const h=harness();await h.start();await h.advance(9000);assert.equal(h.calls.filter(c=>c.url.endsWith('/image')).length,1);await h.advance(1500);assert.equal(h.calls.filter(c=>c.url.endsWith('/image')).length,2);h.statusQueue.push(h.waiting({qr_revision:2}));await h.advance(1500);assert.equal(h.calls.filter(c=>c.url.endsWith('/image')).length,3);h.controller.dispose();});
 await test('Success triggers status refresh callback once, revokes image and never uploads state',async()=>{const h=harness();await h.start();h.statusQueue.push(h.waiting({status:'success'}));await h.advance(1500);assert.equal(h.successes,1);assert.equal(h.controller.snapshot().status,'success');assert.equal(h.controller.snapshot().hasJob,false);assert.equal(h.controller.snapshot().imageUrl,'');assert.equal(h.timers.size,0);assert(h.revoked.length);h.controller.dispose();await flush();assert(!h.calls.some(c=>/upload-state|cancel/.test(c.url)));});
 await test('Expired QR disappears; refreshing uses current capability and resumes polling',async()=>{const h=harness();await h.start();h.statusQueue.push(h.waiting({status:'expired'}));await h.advance(1500);assert.equal(h.controller.snapshot().imageUrl,'');assert.equal(h.controller.snapshot().canRefresh,true);assert.equal(h.timers.size,0);assert.equal(await h.controller.refresh(),true);await flush();const request=h.calls.find(c=>c.url.endsWith('/refresh'));assert.equal(request.data.job_id,'job-one');assert.equal(request.config.headers['X-QR-Job-Token'],TOKEN);assert.equal(request.config.headers['X-Spark-QR-Request'],'1');assert.equal(h.controller.snapshot().status,'waiting');h.controller.dispose();});
 await test('Official verification stays polling and returns automatically to QR without opening a window',async()=>{const h=harness();await h.start();h.statusQueue.push(h.verifying());await h.advance(1500);assert.equal(h.controller.snapshot().status,'verification_required');assert.equal(h.controller.snapshot().canRefresh,false);assert.equal(h.controller.snapshot().imageUrl,'');assert(h.controller.snapshot().verificationImageUrl);assert.equal(h.timers.size,1);assert(!h.calls.some(c=>c.url.endsWith('/verification-action')));await h.advance(1500);assert.equal(h.controller.snapshot().status,'waiting');assert.equal(h.controller.snapshot().verificationImageUrl,'');assert(h.controller.snapshot().imageUrl);h.controller.dispose();});
 await test('Cancellation aborts reads, revokes QR and sends one authenticated cancellation',async()=>{const h=harness();await h.start();assert.equal(await h.controller.cancel(),true);assert.equal(h.controller.snapshot().status,'cancelled');assert.equal(h.timers.size,0);assert.equal(h.controller.snapshot().imageUrl,'');const cancel=h.calls.find(c=>c.url.endsWith('/cancel'));assert.deepEqual(JSON.parse(JSON.stringify(cancel.data)),{job_id:'job-one'});assert.equal(cancel.config.headers['X-Spark-QR-Request'],'1');h.controller.dispose();});
 await test('Late start response after cancellation is cancelled without flashing its QR',async()=>{const h=harness(),late=defer();h.postHandlers.set('/api/credentials/qr/start',()=>late.promise);const start=h.controller.start();await flush();const cancel=h.controller.cancel();late.resolve({data:h.waiting()});assert.equal(await start,false);await cancel;await flush();assert.equal(h.controller.snapshot().status,'cancelled');assert.equal(h.created.length,0);assert(h.calls.some(c=>c.url.endsWith('/cancel')));h.controller.dispose();});
 await test('A refresh invalidates an old status success even if HTTP abort is ignored',async()=>{const h=harness();await h.start();const old=defer();h.getHandlers.set('/api/credentials/qr/status',()=>old.promise);await h.advance(1500);const oldCall=h.calls.at(-1);await h.controller.refresh();await flush();assert(oldCall.config.signal.aborted);old.resolve({data:h.waiting({status:'success'})});await flush();assert.equal(h.successes,0);assert.equal(h.controller.snapshot().status,'waiting');h.controller.dispose();});
 await test('Old image response cannot replace a refreshed QR',async()=>{const h=harness(),old=defer();h.getHandlers.set('/api/credentials/qr/image',()=>old.promise);await h.start();h.getHandlers.delete('/api/credentials/qr/image');await h.controller.refresh();await flush();const fresh=h.controller.snapshot().imageUrl;old.resolve({status:200,data:{type:'image/png',size:10}});await flush();assert.equal(h.controller.snapshot().imageUrl,fresh);assert.equal(h.created.length,1);h.controller.dispose();});
 await test('Dispose uses the same Axios client fetch adapter with keepalive and removes listeners',async()=>{const h=harness();await h.start();const before=h.states.length;h.controller.dispose();await flush();assert.equal(h.window.listeners.size,0);assert.equal(h.timers.size,0);assert.equal(h.states.length,before);const cancel=h.calls.find(c=>c.url.endsWith('/cancel'));assert.equal(cancel.config.adapter,'fetch');assert.equal(cancel.config.fetchOptions.keepalive,true);assert.equal(cancel.config.withCredentials,undefined);assert.equal(cancel.config.headers['X-QR-Job-Token'],TOKEN);});
 await test('Dispose during setup cancels its eventual handle without updating unmounted UI',async()=>{const h=harness(),late=defer();h.postHandlers.set('/api/credentials/qr/start',()=>late.promise);const p=h.controller.start();await flush();h.controller.dispose();const updates=h.states.length;late.resolve({data:h.waiting()});await p;await flush();assert.equal(h.states.length,updates);assert(h.calls.some(c=>c.url.endsWith('/cancel')&&c.config.fetchOptions.keepalive));});
 await test('Failed cancellation retains capability for retry instead of abandoning the job',async()=>{const h=harness();await h.start();h.postHandlers.set('/api/credentials/qr/cancel',()=>Promise.reject(Error('offline')));assert.equal(await h.controller.cancel(),false);assert.equal(h.controller.snapshot().hasJob,true);h.postHandlers.delete('/api/credentials/qr/cancel');assert.equal(await h.controller.cancel(),true);h.controller.dispose();});
 await test('Save success racing with cancellation wins and refreshes state',async()=>{const h=harness();await h.start();h.postHandlers.set('/api/credentials/qr/cancel',()=>({data:{status:'success'}}));await h.controller.cancel();await flush();assert.equal(h.successes,1);assert.equal(h.controller.snapshot().status,'success');h.controller.dispose();});
 await test('Non-image payload never becomes a QR object URL and retries stay throttled',async()=>{const h=harness();h.getHandlers.set('/api/credentials/qr/image',()=>({status:200,data:{type:'application/json',size:10}}));await h.start();await h.advance(9000);assert.equal(h.created.length,0);assert.equal(h.calls.filter(c=>c.url.endsWith('/image')).length,1);h.controller.dispose();});
 await test('Current page loads the QR SDK, retains manual upload, and removes old popup instructions',async()=>{assert(index.includes('/static/qr-login.js'));assert(!index.includes('/static/local-cookie-bridge.js'));assert(index.includes('@click="startQrLogin"'));assert(index.includes('@click="refreshQrLogin"'));assert(index.includes('@click="cancelQrLogin"'));assert(index.includes(':src="qrLogin.imageUrl"'));assert(index.includes('ref="credentialFileInput"'));assert(!index.includes('启动本地Cookie助手.bat'));const integration=index.slice(index.indexOf('function getQrLoginController'),index.indexOf('function startQrLogin'));assert(integration.includes('await loadStatus()'));assert(!/upload-state|FormData|window\.open/.test(integration));assert(!/localStorage|sessionStorage|window\.open|document\.cookie|job_token\s*[:=].*URL/.test(source));});
 await test('Actual Vue setup registers new actions and unmount cleanup without old extraction variables',async()=>{const script=[...index.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m=>!/\bsrc\s*=/.test(m[1])&&m[2].includes('const { createApp'));const hooks={before:[],unmounted:[]};let component;const Vue={ref:v=>({value:v}),reactive:v=>v,computed:fn=>({get value(){return fn()}}),watch(){},nextTick:()=>Promise.resolve(),onMounted(){},onUnmounted:fn=>hooks.unmounted.push(fn),onBeforeUnmount:fn=>hooks.before.push(fn),createApp(options){component=options.setup();return{use(){return this;},mount(){}}}};const window={innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){}};vm.runInNewContext(script[2],{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{},axios:{create:()=>({interceptors:{response:{use(){}}}})},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){}},{filename:'actual-index-setup.js'});assert.equal(typeof component.startQrLogin,'function');assert.equal(typeof component.refreshQrLogin,'function');assert.equal(typeof component.cancelQrLogin,'function');assert.equal(typeof component.handleFileUpload,'function');assert(!('startExtract' in component));assert.equal(component.qrLogin.status,'idle');assert.equal(hooks.before.length,1);hooks.before.forEach(fn=>fn());});
 await test('Console reset disposes its QR controller once and clears the presentation without server logout',async()=>{
  const script=[...index.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/g)].find(m=>!/\bsrc\s*=/.test(m[1])&&m[2].includes('const { createApp'));
  const watchers=new Map(),calls=[];let component,qrOptions,disposed=false,disposeCount=0;
  const controller={start(){qrOptions.onChange({status:'waiting',running:true,hasJob:true});},cancel(){throw Error('Reset uses capability-aware dispose cleanup');},dispose(){if(!disposed){disposed=true;disposeCount++;}}};
  const Vue={ref(initial){let value=initial;const ref={};Object.defineProperty(ref,'value',{get(){return value;},set(next){if(value===next)return;value=next;for(const fn of watchers.get(ref)||[])fn(next);}});return ref;},reactive:v=>v,computed:fn=>({get value(){return fn();}}),watch(ref,fn){watchers.set(ref,[...(watchers.get(ref)||[]),fn]);},nextTick:()=>Promise.resolve(),onMounted(){},onUnmounted(){},onBeforeUnmount(){},createApp(options){component=options.setup();return{use(){return this;},mount(){}};}};
  const window={innerWidth:1280,location:{origin:'http://127.0.0.1:8000',href:'http://127.0.0.1:8000/',search:'',hash:''},history:{replaceState(){}},matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),addEventListener(){},removeEventListener(){},SparkQrLogin:{create(options){qrOptions=options;return controller;}}};
  const api={interceptors:{response:{use(){}}},post:async url=>{calls.push(url);return{data:{ok:true}};}};
  vm.runInNewContext(script[2],{Vue,window,document:{getElementById:()=>({style:{}}),querySelector:()=>null},ElementPlus:{},axios:{create:()=>api},URL,URLSearchParams,console:{error(){},log(){}},localStorage:{removeItem(){},getItem(){return null}},setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){}},{filename:'actual-index-logout.js'});
  component.authed.value=true;component.startQrLogin();await component.resetConsole();
  assert.equal(disposeCount,1);assert(!calls.some(url=>url.startsWith('/api/auth/')));assert.equal(component.authed.value,true);assert.equal(component.qrLogin.hasJob,false);assert.equal(component.qrLogin.running,false);assert.equal(component.qrLogin.imageUrl,'');
 });
 await test('Bundled Axios actually supports authenticated fetch-adapter keepalive cancellation',async()=>{
  const previousFetch=global.fetch;let captured,intercepted=0;
  global.fetch=async(request,options)=>{captured={request,options};return new Response('{}',{status:200,headers:{'Content-Type':'application/json'}});};
  try{
   const axios=require('../static/vendor/axios.min.js');const api=axios.create();api.interceptors.response.use(response=>{intercepted++;return response;});
   await api.post('http://127.0.0.1:8000/api/credentials/qr/cancel',{job_id:'synthetic'},{adapter:'fetch',fetchOptions:{keepalive:true},withCredentials:true,headers:{'X-QR-Job-Token':TOKEN,'X-Spark-QR-Request':'1'}});
   assert(captured);assert(captured.request.keepalive||captured.options?.keepalive);assert.equal(captured.request.credentials,'include');assert.equal(captured.request.headers.get('X-QR-Job-Token'),TOKEN);assert.equal(intercepted,1);
  }finally{global.fetch=previousFetch;}
 });
 await test('Verification screenshots refresh at 1.5 seconds and never reveal a task capability',async()=>{
  const h=await verificationHarness();assert.equal(h.verificationImages().length,1);await h.advance(3000);assert.equal(h.verificationImages().length,3);assert.equal(h.actions().length,0);
  for(const call of h.verificationImages()){assert.equal(call.config.headers['X-QR-Job-Token'],TOKEN);assert(!JSON.stringify(call.config.params).includes(TOKEN));}assert(!JSON.stringify(h.states).includes(TOKEN));h.controller.dispose();
 });
 await test('A real click freezes screenshots through the queued action until a newer completed revision',async()=>{
  const h=await verificationHarness(),before=h.controller.snapshot().verificationImageUrl;
  assert.equal(h.controller.verificationPointerDown(h.pointer(.25,.5,10)),true);await h.advance(1500);assert.equal(h.verificationImages().length,1);assert.equal(h.controller.snapshot().verificationImageUrl,before);
  assert.equal(await h.controller.verificationPointerUp(h.pointer(.25,.5,110)),true);
  const action=h.actions()[0];assert.equal(action.data.action,'click');assert.equal(action.data.points.length,1);assert.equal(action.data.points[0].x,.25);assert.equal(action.data.points[0].y,.5);assert.equal(action.data.verification_revision,1);assert.match(action.data.action_id,/^[0-9a-f-]{36}$/);assert.equal(action.config.headers['X-Spark-QR-Request'],'1');assert.equal(action.config.headers['X-QR-Job-Token'],TOKEN);assert(!JSON.stringify(action.data).includes(TOKEN));
  assert.equal(h.controller.snapshot().interactionPending,true);assert.equal(h.controller.verificationPointerDown(h.pointer(.3,.5,120)),false);await h.advance(1500);assert.equal(h.controller.snapshot().interactionPending,true);assert.equal(h.verificationImages().length,1);
  h.verificationState={verification_revision:2,verification_busy:false};await h.advance(1500);assert.equal(h.controller.snapshot().interactionPending,false);assert.equal(h.controller.snapshot().verificationRevision,2);assert.equal(h.actions().length,1);assert(!h.captured.size);h.controller.dispose();
 });
 await test('Dragging forwards only actual sampled pointer points, bounded to 64 and three seconds',async()=>{
  const h=await verificationHarness();const actual=new Set();const record=(x,y,t)=>{actual.add(`${x},${y},${t}`);return h.pointer(x,y,t);};
  h.controller.verificationPointerDown(record(.1,.5,0));for(let i=1;i<=120;i++)h.controller.verificationPointerMove(record(.1+i/200,.5,i*10));await h.controller.verificationPointerUp(record(.8,.5,1250));
  const data=h.actions()[0].data;assert.equal(data.action,'drag');assert.equal(data.points.length,64);assert.equal(data.points[0].t,0);assert.equal(data.points.at(-1).t,1250);assert.equal(data.points.at(-1).x,.8);assert(data.points.every(p=>p.x>=0&&p.x<=1&&p.y>=0&&p.y<=1&&p.t<=3000));
  // Input x values round through CSS pixels; timestamps still identify genuine observations.
  const times=new Set([0,1250,...Array.from({length:120},(_,i)=>(i+1)*10)]);assert(data.points.every(p=>times.has(p.t)));assert(!h.captured.size);h.controller.dispose();
 });
 await test('A late screenshot received during a gesture never changes the frozen image',async()=>{
  const h=await verificationHarness(),late=defer();h.getHandlers.set('/api/credentials/qr/verification-image',()=>late.promise);await h.advance(1500);
  const call=h.verificationImages().at(-1),before=h.controller.snapshot().verificationImageUrl;h.controller.verificationPointerDown(h.pointer(.3,.4,0));assert(call.config.signal.aborted);late.resolve({status:200,data:{type:'image/png',size:30}});await flush();assert.equal(h.controller.snapshot().verificationImageUrl,before);h.controller.verificationPointerCancel(h.pointer(.3,.4,10));h.controller.dispose();
 });
 await test('Cancelled gestures and invalid coordinates never submit an action',async()=>{
  const h=await verificationHarness();assert.equal(h.controller.verificationPointerDown(h.pointer(.1,.5,0,{isTrusted:false})),false);
  h.controller.verificationPointerDown(h.pointer(.1,.5,0));h.controller.verificationPointerCancel(h.pointer(.1,.5,20));await flush();assert.equal(h.actions().length,0);assert.equal(h.controller.snapshot().interactionPending,false);
  h.controller.verificationPointerDown(h.pointer(.1,.5,0));h.controller.verificationPointerMove(h.pointer(1.1,.5,100));await flush();assert.equal(h.actions().length,0);assert.equal(h.controller.snapshot().gestureActive,false);
  h.controller.verificationPointerDown(h.pointer(.1,.5,0));await h.controller.verificationPointerUp(h.pointer(.5,.5,3001));assert.equal(h.actions().length,0);h.controller.dispose();
 });
 await test('Layout changes during a captured drag cancel it rather than mapping onto a different box',async()=>{
  const h=await verificationHarness();h.controller.verificationPointerDown(h.pointer(.2,.5,0));h.setRect({width:200});h.controller.verificationPointerMove(h.pointer(.7,.5,100));await flush();assert.equal(h.controller.snapshot().interactionPending,false);assert.equal(h.actions().length,0);h.controller.dispose();
 });
 await test('A rejected stale action is not automatically retried and new screenshots allow an explicit retry',async()=>{
  const h=await verificationHarness();h.postHandlers.set('/api/credentials/qr/verification-action',()=>Promise.reject({response:{status:400,data:{detail:'验证图片已变化，请重新操作。'}}}));
  h.controller.verificationPointerDown(h.pointer(.2,.3,0));assert.equal(await h.controller.verificationPointerUp(h.pointer(.2,.3,100)),false);assert.equal(h.controller.snapshot().interactionPending,false);assert.match(h.controller.snapshot().error,/图片已变化/);await h.advance(6000);assert.equal(h.actions().length,1);h.controller.dispose();
 });
 await test('An uncertain POST is never replayed; a fresh idle status releases the local freeze',async()=>{
  const h=await verificationHarness();h.postHandlers.set('/api/credentials/qr/verification-action',()=>Promise.reject(Error('response lost')));
  h.controller.verificationPointerDown(h.pointer(.2,.3,0));await h.controller.verificationPointerUp(h.pointer(.2,.3,100));assert.equal(h.controller.snapshot().interactionPending,true);await h.advance(1500);assert.equal(h.controller.snapshot().interactionPending,false);await h.advance(6000);assert.equal(h.actions().length,1);h.controller.dispose();
 });
 await test('Cancellation discards the gesture and a late action response cannot revive verification',async()=>{
  const h=await verificationHarness(),late=defer();h.postHandlers.set('/api/credentials/qr/verification-action',()=>late.promise);
  h.controller.verificationPointerDown(h.pointer(.2,.3,0));const submitted=h.controller.verificationPointerUp(h.pointer(.2,.3,100));await flush();await h.controller.cancel();late.resolve({status:202,data:h.verifying({verification_busy:true})});await submitted;await flush();
  assert.equal(h.controller.snapshot().status,'cancelled');assert.equal(h.controller.snapshot().verificationImageUrl,'');assert.equal(h.controller.snapshot().interactionPending,false);assert(!h.captured.size);assert.equal(h.actions().length,1);h.controller.dispose();
 });
 await test('Leaving verification during pointer capture clears the trace without posting it',async()=>{
  const h=await verificationHarness();h.controller.verificationPointerDown(h.pointer(.2,.3,0));h.getHandlers.set('/api/credentials/qr/status',()=>({data:h.waiting()}));await h.advance(1500);
  assert.equal(h.controller.snapshot().status,'waiting');assert.equal(h.controller.snapshot().verificationImageUrl,'');assert.equal(h.controller.snapshot().interactionPending,false);assert.equal(await h.controller.verificationPointerUp(h.pointer(.7,.3,200)),false);assert.equal(h.actions().length,0);h.controller.dispose();
 });
 await test('Backend action errors are shown after completion without replaying the action',async()=>{
  const h=await verificationHarness();h.controller.verificationPointerDown(h.pointer(.2,.3,0));await h.controller.verificationPointerUp(h.pointer(.2,.3,100));h.verificationState={verification_revision:2,verification_busy:false,verification_error:'验证区域已变化，请重新操作。'};await h.advance(1500);assert.equal(h.controller.snapshot().interactionPending,false);assert.match(h.controller.snapshot().error,/区域已变化/);assert.equal(h.actions().length,1);h.controller.dispose();
 });
 await test('The real page binds only pointer actions to an in-frame image with no keyboard relay',async()=>{
  for(const event of ['pointerdown','pointermove','pointerup','pointercancel','lostpointercapture'])assert(index.includes('@'+event+'="qrVerificationPointer'));
  assert(index.includes(':src="qrLogin.verificationImageUrl"'));assert(index.includes('touch-action:none'));assert(index.includes('请在图中点击或拖动完成官方验证，通过后会自动显示二维码'));
  assert(!/keyboard\.type|keyboard\.press|verification-(?:key|text)|window\.open/.test(source));
 });
 await test('Refresh failure preserves the capability and resumes status reads without replaying POST',async()=>{
  const h=harness();await h.start();h.postHandlers.set('/api/credentials/qr/refresh',()=>Promise.reject({response:{status:503,data:{detail:'模拟刷新响应丢失'}}}));
  assert.equal(await h.controller.refresh(),false);assert.equal(h.controller.snapshot().status,'sync_pending');assert.equal(h.controller.snapshot().hasJob,true);assert.equal(h.controller.snapshot().canRetrySync,true);assert.equal(h.controller.snapshot().canRefresh,false);assert.equal(h.timers.size,1);
  h.statusQueue.push(h.waiting({status:'success',saved:true,cleanup_complete:true}));await h.advance(1500);
  assert.equal(h.successes,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/refresh')).length,1);assert.equal(h.controller.snapshot().hasJob,false);h.controller.dispose();
 });
 await test('QR image errors survive ordinary status polls and only successful image retry clears them',async()=>{
  const h=harness();h.getHandlers.set('/api/credentials/qr/image',()=>Promise.reject(Error('offline image')));await h.start();const message=h.controller.snapshot().imageError;assert(message);assert.equal(h.controller.snapshot().canRetryImage,true);
  await h.advance(1500);assert.equal(h.controller.snapshot().imageError,message);assert(h.controller.snapshot().error.includes(message));assert.equal(h.controller.snapshot().imageUrl,'');
  h.getHandlers.delete('/api/credentials/qr/image');assert.equal(await h.controller.retryImage(),true);assert.equal(h.controller.snapshot().imageError,'');assert.equal(h.controller.snapshot().error,'');assert(h.controller.snapshot().imageUrl);assert.equal(h.calls.filter(c=>c.method==='POST').length,1);h.controller.dispose();
 });
 await test('Status retry clears status errors independently of a persistent image error',async()=>{
  const h=harness();h.getHandlers.set('/api/credentials/qr/image',()=>Promise.reject(Error('offline image')));await h.start();h.getHandlers.set('/api/credentials/qr/status',()=>Promise.reject(Error('offline status')));await h.advance(1500);
  assert(h.controller.snapshot().statusError);assert(h.controller.snapshot().imageError);assert.equal(h.controller.snapshot().canRetrySync,true);
  h.getHandlers.delete('/api/credentials/qr/status');assert.equal(await h.controller.retrySync(),true);assert.equal(h.controller.snapshot().statusError,'');assert(h.controller.snapshot().imageError);assert.equal(h.controller.snapshot().canRetrySync,false);h.controller.dispose();
 });
 await test('Verification image errors survive status updates and retry only the verification picture',async()=>{
  const h=await verificationHarness();h.getHandlers.set('/api/credentials/qr/verification-image',()=>Promise.reject(Error('offline verification picture')));await h.advance(1500);const error=h.controller.snapshot().verificationImageError;assert(error);await h.advance(1500);assert.equal(h.controller.snapshot().verificationImageError,error);
  h.getHandlers.delete('/api/credentials/qr/verification-image');assert.equal(await h.controller.retryImage(),true);assert.equal(h.controller.snapshot().verificationImageError,'');assert(h.controller.snapshot().verificationImageUrl);assert.equal(h.actions().length,0);h.controller.dispose();
 });
 await test('Explicit sync retry supersedes old status reads and keeps one polling timer',async()=>{
  const h=harness();await h.start();const late=defer();h.getHandlers.set('/api/credentials/qr/status',()=>late.promise);await h.advance(1500);h.getHandlers.set('/api/credentials/qr/status',()=>({data:h.waiting({qr_revision:2})}));
  assert.equal(await h.controller.retrySync(),true);late.resolve({data:h.waiting({status:'success'})});await flush();assert.equal(h.controller.snapshot().status,'waiting');assert.equal(h.successes,0);assert.equal(h.timers.size,1);h.controller.dispose();
 });
 await test('Cleanup pending retains the job and polls until cleanup is actually complete',async()=>{
  const h=harness();h.postHandlers.set('/api/credentials/qr/start',()=>({data:h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false})}));await h.start();
  assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.controller.snapshot().saved,true);assert.equal(h.controller.snapshot().cleanupComplete,false);assert.equal(h.controller.snapshot().hasJob,true);assert.equal(h.controller.snapshot().running,true);assert.equal(h.controller.snapshot().canRetryCleanup,true);assert.equal(h.successes,0);assert.equal(await h.controller.start(),false);assert.equal(await h.controller.refresh(),false);
  h.statusQueue.push(h.waiting({status:'success',saved:true,cleanup_complete:true}));await h.advance(1500);assert.equal(h.successes,1);assert.equal(h.controller.snapshot().hasJob,false);h.controller.dispose();
 });
 await test('Success without confirmed cleanup remains pending and explicit cleanup retry uses authenticated POST once',async()=>{
  const h=harness();h.postHandlers.set('/api/credentials/qr/start',()=>({data:h.waiting({status:'success',saved:true,cleanup_complete:false})}));h.postHandlers.set('/api/credentials/qr/retry-cleanup',()=>({data:h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false})}));await h.start();
  assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.successes,0);assert.equal(await h.controller.retryCleanup(),true);
  const action=h.calls.find(c=>c.url.endsWith('/retry-cleanup'));assert.equal(action.data.job_id,'job-one');assert.equal(action.config.headers['X-QR-Job-Token'],TOKEN);assert.equal(action.config.headers['X-Spark-QR-Request'],'1');
  h.statusQueue.push(h.waiting({status:'success',saved:true,cleanup_complete:true}));await h.advance(1500);assert.equal(h.successes,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,1);h.controller.dispose();
 });
 await test('Failed cleanup retry keeps status polling and never automatically repeats its mutation',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:false,cleanup_complete:false});h.postHandlers.set('/api/credentials/qr/start',()=>({data:pending}));h.getHandlers.set('/api/credentials/qr/status',()=>({data:pending}));h.postHandlers.set('/api/credentials/qr/retry-cleanup',()=>Promise.reject(Error('response lost')));await h.start();
  assert.equal(await h.controller.retryCleanup(),false);assert.equal(h.controller.snapshot().hasJob,true);assert.equal(h.controller.snapshot().running,true);assert.equal(h.controller.snapshot().canRetrySync,true);await h.advance(6000);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,1);assert.equal(h.controller.snapshot().status,'cleanup_pending');h.controller.dispose();
 });
 await test('Cleanup retry and cancellation cannot issue simultaneous mutations',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false}),late=defer();h.postHandlers.set('/api/credentials/qr/start',()=>({data:pending}));h.postHandlers.set('/api/credentials/qr/retry-cleanup',()=>late.promise);await h.start();const action=h.controller.retryCleanup();await flush();
  assert.equal(await h.controller.retryCleanup(),false);assert.equal(await h.controller.cancel(),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/cancel')).length,0);late.resolve({data:pending});assert.equal(await action,true);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,1);h.controller.dispose();
 });
 await test('Image retries cannot interrupt a genuine verification gesture or replay its action',async()=>{
  const h=await verificationHarness();h.controller.verificationPointerDown(h.pointer(.3,.4,0));const count=h.verificationImages().length;assert.equal(await h.controller.retryImage(),false);assert.equal(h.verificationImages().length,count);assert.equal(h.actions().length,0);h.controller.verificationPointerCancel(h.pointer(.3,.4,100));h.controller.dispose();
 });
 await test('Failed cancellation keeps querying the server instead of abandoning its capability',async()=>{
  const h=harness();await h.start();h.postHandlers.set('/api/credentials/qr/cancel',()=>Promise.reject(Error('response lost')));assert.equal(await h.controller.cancel(),false);assert.equal(h.controller.snapshot().canRetrySync,true);h.statusQueue.push(h.waiting({status:'success',saved:true,cleanup_complete:true}));await h.advance(1500);assert.equal(h.successes,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/cancel')).length,1);h.controller.dispose();
 });
 await test('Cleanup retry invalidates older status responses even when abort is ignored',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:false,cleanup_complete:false}),late=defer();h.postHandlers.set('/api/credentials/qr/start',()=>({data:pending}));await h.start();h.getHandlers.set('/api/credentials/qr/status',()=>late.promise);await h.advance(1500);h.postHandlers.set('/api/credentials/qr/retry-cleanup',()=>({data:pending}));
  assert.equal(await h.controller.retryCleanup(),true);late.resolve({data:h.waiting()});await flush();assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.controller.snapshot().canRefresh,false);assert.equal(h.timers.size,1);h.controller.dispose();
 });
 await test('Invalid image responses remain visibly retryable without creating object URLs',async()=>{
  const h=harness();h.getHandlers.set('/api/credentials/qr/image',()=>({status:200,data:{type:'application/json',size:10}}));await h.start();assert(h.controller.snapshot().imageError);assert.equal(h.controller.snapshot().canRetryImage,true);await h.advance(1500);assert(h.controller.snapshot().imageError);assert.equal(h.created.length,0);h.controller.dispose();
 });
 await test('A failed post-save refresh callback retains its useful warning',async()=>{
  const h=harness({onSuccess:()=>Promise.reject(Error('isolated refresh failure'))});await h.start();h.statusQueue.push(h.waiting({status:'success'}));await h.advance(1500);assert.equal(h.controller.snapshot().status,'success');assert.match(h.controller.snapshot().statusError,/凭证已保存.*页面状态刷新失败/);assert.match(h.controller.snapshot().error,/页面状态刷新失败/);h.controller.dispose();
 });
 await test('Known cleanup pending cannot be dismissed by missing cleanup fields or a 404 status error',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false});h.postHandlers.set('/api/credentials/qr/start',()=>({data:pending}));await h.start();h.getHandlers.set('/api/credentials/qr/status',()=>Promise.reject({response:{status:404,data:{detail:'模拟任务暂时不可查'}}}));await h.advance(1500);
  assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.controller.snapshot().hasJob,true);assert.equal(h.controller.snapshot().canRetrySync,true);assert.equal(await h.controller.start(),false);
  h.getHandlers.set('/api/credentials/qr/status',()=>({data:{status:'success',saved:true}}));await h.controller.retrySync();assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.successes,0);
  h.getHandlers.set('/api/credentials/qr/status',()=>({data:{status:'success',saved:true,cleanup_complete:true}}));await h.controller.retrySync();await flush();assert.equal(h.successes,1);h.controller.dispose();
 });
 await test('Server cleanup retrying disables further cleanup mutations until the worker is idle',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:false,cleanup_complete:false});h.postHandlers.set('/api/credentials/qr/start',()=>({data:pending}));h.postHandlers.set('/api/credentials/qr/retry-cleanup',()=>({data:{...pending,cleanup_retrying:true}}));await h.start();await h.controller.retryCleanup();
  assert.equal(h.controller.snapshot().cleanupRetrying,true);assert.equal(h.controller.snapshot().canRetryCleanup,false);assert.equal(await h.controller.retryCleanup(),false);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,1);
  h.getHandlers.set('/api/credentials/qr/status',()=>({data:{...pending,cleanup_retrying:false}}));await h.controller.retrySync();assert.equal(h.controller.snapshot().cleanupRetrying,false);assert.equal(h.controller.snapshot().canRetryCleanup,true);h.controller.dispose();
 });
 await test('A fresh controller recovers one cleanup-only capability after start returns 409',async()=>{
  const h=harness(),pending=h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false,cleanup_retrying:false});h.postHandlers.set('/api/credentials/qr/start',()=>Promise.reject({response:{status:409,data:{detail:'上次浏览器清理尚未完成'}}}));h.postHandlers.set('/api/credentials/qr/recover-cleanup',()=>({data:pending}));await h.start();
  assert.equal(h.controller.snapshot().status,'cleanup_pending');assert.equal(h.controller.snapshot().hasJob,true);assert.equal(h.controller.snapshot().canRetryCleanup,true);assert.equal(h.successes,0);
  const recover=h.calls.find(c=>c.url.endsWith('/recover-cleanup'));assert.deepEqual(JSON.parse(JSON.stringify(recover.data)),{});assert.equal(recover.config.headers['X-Spark-QR-Request'],'1');assert.equal(recover.config.headers['X-QR-Job-Token'],undefined);assert.equal(recover.config.withCredentials,undefined);assert.equal(h.calls.filter(c=>c.url.endsWith('/recover-cleanup')).length,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,0);assert(!JSON.stringify(h.states).includes(TOKEN));h.controller.dispose();
 });
 await test('No recoverable cleanup task preserves the original start error without a retry loop',async()=>{
  const h=harness();h.postHandlers.set('/api/credentials/qr/start',()=>Promise.reject({response:{status:409,data:{detail:'模拟发送任务正在运行'}}}));h.postHandlers.set('/api/credentials/qr/recover-cleanup',()=>Promise.reject({response:{status:409,data:{detail:'没有可恢复的浏览器清理任务'}}}));assert.equal(await h.controller.start(),false);await h.advance(6000);
  assert.equal(h.controller.snapshot().status,'failed');assert.match(h.controller.snapshot().message,/模拟发送任务正在运行/);assert.equal(h.controller.snapshot().hasJob,false);assert.equal(h.calls.filter(c=>c.url.endsWith('/recover-cleanup')).length,1);assert.equal(h.timers.size,0);h.controller.dispose();
 });
 await test('Unexpected non-cleanup recovery cannot adopt a normal login task',async()=>{
  const h=harness();h.postHandlers.set('/api/credentials/qr/start',()=>Promise.reject({response:{status:409,data:{detail:'模拟后台任务忙'}}}));h.postHandlers.set('/api/credentials/qr/recover-cleanup',()=>({data:h.waiting()}));assert.equal(await h.controller.start(),false);assert.equal(h.controller.snapshot().hasJob,false);assert.equal(h.controller.snapshot().status,'failed');assert.match(h.controller.snapshot().message,/模拟后台任务忙/);assert.equal(h.created.length,0);h.controller.dispose();
 });
 await test('Cancellation during cleanup recovery owns the late capability and never shows a QR',async()=>{
  const h=harness(),late=defer(),pending=h.waiting({status:'cleanup_pending',saved:true,cleanup_complete:false});h.postHandlers.set('/api/credentials/qr/start',()=>Promise.reject({response:{status:409,data:{detail:'模拟清理未完成'}}}));h.postHandlers.set('/api/credentials/qr/recover-cleanup',()=>late.promise);const start=h.controller.start();await flush();const cancel=h.controller.cancel();late.resolve({data:pending});assert.equal(await start,false);await cancel;await flush();
  assert.equal(h.controller.snapshot().status,'cancelled');assert.equal(h.created.length,0);assert.equal(h.calls.filter(c=>c.url.endsWith('/cancel')).length,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/retry-cleanup')).length,0);h.controller.dispose();
 });
 await test('Disposal during cleanup recovery cancels its late handle without updating an unmounted page',async()=>{
  const h=harness(),late=defer(),pending=h.waiting({status:'cleanup_pending',saved:false,cleanup_complete:false});h.postHandlers.set('/api/credentials/qr/start',()=>Promise.reject({response:{status:409,data:{detail:'模拟清理未完成'}}}));h.postHandlers.set('/api/credentials/qr/recover-cleanup',()=>late.promise);const start=h.controller.start();await flush();h.controller.dispose();const before=h.states.length;late.resolve({data:pending});await start;await flush();assert.equal(h.states.length,before);assert.equal(h.created.length,0);const cancel=h.calls.find(c=>c.url.endsWith('/cancel'));assert(cancel.config.fetchOptions.keepalive);assert.equal(h.calls.filter(c=>c.url.endsWith('/recover-cleanup')).length,1);
 });
 await test('Cancellation before the initial 409 prevents a new recovery request',async()=>{
  const h=harness(),late=defer();h.postHandlers.set('/api/credentials/qr/start',()=>late.promise);const start=h.controller.start();await flush();const cancel=h.controller.cancel();late.reject({response:{status:409,data:{detail:'模拟清理未完成'}}});await start;await cancel;await flush();assert.equal(h.calls.filter(c=>c.url.endsWith('/recover-cleanup')).length,0);assert.equal(h.controller.snapshot().status,'cancelled');h.controller.dispose();
 });
 console.log(`QR frontend: ${results.length} isolated scenarios passed`);results.forEach(name=>console.log('  PASS '+name));
})().catch(error=>{console.error(error);process.exitCode=1;});
