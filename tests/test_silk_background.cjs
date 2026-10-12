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
  hidden = false, webgl = true, shaderOK = true, linkOK = true, precision = 23, noCanvas = false, storageThrows = false} = {}) {
  const rafs = new Map(), timers = new Map(), events = [], writes = [], deleted = [], shaderSources = [];
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
  canvas.clientWidth = width; canvas.clientHeight = height; canvas.width = 0; canvas.height = 0;
  canvas.setAttribute = (name, value) => canvas.attrs.set(name, value);
  canvas.getContext = () => webgl ? gl : null;
  const reduceQuery = new Surface(); reduceQuery.matches = reduced;
  const mobileQuery = new Surface(); mobileQuery.matches = width <= 768;
  const document = new Surface(); document.hidden = hidden; document.readyState = noCanvas ? 'loading' : 'complete';
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
    rafs, timers, events, writes, deleted, shaderSources,
    get drawCalls() {return drawCalls;},
    frame(timestamp) {const callbacks = [...rafs.values()]; rafs.clear(); callbacks.forEach(fn => fn(timestamp));},
    flushTimers() {const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn());},
    resize(nextWidth, nextHeight) {window.innerWidth = canvas.clientWidth = nextWidth; window.innerHeight = canvas.clientHeight = nextHeight; mobileQuery.matches = nextWidth <= 768; window.emit('resize'); this.flushTimers();},
    showCanvas() {canvasVisible = true; document.readyState = 'complete'; document.emit('DOMContentLoaded');},
    listeners() {return [window, document, canvas, reduceQuery, mobileQuery].reduce((n, target) => n + target.count(), 0);}
  };
}

const results = [];
function test(name, run) {run(); results.push(name);}

test('Self-initialization creates one decorative renderer and no pointer behavior', () => {
  const h = harness(); assert(h.api.getState().available); assert.equal(h.rafs.size, 1);
  assert.equal(h.canvas.attrs.get('aria-hidden'), 'true'); assert.equal(h.canvas.style.pointerEvents, 'none');
  assert.equal(h.document.events.get('pointermove'), undefined);
  assert.deepEqual(Object.keys(h.api).sort(), ['destroy', 'getState', 'setEnabled', 'setInputPaused']); h.api.destroy();
});

test('Desktop caps simulated 240 Hz rendering near 120 draws per second', () => {
  const h = harness(); assert.equal(h.api.getState().targetFps, 120); const initial = h.drawCalls;
  for (let i = 0; i <= 240; i++) h.frame(i * 1000 / 240);
  const draws = h.drawCalls - initial; assert(draws >= 119 && draws <= 122, String(draws));
  assert.equal(h.rafs.size, 1); assert(h.stats.observedFps <= 122); h.api.destroy();
});

test('A 60 Hz display remains limited by RAF rather than manufacturing 120 frames', () => {
  const h = harness(); const initial = h.drawCalls;
  for (let i = 0; i <= 60; i++) h.frame(i * 1000 / 60);
  assert.equal(h.drawCalls - initial, 61); assert.equal(h.api.getState().targetFps, 120); h.api.destroy();
});

test('Mobile uses 30 FPS with lower pixel ratio and bounded pixel area', () => {
  const h = harness({width: 390, height: 844, dpr: 3}); assert.equal(h.api.getState().targetFps, 30);
  assert(h.stats.pixelRatio <= 0.8); assert(h.canvas.width * h.canvas.height <= 460800);
  const initial = h.drawCalls; for (let i = 0; i <= 120; i++) h.frame(i * 1000 / 120);
  const draws = h.drawCalls - initial; assert(draws >= 30 && draws <= 32, String(draws)); h.api.destroy();
});

test('Desktop high-DPI and 4K canvases stay inside the explicit pixel budget', () => {
  const h = harness({width: 3840, height: 2160, dpr: 4});
  assert(h.canvas.width <= 1920 && h.canvas.height <= 1440); assert(h.canvas.width * h.canvas.height <= 1800000);
  assert(h.stats.pixelRatio <= 1.25); h.api.destroy();
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
  assert.equal(h.api.getState().targetFps, 120); assert.equal(h.drawCalls, before + 1); assert.equal(h.rafs.size, 0); h.api.destroy();
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
