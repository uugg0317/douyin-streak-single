/* Reference hover motion only. No account data, requests or inline transforms. */
(function () {
  'use strict';

  if (typeof window.SparkReferenceMotion?.destroy === 'function') window.SparkReferenceMotion.destroy();

  const TARGETS = '.main-view .card, .stat-box, .card-3d, .login-card';
  const QR_ZONE = '.qr-code-frame, .qr-login-panel, [data-qr-interaction]';
  const MARKER = 'data-reference-tilt';
  const TILT = '--reference-tilt';
  const SHADOW = '--reference-tilt-shadow';
  const normal = {angle: 13, lift: '-5px', scale: '1.025',
    shadow: '0 24px 60px rgba(0,0,0,0.18), 0 10px 24px rgba(0,0,0,0.1)'};
  const login = {angle: 9, lift: '-7px', scale: '1.015',
    shadow: '0 30px 80px rgba(0,0,0,0.2), 0 8px 24px rgba(0,0,0,0.1)'};
  const fine = window.matchMedia?.('(hover: hover) and (pointer: fine)');
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const bound = new Map();
  const cleanups = [];
  let initialized = false, destroyed = false, observer = null;
  let active = null, motionFrame = null, scanFrame = null, generation = 0;

  function listen(target, type, callback, options) {
    if (!target?.addEventListener) return;
    target.addEventListener(type, callback, options);
    cleanups.push(() => target.removeEventListener(type, callback, options));
  }
  function listenMedia(query, callback) {
    if (query?.addEventListener) listen(query, 'change', callback);
    else if (query?.addListener) {
      query.addListener(callback);
      cleanups.push(() => query.removeListener(callback));
    }
  }
  function enabled() {
    return initialized && !destroyed && !document.hidden && !!fine?.matches && !reduced?.matches;
  }
  function eligible(element) {
    return !!element?.matches && element.isConnected !== false && element.matches(TARGETS)
      && !element.closest('.no-tilt') && !element.closest(QR_ZONE) && !element.querySelector(QR_ZONE);
  }
  function reset(element) {
    if (!element?.style) return;
    element.style.setProperty(TILT, 'none');
    element.style.removeProperty(SHADOW);
  }
  function deactivate() {
    generation++;
    if (motionFrame !== null) cancelAnimationFrame(motionFrame);
    motionFrame = null;
    if (active) reset(active.element);
    active = null;
  }
  function bind(element) {
    if (bound.has(element)) return;
    bound.set(element, {
      marker: element.getAttribute(MARKER),
      tilt: element.style.getPropertyValue(TILT), tiltPriority: element.style.getPropertyPriority(TILT),
      shadow: element.style.getPropertyValue(SHADOW), shadowPriority: element.style.getPropertyPriority(SHADOW),
    });
    element.setAttribute(MARKER, 'true');
    reset(element);
  }
  function restore(element) {
    const previous = bound.get(element);
    if (!previous) return;
    if (active?.element === element) deactivate();
    if (previous.marker === null) element.removeAttribute(MARKER);
    else element.setAttribute(MARKER, previous.marker);
    if (previous.tilt) element.style.setProperty(TILT, previous.tilt, previous.tiltPriority);
    else element.style.removeProperty(TILT);
    if (previous.shadow) element.style.setProperty(SHADOW, previous.shadow, previous.shadowPriority);
    else element.style.removeProperty(SHADOW);
    bound.delete(element);
  }
  function scan() {
    if (!initialized || destroyed) return;
    const present = new Set([...document.querySelectorAll(TARGETS)].filter(eligible));
    for (const element of bound.keys()) if (!present.has(element)) restore(element);
    for (const element of present) bind(element);
    if (!enabled()) deactivate();
  }
  function scheduleScan(records) {
    if (destroyed) return;
    // Removed nodes cannot receive a late hover frame even before the scan runs.
    if (active && !eligible(active.element)) deactivate();
    if (records && !records.some(record => [...record.addedNodes, ...record.removedNodes]
      .some(node => node.nodeType === 1))) return;
    if (scanFrame !== null) return;
    scanFrame = requestAnimationFrame(() => {
      scanFrame = null;
      if (!destroyed) scan();
    });
  }
  function targetFrom(node) {
    if (!enabled()) return null;
    const element = node?.nodeType === 1 ? node : node?.parentElement;
    if (!element?.closest || element.closest('.no-tilt') || element.closest(QR_ZONE)) return null;
    const candidate = element.closest(TARGETS);
    if (!eligible(candidate)) return null;
    bind(candidate);
    return candidate;
  }
  function activate(element) {
    if (active?.element === element) return true;
    deactivate();
    if (!element) return false;
    const rect = element.getBoundingClientRect();
    if (!(rect.width > 0) || !(rect.height > 0)) return false;
    active = {element, rect, parameters: element.matches('.login-card') ? login : normal, x: 0, y: 0};
    return true;
  }
  function onOver(event) { activate(targetFrom(event.target)); }
  function onOut(event) {
    if (active && targetFrom(event.relatedTarget) !== active.element) deactivate();
  }
  function onMove(event) {
    const element = targetFrom(event.target);
    if (!element || !Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) {
      deactivate();
      return;
    }
    if (!activate(element)) return;
    active.x = event.clientX;
    active.y = event.clientY;
    if (motionFrame !== null) return;
    const turn = generation, current = active;
    motionFrame = requestAnimationFrame(() => {
      // A cancelled callback must not clear a newer card's pending frame either.
      if (destroyed || turn !== generation || active !== current) return;
      motionFrame = null;
      if (!enabled() || !eligible(current.element)) { deactivate(); return; }
      const {rect, parameters} = current;
      const rawX = (current.x - rect.left - rect.width / 2) / (rect.width / 2);
      const rawY = (current.y - rect.top - rect.height / 2) / (rect.height / 2);
      const smoothX = rawX * (1 - Math.abs(rawX) * .25);
      const smoothY = rawY * (1 - Math.abs(rawY) * .25);
      current.element.style.setProperty(TILT,
        `perspective(650px) rotateX(${(-smoothY * parameters.angle).toFixed(2)}deg) `
        + `rotateY(${(smoothX * parameters.angle).toFixed(2)}deg) translateY(${parameters.lift}) scale(${parameters.scale})`);
      current.element.style.setProperty(SHADOW, parameters.shadow);
    });
  }
  function resetMotion() {
    deactivate();
    for (const element of bound.keys()) reset(element);
  }
  function initialize() {
    if (initialized || destroyed) return;
    initialized = true;
    listen(document, 'mouseover', onOver, {passive: true});
    listen(document, 'mousemove', onMove, {passive: true});
    listen(document, 'mouseout', onOut, {passive: true});
    listen(document, 'pointercancel', resetMotion, {passive: true});
    listen(document, 'visibilitychange', resetMotion);
    listen(document, 'scroll', resetMotion, {passive: true, capture: true});
    listen(window, 'resize', resetMotion, {passive: true});
    listen(window, 'blur', resetMotion);
    listenMedia(fine, resetMotion);
    listenMedia(reduced, resetMotion);
    const Observer = window.MutationObserver;
    if (typeof Observer === 'function') {
      observer = new Observer(scheduleScan);
      observer.observe(document.documentElement, {childList: true, subtree: true});
    }
    scan();
  }
  function refresh() {
    if (destroyed) return;
    if (!initialized) initialize();
    else scan();
  }
  function destroy() {
    if (destroyed) return;
    destroyed = true;
    deactivate();
    if (scanFrame !== null) cancelAnimationFrame(scanFrame);
    scanFrame = null;
    observer?.disconnect();
    observer = null;
    cleanups.splice(0).forEach(remove => remove());
    for (const element of [...bound.keys()]) restore(element);
  }

  window.SparkReferenceMotion = Object.freeze({refresh, destroy});
  if (document.readyState === 'loading') listen(document, 'DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
