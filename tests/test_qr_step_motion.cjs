'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/qr-step-motion.js'), 'utf8');

function harness(reduced = false) {
  let now = 0, serial = 0;
  const timers = new Map(), listeners = new Set(), changes = [];
  const media = {matches: reduced, addEventListener(type, fn) {listeners.add(fn);},
    removeEventListener(type, fn) {listeners.delete(fn);}};
  const window = {matchMedia: () => media};
  const context = {window, setTimeout(fn, delay) {timers.set(++serial, {at: now + delay, fn});return serial;},
    clearTimeout(id) {timers.delete(id);}, console};
  vm.runInNewContext(source, context, {filename: 'qr-step-motion.js'});
  const controller = window.SparkQrStepMotion.create({onChange: state => changes.push({...state})});
  function tick(ms) {
    const end = now + ms;
    for (let steps = 0; steps < 100; steps++) {
      const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) {now = end;return;}
      now = next[1].at;timers.delete(next[0]);next[1].fn();
    }
    throw Error('unbounded animation timer');
  }
  return {controller, tick, timers, changes, listeners,
    update: value => controller.update(value), state: () => controller.snapshot(),
    reduce(value) {media.matches = value;for (const fn of listeners) fn({matches: value});}};
}
const cases = [];
function test(name, fn) {fn();cases.push(name);}

test('Idle is neutral and a waiting QR never advances just because time passes', () => {
  const h = harness();assert.equal(h.state().index, -1);
  h.update({status: 'starting', running: true, hasJob: false});
  h.update({status: 'qr_ready', running: true, hasJob: true});
  h.tick(300000);assert.equal(h.state().index, 0);assert.equal(h.state().completed, 0);
  assert.equal(h.timers.size, 0);assert.equal(h.state().finalSuccess, false);h.controller.dispose();
});
test('Skipped server stages traverse all four positions at 420ms without inventing status text', () => {
  const h = harness();h.update({status: 'waiting', running: true, hasJob: true});
  h.update({status: 'success', saved: true, cleanupComplete: true});
  assert.equal(h.state().index, 1);assert.equal(h.state().arrived, false);
  h.tick(419);assert.equal(h.state().index, 1);assert.equal(h.state().finalSuccess, false);
  h.tick(1);assert.equal(h.state().index, 2);
  h.tick(420);assert.equal(h.state().index, 3);assert.equal(h.state().finalSuccess, false);
  h.tick(420);assert.equal(h.state().finalSuccess, true);assert.equal(h.state().completed, 4);
  assert.deepEqual([...new Set(h.changes.map(s => s.index).filter(index => index >= 0))], [0, 1, 2, 3]);
  assert(!h.changes.some(s => 'message' in s || 'status' in s));h.controller.dispose();
});
test('The first snapshot can be success without skipping stage one visually', () => {
  const h = harness();h.update({status: 'success', saved: true});
  assert.equal(h.state().index, 0);h.tick(420);assert.equal(h.state().index, 1);
  h.tick(1260);assert.equal(h.state().finalSuccess, true);h.controller.dispose();
});
test('Each confirmed phase advances only up to its server-confirmed position', () => {
  const h = harness();h.update({status: 'waiting', hasJob: true});
  h.update({status: 'scanned', hasJob: true});h.tick(420);assert.equal(h.state().index, 1);
  h.tick(10000);assert.equal(h.state().index, 1);
  h.update({status: 'saving', hasJob: true});h.tick(420);assert.equal(h.state().index, 2);
  h.tick(10000);assert.equal(h.state().finalSuccess, false);h.controller.dispose();
});
test('Verification and synchronization preserve confirmed phase without regression', () => {
  const h = harness();h.update({status: 'confirming', hasJob: true});h.tick(840);
  h.update({status: 'sync_pending', hasJob: true});h.tick(10000);assert.equal(h.state().index, 1);
  h.update({status: 'verification_required', hasJob: true});assert.equal(h.state().index, 1);h.controller.dispose();
});
test('Same-job QR refresh does not flash left or reset the run', () => {
  const h = harness();h.update({status: 'starting', hasJob: false});
  h.update({status: 'scanned', hasJob: true});h.tick(420);const key = h.state().runKey;
  h.update({status: 'starting', hasJob: true});h.update({status: 'qr_ready', hasJob: true});
  assert.equal(h.state().index, 1);assert.equal(h.state().runKey, key);h.controller.dispose();
});
test('Saved credentials pending cleanup stay at save until real success', () => {
  const h = harness();h.update({status: 'waiting', hasJob: true});
  h.update({status: 'cleanup_pending', saved: true, cleanupComplete: false, hasJob: true});
  h.tick(10000);assert.equal(h.state().index, 2);assert.equal(h.state().finalSuccess, false);
  h.update({status: 'success', saved: true, cleanupComplete: true});h.tick(420);
  assert.equal(h.state().index, 3);assert.equal(h.state().finalSuccess, true);h.controller.dispose();
});
test('Premature success with incomplete cleanup never activates stage four', () => {
  const h = harness();h.update({status: 'success', saved: true, cleanup_complete: false});h.tick(10000);
  assert.equal(h.state().index, 2);assert.equal(h.state().finalSuccess, false);h.controller.dispose();
});
test('New start cancels the old queue, resets progress and changes run key', () => {
  const h = harness();h.update({status: 'waiting', hasJob: true});h.update({status: 'success'});
  const old = h.state().runKey, oldCallback = [...h.timers.values()][0].fn;
  h.update({status: 'starting', running: true, hasJob: false});
  assert.equal(h.state().index, 0);assert.notEqual(h.state().runKey, old);
  oldCallback();h.tick(10000);assert.equal(h.state().index, 0);assert.equal(h.state().finalSuccess, false);h.controller.dispose();
});
test('Explicit run keys reset independent jobs even when their statuses match', () => {
  const h = harness();h.update({status: 'saving', runKey: 'first', hasJob: true});h.tick(1260);
  h.update({status: 'waiting', runKey: 'second', hasJob: true});
  assert.equal(h.state().runKey, 'second');assert.equal(h.state().index, 0);assert.equal(h.state().completed, 0);h.controller.dispose();
});
test('Cancel or failure interrupts a queued success without a completion effect', () => {
  for (const status of ['cancelled', 'failed', 'timeout', 'blocked']) {
    const h = harness();h.update({status: 'waiting', hasJob: true});h.update({status: 'success'});
    h.update({status});const stopped = h.state().index;h.tick(10000);
    assert.equal(h.state().index, stopped);assert.equal(h.state().finalSuccess, false);assert.equal(h.timers.size, 0);h.controller.dispose();
  }
});
test('Reduced motion immediately locates the confirmed phase with no timers', () => {
  const h = harness(true);h.update({status: 'success'});
  assert.equal(h.state().index, 3);assert.equal(h.state().finalSuccess, true);assert.equal(h.timers.size, 0);h.controller.dispose();
});
test('Enabling reduced motion mid-transition cancels old arrivals and snaps once', () => {
  const h = harness();h.update({status: 'waiting', hasJob: true});h.update({status: 'success'});
  const old = [...h.timers.values()][0].fn;h.reduce(true);
  assert.equal(h.state().index, 3);assert.equal(h.timers.size, 0);const count = h.changes.length;
  old();assert.equal(h.changes.length, count);h.controller.dispose();
});
test('Dispose removes media listener, clears timers and ignores already queued callbacks', () => {
  const h = harness();h.update({status: 'success'});const old = [...h.timers.values()][0].fn;
  h.controller.dispose();h.controller.dispose();const count = h.changes.length;
  old();h.update({status: 'starting'});h.reduce(true);h.tick(10000);
  assert.equal(h.changes.length, count);assert.equal(h.timers.size, 0);assert.equal(h.listeners.size, 0);
});
test('Snapshots cannot mutate internal presentation state and contain no account values', () => {
  const h = harness();h.update({status: 'waiting', job_token: 'never-public', cookies: ['never-public']});
  const publicState = h.state();publicState.index = 99;
  assert.equal(h.state().index, 0);assert(!JSON.stringify(h.state()).includes('never-public'));h.controller.dispose();
});
console.log(JSON.stringify({passed: cases.length, cases}, null, 2));
