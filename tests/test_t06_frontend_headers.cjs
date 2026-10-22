// T06 验证实际页面默认头、独立QR SDK与bundled Axios运输，不发起网络请求。
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const baseline=fs.readFileSync(path.join(__dirname,'test_public_qr_frontend.cjs'),'utf8');
const context=vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout});
vm.runInContext(baseline.slice(0,baseline.indexOf('const cases=[];'))+';globalThis.t06Fixture=fixture;globalThis.t06Flush=flush;',context);
const fixture=context.t06Fixture,flush=context.t06Flush;
const axiosSource=fs.readFileSync(path.join(__dirname,'../static/vendor/axios.min.js'),'utf8');
function browserAxios(defaults){
 const records=[];
 const location={href:'http://127.0.0.1:8000/',origin:'http://127.0.0.1:8000'};
 const scope={window:{location,addEventListener(){},removeEventListener(){}},document:{cookie:''},navigator:{userAgent:'isolated-fixture'},
  URL,URLSearchParams,Request,Response,Headers,FormData,Blob,File,AbortController,ReadableStream,TextEncoder,TextDecoder,Uint8Array,
  setTimeout,clearTimeout,queueMicrotask,console:{warn(){},error(){},log(){}},
  fetch:async(request,options)=>{assert.equal(new URL(request.url).origin,location.origin);records.push({request,options});return new Response('{"ok":true}',{status:200,headers:{'Content-Type':'application/json'}});}};
 vm.createContext(scope);vm.runInContext(axiosSource,scope,{filename:'actual-bundled-axios.js'});
 return{api:scope.axios.create(defaults),records};
}
const tests=[];async function test(name,run){await run();tests.push(name);}
(async()=>{
 await test('页面Axios默认marker和连接epoch适用于无密码状态读取',async()=>{
  const h=fixture();await flush();assert.equal(h.api.defaults.headers['X-Requested-With'],'sparkkeeper');await h.component.connectConsole();const call=h.calls.find(c=>c.url==='/api/status');assert(!h.calls.some(c=>c.url.startsWith('/api/auth/')));assert(call);assert.equal(call.config.headers['X-Requested-With'],'sparkkeeper');assert.equal(typeof call.config.sparkManagementEpoch,'number');await h.close();
 });
 await test('实际上传处理器的FormData请求不丢默认marker或请求独立Content-Type',async()=>{
  const h=fixture();await h.connect();const file=new File(['{"cookies":[]}'],'synthetic-state.json',{type:'application/json'});await h.component.handleFileUpload({target:{files:[file]}});const call=h.calls.find(c=>c.url==='/api/upload-state');assert(call);assert(call.data instanceof FormData);assert.equal(call.config.headers['X-Requested-With'],'sparkkeeper');assert.equal(call.config.headers['Content-Type'],'multipart/form-data');assert.equal(call.data.get('file').name,'synthetic-state.json');await h.close();
 });
 await test('bundled Axios JSON默认头和每请求头实际合并后交给fetch',async()=>{
  const h=fixture(),b=browserAxios({headers:h.api.defaults.headers});await b.api.post('http://127.0.0.1:8000/api/config',{synthetic:true},{adapter:'fetch',headers:{'X-Fixture-Extra':'kept'}});const request=b.records[0].request;assert.equal(request.headers.get('X-Requested-With'),'sparkkeeper');assert.equal(request.headers.get('X-Fixture-Extra'),'kept');assert.match(request.headers.get('Content-Type'),/application\/json/);assert.deepEqual(await request.json(),{synthetic:true});await h.close();
 });
 await test('bundled Axios实际multipart编码保留marker并生成boundary与文件内容',async()=>{
  const h=fixture(),b=browserAxios({headers:h.api.defaults.headers}),form=new FormData();form.append('file',new File(['synthetic-upload-only'],'synthetic-state.json',{type:'application/json'}));await b.api.post('http://127.0.0.1:8000/api/upload-state',form,{adapter:'fetch',headers:{'Content-Type':'multipart/form-data'}});const request=b.records[0].request;assert.equal(request.headers.get('X-Requested-With'),'sparkkeeper');assert.match(request.headers.get('Content-Type'),/^multipart\/form-data; boundary=/);const data=await request.formData();assert.equal(data.get('file').name,'synthetic-state.json');assert.equal(await data.get('file').text(),'synthetic-upload-only');await h.close();
 });
 await test('实际SDK的keepalive取消经bundled Axios保留通用QR和能力头及同源凭证策略',async()=>{
  const h=fixture();await h.connect();await h.component.startQrLogin();await flush();await h.close();const cancel=h.calls.find(c=>c.url==='/api/credentials/qr/cancel');assert(cancel);assert.equal(cancel.config.adapter,'fetch');assert.equal(cancel.config.headers['X-Requested-With'],'sparkkeeper');assert.equal(cancel.config.headers['X-Spark-QR-Request'],'1');const capability=cancel.config.headers['X-QR-Job-Token'];assert(capability);const b=browserAxios({headers:h.api.defaults.headers});await b.api.post('http://127.0.0.1:8000'+cancel.url,cancel.data,cancel.config);const request=b.records[0].request;assert.equal(request.headers.get('X-Requested-With'),'sparkkeeper');assert.equal(request.headers.get('X-Spark-QR-Request'),'1');assert.equal(request.headers.get('X-QR-Job-Token'),capability);assert.equal(request.keepalive,true);assert.equal(request.credentials,'same-origin');assert(!request.url.includes(capability));assert(!JSON.stringify(await request.json()).includes(capability));
 });
 console.log(`T06 前端请求头运输: ${tests.length} isolated scenarios passed`);tests.forEach(name=>console.log('  PASS '+name));
})().catch(error=>{console.error(error);process.exitCode=1;});