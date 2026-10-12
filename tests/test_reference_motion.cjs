'use strict';
// Execute the real motion module against a minimal DOM, media and RAF model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/reference-motion.js'), 'utf8');
const TILT = '--reference-tilt', SHADOW = '--reference-tilt-shadow';

class Surface {
  constructor() { this.events = new Map(); }
  addEventListener(type, fn) {
    if (!this.events.has(type)) this.events.set(type, new Set());
    this.events.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.events.get(type)?.delete(fn); }
  emit(type, event = {}) { for (const fn of [...(this.events.get(type) || [])]) fn(event); }
  count() { return [...this.events.values()].reduce((sum, listeners) => sum + listeners.size, 0); }
}
class Style {
  constructor() {
    this.values = new Map(); this.priorities = new Map();
    Object.defineProperty(this, 'transform', {set() {throw Error('must not write inline transform');}});
    Object.defineProperty(this, 'boxShadow', {set() {throw Error('must not write inline shadow');}});
  }
  setProperty(key, value, priority = '') { this.values.set(key, value); this.priorities.set(key, priority); }
  getPropertyValue(key) { return this.values.get(key) || ''; }
  getPropertyPriority(key) { return this.priorities.get(key) || ''; }
  removeProperty(key) { this.values.delete(key); this.priorities.delete(key); }
}
class Element {
  constructor(classes = '', rect = {}) {
    this.nodeType = 1; this.classes = new Set(classes.split(/\s+/).filter(Boolean));
    this.parentElement = null; this.children = []; this.attrs = new Map(); this.style = new Style();
    this.isConnected = false; this.rectReads = 0;
    this.rect = {left: 100, top: 50, width: 200, height: 100, ...rect};
  }
  connected(value) { this.isConnected = value; this.children.forEach(child => child.connected(value)); }
  append(child) { child.parentElement = this; this.children.push(child); child.connected(this.isConnected); return child; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
    this.parentElement = null; this.connected(false);
  }
  matches(selector) {
    return selector.split(',').some(part => {
      const simple = part.trim();
      if (simple === '.main-view .card') return this.classes.has('card') && !!this.parentElement?.closest('.main-view');
      if (simple === '[data-qr-interaction]') return this.attrs.has('data-qr-interaction');
      return simple.startsWith('.') && this.classes.has(simple.slice(1));
    });
  }
  closest(selector) {
    for (let element = this; element; element = element.parentElement) if (element.matches(selector)) return element;
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    for (const child of this.children) {
      if (child.matches(selector)) found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getBoundingClientRect() { this.rectReads++; return {...this.rect}; }
  getAttribute(key) { return this.attrs.has(key) ? this.attrs.get(key) : null; }
  setAttribute(key, value) { this.attrs.set(key, value); }
  removeAttribute(key) { this.attrs.delete(key); }
}

function harness({fine = true, reduced = false, loading = false, noMedia = false, noObserver = false, legacyMedia = false} = {}) {
  const document = new Surface(), window = new Surface(), frames = new Map(), cancelled = [], observers = [];
  let serial = 0;
  const root = new Element(), body = root.append(new Element()), main = body.append(new Element('main-view'));
  root.connected(true);
  document.documentElement = root; document.hidden = false; document.readyState = loading ? 'loading' : 'complete';
  document.querySelectorAll = selector => root.querySelectorAll(selector);
  const fineQuery = new Surface(), reduceQuery = new Surface();
  fineQuery.matches = fine; reduceQuery.matches = reduced;
  if (legacyMedia) for (const query of [fineQuery, reduceQuery]) {
    query.addListener = fn => Surface.prototype.addEventListener.call(query, 'change', fn);
    query.removeListener = fn => Surface.prototype.removeEventListener.call(query, 'change', fn);
    query.addEventListener = query.removeEventListener = undefined;
  }
  if (!noMedia) window.matchMedia = query => query.includes('reduced-motion') ? reduceQuery : fineQuery;
  if (!noObserver) window.MutationObserver = class {
    constructor(callback) {this.callback = callback; this.disconnected = false; observers.push(this);}
    observe(target, options) {this.target = target; this.options = options;}
    disconnect() {this.disconnected = true;}
  };
  const sandbox = {window, document,
    requestAnimationFrame(fn) {frames.set(++serial, fn); return serial;},
    cancelAnimationFrame(id) {cancelled.push(id); frames.delete(id);}};
  function load() {vm.runInNewContext(source, sandbox, {filename: 'reference-motion.js'});}
  load();
  const api = window.SparkReferenceMotion;
  return {api, window, document, root, body, main, frames, cancelled, fineQuery, reduceQuery, observers, load,
    card(classes = 'card', rect = {}) {return main.append(new Element(classes, rect));},
    ready() {document.readyState = 'complete'; document.emit('DOMContentLoaded');},
    move(card, clientX, clientY, target = card) {document.emit('mousemove', {target, clientX, clientY});},
    over(card) {document.emit('mouseover', {target: card});},
    out(card, relatedTarget = null) {document.emit('mouseout', {target: card, relatedTarget});},
    frame() {const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn());},
    mutate(addedNodes = [], removedNodes = []) {for (const observer of observers) if (!observer.disconnected)
      observer.callback([{addedNodes, removedNodes}]);},
    preference(query, value) {query.matches = value; query.emit('change', {matches: value});},
    listeners() {return [document, window, fineQuery, reduceQuery].reduce((sum, surface) => sum + surface.count(), 0);}
  };
}
const passed = [];
function test(name, run) {run(); passed.push(name);}

test('Ready initialization marks eligible cards without inline transform writes or per-card listeners', () => {
  const h = harness({loading: true}); const card = h.card(), login = h.body.append(new Element('login-card'));
  assert.equal(card.getAttribute('data-reference-tilt'), null); h.ready();
  assert.equal(card.getAttribute('data-reference-tilt'), 'true'); assert.equal(login.getAttribute('data-reference-tilt'), 'true');
  assert.equal(card.style.getPropertyValue(TILT), 'none'); assert.equal(h.frames.size, 0); assert.equal(h.observers.length, 1);
  assert.deepEqual(Object.keys(h.api).sort(), ['destroy', 'refresh']); h.api.destroy();
});
test('Ordinary cards use the reference nonlinear mouse formula and reference lift/scale', () => {
  const h = harness(); const card = h.card(); h.api.refresh();
  h.over(card); h.move(card, 250, 150); assert.equal(h.frames.size, 1); h.frame();
  assert.equal(card.style.getPropertyValue(TILT), 'perspective(650px) rotateX(-9.75deg) rotateY(5.69deg) translateY(-5px) scale(1.025)');
  assert.equal(card.style.getPropertyValue(SHADOW), '0 24px 60px rgba(0,0,0,0.18), 0 10px 24px rgba(0,0,0,0.1)');
  assert.equal(card.rectReads, 1); h.api.destroy();
});
test('Login card uses the reference nine-degree parameter and its separate shadow', () => {
  const h = harness(); const login = h.body.append(new Element('login-card')); h.api.refresh();
  h.move(login, 300, 150); h.frame();
  assert.equal(login.style.getPropertyValue(TILT), 'perspective(650px) rotateX(-6.75deg) rotateY(6.75deg) translateY(-7px) scale(1.015)');
  assert.equal(login.style.getPropertyValue(SHADOW), '0 30px 80px rgba(0,0,0,0.2), 0 8px 24px rgba(0,0,0,0.1)'); h.api.destroy();
});
test('Multiple mouse moves coalesce into one frame using the latest coordinates', () => {
  const h = harness(); const card = h.card(); h.move(card, 100, 50); h.move(card, 200, 100); h.move(card, 300, 150);
  assert.equal(h.frames.size, 1); assert.equal(card.rectReads, 1); h.frame();
  assert.match(card.style.getPropertyValue(TILT), /rotateX\(-9\.75deg\) rotateY\(9\.75deg\)/); h.api.destroy();
});
test('Leaving cancels a queued frame and even a late cancelled callback cannot restore tilt', () => {
  const h = harness(); const card = h.card(); h.move(card, 300, 150); const stale = [...h.frames.values()][0];
  h.out(card); assert.equal(h.frames.size, 0); stale();
  assert.equal(card.style.getPropertyValue(TILT), 'none'); assert.equal(card.style.getPropertyValue(SHADOW), ''); h.api.destroy();
});
test('A stale callback from the previous card cannot cancel or overwrite a newer hover', () => {
  const h = harness(); const first = h.card(), second = h.card();
  h.move(first, 100, 50); const stale = [...h.frames.values()][0]; h.out(first, second); h.move(second, 300, 150);
  stale(); assert.equal(h.frames.size, 1); h.frame();
  assert.equal(first.style.getPropertyValue(TILT), 'none'); assert.match(second.style.getPropertyValue(TILT), /rotateY\(9\.75deg\)/); h.api.destroy();
});
test('Mouse movement between child nodes inside one card keeps its cached geometry', () => {
  const h = harness(); const card = h.card(), a = card.append(new Element()), b = card.append(new Element());
  h.move(card, 200, 100, a); h.out(card, b); h.move(card, 300, 150, b); h.frame();
  assert.equal(card.rectReads, 1); assert.match(card.style.getPropertyValue(TILT), /rotateY\(9\.75deg\)/); h.api.destroy();
});
test('No-tilt cards, their descendants and QR interaction containers stay flat', () => {
  const h = harness(); const noTilt = h.card('card no-tilt'), child = noTilt.append(new Element('stat-box'));
  const qrCard = h.card(), frame = qrCard.append(new Element('qr-code-frame')), image = frame.append(new Element());
  h.api.refresh();
  for (const element of [noTilt, child, qrCard, frame, image]) {h.move(element, 300, 150); assert.equal(h.frames.size, 0);}
  assert.equal(noTilt.getAttribute('data-reference-tilt'), null); assert.equal(child.getAttribute('data-reference-tilt'), null);
  assert.equal(qrCard.getAttribute('data-reference-tilt'), null); h.api.destroy();
});
test('Coarse or missing hover capability disables decorative tilt', () => {
  for (const options of [{fine: false}, {noMedia: true}]) {
    const h = harness(options); const card = h.card(); h.api.refresh(); h.move(card, 300, 150);
    assert.equal(h.frames.size, 0); assert.equal(card.style.getPropertyValue(TILT), 'none'); h.api.destroy();
  }
});
test('Reduced motion cancels pending work, resets active cards and supports live changes', () => {
  const h = harness(); const card = h.card(); h.move(card, 300, 150); const stale = [...h.frames.values()][0];
  h.preference(h.reduceQuery, true); stale(); assert.equal(h.frames.size, 0); assert.equal(card.style.getPropertyValue(TILT), 'none');
  h.move(card, 300, 150); assert.equal(h.frames.size, 0); h.preference(h.reduceQuery, false); h.move(card, 300, 150); h.frame();
  assert.match(card.style.getPropertyValue(TILT), /rotateY\(9\.75deg\)/); h.api.destroy();
});
test('Losing fine-hover capability resets previously rendered tilt', () => {
  const h = harness(); const card = h.card(); h.move(card, 300, 150); h.frame();
  h.preference(h.fineQuery, false); assert.equal(card.style.getPropertyValue(TILT), 'none');
  assert.equal(card.style.getPropertyValue(SHADOW), ''); h.api.destroy();
});
test('Mutation refresh binds v-if additions and prunes removed active nodes', () => {
  const h = harness(); const card = h.card(); h.mutate([card]); assert.equal(h.frames.size, 1); h.frame();
  assert.equal(card.getAttribute('data-reference-tilt'), 'true'); h.move(card, 300, 150); const stale = [...h.frames.values()][0];
  card.remove(); h.mutate([], [card]); stale(); h.frame();
  assert.equal(card.getAttribute('data-reference-tilt'), null); assert.equal(card.style.getPropertyValue(TILT), ''); h.api.destroy();
});
test('Manual refresh works without MutationObserver and recognizes changed no-tilt eligibility', () => {
  const h = harness({noObserver: true}); const card = h.card(); h.api.refresh();
  assert.equal(card.getAttribute('data-reference-tilt'), 'true'); card.classes.add('no-tilt'); h.api.refresh();
  assert.equal(card.getAttribute('data-reference-tilt'), null); h.api.destroy();
});
test('Mutation bursts coalesce and growing card counts do not grow event listener counts', () => {
  const h = harness(); const listeners = h.listeners();
  for (let i = 0; i < 100; i++) {const card = h.card(); h.mutate([card]);}
  assert.equal(h.frames.size, 1); h.frame(); assert.equal(h.listeners(), listeners);
  assert.equal(h.observers.length, 1); h.api.destroy();
});
test('Text-only DOM mutations do not schedule unnecessary card scans', () => {
  const h = harness(); h.mutate([{nodeType: 3}], [{nodeType: 3}]); assert.equal(h.frames.size, 0); h.api.destroy();
});
test('Resize, scroll, blur, cancellation and page hiding reset motion', () => {
  const h = harness(); const card = h.card();
  for (const [surface, event] of [[h.window, 'resize'], [h.document, 'scroll'], [h.window, 'blur'],
    [h.document, 'pointercancel'], [h.document, 'visibilitychange']]) {
    h.move(card, 300, 150); h.frame(); surface.emit(event); assert.equal(card.style.getPropertyValue(TILT), 'none');
  }
  h.document.hidden = true; h.move(card, 300, 150); assert.equal(h.frames.size, 0); h.api.destroy();
});
test('Nonfinite coordinates and zero-size geometry cannot produce invalid CSS', () => {
  const h = harness(); const card = h.card(), zero = h.card('stat-box', {width: 0});
  h.move(card, NaN, 100); h.move(card, 200, Infinity); h.move(zero, 200, 100);
  assert.equal(h.frames.size, 0); assert.equal(card.style.getPropertyValue(TILT), 'none'); h.api.destroy();
});
test('Destroy is idempotent, restores previous variables and removes every listener and observer', () => {
  const h = harness({loading: true}); const card = h.card();
  card.setAttribute('data-reference-tilt', 'previous'); card.style.setProperty(TILT, 'previous-transform', 'important');
  card.style.setProperty(SHADOW, 'previous-shadow', 'important'); h.ready(); h.move(card, 300, 150);
  const stale = [...h.frames.values()][0]; h.api.destroy(); h.api.destroy(); stale(); h.api.refresh();
  assert.equal(card.getAttribute('data-reference-tilt'), 'previous'); assert.equal(card.style.getPropertyValue(TILT), 'previous-transform');
  assert.equal(card.style.getPropertyPriority(TILT), 'important'); assert.equal(card.style.getPropertyValue(SHADOW), 'previous-shadow');
  assert.equal(h.frames.size, 0); assert.equal(h.listeners(), 0); assert(h.observers.every(observer => observer.disconnected));
});
test('Destroy before DOM ready prevents later initialization', () => {
  const h = harness({loading: true}); const card = h.card(); h.api.destroy(); h.ready();
  assert.equal(card.getAttribute('data-reference-tilt'), null); assert.equal(h.observers.length, 0); assert.equal(h.listeners(), 0);
});
test('Legacy media-query listeners are removed on destruction', () => {
  const h = harness({legacyMedia: true}); const card = h.card(); h.move(card, 300, 150); h.frame();
  h.preference(h.reduceQuery, true); assert.equal(card.style.getPropertyValue(TILT), 'none'); h.api.destroy(); assert.equal(h.listeners(), 0);
});
test('Loading the module twice disposes its previous observer and delegated listeners', () => {
  const h = harness(); const listeners = h.listeners(); const card = h.card(); h.api.refresh();
  h.load(); assert(h.observers[0].disconnected); assert.equal(h.listeners(), listeners);
  h.window.SparkReferenceMotion.destroy(); assert.equal(h.listeners(), 0);
});

console.log(`${passed.length} reference motion tests passed`);
for (const name of passed) console.log('PASS ' + name);
