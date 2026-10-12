/* Decorative silk only: no network, account data, external assets or pointer effects. */
(function () {
  'use strict';

  const PREFERENCE_KEY = 'spark_silk_motion';
  const EVENT_NAME = 'spark-silk-change';
  const reduceQuery = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const mobileQuery = window.matchMedia?.('(max-width: 768px)');
  const cleanups = [];
  let canvas = null, gl = null, program = null, buffer = null, vertexShader = null, fragmentShader = null;
  let timeUniform = null, aspectUniform = null, widthUniform = null, heightUniform = null;
  let initialized = false, destroyed = false, contextLost = false, available = false;
  let enabled = true, inputPaused = false, mobile = false, targetFps = 120;
  let raf = null, resizeTimer = null, lastTick = null, drawBudget = 0, sceneTime = 0;
  let fpsStartedAt = null, fpsDraws = 0, lastEventKey = '';
  try { enabled = localStorage.getItem(PREFERENCE_KEY) !== 'off'; } catch (_) {}

  const stats = {
    frames: 0, rafTicks: 0, sceneTime: 0, observedFps: 0, targetFps: 120,
    pixelWidth: 0, pixelHeight: 0, pixelRatio: 0, mobile: false,
    enabled, running: false, reducedMotion: !!reduceQuery?.matches,
    hidden: !!document.hidden, inputPaused: false, available: false, contextLost: false
  };
  window.__silkStats = stats;

  const vertexSource = `
    attribute vec2 aPosition;
    varying vec2 vUv;
    void main() {
      vUv = aPosition * 0.5 + 0.5;
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }
  `;

  // Restore the pre-redesign flowing texture; keep the current renderer lifecycle.
  const fragmentSource = `
    precision highp float;
    varying vec2 vUv;
    uniform float uTime;
    uniform float uAspect;
    uniform float uWidth;
    uniform float uHeight;
    vec3 permute(vec3 x) { return mod(((x*34.0)+1.0)*x, 289.0); }
    float snoise(vec2 v){
      const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
      vec2 i  = floor(v + dot(v, C.yy));
      vec2 x0 = v -   i + dot(i, C.xx);
      vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
      vec4 x12 = x0.xyxy + C.xxzz;
      x12.xy -= i1;
      i = mod(i, 289.0);
      vec3 p = permute( permute( i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0 ));
      vec3 m = max(0.5 - vec3(dot(x0,x0), dot(x12.xy,x12.xy), dot(x12.zw,x12.zw)), 0.0);
      m = m*m; m = m*m;
      vec3 x = 2.0 * fract(p * C.www) - 1.0;
      vec3 h = abs(x) - 0.5;
      vec3 ox = floor(x + 0.5);
      vec3 a0 = x - ox;
      m *= 1.79284291400159 - 0.85373472095314 * ( a0*a0 + h*h );
      vec3 g;
      g.x  = a0.x  * x0.x  + h.x  * x0.y;
      g.yz = a0.yz * x12.xz + h.yz * x12.yw;
      return 130.0 * dot(m, g);
    }
    float fbm(vec2 p) {
      float v = 0.0;
      float a = 0.5;
      for(int i = 0; i < 3; i++) { v += a * snoise(p); p *= 2.0; a *= 0.5; }
      return v;
    }
    vec2 rotate(vec2 p, float a) {
      float c = cos(a), s = sin(a);
      return vec2(p.x * c - p.y * s, p.x * s + p.y * c);
    }
    void main() {
      vec2 uResolution = vec2(uWidth, uHeight);
      vec2 uv = vUv;
      vec2 p = uv * 2.0;
      float aspect = uAspect;
      p.x *= aspect;
      float t = uTime * 0.02;
      float wave1 = sin(p.x * 1.2 + p.y * 0.6 + t * 0.4) * 0.5;
      float wave2 = sin(p.x * 0.5 - p.y * 0.9 + t * 0.25 + 2.0) * 0.5;
      float wave3 = cos(p.x * 0.8 + p.y * 1.1 + t * 0.3 + 4.0) * 0.3;
      float waves = wave1 + wave2 + wave3;
      vec2 q = vec2(fbm(p * 0.7 + vec2(0.0, t) + waves * 0.12), fbm(p * 0.7 + vec2(5.2, 1.3 + t * 0.5) + waves * 0.1));
      float f = fbm(p * 0.8 + 1.6 * q + t * 0.2 + waves * 0.08);
      float flow = f * 0.55 + waves * 0.35 + q.x * 0.25;
      vec3 baseColor = vec3(0.008, 0.006, 0.004);
      vec3 goldDeepest = vec3(0.12, 0.08, 0.03);
      vec3 goldDeep = vec3(0.25, 0.17, 0.06);
      vec3 goldMid = vec3(0.45, 0.32, 0.10);
      vec3 gold = vec3(0.75, 0.58, 0.20);
      vec3 goldBright = vec3(0.90, 0.76, 0.40);
      vec3 goldLight = vec3(0.97, 0.89, 0.65);
      vec3 goldWhite = vec3(1.0, 0.96, 0.82);
      vec3 warmWhite = vec3(1.0, 0.98, 0.92);
      vec3 col = baseColor;
      col = mix(col, goldDeepest, smoothstep(-0.35, -0.05, flow));
      col = mix(col, goldDeep, smoothstep(-0.15, 0.15, flow));
      col = mix(col, goldMid, smoothstep(0.0, 0.3, flow));
      col = mix(col, gold, smoothstep(0.15, 0.4, flow));
      col = mix(col, goldBright, smoothstep(0.3, 0.5, flow));
      col = mix(col, goldLight, smoothstep(0.45, 0.65, flow));
      col = mix(col, goldWhite, smoothstep(0.6, 0.78, flow));
      col = mix(col, warmWhite, smoothstep(0.75, 0.9, flow));
      float e = 0.002;
      vec2 grad = vec2(fbm(p * 0.8 + vec2(e, 0.0) + q * 0.6 + vec2(t * 0.1, 0.0)) - fbm(p * 0.8 - vec2(e, 0.0) + q * 0.6 + vec2(t * 0.1, 0.0)), fbm(p * 0.8 + vec2(0.0, e) + q * 0.6 + vec2(0.0, t * 0.08)) - fbm(p * 0.8 - vec2(0.0, e) + q * 0.6 + vec2(0.0, t * 0.08))) / (2.0 * e);
      float silk1 = smoothstep(0.15, 0.65, 1.0 - length(grad - 0.25));
      col += silk1 * vec3(0.5, 0.4, 0.2) * 0.12;
      float silk2 = pow(smoothstep(0.35, 0.75, 1.0 - length(grad * 1.4 - 0.4 + sin(t * 0.5) * 0.1)), 2.0);
      col += silk2 * vec3(0.7, 0.65, 0.5) * 0.15;
      float silk3 = snoise(p * 5.0 + grad * 1.5 + t * 0.3) * 0.5 + 0.5;
      col += silk3 * 0.03 * vec3(0.95, 0.85, 0.55);
      for(int i = 0; i < 3; i++) {
        float fi = float(i);
        vec2 rp = rotate(p - vec2(0.0, 0.0), t * 0.03 + fi * 2.094);
        float band = sin(rp.x * 1.5 + rp.y * 0.8 + t * (0.3 + fi * 0.1) + fi * 3.0) * 0.5 + 0.5;
        band = pow(band, 8.0 + fi * 3.0);
        float bandMask = smoothstep(0.2, 0.6, flow + 0.2);
        col += band * bandMask * vec3(0.7, 0.6, 0.35) * (0.08 - fi * 0.015);
      }
      float arcHighlight = smoothstep(0.5, 0.82, sin(flow * 3.14 + t * 0.15) * 0.5 + 0.5);
      col += arcHighlight * vec3(0.85, 0.7, 0.38) * 0.15;
      vec2 gp = p * 3.0;
      float goldNoise = snoise(gp + vec2(t * 0.15, t * 0.1));
      float goldNoise2 = snoise(gp * 2.3 + vec2(-t * 0.12, t * 0.08));
      float particles = smoothstep(0.55, 0.85, goldNoise) * smoothstep(0.4, 0.7, goldNoise2);
      float twinkle = sin(uTime * 2.0 + goldNoise * 20.0) * 0.5 + 0.5;
      particles *= 0.5 + twinkle * 0.5;
      col += particles * vec3(0.7, 0.6, 0.4) * 0.18;
      float fineDust = snoise(p * 8.0 + vec2(t * 0.2, -t * 0.15)) * 0.5 + 0.5;
      fineDust = pow(fineDust, 6.0);
      col += fineDust * vec3(1.0, 0.85, 0.5) * 0.12;
      vec2 center = uv - 0.5;
      float vignette = 1.0 - dot(center, center) * 1.5;
      vignette = smoothstep(0.0, 0.75, vignette);
      col *= vignette;
      float spot = 1.0 - length(center) * 0.85;
      spot = smoothstep(0.0, 0.65, spot);
      col += spot * 0.06 * goldBright;
      float topGlow = smoothstep(0.0, 0.4, 1.0 - abs(uv.y - 0.15) * 3.0) * smoothstep(0.0, 0.5, uv.x) * smoothstep(0.0, 0.5, 1.0 - uv.x);
      col += topGlow * vec3(0.6, 0.45, 0.2) * 0.08;
      float grain = fract(sin(dot(uv * uResolution, vec2(12.9898, 78.233)) + uTime) * 43758.5453);
      col += (grain - 0.5) * 0.015;
      col = col / (col + vec3(0.5));
      col = pow(col, vec3(0.85));
      col *= vec3(1.02, 1.0, 0.96);
      gl_FragColor = vec4(col, 1.0);
    }
  `;

  function getState() {
    return {enabled, running: raf !== null, reducedMotion: !!reduceQuery?.matches,
      available, targetFps, inputPaused, mobile, destroyed};
  }

  function diagnostics() {
    const state = getState();
    Object.assign(stats, state, {sceneTime, hidden: !!document.hidden, contextLost});
    if (canvas) {
      canvas.setAttribute('data-silk-running', String(state.running));
      canvas.setAttribute('data-silk-enabled', String(enabled));
      canvas.setAttribute('data-silk-reduced', String(state.reducedMotion));
      canvas.setAttribute('data-silk-available', String(available));
      canvas.setAttribute('data-silk-target-fps', String(targetFps));
      publishMeasurements();
    }
  }

  // Low-frequency DOM diagnostics allow read-only browser verification without
  // inspecting application state or updating Vue on each frame.
  function publishMeasurements() {
    if (!canvas) return;
    canvas.setAttribute('data-silk-frames', String(stats.frames));
    canvas.setAttribute('data-silk-observed-fps', String(stats.observedFps));
    canvas.setAttribute('data-silk-scene-time', String(Math.round(sceneTime * 1000) / 1000));
    canvas.setAttribute('data-silk-pixels', stats.pixelWidth + 'x' + stats.pixelHeight);
  }

  function announce() {
    diagnostics();
    const detail = {enabled, running: raf !== null, reducedMotion: !!reduceQuery?.matches, available, targetFps};
    const key = JSON.stringify(detail);
    if (key === lastEventKey) return;
    lastEventKey = key;
    if (typeof window.CustomEvent === 'function') window.dispatchEvent(new window.CustomEvent(EVENT_NAME, {detail}));
  }

  function listen(target, event, callback, options) {
    if (!target?.addEventListener) return;
    target.addEventListener(event, callback, options);
    cleanups.push(() => target.removeEventListener(event, callback, options));
  }

  function listenMedia(query, callback) {
    if (!query) return;
    if (query.addEventListener) listen(query, 'change', callback);
    else if (query.addListener) {
      query.addListener(callback);
      cleanups.push(() => query.removeListener(callback));
    }
  }

  function releaseRenderer() {
    if (gl && !contextLost) {
      if (buffer) gl.deleteBuffer(buffer);
      if (program) gl.deleteProgram(program);
      if (vertexShader) gl.deleteShader(vertexShader);
      if (fragmentShader) gl.deleteShader(fragmentShader);
    }
    buffer = program = vertexShader = fragmentShader = null;
    timeUniform = aspectUniform = widthUniform = heightUniform = null;
    available = false;
  }

  function compile(kind, source) {
    const shader = gl.createShader(kind);
    if (!shader) throw new Error('Shader unavailable');
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      throw new Error('Shader unavailable');
    }
    return shader;
  }

  function createRenderer() {
    try {
      gl = canvas.getContext('webgl', {alpha: false, antialias: false, depth: false, stencil: false,
        preserveDrawingBuffer: true, powerPreference: mobile ? 'low-power' : 'high-performance'});
      if (!gl) return false;
      const precision = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT);
      const fragment = precision?.precision > 0 ? fragmentSource : fragmentSource.replace('precision highp float;', 'precision mediump float;');
      vertexShader = compile(gl.VERTEX_SHADER, vertexSource);
      fragmentShader = compile(gl.FRAGMENT_SHADER, fragment);
      program = gl.createProgram();
      if (!program) throw new Error('Program unavailable');
      gl.attachShader(program, vertexShader);
      gl.attachShader(program, fragmentShader);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('Program unavailable');
      gl.useProgram(program);
      buffer = gl.createBuffer();
      if (!buffer) throw new Error('Buffer unavailable');
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
      const position = gl.getAttribLocation(program, 'aPosition');
      if (position < 0) throw new Error('Position unavailable');
      gl.enableVertexAttribArray(position);
      gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
      timeUniform = gl.getUniformLocation(program, 'uTime');
      aspectUniform = gl.getUniformLocation(program, 'uAspect');
      widthUniform = gl.getUniformLocation(program, 'uWidth');
      heightUniform = gl.getUniformLocation(program, 'uHeight');
      available = true;
      return true;
    } catch (_) {
      releaseRenderer();
      gl = null;
      return false;
    }
  }

  function measureCanvas() {
    if (!canvas || destroyed) return;
    const width = Math.max(1, Math.round(canvas.clientWidth || window.innerWidth || 1));
    const height = Math.max(1, Math.round(canvas.clientHeight || window.innerHeight || 1));
    mobile = mobileQuery ? mobileQuery.matches : width <= 768;
    targetFps = mobile ? 30 : 120;
    const dpr = Math.max(0.5, Math.min(window.devicePixelRatio || 1, mobile ? 0.8 : 1.25));
    const maxPixels = mobile ? 460800 : 1800000;
    const maxWidth = mobile ? 640 : 1920;
    const maxHeight = mobile ? 960 : 1440;
    const scale = Math.min(dpr, maxWidth / width, maxHeight / height, Math.sqrt(maxPixels / (width * height)));
    canvas.width = Math.max(1, Math.floor(width * scale));
    canvas.height = Math.max(1, Math.floor(height * scale));
    stats.pixelWidth = canvas.width;
    stats.pixelHeight = canvas.height;
    stats.pixelRatio = Math.min(canvas.width / width, canvas.height / height);
    if (available) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.useProgram(program);
      gl.uniform1f(aspectUniform, width / height);
      gl.uniform1f(widthUniform, width);
      gl.uniform1f(heightUniform, height);
    }
    drawBudget = 1000 / targetFps;
    diagnostics();
  }

  function render(timestamp = null) {
    if (!available || destroyed || contextLost || document.hidden) return;
    gl.useProgram(program);
    gl.uniform1f(timeUniform, sceneTime);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    stats.frames++;
    stats.sceneTime = sceneTime;
    if (timestamp !== null) {
      if (fpsStartedAt === null) { fpsStartedAt = timestamp; fpsDraws = 0; }
      else fpsDraws++;
      if (timestamp - fpsStartedAt >= 1000) {
        stats.observedFps = Math.round(fpsDraws * 10000 / (timestamp - fpsStartedAt)) / 10;
        fpsStartedAt = timestamp;
        fpsDraws = 0;
        publishMeasurements();
      }
    }
  }

  function canRun() {
    return initialized && !destroyed && available && !contextLost && enabled && !inputPaused && !document.hidden && !reduceQuery?.matches;
  }

  function stopLoop() {
    if (raf !== null) cancelAnimationFrame(raf);
    raf = null;
    lastTick = null;
    drawBudget = 1000 / targetFps;
    fpsStartedAt = null;
    fpsDraws = 0;
    stats.observedFps = 0;
    publishMeasurements();
  }

  function tick(timestamp) {
    raf = null;
    if (!canRun()) { announce(); return; }
    stats.rafTicks++;
    const delta = lastTick === null ? 0 : Math.max(0, Math.min(50, timestamp - lastTick));
    lastTick = timestamp;
    sceneTime += delta / 1000;
    drawBudget += delta;
    const interval = 1000 / targetFps;
    if (drawBudget + 0.01 >= interval) {
      const intervals = Math.max(1, Math.floor((drawBudget + 0.01) / interval));
      drawBudget = Math.max(0, drawBudget - intervals * interval);
      render(timestamp);
    }
    raf = requestAnimationFrame(tick);
    stats.running = true;
    stats.sceneTime = sceneTime;
  }

  function syncLoop() {
    if (!canRun()) stopLoop();
    else if (raf === null) {
      lastTick = null;
      drawBudget = 1000 / targetFps;
      raf = requestAnimationFrame(tick);
    }
    announce();
  }

  function resizeNow() {
    resizeTimer = null;
    if (destroyed) return;
    measureCanvas();
    render();
    syncLoop();
  }

  function scheduleResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeNow, 80);
  }

  function setEnabled(value) {
    if (destroyed) return getState();
    enabled = !!value;
    try { localStorage.setItem(PREFERENCE_KEY, enabled ? 'on' : 'off'); } catch (_) {}
    syncLoop();
    return getState();
  }

  function setInputPaused(value) {
    if (destroyed) return getState();
    inputPaused = !!value;
    syncLoop();
    return getState();
  }

  function initialize() {
    if (destroyed || initialized) return;
    canvas = document.getElementById('fluid-canvas');
    if (!canvas) { announce(); return; }
    initialized = true;
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.pointerEvents = 'none';
    canvas.style.background = 'radial-gradient(ellipse at 68% 32%, #4b3920 0%, #1b140b 32%, #060505 72%)';
    mobile = mobileQuery ? mobileQuery.matches : (window.innerWidth || 0) <= 768;
    createRenderer();
    measureCanvas();
    render();
    listen(window, 'resize', scheduleResize, {passive: true});
    listen(document, 'visibilitychange', () => {
      if (document.hidden) { clearTimeout(resizeTimer); resizeTimer = null; }
      else { measureCanvas(); render(); }
      syncLoop();
    });
    listenMedia(reduceQuery, () => { if (!document.hidden) render(); syncLoop(); });
    listenMedia(mobileQuery, scheduleResize);
    listen(canvas, 'webglcontextlost', event => {
      event.preventDefault();
      contextLost = true;
      releaseRenderer();
      syncLoop();
    });
    listen(canvas, 'webglcontextrestored', () => {
      if (destroyed) return;
      contextLost = false;
      createRenderer();
      measureCanvas();
      render();
      syncLoop();
    });
    syncLoop();
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    stopLoop();
    clearTimeout(resizeTimer);
    resizeTimer = null;
    cleanups.splice(0).forEach(remove => remove());
    releaseRenderer();
    gl = null;
    announce();
  }

  window.SparkSilkBackground = Object.freeze({setEnabled, setInputPaused, getState, destroy});
  if (!document.getElementById('fluid-canvas') && document.readyState === 'loading') listen(document, 'DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
