/* Manual login and export stay on the user's computer. No server requests. */
(function () {
  'use strict';
  const HELPER_ORIGIN = 'http://127.0.0.1:17653';
  const PREFIX = 'sparkkeeper-local-cookie-';
  const HELPER_HINT = '请先在你的电脑上双击“启动本地Cookie助手.bat”，并配置允许连接的网站，再回到这里提取；请允许打开本地助手窗口。';
  const ACTIVE = ['starting', 'waiting', 'cancelling'];
  const TERMINAL = ['success', 'failed', 'cancelled', 'timeout'];
  function requestId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
    return 'request-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
  function create(options = {}) {
    const origin = window.location.origin;
    let disposed = false, generation = 0;
    let popup = null, readyResolve = null, ackResolve = null;
    let readyTimer = null, ackTimer = null, closeTimer = null, deadlineTimer = null, cancelTimer = null;
    let state = {status: 'idle', running: false, request_id: null, job_id: null, count: 0, error: '', deadline: 0, stage: ''};
    function snapshot() { return {...state}; }
    function publish(patch) {
      state = {...state, ...patch};
      if (!disposed && typeof options.onChange === 'function') options.onChange(snapshot());
    }
    function clearTimers() {
      for (const timer of [readyTimer, ackTimer, closeTimer, deadlineTimer, cancelTimer]) if (timer !== null) clearTimeout(timer);
      readyTimer = ackTimer = closeTimer = deadlineTimer = cancelTimer = null;
    }
    function settleHandshake(value) {
      const ready = readyResolve, ack = ackResolve;
      readyResolve = ackResolve = null;
      if (ready) ready(value);
      if (ack) ack(value);
    }
    function closePopup() {
      const current = popup; popup = null;
      try { if (current && !current.closed) current.close(); } catch (_) {}
    }
    function finish(status, error = '', count = state.count) {
      clearTimers(); settleHandshake(false); if (status !== 'success') closePopup();
      publish({status, running: false, error, count, stage: ''});
    }
    function current(attempt) { return !disposed && generation === attempt; }
    function validOrigin() {
      try {
        const url = new URL(origin);
        return url.origin === origin && (url.protocol === 'https:' || url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname));
      } catch (_) { return false; }
    }
    function sendCancel() {
      try {
        if (popup && !popup.closed && state.request_id) popup.postMessage({type: PREFIX + 'cancel', payload: {request_id: state.request_id, job_id: state.job_id}}, HELPER_ORIGIN);
      } catch (_) {}
    }
    function watchPopup(attempt) {
      if (!current(attempt) || !state.running || !popup) return;
      closeTimer = setTimeout(() => {
        closeTimer = null;
        if (!current(attempt) || !state.running) return;
        if (!popup || popup.closed) { finish('cancelled', '本地助手窗口已关闭，请重新发起本机提取。'); return; }
        watchPopup(attempt);
      }, document.hidden ? 2000 : 500);
    }
    function onMessage(event) {
      if (disposed || !popup || event.origin !== HELPER_ORIGIN || event.source !== popup || !event.data || typeof event.data !== 'object') return;
      const type = event.data.type, payload = event.data.payload || {};
      if (type === PREFIX + 'ready' && readyResolve && payload.origin === origin) {
        const resolve = readyResolve; readyResolve = null;
        clearTimeout(readyTimer); readyTimer = null; resolve(true); return;
      }
      if (!state.running) return;
      if (type === PREFIX + 'error' && state.stage === 'helper') { finish('failed', '本地助手尚未就绪。' + HELPER_HINT); return; }
      if (payload.request_id !== state.request_id) return;
      if (type === PREFIX + 'ack' && payload.ok === true && typeof payload.job_id === 'string' && payload.job_id) {
        if (state.job_id && payload.job_id !== state.job_id) return;
        if (state.status === 'cancelling') { publish({job_id: payload.job_id}); sendCancel(); return; }
        if (ackResolve) {
          publish({job_id: payload.job_id});
          const resolve = ackResolve; ackResolve = null;
          clearTimeout(ackTimer); ackTimer = null; resolve(true); return;
        }
      }
      if (type === PREFIX + 'error') {
        if (payload.job_id && state.job_id && payload.job_id !== state.job_id) return;
        sendCancel(); finish('failed', '本机提取未完成，请查看本地助手后重试。'); return;
      }
      if (type !== PREFIX + 'progress' || !state.job_id || payload.job_id !== state.job_id) return;
      if (TERMINAL.includes(payload.status)) {
        const count = Math.max(0, Math.min(10000, Math.floor(Number(payload.count) || 0)));
        const error = payload.status === 'failed' ? '本机提取未完成，请查看本地助手后重试。' : payload.status === 'timeout' ? '五分钟登录期限已到，请重新发起本机提取。' : payload.status === 'cancelled' ? '本次本机提取已取消。' : '';
        finish(payload.status, error, count); return;
      }
      if (ACTIVE.includes(payload.status) && state.status !== 'cancelling') publish({status: payload.status, running: true, error: '', stage: ''});
    }
    // The local popup opens synchronously within the user's click activation.
    async function start() {
      if (disposed || state.running) return false;
      if (!validOrigin()) { publish({status: 'failed', running: false, error: '请从 HTTPS 网站或本机 localhost 页面启动提取。', stage: ''}); return false; }
      const attempt = ++generation;
      clearTimers(); closePopup();
      publish({status: 'starting', running: true, request_id: requestId(), job_id: null, count: 0, error: '', deadline: Date.now() + 300000, stage: 'helper'});
      const ready = new Promise(resolve => { readyResolve = resolve; });
      try { popup = window.open(HELPER_ORIGIN + '/#origin=' + encodeURIComponent(origin), '_blank', 'popup,width=560,height=500'); } catch (_) { popup = null; }
      if (!popup) { finish('failed', '浏览器未能打开本地助手窗口。' + HELPER_HINT); return false; }
      readyTimer = setTimeout(() => { readyTimer = null; if (current(attempt) && readyResolve) finish('failed', '未连接到本地 Cookie 助手。' + HELPER_HINT); }, 5000);
      watchPopup(attempt);
      if (!await ready || !current(attempt) || !state.running) return false;
      const acknowledged = new Promise(resolve => { ackResolve = resolve; });
      publish({stage: 'launch'});
      ackTimer = setTimeout(() => { ackTimer = null; if (current(attempt) && ackResolve) { sendCancel(); finish('failed', '本地助手未能启动提取。' + HELPER_HINT); } }, 10000);
      try { popup.postMessage({type: PREFIX + 'start', payload: {origin, request_id: state.request_id, deadline: state.deadline / 1000}}, HELPER_ORIGIN); }
      catch (_) { finish('failed', '本地助手连接已中断。' + HELPER_HINT); return false; }
      if (!await acknowledged || !current(attempt) || !state.running) return false;
      publish({status: 'waiting', running: true, stage: '', error: ''});
      deadlineTimer = setTimeout(() => { deadlineTimer = null; if (current(attempt) && state.running) { sendCancel(); finish('timeout', '五分钟登录期限已到，本机窗口已关闭。'); } }, Math.max(0, state.deadline - Date.now()));
      return true;
    }
    function cancel() {
      if (disposed || !state.running || state.status === 'cancelling') return false;
      generation++; clearTimers(); settleHandshake(false);
      if (state.stage === 'helper') { finish('cancelled', '本次本机提取已取消。'); return true; }
      sendCancel(); publish({status: 'cancelling', running: true, stage: 'cancel', error: ''});
      const attempt = generation;
      watchPopup(attempt);
      cancelTimer = setTimeout(() => { cancelTimer = null; if (current(attempt) && state.running) finish('failed', '取消结果尚未确认，请查看本地助手；网站凭据未修改。'); }, 30000);
      return true;
    }
    function onVisibility() {
      if (!disposed && state.running && popup) {
        if (closeTimer !== null) clearTimeout(closeTimer);
        closeTimer = null; watchPopup(generation);
      }
    }
    function dispose() {
      if (disposed) return;
      if (state.running) sendCancel();
      disposed = true; generation++;
      clearTimers(); settleHandshake(false); if (state.status !== 'success') closePopup(); else popup = null;
      window.removeEventListener('message', onMessage);
      window.removeEventListener('pagehide', dispose);
      document.removeEventListener('visibilitychange', onVisibility);
      state = {...state, running: false};
    }
    window.addEventListener('message', onMessage);
    window.addEventListener('pagehide', dispose);
    document.addEventListener('visibilitychange', onVisibility);
    return Object.freeze({start, cancel, dispose, snapshot});
  }
  window.SparkLocalCookieBridge = Object.freeze({create, helperOrigin: HELPER_ORIGIN});
})();