// Exercise the real page handlers against synthetic HTTP and a stable log DOM.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const fixtureFile = fs.existsSync(path.join(__dirname, 'test_public_qr_frontend.cjs'))
  ? 'test_public_qr_frontend.cjs' : 'test_gated_qr_frontend.cjs';
const baseline = fs.readFileSync(path.join(__dirname, fixtureFile), 'utf8');
const prefix = baseline.slice(0, baseline.indexOf('const forbidden='));
assert(prefix.includes('getElementById:()=>({style:{}})'), 'Fixture DOM injection point changed');
assert(prefix.includes('nextTick:fn=>Promise.resolve().then(fn)'), 'Fixture Vue tick injection point changed');
const injected = prefix
  .replace('getElementById:()=>({style:{}})', "getElementById:id=>id==='terminal-view'?__logDom.terminal:id==='app'?{style:{}}:null")
  .replace('nextTick:fn=>Promise.resolve().then(fn)', "nextTick:fn=>Promise.resolve().then(()=>{__logDom.sync(component);return typeof fn==='function'?fn():undefined;})");
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise, resolve, reject}; };

async function harness() {
  let height = 40, top = 0, renderedText = '';
  const writes = [];
  const terminal = {style:{}, get scrollHeight(){return height;}, get scrollTop(){return top;},
    set scrollTop(value){top=value;writes.push({value, renderedText});}};
  const dom = {terminal, sync(component){
    if (!component) return;
    renderedText = component.logsContent.value;
    height = Math.max(40, renderedText.split('\n').length * 20);
  }};
  const context = vm.createContext({require,__dirname,console,URL,URLSearchParams,Response,AbortController,FormData,Blob,File,setTimeout,clearTimeout,__logDom:dom});
  vm.runInContext(injected+';globalThis.logFixture=fixture;globalThis.logFlush=flush;',context);
  const h = context.logFixture();
  await (h.connect ? h.connect() : h.login());
  await context.logFlush();
  terminal.scrollTop = 0; writes.length = 0; h.calls.length = 0;
  return {...h, c:h.component, terminal, writes, sync:()=>dom.sync(h.component), flush:context.logFlush,
    reads:()=>h.calls.filter(call=>call.url==='/api/logs')};
}
const tests=[];
const test=(name,run)=>tests.push({name,run});

test('Fresh logs scroll after Vue has rendered the new content height',async()=>{
  const h=await harness();
  try {
    const text=Array.from({length:80},(_,i)=>'new synthetic log '+i).join('\n');
    h.handlers.set('/api/logs',()=>({data:{logs:text}}));
    await h.c.refreshLogs();await h.flush();
    assert.equal(h.c.logsContent.value,text);
    assert.equal(h.terminal.scrollTop,1600);
    assert.equal(h.writes.at(-1).renderedText,text,'Scrolling must follow DOM update');
    assert.equal(h.c.logsRefreshing.value,false);
  } finally {await h.close();}
});

test('Refreshing unchanged logs still scrolls a reader back to the latest line',async()=>{
  const h=await harness();
  try {
    const text=Array.from({length:60},(_,i)=>'same synthetic log '+i).join('\n');
    h.c.logsContent.value=text;h.sync();h.terminal.scrollTop=20;h.writes.length=0;
    h.handlers.set('/api/logs',()=>({data:{logs:text}}));
    await h.c.refreshLogs();await h.flush();
    assert.equal(h.terminal.scrollTop,h.terminal.scrollHeight);
    assert.equal(h.writes.length,1);
    assert.equal(h.c.logsRefreshing.value,false);
  } finally {await h.close();}
});

test('Repeated manual refresh clicks share one pending read',async()=>{
  const h=await harness(),pending=deferred();
  try {
    h.handlers.set('/api/logs',()=>pending.promise);
    const first=h.c.refreshLogs(),second=h.c.refreshLogs();await h.flush();
    assert.equal(h.reads().length,1);assert.equal(h.c.logsRefreshing.value,true);
    pending.resolve({data:{logs:'completed synthetic refresh'}});
    await Promise.all([first,second]);await h.flush();
    assert.equal(h.c.logsRefreshing.value,false);assert.equal(h.terminal.scrollTop,h.terminal.scrollHeight);
  } finally {await h.close();}
});

test('Failed refresh preserves displayed logs and releases its loading state',async()=>{
  const h=await harness();
  try {
    h.c.logsContent.value='previous synthetic log';h.sync();h.terminal.scrollTop=12;h.writes.length=0;
    h.handlers.set('/api/logs',()=>Promise.reject(Error('synthetic offline')));
    await h.c.refreshLogs();await h.flush();
    assert.equal(h.c.logsContent.value,'previous synthetic log');assert.equal(h.c.logsRefreshing.value,false);
    assert.match(h.c.logsRefreshMessage.value,/失败|未能|重试|断开/);
    assert.equal(h.terminal.scrollTop,12);assert.equal(h.writes.length,0);
  } finally {await h.close();}
});

for (const action of ['clear','reset']) {
  test(action+' discards a late response without refilling logs or scrolling it into view',async()=>{
    const h=await harness(),pending=deferred();
    try {
      h.c.logsContent.value='old synthetic log';h.sync();
      h.handlers.set('/api/logs',()=>pending.promise);
      const reading=h.c.refreshLogs();await h.flush();const call=h.reads()[0];
      if(action==='clear')h.c.clearLogs();else await (h.c.resetConsole?h.c.resetConsole():h.c.logout());
      await h.flush();const writeCount=h.writes.length;
      assert.equal(call.config.signal.aborted,true);
      pending.resolve({data:{logs:'late synthetic data must stay discarded'}});await reading;await h.flush();
      assert.equal(h.c.logsContent.value,'');assert.equal(h.writes.length,writeCount);
      assert.equal(h.c.logsRefreshing.value,false);
    } finally {await h.close();}
  });
}

test('Opening the logs page fetches current content and reveals its last line',async()=>{
  const h=await harness();
  try {
    h.c.tab.value='overview';h.handlers.set('/api/logs',()=>({data:{logs:'first\nlatest'}}));
    await h.c.openTab('logs');await h.flush();
    assert.equal(h.c.tab.value,'logs');assert.equal(h.reads().length,1);
    assert.equal(h.c.logsContent.value,'first\nlatest');assert.equal(h.terminal.scrollTop,h.terminal.scrollHeight);
  } finally {await h.close();}
});

(async()=>{let failed=0;for(const{name,run}of tests){try{await run();console.log('PASS '+name);}catch(error){failed++;console.error('FAIL '+name+'\n'+error.stack);}}console.log(JSON.stringify({passed:tests.length-failed,failed,total:tests.length}));process.exitCode=failed?1:0;})();
