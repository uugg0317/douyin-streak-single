// Real decorative renderer with fake WebGL, animation frames and browser media.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '../static/silk-background.js'), 'utf8');

class Surface {
  constructor() {this.events = new Map();}
  addEventListener(type, callback) {
    if (!this.events.has(type)) this.events.set(type, new Set());
    this.events.get(type).add(callback);
  }
  removeEventListener(type, callback) {this.events.get(type)?.delete(callback);}
  emit(type, event = {}) {for (const callback of [...(this.events.get(type) || [])]) callback(event);}
  count() {return [...this.events.values()].reduce((n, callbacks) => n + callbacks.size, 0);}
}

function harness({width = 1280, height = 900, dpr = 2, reduced = false, enabled = true,
  hidden = false, focused = true, focusAPI = true, webgl = true, shaderOK = true, linkOK = true, precision = 23, noCanvas = false, storageThrows = false} = {}) {
  const rafs = new Map(), timers = new Map(), events = [], writes = [], deleted = [], shaderSources = [], contextOptions = [];
  const sizeWrites = {width: 0, height: 0};
  let id = 0, drawCalls = 0, canvasVisible = !noCanvas;
  const gl = {
    VERTEX_SHADER: 35633, FRAGMENT_SHADER: 35632, HIGH_FLOAT: 36338,
    COMPILE_STATUS: 35713, LINK_STATUS: 35714, ARRAY_BUFFER: 34962, STATIC_DRAW: 35044,
    FLOAT: 5126, TRIANGLE_STRIP: 5,
    createShader(kind) {return {kind};}, shaderSource(_, value) {shaderSources.push(value);}, compileShader() {},
    getShaderParameter() {return shaderOK;}, getShaderPrecisionFormat() {return {precision};},
    createProgram() {return {};}, attachShader() {}, linkProgram() {}, getProgramParameter() {return linkOK;},
    useProgram() {}, createBuffer() {return {};}, bindBuffer() {}, bufferData() {},
    getAttribLocation() {return 0;}, enableVertexAttribArray() {}, vertexAttribPointer() {},
    getUniformLocation(_, name) {return {name};}, uniform1f() {}, viewport() {}, drawArrays() {drawCalls++;},
    deleteShader() {deleted.push('shader');}, deleteProgram() {deleted.push('program');}, deleteBuffer() {deleted.push('buffer');}
  };
  const canvas = new Surface(); canvas.style = {}; canvas.attrs = new Map();
  canvas.clientWidth = width; canvas.clientHeight = height;
  let canvasWidth = 0, canvasHeight = 0;
  Object.defineProperties(canvas, {
    width: {get: () => canvasWidth, set: value => {canvasWidth = value; sizeWrites.width++;}},
    height: {get: () => canvasHeight, set: value => {canvasHeight = value; sizeWrites.height++;}},
  });
  canvas.setAttribute = (name, value) => canvas.attrs.set(name, value);
  canvas.getContext = (type, options) => {contextOptions.push({type, ...options}); return webgl ? gl : null;};
  const reduceQuery = new Surface(); reduceQuery.matches = reduced;
  const mobileQuery = new Surface(); mobileQuery.matches = width <= 768;
  const document = new Surface(); document.hidden = hidden; document.readyState = noCanvas ? 'loading' : 'complete';
  if (focusAPI) document.hasFocus = () => focused;
  document.getElementById = name => name === 'fluid-canvas' && canvasVisible ? canvas : null;
  const window = new Surface(); window.innerWidth = width; window.innerHeight = height; window.devicePixelRatio = dpr;
  window.matchMedia = query => query.includes('reduced-motion') ? reduceQuery : mobileQuery;
  window.CustomEvent = class {constructor(type, {detail}) {this.type = type; this.detail = detail;}};
  window.dispatchEvent = event => {events.push(event); window.emit(event.type, event);};
  const sandbox = {
    window, document, Float32Array,
    localStorage: {
      getItem(key) {if (storageThrows) throw Error('blocked storage'); assert.equal(key, 'spark_silk_motion'); return enabled ? 'on' : 'off';},
      setItem(key, value) {if (storageThrows) throw Error('blocked storage'); writes.push({key, value});}
    },
    requestAnimationFrame(fn) {const key = ++id; rafs.set(key, fn); return key;},
    cancelAnimationFrame(key) {rafs.delete(key);},
    setTimeout(fn) {const key = ++id; timers.set(key, fn); return key;}, clearTimeout(key) {timers.delete(key);},
    console: {error() {throw Error('decorative fallback must not log errors');}}
  };
  vm.runInNewContext(source, sandbox, {filename: 'silk-background.js'});
  return {
    api: window.SparkSilkBackground, stats: window.__silkStats, window, document, canvas, reduceQuery, mobileQuery,
    rafs, timers, events, writes, deleted, shaderSources, contextOptions, sizeWrites,
    get drawCalls() {return drawCalls;},
    frame(timestamp) {const callbacks = [...rafs.values()]; rafs.clear(); callbacks.forEach(fn => fn(timestamp));},
    flushTimers() {const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());},
    focus(value) {focused = !!value; window.emit(focused ? 'focus' : 'blur');},
    resize(nextWidth, nextHeight) {window.innerWidth = canvas.clientWidth = nextWidth; window.innerHeight = canvas.clientHeight = nextHeight; mobileQuery.matches = nextWidth <= 768; window.emit('resize'); this.flushTimers();},
    showCanvas() {canvasVisible = true; document.readyState = 'complete'; document.emit('DOMContentLoaded');},
    listeners() {return [window, document, canvas, reduceQuery, mobileQuery].reduce((n, target) => n + target.count(), 0);}
  };
}

const results = [];
function test(name, run) {run(); results.push(name);}
function clockFor(h) {
  let time = 0;
  h.frame(time);
  return {
    run(hz, milliseconds) {
      for (let i = 0; i < Math.ceil(milliseconds * hz / 1000); i++) {
        time += 1000 / hz; h.frame(time);
      }
      return time;
    },
  };
}

test('Self-initialization creates one decorative renderer and no pointer behavior', () => {
  const h = harness(); assert(h.api.getState().available); assert.equal(h.rafs.size, 1);
  assert.equal(h.canvas.attrs.get('aria-hidden'), 'true'); assert.equal(h.canvas.style.pointerEvents, 'none');
  assert.equal(h.document.events.get('pointermove'), undefined);
  assert.deepEqual(Object.keys(h.api).sort(), ['destroy', 'getState', 'setEnabled', 'setInputPaused']); h.api.destroy();
});

test('Desktop starts with a 60 FPS budget even on a simulated 240 Hz display', () => {
  const h = harness(); assert.equal(h.api.getState().targetFps, 60); const initial = h.drawCalls;
  for (let i = 0; i <= 240; i++) h.frame(i * 1000 / 240);
  const draws = h.drawCalls - initial; assert(draws >= 59 && draws <= 62, String(draws));
  assert.equal(h.rafs.size, 1); assert(h.stats.observedFps <= 62); h.api.destroy();
});

test('A 60 Hz display remains limited by RAF rather than manufacturing 120 frames', () => {
  const h = harness(); const initial = h.drawCalls;
  for (let i = 0; i <= 60; i++) h.frame(i * 1000 / 60);
  assert.equal(h.drawCalls - initial, 61); assert.equal(h.api.getState().targetFps, 60); h.api.destroy();
});

test('Mobile uses 30 FPS with lower pixel ratio and bounded pixel area', () => {
  const h = harness({width: 390, height: 844, dpr: 3}); assert.equal(h.api.getState().targetFps, 30);
  assert(h.stats.pixelRatio <= 0.8); assert(h.canvas.width * h.canvas.height <= 230400);
  const initial = h.drawCalls; for (let i = 0; i <= 120; i++) h.frame(i * 1000 / 120);
  const draws = h.drawCalls - initial; assert(draws >= 30 && draws <= 32, String(draws)); h.api.destroy();
});

test('Desktop high-DPI and 4K canvases stay inside the explicit pixel budget', () => {
  const h = harness({width: 3840, height: 2160, dpr: 4});
  assert(h.canvas.width <= 1920 && h.canvas.height <= 1440); assert(h.canvas.width * h.canvas.height <= 720000);
  assert(h.stats.pixelRatio <= 1); h.api.destroy();
});

test('WebGL uses discardable drawing buffers and keeps unnecessary attachments disabled', () => {
  const h = harness();
  assert.equal(h.contextOptions.length, 1);
  const options = h.contextOptions[0];
  assert.equal(options.type, 'webgl');
  for (const flag of ['alpha','antialias','depth','stencil','preserveDrawingBuffer']) assert.equal(options[flag], false, flag);
  h.api.destroy();
});

test('Sustained slow RAF cadence reduces pixel work and drawing rate rather than keeping full quality', () => {
  const h = harness({width: 1920, height: 1080, dpr: 2}), clock = clockFor(h);
  const initialPixels = h.canvas.width * h.canvas.height;
  clock.run(30, 6000);
  assert(h.stats.qualityLevel >= 2 && h.stats.qualityLevel <= 3);
  assert(h.canvas.width * h.canvas.height < initialPixels);
  assert.equal(h.api.getState().targetFps, 30);
  assert.equal(h.rafs.size, 1); assert.equal(h.writes.length, 0);
  h.api.destroy();
});

test('Frequent long frames trigger adaptation even when their mean interval stays below 25 ms', () => {
  const h = harness(); let time = 0; h.frame(time);
  for (let i = 0; i < 300; i++) {time += i % 5 === 4 ? 45 : 16; h.frame(time);}
  assert(h.stats.qualityLevel > 0);
  assert(h.canvas.width * h.canvas.height < 720000);
  h.api.destroy();
});

test('A stable high-refresh display earns 120 FPS gradually and never draws above that ceiling', () => {
  const h = harness(), clock = clockFor(h);
  clock.run(240, 3000); assert.equal(h.api.getState().targetFps, 60);
  clock.run(240, 2000); assert.equal(h.api.getState().targetFps, 120);
  const before = h.drawCalls; clock.run(240, 1000);
  const draws = h.drawCalls - before; assert(draws >= 119 && draws <= 121, String(draws));
  assert.equal(h.stats.qualityLevel, 0); h.api.destroy();
});

test('Stable 60 Hz windows cannot replace the three high-refresh warm-up windows', () => {
  const h = harness(), clock = clockFor(h);
  clock.run(60, 3300); assert.equal(h.api.getState().targetFps, 60);
  clock.run(120, 3000); assert.equal(h.api.getState().targetFps, 60);
  clock.run(120, 2000); assert.equal(h.api.getState().targetFps, 120);
  h.api.destroy();
});

test('An adaptive quality change never publishes a stopped loop while frames continue', () => {
  const h = harness(), clock = clockFor(h), before = h.events.length;
  clock.run(30, 1800);
  assert(h.stats.qualityLevel > 0);
  assert.equal(h.api.getState().running, true);
  assert.equal(h.canvas.attrs.get('data-silk-running'), 'true');
  clock.run(30, 1800);
  assert.equal(h.api.getState().targetFps, 30);
  const changed = h.events.slice(before); assert(changed.length > 0);
  assert(changed.every(event => event.detail.running === true));
  assert.equal(h.canvas.attrs.get('data-silk-running'), 'true');
  assert.equal(h.rafs.size, 1); h.api.destroy();
});

test('A stable 60 Hz display does not request a manufactured 120 FPS mode', () => {
  const h = harness(), clock = clockFor(h); clock.run(60, 12000);
  assert.equal(h.api.getState().targetFps, 60);
  assert.equal(h.stats.qualityLevel, 0); h.api.destroy();
});

test('A visible but unfocused browser throttled to 1 FPS is not classified as a slow GPU', () => {
  const h = harness({focused:false}), clock = clockFor(h);
  const pixels = h.canvas.width * h.canvas.height;
  clock.run(1, 12000);
  assert.equal(h.document.hidden, false);
  assert.equal(h.stats.qualityLevel, 0);
  assert.equal(h.canvas.width * h.canvas.height, pixels);
  assert.equal(h.api.getState().targetFps, 60);
  assert.equal(h.api.getState().enabled, true); assert.equal(h.rafs.size, 1);
  assert.equal(h.writes.length, 0); h.api.destroy();
});

test('Focus transitions discard partial slow samples without changing the user animation preference', () => {
  const h = harness(), clock = clockFor(h);
  clock.run(30, 1000); assert.equal(h.stats.qualityLevel, 0);
  h.focus(false); clock.run(1, 5000);
  h.focus(true); clock.run(60, 2000);
  assert.equal(h.stats.qualityLevel, 0); assert.equal(h.api.getState().targetFps, 60);
  assert.equal(h.api.getState().enabled, true); assert.equal(h.writes.length, 0);
  h.api.destroy();
});

test('Returning focus after no callbacks does not sample the occlusion gap as rendering work', () => {
  const h = harness(); h.frame(0); h.frame(16);
  h.focus(false); h.focus(true); h.frame(120000);
  for (let i=1;i<=120;i++) h.frame(120000+i*1000/60);
  assert.equal(h.stats.qualityLevel, 0);
  assert.equal(h.api.getState().enabled, true); assert.equal(h.writes.length, 0);
  h.api.destroy();
});

test('Browsers without document.hasFocus still perform normal workload adaptation', () => {
  const h = harness({focusAPI:false}), clock = clockFor(h); clock.run(30, 6000);
  assert(h.stats.qualityLevel >= 2); assert.equal(h.api.getState().targetFps, 30);
  h.api.destroy();
});

test('Dropping from 120 FPS starts a promotion cooldown instead of oscillating back after three windows', () => {
  const h = harness(), clock = clockFor(h);
  clock.run(120, 5000); assert.equal(h.api.getState().targetFps, 120);
  clock.run(60, 2000); assert.equal(h.api.getState().targetFps, 60);
  const before = h.events.length;
  clock.run(120, 24000);
  assert.equal(h.api.getState().targetFps, 60);
  assert(h.events.slice(before).every(event => event.detail.targetFps <= 60));
  clock.run(120, 12000); assert.equal(h.api.getState().targetFps, 120);
  assert.equal(h.stats.qualityLevel, 0); h.api.destroy();
});

test('Quality recovers only after sustained headroom instead of bouncing after one fast window', () => {
  const h = harness(), clock = clockFor(h);
  clock.run(30, 6000); const degraded = h.stats.qualityLevel; assert(degraded >= 2);
  clock.run(120, 1300); assert.equal(h.stats.qualityLevel, degraded);
  clock.run(120, 21000); assert.equal(h.stats.qualityLevel, 0);
  assert.equal(h.api.getState().targetFps, 120); assert.equal(h.writes.length, 0);
  h.api.destroy();
});

test('Mobile keeps its lower workload and adapts further when the device is struggling', () => {
  const h = harness({width:390,height:844,dpr:3}), clock = clockFor(h);
  clock.run(20, 6000);
  assert(h.stats.qualityLevel >= 2); assert.equal(h.api.getState().targetFps, 20);
  assert(h.canvas.width * h.canvas.height < 230400);
  assert(h.stats.pixelRatio <= 0.8); h.api.destroy();
});

test('A long hidden pause is not sampled as a slow frame and keeps the chosen quality', () => {
  const h = harness(), clock = clockFor(h); clock.run(30, 3200);
  const quality = h.stats.qualityLevel, pixels = h.canvas.width * h.canvas.height;
  h.document.hidden = true; h.document.emit('visibilitychange');
  assert.equal(h.rafs.size, 0); h.frame(120000);
  h.document.hidden = false; h.document.emit('visibilitychange'); h.frame(120000);
  for (let i = 1; i <= 60; i++) h.frame(120000 + i * 1000 / 60);
  assert.equal(h.stats.qualityLevel, quality); assert.equal(h.canvas.width * h.canvas.height, pixels);
  h.api.destroy();
});

test('An unchanged viewport does not reallocate or clear the canvas backing buffer', () => {
  const h = harness(); const before = {...h.sizeWrites};
  h.resize(1280, 900); h.resize(1280, 900);
  assert.deepEqual(h.sizeWrites, before);
  h.document.hidden = true; h.document.emit('visibilitychange');
  h.document.hidden = false; h.document.emit('visibilitychange');
  assert.deepEqual(h.sizeWrites, before); h.api.destroy();
});

test('Persisted off preference draws a static frame without scheduling RAF', () => {
  const h = harness({enabled: false}); assert.equal(h.api.getState().enabled, false); assert.equal(h.drawCalls, 1); assert.equal(h.rafs.size, 0);
  h.api.setEnabled(true); assert.equal(h.rafs.size, 1); h.api.setEnabled(false); assert.equal(h.rafs.size, 0);
  assert(h.writes.every(entry => entry.key === 'spark_silk_motion')); assert.deepEqual(h.writes.map(entry => entry.value), ['on', 'off']); h.api.destroy();
});

test('Hidden pages really cancel RAF and resume without elapsed-time jumps', () => {
  const h = harness(); h.frame(100); h.frame(116); const time = h.stats.sceneTime, draws = h.drawCalls;
  h.document.hidden = true; h.document.emit('visibilitychange'); assert.equal(h.rafs.size, 0);
  h.frame(30000); assert.equal(h.drawCalls, draws); assert.equal(h.stats.sceneTime, time);
  h.document.hidden = false; h.document.emit('visibilitychange'); assert.equal(h.rafs.size, 1);
  h.frame(30000); assert.equal(h.stats.sceneTime, time); h.frame(30008); assert(h.stats.sceneTime - time <= 0.009); h.api.destroy();
});

test('Input focus pauses the loop, then resumes with integrated bounded delta', () => {
  const h = harness(); h.frame(0); h.frame(10); const time = h.stats.sceneTime;
  h.api.setInputPaused(true); assert.equal(h.rafs.size, 0); assert(h.api.getState().inputPaused);
  h.api.setInputPaused(false); assert.equal(h.rafs.size, 1); h.frame(100000); assert.equal(h.stats.sceneTime, time);
  h.frame(105000); assert(Math.abs(h.stats.sceneTime - time - 0.05) < 1e-8); assert.equal(h.writes.length, 0); h.api.destroy();
});

test('Reduced motion overrides enabled preference and changes live without rewriting it', () => {
  const h = harness({reduced: true}); assert.equal(h.rafs.size, 0); assert.equal(h.drawCalls, 1);
  h.api.setEnabled(true); assert.equal(h.rafs.size, 0); assert(h.api.getState().reducedMotion);
  h.reduceQuery.matches = false; h.reduceQuery.emit('change'); assert.equal(h.rafs.size, 1);
  h.reduceQuery.matches = true; h.reduceQuery.emit('change'); assert.equal(h.rafs.size, 0);
  assert.equal(h.writes.length, 1); h.api.destroy();
});

test('Repeated enabled/resume calls never duplicate animation loops', () => {
  const h = harness(); for (let i = 0; i < 10; i++) {h.api.setEnabled(true); h.api.setInputPaused(false);}
  assert.equal(h.rafs.size, 1); h.frame(100); assert.equal(h.rafs.size, 1); h.api.destroy();
});

test('Viewport changes switch budgets while disabled resize remains a static repaint', () => {
  const h = harness(); h.resize(390, 844); assert.equal(h.api.getState().targetFps, 30); assert(h.stats.pixelRatio <= 0.8);
  h.api.setEnabled(false); const before = h.drawCalls; h.resize(1280, 900);
  assert.equal(h.api.getState().targetFps, 60); assert.equal(h.drawCalls, before + 1); assert.equal(h.rafs.size, 0); h.api.destroy();
});

test('Unavailable WebGL or invalid shaders keep a static fallback with no RAF', () => {
  for (const config of [{webgl: false}, {shaderOK: false}, {linkOK: false}]) {
    const h = harness(config); assert.equal(h.api.getState().available, false); assert.equal(h.rafs.size, 0);
    assert(h.canvas.style.background.includes('radial-gradient')); assert.equal(h.stats.frames, 0); h.api.destroy();
  }
});

test('Low fragment precision uses a compatible shader rather than failing the page', () => {
  const h = harness({precision: 0}); assert(h.shaderSources.some(value => value.includes('precision mediump float;')));
  assert(h.api.getState().available); h.api.destroy();
});

test('Context loss pauses and a hidden restoration does not restart rendering early', () => {
  const h = harness(); let prevented = false; const listeners = h.listeners();
  h.canvas.emit('webglcontextlost', {preventDefault() {prevented = true;}}); assert(prevented);
  assert.equal(h.rafs.size, 0); assert.equal(h.api.getState().available, false);
  h.document.hidden = true; h.document.emit('visibilitychange'); h.canvas.emit('webglcontextrestored');
  assert(h.api.getState().available); assert.equal(h.rafs.size, 0); assert.equal(h.listeners(), listeners);
  h.document.hidden = false; h.document.emit('visibilitychange'); assert.equal(h.rafs.size, 1); h.api.destroy();
});

test('Destroy releases resources, timers and every lifecycle listener idempotently', () => {
  const h = harness(); h.window.emit('resize'); assert.equal(h.timers.size, 1); h.api.destroy();
  assert.equal(h.rafs.size, 0); assert.equal(h.timers.size, 0); assert.equal(h.listeners(), 0);
  assert.equal(h.api.getState().available, false); assert(h.api.getState().destroyed);
  assert.deepEqual(h.deleted.sort(), ['buffer', 'program', 'shader', 'shader'].sort());
  h.api.destroy(); h.api.setEnabled(true); h.api.setInputPaused(false); assert.equal(h.rafs.size, 0);
});

test('A pre-DOM toggle remains off when the deferred canvas becomes available', () => {
  const h = harness({noCanvas: true}); assert.equal(h.rafs.size, 0); h.api.setEnabled(false); h.showCanvas();
  assert(h.api.getState().available); assert.equal(h.api.getState().enabled, false); assert.equal(h.rafs.size, 0); h.api.destroy();
});

test('Blocked local storage cannot break decorative rendering or toggling', () => {
  const h = harness({storageThrows: true}); assert(h.api.getState().available); h.api.setEnabled(false);
  assert.equal(h.rafs.size, 0); h.api.setEnabled(true); assert.equal(h.rafs.size, 1); h.api.destroy();
});

test('State events have only the documented safe fields and are not emitted every frame', () => {
  const h = harness(); const before = h.events.length;
  for (let i = 0; i < 120; i++) h.frame(i * 1000 / 120);
  assert.equal(h.events.length, before); h.api.setEnabled(false); assert(h.events.length > before);
  for (const event of h.events) {
    assert.equal(event.type, 'spark-silk-change');
    assert.deepEqual(Object.keys(event.detail).sort(), ['available', 'enabled', 'reducedMotion', 'running', 'targetFps']);
  }
  assert(Object.values(h.stats).every(value => typeof value === 'boolean' || Number.isFinite(value))); h.api.destroy();
});

console.log(`Silk background: ${results.length} isolated lifecycle scenarios passed`);
results.forEach(name => console.log('  PASS ' + name));
