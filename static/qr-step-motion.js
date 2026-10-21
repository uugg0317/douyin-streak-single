/* Presentation only: confirmed QR stages move right, never advance on a clock alone. */
(function () {
  'use strict';
  const DURATION = 420;
  const STAGES = {starting: 0, waiting: 0, qr_ready: 0, qrready: 0, scanned: 1, confirming: 1, saving: 2};
  const STOP = new Set(['failed', 'error', 'cancelled', 'timeout', 'cancelling', 'blocked']);

  function create(options = {}) {
    let disposed = false, timer = null, epoch = 0, sequence = 0;
    let lastStatus = 'idle', terminal = false, successConfirmed = false;
    const media = typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    let reduced = !!media?.matches;
    let state = {index: -1, completed: 0, arrived: true, runKey: 0, finalSuccess: false, confirmedIndex: -1};

    const snapshot = () => ({...state});
    function publish(patch) {
      const changed = Object.keys(patch).some(key => state[key] !== patch[key]);
      state = {...state, ...patch};
      if (changed && !disposed && typeof options.onChange === 'function') options.onChange(snapshot());
    }
    function stopTimer() {
      epoch++;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    }
    function completion(index) {
      return index < 0 ? 0 : successConfirmed && index === 3 ? 4 : Math.min(index, state.confirmedIndex);
    }
    function settle() {
      publish({arrived: true, completed: completion(state.index),
        finalSuccess: successConfirmed && state.index === 3});
    }
    function scheduleArrival() {
      const turn = ++epoch;
      timer = setTimeout(() => {
        if (disposed || turn !== epoch) return;
        timer = null;
        settle();
        advance();
      }, DURATION);
    }
    function advance() {
      if (disposed || terminal || timer !== null) return;
      if (reduced) {
        publish({index: state.confirmedIndex});
        settle();
        return;
      }
      if (state.index < state.confirmedIndex) {
        publish({index: state.index + 1, arrived: false, finalSuccess: false});
        scheduleArrival();
      } else if (!state.arrived) settle();
    }
    function reset(runKey) {
      stopTimer();
      terminal = successConfirmed = false;
      sequence++;
      publish({index: -1, completed: 0, arrived: true, finalSuccess: false,
        confirmedIndex: -1, runKey: runKey === undefined ? sequence : runKey});
    }
    function update(value) {
      if (disposed || !value || typeof value !== 'object') return snapshot();
      const status = typeof value.status === 'string' ? value.status : value.state;
      if (typeof status !== 'string') return snapshot();
      const suppliedKey = (typeof value.runKey === 'string' && value.runKey.length > 0 && value.runKey.length <= 128)
        || (typeof value.runKey === 'number' && Number.isFinite(value.runKey)) ? value.runKey : undefined;
      const newKey = suppliedKey !== undefined && suppliedKey !== state.runKey;
      const newStart = status === 'starting' && value.hasJob !== true && lastStatus !== 'starting';
      if (newKey || newStart) reset(suppliedKey);
      lastStatus = status;

      if (status === 'idle' && !value.running && !value.hasJob) {
        stopTimer();
        terminal = successConfirmed = false;
        publish({index: -1, completed: 0, arrived: true, finalSuccess: false, confirmedIndex: -1});
        return snapshot();
      }
      if (STOP.has(status)) {
        stopTimer();
        terminal = true;
        successConfirmed = false;
        publish({arrived: true, finalSuccess: false, completed: Math.min(state.completed, Math.max(0, state.index))});
        return snapshot();
      }
      // Errors/verification/synchronization can only retain confirmed progress.
      // A same-job QR refresh also retains its position rather than flashing left.
      terminal = false;
      let target = STAGES[status];
      const cleanupComplete = value.cleanupComplete !== false && value.cleanup_complete !== false;
      const saved = value.saved === true;
      if (status === 'success') {
        successConfirmed = cleanupComplete;
        target = cleanupComplete ? 3 : 2;
      } else if (status === 'cleanup_pending') {
        successConfirmed = false;
        if (saved) target = 2;
        // Even a prematurely labelled success must not show stage 4 while the
        // server still says its browser cleanup is incomplete.
        if (state.confirmedIndex > 2 || state.index > 2) {
          stopTimer();
          publish({index: Math.min(state.index, 2), confirmedIndex: 2,
            completed: Math.min(state.completed, 2), arrived: true, finalSuccess: false});
        }
      }
      if (target === undefined) {
        target = state.confirmedIndex;
        if (target < 0 && (value.running || value.hasJob)) target = 0;
      }
      if (typeof target !== 'number' || target < 0) return snapshot();
      const confirmed = Math.max(state.confirmedIndex, Math.min(3, target));
      publish({confirmedIndex: confirmed});
      if (state.index < 0) {
        // Render the first stage for a frame-sized segment when an initial
        // observation already confirms several stages, instead of skipping it.
        publish({index: 0, arrived: reduced || confirmed === 0, completed: 0});
        if (!reduced && confirmed > 0) scheduleArrival();
      }
      advance();
      if (state.index === state.confirmedIndex && state.arrived) settle();
      return snapshot();
    }
    function onPreference(event) {
      if (disposed) return;
      reduced = !!event.matches;
      if (reduced && !terminal) {
        stopTimer();
        publish({index: state.confirmedIndex});
        settle();
      }
    }
    if (media?.addEventListener) media.addEventListener('change', onPreference);
    else if (media?.addListener) media.addListener(onPreference);
    function dispose() {
      if (disposed) return;
      disposed = true;
      stopTimer();
      if (media?.removeEventListener) media.removeEventListener('change', onPreference);
      else if (media?.removeListener) media.removeListener(onPreference);
    }
    return Object.freeze({update, snapshot, dispose});
  }
  window.SparkQrStepMotion = Object.freeze({create, duration: DURATION});
})();
