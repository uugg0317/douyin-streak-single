/* Console lifecycle and actions; shared result formatting lives in console-results.js. */
const { createApp, ref, reactive, computed, onMounted, onUnmounted, onBeforeUnmount, nextTick, watch } = Vue;

const ICONS = {
  fire: `<svg class="icon-svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12.003 2c-.12 0-.24.03-.35.08-1.56.75-3.66 2.37-4.52 4.41-.9 2.13-.53 4.2.49 5.86-.48-.68-.86-1.48-1.02-2.35-.07-.37-.47-.59-.83-.46-.35.12-.55.49-.47.85.34 1.63 1.13 3.09 2.24 4.19C6.46 15.65 6 17.27 6 19c0 2.76 2.69 5 6 5s6-2.24 6-5c0-1.89-.54-3.57-1.74-4.88-.23-.25-.49-.48-.76-.69 1.15-1.12 1.95-2.61 2.29-4.28.08-.36-.12-.73-.47-.85-.36-.13-.76.09-.83.46-.16.87-.54 1.67-1.02 2.35 1.02-1.66 1.39-3.73.49-5.86-.86-2.04-2.96-3.66-4.52-4.41-.11-.05-.23-.08-.35-.08zM12 14c1.66 0 3 1.34 3 3 0 1.66-1.34 3-3 3s-3-1.34-3-3c0-1.66 1.34-3 3-3z"/></svg>`,
  grid: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>`,
  users: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>`,
  clock: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>`,
  terminal: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>`,
  key: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-1.5 1.5L14 9m-1.5 1.5l-2.5 2.5a6 6 0 1 1-8.5 8.5 6 6 0 0 1 8.5-8.5l2.5-2.5m1.5-1.5L16 7m1.5-1.5L20 4"></path></svg>`,
  mail: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>`,
  reload: `<svg class="icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6"></path><path d="M1 20v-6h6"></path><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>`
};

const app = createApp({
  setup() {
    const tab = ref('overview');
    const syncing = ref(false);
    const contactsSync = reactive({status:'idle', jobId:'', message:'', error:'', updatedAt:''});
    const contactsSyncTimeLabel = computed(() => {
      const raw = contactsSync.updatedAt;
      const date = new Date(typeof raw === 'number' && raw < 1e12 ? raw * 1000 : raw);
      return raw && Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12:false}) : '更新时间未确认';
    });
    let contactsSyncTimer = null;
    let contactsSyncRequest = null;
    let contactsSyncGeneration = 0;
    let contactsSyncChecks = 0;
    let contactsSyncPolling = false;
    let contactsSyncDisposed = false;
    const savingLedger = ref(false);
    const ledgerWriteBusy = ref(false);
    const ledgerWriteLabel = ref('');
    let ledgerWriteOwner = null;
    function beginLedgerWrite(label) {
      if (ledgerWriteBusy.value) {
        ElementPlus.ElMessage.warning('好友台账正在更新，请等待本次请求完成后再操作。');
        return null;
      }
      const owner = {epoch:managementEpoch};
      ledgerWriteOwner = owner;
      ledgerWriteBusy.value = true;
      ledgerWriteLabel.value = label;
      invalidateManagedRead('ledger');
      return owner;
    }
    function finishLedgerWrite(owner) {
      // A late finally from a logged-out session cannot unlock a new write.
      if (ledgerWriteOwner !== owner) return;
      ledgerWriteOwner = null;
      ledgerWriteBusy.value = false;
      ledgerWriteLabel.value = '';
    }
    const savingConfig = ref(false);
    // 管理后台登录状态；通过密码门后才进入控制台
    const authed = ref(false);
    const loginResult = ref('');
    const loginError = ref('');
    const loginLoading = ref(false);
    const logoutBusy = ref(false);
    const logoutPending = ref(false);
    // Store only the logout intent; no password, Cookie or capability enters storage.
    try { logoutPending.value = typeof sessionStorage !== 'undefined' && sessionStorage.getItem('dcs_logout_pending') === '1'; } catch (_) {}
    if (logoutPending.value) loginError.value = '本页已锁定，但服务器退出尚未确认，请点击“重试退出”。';

    // 移动端标记：仅移动端把 fixed 底栏/横幅 Teleport 到 body，脱离 #app 层叠上下文（PC 端留在 grid 内）
    const isMobile = ref(false);
    try {
      const mqList = window.matchMedia('(max-width: 768px)');
      isMobile.value = mqList.matches;
      const onMqChange = (e) => { isMobile.value = e.matches; };
      mqList.addEventListener('change', onMqChange);
      onUnmounted(() => mqList.removeEventListener('change', onMqChange));
    } catch (e) {}
    const connectionError = ref('');
    let managementEpoch = 0;
    let managementDisposed = false;
    const readSequences = Object.create(null);
    const managedReads = new Map();
    let statusReadPromise = null;
    let statusPollingEnabled = false;
    const managementCurrent = epoch => !managementDisposed && epoch === managementEpoch;
    function invalidateManagedRead(key) {
      readSequences[key] = (readSequences[key] || 0) + 1;
      managedReads.get(key)?.controller?.abort();
      managedReads.delete(key);
    }
    function beginManagedRead(key) {
      invalidateManagedRead(key);
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      const ticket = {key, epoch:managementEpoch, sequence:readSequences[key], controller};
      ticket.current = () => managementCurrent(ticket.epoch) && readSequences[key] === ticket.sequence;
      ticket.options = {timeout:15000, signal:controller?.signal, sparkReadKey:key, sparkReadSequence:ticket.sequence};
      managedReads.set(key, ticket);
      return ticket;
    }
    function finishManagedRead(ticket) {
      if (managedReads.get(ticket.key) === ticket) managedReads.delete(ticket.key);
    }
    function stopManagementReads() {
      statusPollingEnabled = false;
      if (statusTimer !== null) clearTimeout(statusTimer);
      statusTimer = null;
      statusReadPromise = null;
      for (const key of [...managedReads.keys()]) invalidateManagedRead(key);
    }
    function scheduleStatusPoll() {
      if (statusTimer !== null) clearTimeout(statusTimer);
      statusTimer = null;
      if (!statusPollingEnabled || !authed.value || managementDisposed || statusReadPromise || document.hidden) return;
      pollDelay = status.running || status.retry_dispatching ? 1500 : 10000;
      const epoch = managementEpoch;
      statusTimer = setTimeout(() => {
        statusTimer = null;
        if (managementCurrent(epoch) && authed.value && statusPollingEnabled && !document.hidden) loadStatus({reuse:true});
      }, pollDelay);
    }
    function onConsoleVisibility() {
      if (document.hidden) {
        if (statusTimer !== null) clearTimeout(statusTimer);
        statusTimer = null;
      } else if (statusPollingEnabled && authed.value && !managementDisposed) {
        loadStatus({reuse:true});
      }
    }
    document.addEventListener?.('visibilitychange', onConsoleVisibility);
    const appVersion = ref('');
    function rememberLogoutPending(pending) {
      logoutPending.value = pending;
      try { if (typeof sessionStorage !== 'undefined') { if (pending) sessionStorage.setItem('dcs_logout_pending', '1'); else sessionStorage.removeItem('dcs_logout_pending'); } } catch (_) {}
    }
    // URL 令牌 + 密码验证状态
    const urlAccessToken = ref('');
    // 裸域名是否直接显示访问密码框（服务端 PASSWORD_PUBLIC_LOGIN 开关）
    const publicPasswordMode = ref(false);
    // 手动切换到管理令牌登录卡
    const showAdminGate = ref(false);
    const urlPassword = ref('');
    function switchLoginGate(target) {
      showAdminGate.value = (target === 'admin');
      loginError.value = '';
    }
    // 公开端点（无需鉴权）：决定裸域名默认展示哪种登录卡
    if (typeof fetch === 'function') {
      fetch('/api/auth/mode', { cache: 'no-store' })
        .then(r => r.ok ? r.json() : null)
        .then(m => { if (m && m.password_public) publicPasswordMode.value = true; })
        .catch(() => { /* 拉取失败则保持默认：管理令牌登录卡 */ });
    }
    const urlPasswordLoading = ref(false);
    // 推荐把 access_token 放在 URL fragment（#access_token=...），fragment 不会
    // 发给 Nginx/应用日志；仍兼容旧 query 链接，并在读取后立刻清理地址栏。
    const urlParams = new URLSearchParams(window.location.search);
    const hashParams = new URLSearchParams(window.location.hash.replace(/^#/, ''));
    const urlTokenFromUrl = hashParams.get('access_token') || urlParams.get('access_token') || '';
    if (urlTokenFromUrl) {
      urlAccessToken.value = urlTokenFromUrl;
    }
    // 令牌只保留在当前页面内存，避免进入后续历史、书签或复制链接。
    const stripUrlToken = () => {
      try {
        const u = new URL(window.location.href);
        u.searchParams.delete('access_token');
        const hp = new URLSearchParams(u.hash.replace(/^#/, ''));
        hp.delete('access_token');
        const hash = hp.toString() ? '#' + hp.toString() : '';
        window.history.replaceState({}, '', u.pathname + (u.search || '') + hash);
      } catch (e) { /* 忽略：清理地址栏失败不影响登录 */ }
    };
    if (urlTokenFromUrl) stripUrlToken();

    function setFluidPaused(v) { window.SparkSilkBackground?.setInputPaused(!!v); }
    const backgroundMotion = reactive(window.SparkSilkBackground?.getState() || {enabled:true,available:false,reducedMotion:false,running:false});
    const updateBackgroundMotion = event => Object.assign(backgroundMotion, event.detail || {});
    window.addEventListener('spark-silk-change', updateBackgroundMotion);
    const backgroundMotionLabel = computed(() => backgroundMotion.reducedMotion ? '静态背景' : backgroundMotion.enabled && backgroundMotion.available ? '动态背景' : '静态背景');
    const backgroundMotionTitle = computed(() => backgroundMotion.reducedMotion ? '已遵循系统减少动态效果设置' : !backgroundMotion.available ? '当前设备使用静态背景' : backgroundMotion.enabled ? '暂停动态背景' : '开启动感背景');
    function toggleBackgroundMotion() { window.SparkSilkBackground?.setEnabled(!backgroundMotion.enabled); }
    let statusTimer = null;
    const searchKeyword = ref('');
    const showHistoricalContacts = ref(false);
    const showOnlySparkFriends = ref(false);
    const historicalContactCount = computed(() => contacts.value.filter(row => row.observation_status === 'not_observed').length);
    const avatarErrors = ref(new Set());
    const avatarProxy = (url) => url ? `/api/avatar?url=${encodeURIComponent(url)}` : '';

    // ── iOS 风格顶部横幅弹窗 ──────────────────────────────────────
    const iosBanner = reactive({ visible: false, title: '', subtitle: '' });
    let iosBannerTimer = null;
    const showIOSBanner = (title, subtitle, duration = 3200) => {
      iosBanner.title = title;
      iosBanner.subtitle = subtitle || '';
      iosBanner.visible = true;
      if (iosBannerTimer) clearTimeout(iosBannerTimer);
      iosBannerTimer = setTimeout(() => { iosBanner.visible = false; }, duration);
    };
    const logsContent = ref('');
    const logsRefreshing = ref(false);
    const logsRefreshMessage = ref('');
    const defaultQrLogin = {status:'idle',running:false,message:'点击开始扫码登录，在本页完成凭证更新。',error:'',imageUrl:'',deadline:0,hasJob:false,canRefresh:false,canRetrySync:false,canRetryImage:false,canRetryCleanup:false,canRetryStart:false,startUnconfirmed:false,saved:false,imageError:'',verificationImageError:'',verificationImageUrl:'',verificationReady:false,verificationRevision:null,verificationWidth:0,verificationHeight:0,verificationBusy:false,interactionPending:false,gestureActive:false};
    const qrLogin = reactive({...defaultQrLogin});
    let qrLoginController = null;
    const qrSteps = ['等待扫码', '手机确认', '保存凭证', '完成'];
    const qrClock = ref(Date.now());
    let qrClockTimer = null;
    const qrVisualStep = reactive({index:-1,completed:0,arrived:false,runKey:0,finalSuccess:false,confirmedIndex:-1});
    const qrStepMotion = window.SparkQrStepMotion?.create({onChange: snapshot => Object.assign(qrVisualStep, snapshot)}) || null;
    const qrStageIndex = computed(() => qrVisualStep.index);
    function updateQrPresentation(snapshot) {
      Object.assign(qrLogin, snapshot);
      if (qrStepMotion) qrStepMotion.update(snapshot);
    }
    const qrRemainingSeconds = computed(() => Math.max(0, Math.ceil((Number(qrLogin.deadline) - qrClock.value) / 1000)));
    const qrRemainingLabel = computed(() => Math.floor(qrRemainingSeconds.value / 60) + ':' + String(qrRemainingSeconds.value % 60).padStart(2, '0'));
    watch(() => qrLogin.running && !!qrLogin.deadline && qrLogin.status !== 'cleanup_pending', active => {
      if (qrClockTimer !== null) clearInterval(qrClockTimer);
      qrClockTimer = null;
      qrClock.value = Date.now();
      if (active) qrClockTimer = setInterval(() => { qrClock.value = Date.now(); }, 1000);
    });

    const navItems = [
      { name: 'overview', label: '概览', icon: 'grid' },
      { name: 'friends', label: '好友', icon: 'users' },
      { name: 'settings', label: '定时', icon: 'clock' },
      { name: 'logs', label: '日志', icon: 'terminal' },
      { name: 'credentials', label: '凭证', icon: 'key' },
      { name: 'email', label: '邮箱', icon: 'mail' },
    ];

    const defaultStatus = {
      version: '',
      state_file_exists: false,
      session_status: 'unknown',
      session_checked_at: null,
      session_check_stale: false,
      session_check_age_seconds: null,
      session_check_reason: null,
      ledger_recovery_required: false,
      task_phase: null,
      background_tasks_enabled: null,
      next_run: null,
      status_connected: false,
      running: false,
      stopping: false,
      consecutive_failures: 0,
      auto_paused_until: 0,
      send_progress: null,
      pending_retry_count: 0,
      retry_status: 'none',
      retry_dispatching: false,
      pending_retry_entries: [],
      active_send_diagnostics: [],
    };
    const status = reactive({...defaultStatus});
    const todaySummary = ref(null);
    const parseTodaySummary = value => {
      if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.business_date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value.business_date)) return null;
      const date = new Date(value.business_date + 'T00:00:00Z');
      if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0,10) !== value.business_date) return null;
      if (!['accepted','unconfirmed','pending','selected'].every(key => Number.isSafeInteger(value[key]) && value[key] >= 0) || typeof value.includes_historical_unconfirmed !== 'boolean') return null;
      return {business_date:value.business_date, accepted:value.accepted, unconfirmed:value.unconfirmed,
        pending:value.pending, selected:value.selected, includes_historical_unconfirmed:value.includes_historical_unconfirmed,
        historical_unreviewed:Number.isSafeInteger(value.historical_unreviewed) && value.historical_unreviewed >= 0 ? value.historical_unreviewed : 0,
        streak_warnings:(() => {
          const rows = Array.isArray(value.streak_warnings) ? value.streak_warnings : [];
          return rows.flatMap(item => {
            if (!item || typeof item !== 'object') return [];
            const name = typeof item.display_name === 'string' ? item.display_name.slice(0, 80) : '';
            const label = typeof item.streak_label === 'string' ? item.streak_label.slice(0, 32) : '';
            const days = Number(item.expires_in_days);
            if (!name || !/^[0-9]+\s*天后消失$/.test(label) || !Number.isSafeInteger(days) || days < 0 || days > 3) return [];
            return [{display_name:name, streak_label:label, expires_in_days:days, observed_at:typeof item.observed_at === 'string' ? item.observed_at.slice(0, 40) : ''}];
          });
        })()};
    };
    const backgroundPauseMessage = computed(() => status.status_connected && status.background_tasks_enabled === false
      ? (config.auto_run_enabled
        ? '每日自动续火花已打开，但后台任务被启动参数或恢复保护暂停；请到“定时”页查看原因。'
        : '每日自动续火花未开启：现在只能手动发送。可在“定时”页打开开关。') : '');
    const todayResultMessage = computed(() => {
      const value = status.status_connected && todaySummary.value;
      if (!value) return '尚未获取今日结果，请等待可靠的台账汇总。';
      const accepted = value.accepted > 0 ? `已确认接受 ${value.accepted} 人` : '今天暂无已确认的发送记录（已确认接受 0 人）';
      return `${accepted} · 待核对 ${value.unconfirmed} 人 · 安全待处理 ${value.pending} 人`;
    });
    const todayResultDetail = computed(() => {
      const value = status.status_connected && todaySummary.value;
      if (!value) return '运行记录或模拟演练不能代替发送证据，暂不推断今天是否已经发送。';
      return (value.historical_unreviewed > 0 ? `另有 ${value.historical_unreviewed} 人的往日结果未核对，不影响今天发送。` : '') +
        (value.unconfirmed > 0 ? '今天待核对的好友请在抖音看一眼，再到“好友”页点核对按钮。' : '') +
        '已确认接受不代表对方已读，也不保证火花增长。';
    });
    const streakWarningMessage = computed(() => {
      const warnings = (todaySummary.value && todaySummary.value.streak_warnings) || [];
      if (!warnings.length) return '';
      return '火花即将消失：' + warnings.map(item => item.display_name + '（' + item.streak_label + (item.observed_at ? '，观察于 ' + item.observed_at : '') + '）').join('、');
    });
    const activeDiagnosticMessage = computed(() => {
      const items = status.active_send_diagnostics || [];
      if (!items.length) return '';
      return '仍待核对：' + items.map(item => item.display_name + '（' + item.reason + '）').join('、') + '。确认发出后，这些诊断会标记为已取代。';
    });
    const baseDocTitle = () => `抖音自动续火花${status.version || appVersion.value ? ' ' + (status.version || appVersion.value) : ''} · Douyin Cloud Streak`;
    let pollDelay = 10000;

    const defaultConfig = {
      schedule_time: '00:00',
      jitter_minutes: 0,
      send_gap_min: 1,
      send_gap_max: 2,
      messages: ['[续火花吧]'],
      auto_run_enabled: true
    };
    const config = reactive({...defaultConfig});
    const lockedConfigFields = ref([]);
    const configFeedback = ref('');
    const configLabels = {messages:'发送文案', schedule_time:'执行时间', jitter_minutes:'启动延迟', send_gap_min:'最小发送间隔', send_gap_max:'最大发送间隔', auto_run_enabled:'自动任务开关'};
    const configLockMessage = computed(() => '本页配置只读，显示后台当前生效值。' + (lockedConfigFields.value.length ? '固定字段：' + lockedConfigFields.value.map(key => configLabels[key] || key).join('、') + '；修改会被后台拒绝。' : '未开放的控件不能在本页修改。'));
    const applyConfigResponse = data => {
      const object = value => value && typeof value === 'object' && !Array.isArray(value);
      const source = object(data?.effective_config) ? data.effective_config : object(data?.config) ? data.config : data || {};
      // Metadata lives outside the business configuration and is never echoed back.
      for (const key of Object.keys(config)) if (!Object.hasOwn(defaultConfig, key)) delete config[key];
      for (const key of Object.keys(defaultConfig)) if (Object.hasOwn(source, key)) config[key] = Array.isArray(source[key]) ? [...source[key]] : source[key];
      lockedConfigFields.value = Array.isArray(data?.locked_fields) ? data.locked_fields.filter(key => typeof key === 'string' && Object.hasOwn(defaultConfig, key)) : [];
      messagesText.value = Array.isArray(config.messages) ? config.messages.join('\n') : '';
    };

    const messagesText = ref('');
    const contacts = ref([]);
    const contactsCapture = ref(null);
    // Server-confirmed selection is independent from the editable checkboxes.
    const savedSelection = ref([]);
    const savedLedgerContacts = ref([]);
    const ledgerVersion = ref(null);
    const ledgerReady = ref(false);
    const selectionFeedback = ref('');
    const lastRunInfo = reactive({
      time: '',
      summary: '',
      success: false
    });
    const lastRunDetails = ref([]);
    const resultDetailsOpen = ref(false);
    const {describeLastRun, summarizeLastRun} = window.SparkConsoleResults;

    const api = axios.create({headers: {'X-Requested-With': 'sparkkeeper'}});
    api.interceptors.request?.use(config => {
      config.sparkManagementEpoch = managementEpoch;
      return config;
    });
    api.interceptors.response.use(
      (res) => res,
      (err) => {
        const staleRead = err.config?.sparkReadKey && readSequences[err.config.sparkReadKey] !== err.config.sparkReadSequence;
                    if (!staleRead && err.response && err.response.status === 401) {
          if (authed.value && (err.config?.sparkManagementEpoch === undefined || err.config.sparkManagementEpoch === managementEpoch)) {
            ++managementEpoch;
            stopManagementReads();
            cancelContactSync();
            clearManagementData();
            authed.value = false;
            loginError.value = '会话已失效，请重新验证';
            loginResult.value = '';
          }
        }

        return Promise.reject(err);
      }
    );

    const totalContactCount = computed(() => contacts.value.length);
    const draftSelection = computed(() => contacts.value.filter(c => c.selected));
    const selectedFriendCount = computed(() => draftSelection.value.length);
    const savedFriendCount = computed(() => savedSelection.value.length);
    const selectionFingerprint = rows => rows.map(contactKey).sort().join('\n');
    const selectionDirty = computed(() => selectionFingerprint(draftSelection.value) !== selectionFingerprint(savedSelection.value));
    const savedSelectionLabel = computed(() => savedSelection.value.map(row => row.display_name || row.nickname).join('、') || (ledgerReady.value ? '暂无已生效好友' : '等待同步已生效名单'));
    const selectionStatusMessage = computed(() => selectionFeedback.value || (selectionDirty.value ? `草稿已变更，当前仍生效 ${savedFriendCount.value} 人。请先保存或放弃未保存变更，再执行。` : ''));
    const streakUsable = row => !['unknown','suspect','reset_pending'].includes(row.streak_status);
    const streakResetCandidates = computed(() => savedLedgerContacts.value.filter(row=>['reset_pending','correction_pending'].includes(row.streak_status)));
    const sendReviewCandidates = computed(() => savedLedgerContacts.value.filter(row=>row.send_review_status));
    const identitySendBlocked = row => row.identity_ambiguous || !!row.send_block_reason || row.observation_status === 'not_observed' || row.rename_candidates?.some(pair=>pair.unresolved_send);
    const identityDescription = row => {
      if (row.send_block_reason === 'related_send_unconfirmed' || row.rename_candidates?.some(pair=>pair.unresolved_send)) return '历史发送结果待核对，暂不重复发送。';
      if (row.send_block_reason === 'related_sent_today') return '关联历史今天已发送，今日不重复发送。';
      if (row.observation_status === 'not_observed') return '本次未观察到' + (row.last_capture_complete ? '，可能已改名或不在列表中，请核实。' : '（列表范围未完整确认），请核实后再发送。');
      return '';
    };
    const captureSummary = computed(() => {
      const capture = contactsCapture.value, counts = capture?.streak_counts;
      const keys = ['normal', 'rekindling', 'expiring', 'none', 'unknown'];
      if (!capture || !Number.isSafeInteger(capture.rows) || capture.rows < 0 || !counts ||
          !keys.every(key => Number.isSafeInteger(counts[key]) && counts[key] >= 0) ||
          keys.reduce((sum, key) => sum + counts[key], 0) !== capture.rows) return null;
      return {rows:capture.rows, ...Object.fromEntries(keys.map(key => [key, counts[key]]))};
    });
    const captureMessage = computed(() => {
      const capture = contactsCapture.value, count = captureSummary.value;
      if (!capture) return '暂无扫描摘要；同步后将显示本轮读取数、火花分类及扫描范围。';
      const summary = count
        ? `最近一次扫描读取 ${count.rows} 条会话记录：正常火花 ${count.normal} · 重燃中 ${count.rekindling} · 即将消失 ${count.expiring} · 明确无火花 ${count.none} · 无法判断 ${count.unknown}。`
        : '最近一次扫描未记录可核对的分类数量，可重新同步更新。';
      if (capture.complete === true) return summary + ' 列表已到底且多轮稳定；扫描完整不代表每条火花均已确认，最终状态以台账各行为准。';
      const reasons = {unknown_scroll_container:'未确认列表滚动区域', loading:'列表仍在加载',
        profiles_unresolved:'部分好友资料尚未加载完成', round_limit:'已达到本轮扫描上限',
        not_reported:'未返回扫描范围', unstable_rows:'连续读取结果尚未稳定'};
      return summary + ` 扫描范围未确认完整（${reasons[capture.reason] || '未取得完整范围证据'}），按部分观察处理；未出现的旧记录不代表已删除或改名。`;
    });
    const identityDecisionAllowed = computed(() => ledgerReady.value && !selectionDirty.value && !ledgerWriteBusy.value && !status.running && !status.stopping && !status.retry_dispatching && !syncing.value && !['running','unconfirmed'].includes(contactsSync.status));
    const streakDisplay = row => {
      const days = Number(row.streak_days ?? row.spark_days ?? 0);
      const history = days > 0 ? `（历史 ${days} 天）` : '（暂无确认值）';
      if (row.streak_status === 'unknown') return (row.streak_label || '未更新') + history;
      if (row.streak_status === 'suspect') return '读数待核对' + history;
      if (row.streak_status === 'reset_pending') return `可能已重新开始（历史 ${days} 天 → 新读数 ${row.streak_suspect_value} 天，待确认）`;
      if (row.streak_status === 'none') return '已确认无火花';
      return days > 0 ? (`🔥 ${days} 天` + (row.streak_at_risk === true ? ' · 灰色' : '')) : '尚无确认火花记录';
    };
    const streakAge = row => {
      let day=row.streak_confirmed_date, days=row.streak_unconfirmed_days;
      const known=row.streak_age_known===true || (row.streak_age_known!==false && Object.hasOwn(row,'streak_unconfirmed_days'));
      if(!known) {
        const date=new Date(row.streak_confirmed_at || '');
        if(!Number.isFinite(date.getTime()) || date.getTime()>Date.now())return {label:'最后确认时间未记录或异常',stale:false};
        const parts=value=>Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(value).filter(p=>['year','month','day'].includes(p.type)).map(p=>[p.type,p.value]));
        const old=parts(date),today=parts(new Date());day=`${old.year}-${old.month}-${old.day}`;
        days=Math.floor((Date.UTC(Number(today.year),Number(today.month)-1,Number(today.day))-Date.UTC(Number(old.year),Number(old.month)-1,Number(old.day)))/86400000);
      }
      if(typeof day!=='string' || !/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isInteger(days) || days<0)return {label:'最后确认时间未记录或异常',stale:false};
      const stale=known ? row.streak_may_be_stale===true : ['unknown','suspect','reset_pending'].includes(row.streak_status) && days>3;
      return {label:`最后确认于 ${day}（${days} 天前）${stale?' · 可能已过期':''}`,stale};
    };
    const formatObserved = value => {
      const date = new Date(value || '');
      if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now() + 120000) return '';
      return date.toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'});
    };
    const observationDay = value => {
      const date = new Date(value || '');
      if (!Number.isFinite(date.getTime())) return '';
      return new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).format(date);
    };
    const streakExpiring = row => {
      const matched = String(row.streak_label || '').match(/^([0-9]+)\s*天后消失$/);
      return !!matched && Number(matched[1]) <= 3;
    };
    const observationStale = row => {
      const day = observationDay(row.streak_seen_at || row.streak_confirmed_at);
      if (!day) return streakAge(row).stale;
      return day !== observationDay(new Date().toISOString());
    };
    const streakDetail = row => {
      const age = streakAge(row);
      const observed = formatObserved(row.streak_seen_at || row.streak_confirmed_at);
      const stale = observationStale(row);
      const observedLabel = observed ? ((row.streak_at_risk === true ? '灰色火花 · ' : '') + (stale ? '缓存读数 · ' : '') + '观察于 ' + observed) : age.label;
      if (streakExpiring(row)) return `火花即将消失：${row.streak_label} · ${observedLabel}`;
      if (stale && observed) return `${observedLabel} · ${age.label}`;
      return observed ? observedLabel : age.label;
    };
    const streakStale = row => streakAge(row).stale || observationStale(row) || streakExpiring(row);
    const verificationLabel = row => {
      if (row.send_verification_status === 'confirmed' && row.send_verification_by === 'user_confirmed_sent') return '已按人工核对标记为发出，不会自动重发';
      if (row.send_verification_status === 'confirmed') return '发送后已在会话中核对，不会自动重发';
      if (row.send_verification_status === 'failed' && row.send_verification_by === 'user_confirmed_not_sent') return '已按人工核对标记为未发出';
      if (row.send_verification_status === 'failed') return '核对到发送失败，不会自动重发';
      if (row.send_verification_status === 'pending_review') return '发送后仍待核对，不会自动重发';
      return '';
    };
    const streakBadge = row => row.streak_status==='reset_pending' ? '重置待确认' : streakDisplay(row);
    // 特殊火花纳入人数和勾选，但标签数字不是火花天数。
    const hasSpecialStreak = row => !['suspect','reset_pending','none'].includes(row.streak_status) && /^(?:重燃中\s*[0-9]+\s*\/\s*[0-9]+|[0-9]+\s*天后消失)$/.test(row.streak_label || '');
    const hasCurrentStreak = row => row.observation_status !== 'not_observed' && (hasSpecialStreak(row) || streakUsable(row) && Number(row.spark_days ?? row.streak_days ?? 0) > 0);
    const sparkFriendCount = computed(() => contacts.value.filter(hasCurrentStreak).length);

    const filteredContacts = computed(() => {
      const kw = searchKeyword.value.trim().toLowerCase();
      // 仅改变展示顺序；原始联系人顺序、勾选及发送顺序不变。
      const rank = row => hasCurrentStreak(row) ? 0 : row.observation_status === 'not_observed' ? 2 : 1;
      let visibleRows = contacts.value.filter(row => showHistoricalContacts.value || row.observation_status !== 'not_observed').sort((a,b) => rank(a) - rank(b));
      if (showOnlySparkFriends.value) visibleRows = visibleRows.filter(hasCurrentStreak);
      if (!kw) return visibleRows;
      return visibleRows.filter(c =>
        (c.nickname && c.nickname.toLowerCase().includes(kw)) ||
        (String(c.spark_days || '').includes(kw))
      );
    });

    const sessionCheckedLabel = computed(() => {
      if (!status.status_connected) return '等待服务器同步状态';
      if (!status.state_file_exists) return '请在凭证页扫码登录';
      if (['invalid','future'].includes(status.session_check_reason)) return '检查时间异常，请重新检查登录状态';
      const seconds = Number(status.session_checked_at);
      if (!(seconds > 0) || !Number.isFinite(seconds)) return '尚无检查记录；保存文件不代表登录有效';
      const date = new Date(seconds * 1000);
      return Number.isFinite(date.getTime()) ? '上次确认：' + date.toLocaleString('zh-CN', {hour12:false}) + (status.session_check_stale ? ' · 已超过 24 小时，需重新检查' : '') : '尚无有效检查时间';
    });
    const credentialDisplay = computed(() => {
      const view = (label, tone) => ({label, tone, color: tone === 'ok' ? 'var(--status-valid)' : tone === 'bad' ? 'var(--status-invalid)' : 'var(--status-pending)'});
      if (!status.status_connected) return view('状态待同步', 'warn');
      if (!status.state_file_exists) return view('未保存凭证', 'bad');
      if (status.session_status === 'expired') return view('已失效，需扫码', 'bad');
      if (['failed', 'partial'].includes(status.session_status)) return view('登录状态待检查', 'warn');
      if (status.session_status === 'ok' && ['invalid','future'].includes(status.session_check_reason)) return view('检查时间异常，需重新检查', 'warn');
      if (status.session_status === 'ok' && status.session_check_stale) return view('上次有效，需重新检查', 'warn');
      if (status.session_status === 'ok' && Number(status.session_checked_at) > 0 && Number.isFinite(Number(status.session_checked_at))) return view('上次检查有效', 'ok');
      return view('已保存，待检查', 'warn');
    });
    const taskDisplay = computed(() => {
      const view = (label, detail, color = 'var(--status-pending)') => ({label, detail, color});
      if (status.stopping) return view('正在停止／结果待核对', status.status_connected ? '后台任务尚未结束，勿重复启动；结束后先核对发送结果' : '上次状态为停止中，尚未确认后台任务结束；连接恢复前勿重复启动', 'var(--status-invalid)');
      if (!status.running && status.retry_dispatching) return view('正在启动补发', status.status_connected ? '后台已开始启动补发，请等待任务更新，勿重复启动' : '补发启动结果尚未确认，连接恢复前勿重复启动', status.status_connected ? 'var(--status-valid)' : 'var(--status-pending)');
      if (!status.status_connected) return view('等待状态同步', '连接恢复后会自动更新');
      if (status.ledger_recovery_required) return view('恢复保护，等待核对', '发送历史可能不完整，今天已暂停发送；核对后可点“我已核对，解除发送保护”，明天也会自动解除', 'var(--status-invalid)');
      if (status.running) {
        const phases = {starting:'正在准备任务', opening_browser:'正在准备自动化浏览器', loading_contacts:'正在读取会话列表', locating:'正在定位目标好友', confirming:'正在等待页面确认发送结果', waiting_gap:'正在等待好友之间的发送间隔', finishing:'正在保存结果并收尾'};
        return view('正在执行', phases[status.task_phase] || '请等待当前任务完成', 'var(--status-valid)');
      }
      if (status.background_tasks_enabled === false) return view('后台已暂停', '本次启动暂停后台任务，仍可手动操作', 'var(--status-invalid)');
      if (status.background_tasks_enabled !== true) return view('等待状态同步', '尚未确认后台任务状态');
      if (!config.auto_run_enabled) return view('自动发送已暂停', '可在设置中调整自动发送开关', 'var(--status-invalid)');
      if (!status.next_run) return view('等待调度', '尚未收到下一次执行时间', 'var(--status-valid)');
      const next = new Date(status.next_run);
      return view('等待定时执行', Number.isFinite(next.getTime()) ? '下次：' + next.toLocaleString('zh-CN', {hour12:false}) : '已登记下一次定时任务', 'var(--status-valid)');
    });

    // Display-only normalization keeps malformed/transient progress bounded.
    const progressDisplay = computed(() => {
      const progress = status.send_progress || {};
      const count = value => {
        const number = Number(value);
        return Number.isFinite(number) ? Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, Math.floor(number))) : 0;
      };
      const total = count(progress.total);
      const ok = count(progress.ok);
      const failed = count(progress.failed);
      const hasDone = progress.done !== null && progress.done !== undefined && progress.done !== '' && Number.isFinite(Number(progress.done));
      const processed = hasDone ? count(progress.done) : count(ok + failed);
      const done = total ? Math.min(processed, total) : 0;
      const ratio = total ? Math.min(1, Math.max(0, done / total)) : 0;
      return {
        current: String(progress.current || '').trim() || '准备中',
        total, done, ok, failed, ratio,
        percent: Math.round(ratio * 100),
      };
    });
    const taskStatusAnnouncement = computed(() => {
      if (status.stopping) return '正在停止／结果待核对。后台任务尚未确认结束，勿重复启动；结束后先核对发送结果。';
      if (!status.running && status.retry_dispatching) return '正在启动补发。请等待后台任务更新，勿重复启动。';
      const warning = status.consecutive_failures > 0
        ? '上一轮发送失败。连续失败 ' + status.consecutive_failures + ' 轮，请检查登录态。'
        : '';
      return warning + (status.running ? '发送任务正在执行。' : '当前没有发送任务。');
    });
    const formatTime = (timeStr) => {
      if (!timeStr) return '今日待发送';
      try {
        const d = new Date(timeStr);
        if (isNaN(d.getTime())) return String(timeStr);
        const m = String(d.getMonth() + 1).padStart(2, '0');
        const day = String(d.getDate()).padStart(2, '0');
        const h = String(d.getHours()).padStart(2, '0');
        const min = String(d.getMinutes()).padStart(2, '0');
        return `上次：${m}-${day} ${h}:${min}`;
      } catch (e) {
        return '今日待发送';
      }
    };

    const contactTimeDisplay = row => {
      if (row.last_status === 'sending') return '发送结果尚未确认，请等待并核对';
      if (row.last_status === 'unconfirmed') return '结果待核对，请勿直接重发';
      if (!row.last_sent_at) return '尚无已确认的发送记录';
      if (!Number.isFinite(new Date(row.last_sent_at).getTime())) return '尚无可核验的发送时间';
      return '上次已确认接受：' + formatTime(row.last_sent_at).replace(/^上次：/, '');
    };
    const loadStatus = (options = {}) => {
      if (options.reuse && statusReadPromise) return statusReadPromise;
      if (managementDisposed) return Promise.resolve(false);
      if (statusTimer !== null) clearTimeout(statusTimer);
      statusTimer = null;
      const ticket = beginManagedRead('status');
      const pending = (async () => {
      try {
        const res = await api.get('/api/status', ticket.options);
        if (!ticket.current()) return false;
        status.status_connected = true;
        connectionError.value = '';
        status.version = res.data.version || status.version;
        status.session_status = res.data.session_status || 'unknown';
        status.session_checked_at = res.data.session_checked_at ?? null;
        status.session_check_stale = res.data.session_check_stale === true;
        status.session_check_age_seconds = res.data.session_check_age_seconds ?? null;
        status.session_check_reason = res.data.session_check_reason || null;
        status.ledger_recovery_required = res.data.ledger_recovery_required === true;
        status.task_phase = res.data.task_phase || null;
        status.background_tasks_enabled = typeof res.data.background_tasks_enabled === 'boolean' ? res.data.background_tasks_enabled : null;
        status.next_run = res.data.next_run || null;
        status.stopping = res.data.stopping === true;
        status.running = res.data.running === true || status.stopping;
        status.state_file_exists = res.data.state_file_exists;
        status.consecutive_failures = res.data.consecutive_failures || 0;
        // 忽略旧版本的历史冷却值，失败告警不再表示任务已暂停。
        status.auto_paused_until = 0;
        status.send_progress = res.data.send_progress || null;
        status.pending_retry_count = Number.isSafeInteger(res.data.pending_retry_count) && res.data.pending_retry_count > 0 ? res.data.pending_retry_count : 0;
        status.retry_dispatching = res.data.retry_dispatching === true || res.data.retry_status === 'dispatching';
        status.retry_status = status.retry_dispatching ? 'dispatching' : ['manual','scheduled'].includes(res.data.retry_status) ? res.data.retry_status : 'none';
        status.pending_retry_entries = Array.isArray(res.data.pending_retry_entries) ? res.data.pending_retry_entries : [];
        todaySummary.value = parseTodaySummary(res.data.today_summary);
        status.active_send_diagnostics = Array.isArray(res.data.active_send_diagnostics) ? res.data.active_send_diagnostics.flatMap(item => {
          if (!item || typeof item !== 'object') return [];
          const name = typeof item.display_name === 'string' ? item.display_name.slice(0, 80) : '';
          const reason = typeof item.reason === 'string' ? item.reason.slice(0, 80) : '';
          if (!name || !reason) return [];
          return [{display_name:name, reason, business_date:typeof item.business_date === 'string' ? item.business_date.slice(0, 40) : '', at:typeof item.at === 'string' ? item.at.slice(0, 40) : ''}];
        }) : [];
        Object.assign(lastRunInfo, summarizeLastRun(res.data.last_run));
        lastRunDetails.value = describeLastRun(res.data.last_run);
        // 连续失败时标签页标题加警示（比声音/震动可靠，不依赖浏览器前台或静音）
        document.title = status.consecutive_failures > 0
          ? `⚠️ 连续失败${status.consecutive_failures}轮 · 抖音自动续火花`
          : baseDocTitle();
        // 动态轮询：发送中 1.5s 看实时进度，空闲 10s
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        status.status_connected = false;
        console.error('Failed to load status', e);
        return false;
      } finally {
        finishManagedRead(ticket);
        if (statusReadPromise === pending) {
          statusReadPromise = null;
          scheduleStatusPoll();
        }
      }
      })();
      statusReadPromise = pending;
      return pending;
    };

    const loadConfig = async () => {
      const ticket = beginManagedRead('config');
      try {
        const res = await api.get('/api/config', ticket.options);
        if (!ticket.current()) return false;
        applyConfigResponse(res.data);
        configFeedback.value = '';
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        console.error('Failed to load config', e);
        return false;
      } finally { finishManagedRead(ticket); }
    };

    const normalizeLedgerContacts = list => {
        const nameCounts = new Map();
        const normalizedName = item => (item.display_name || item.nickname || '').replace(/\u00a0/g,' ').trim();
        for (const item of list) { if(item.observation_status === 'not_observed') continue; const name = normalizedName(item); nameCounts.set(name, (nameCounts.get(name) || 0) + 1); }
        // 后台已区分当前会话与历史行；旧接口缺字段时才使用保守回退。
        const isAmbiguous = item => typeof item.identity_ambiguous === 'boolean' ? item.identity_ambiguous : (nameCounts.get(normalizedName(item)) || 0) > 1;
        return list.map((item, index) => ({
          entry_id: typeof item.entry_id === 'string' ? item.entry_id : '',
          identity_ambiguous: isAmbiguous(item),
          legacy_row_key: 'legacy:' + index + ':' + (item.display_name || item.nickname || ''),
          display_name: item.display_name || item.nickname || '',
          nickname: item.nickname || item.display_name || '',
          avatar: item.avatar || '',
          spark_days: Number(item.spark_days || item.streak_days || 0),
          streak_days: Number(item.streak_days || item.spark_days || 0),
          streak_status: item.streak_status || null,
          streak_label: item.streak_label || null,
          streak_at_risk: item.streak_at_risk === true,
          streak_source: item.streak_source || null,
          streak_seen_at: item.streak_seen_at || null,
          streak_confirmed_at: item.streak_confirmed_at || null,
          streak_confirmed_date: item.streak_confirmed_date || null,
          streak_unconfirmed_days: Number.isInteger(item.streak_unconfirmed_days) ? item.streak_unconfirmed_days : null,
          streak_may_be_stale: item.streak_may_be_stale===true,
          streak_age_known: Object.hasOwn(item,'streak_unconfirmed_days'),
          streak_suspect_value: item.streak_suspect_value ?? null,
          streak_suspect_since: item.streak_suspect_since || null,
          streak_suspect_count: item.streak_suspect_count || 0,
          observation_status: item.observation_status || null,
          last_capture_complete: item.last_capture_complete === true,
          identity_review_pending: item.identity_review_pending === true,
          send_block_reason: item.send_block_reason || null,
          send_review_status: ['unconfirmed','rejected'].includes(item.send_review_status) ? item.send_review_status : null,
          send_review_date: typeof item.send_review_date === 'string' ? item.send_review_date : null,
          send_review_blocks_today: item.send_review_blocks_today === true,
          send_verification_status: ['confirmed','failed','pending_review'].includes(item.send_verification_status) ? item.send_verification_status : '',
          send_verification_by: typeof item.send_verification_by === 'string' ? item.send_verification_by : '',
          active_diagnostic_reason: typeof item.active_diagnostic_reason === 'string' ? item.active_diagnostic_reason.slice(0, 80) : '',
          rename_candidates: Array.isArray(item.rename_candidates) ? item.rename_candidates.map(pair=>({...pair})) : [],
          selected: Boolean(item.selected) && !isAmbiguous(item),
          last_status: item.last_status || (item.last_sent_at ? 'success' : 'pending'),
          last_sent_at: item.last_sent_at || ''
        }));
    };
    const applySavedLedger = (rows, version, preserveDraft) => {
      invalidateManagedRead('ledger');
      savedLedgerContacts.value = rows.map(row => ({...row}));
      savedSelection.value = rows.filter(row => row.selected).map(row => ({...row}));
      ledgerVersion.value = typeof version === 'string' && version ? version : null;
      ledgerReady.value = ledgerVersion.value !== null;
      if (!preserveDraft) contacts.value = rows.map(row => ({...row}));
    };
    const loadLedger = async () => {
      const ticket = beginManagedRead('ledger');
      try {
        const res = await api.get('/api/ledger', ticket.options);
        if (!ticket.current()) return null;
        const rows = normalizeLedgerContacts(res.data.contacts || res.data.entries || []);
        contactsCapture.value = res.data.contacts_capture && typeof res.data.contacts_capture.complete === 'boolean' ? {...res.data.contacts_capture} : null;
        // Refresh the effective snapshot without silently overwriting a draft.
        applySavedLedger(rows, res.data.ledger_version, selectionDirty.value || savingLedger.value);
        if (!savingLedger.value) selectionFeedback.value = '';
        return true;
      } catch (e) {
        if (!ticket.current()) return null;
        console.error('Failed to load ledger', e);
        return false;
      } finally {
        finishManagedRead(ticket);
        nextTick(() => { if (managementCurrent(ticket.epoch) && typeof init3DCards === 'function') stabilizeCards(); });
      }
    };

    const fetchLogs = async () => {
      const ticket = beginManagedRead('logs');
      logsRefreshing.value = true;
      logsRefreshMessage.value = '';
      try {
        const res = await api.get('/api/logs', ticket.options);
        if (!ticket.current()) return false;
        const nextLogs = res.data.logs || '';
        const changed = logsContent.value !== nextLogs;
        logsContent.value = nextLogs;
        // Wait for Vue to mount/update the scroll container, including when
        // entering this tab. Even unchanged text must reveal the latest row.
        await nextTick();
        if (!ticket.current()) return false;
        const el = document.getElementById('terminal-view');
        if (el) el.scrollTop = el.scrollHeight;
        logsRefreshMessage.value = !nextLogs ? '暂无日志输出' : changed ? '已更新，已定位到最新日志' : '暂无新日志，已定位到末尾';
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        logsRefreshMessage.value = '刷新失败，保留原日志，请重试';
        console.error('Failed to fetch logs', e);
        return false;
      } finally {
        if (ticket.current()) logsRefreshing.value = false;
        finishManagedRead(ticket);
      }
    };
    const refreshLogs = () => {
      if (logsRefreshing.value) return;
      return fetchLogs();
    };
    const clearLogs = () => {
      invalidateManagedRead('logs');
      logsContent.value = '';
      logsRefreshing.value = false;
      logsRefreshMessage.value = '';
    };

    // ── 邮箱告警配置 ──────────────────────────────────────
    const defaultEmailConfig = {
      smtp_host: '', smtp_port: 465, smtp_user: '', smtp_pass: '',
      mail_to: '', subject: '', body: ''
    };
    const emailCfg = reactive({...defaultEmailConfig});
    const savedEmailConfig = ref({...defaultEmailConfig});
    const emailConfigStatus = ref('');
    const emailSnapshot = data => Object.fromEntries(Object.keys(defaultEmailConfig).map(key => [key, key === 'smtp_pass' ? '' : data?.[key] ?? defaultEmailConfig[key]]));
    const emailFingerprint = data => JSON.stringify(Object.keys(defaultEmailConfig).map(key => data[key]));
    const emailConfigDirty = computed(() => emailFingerprint(emailCfg) !== emailFingerprint(savedEmailConfig.value));
    function clearManagementData() {
      contacts.value = []; savedSelection.value = []; savedLedgerContacts.value = [];
      contactsCapture.value = null;
      ledgerVersion.value = null; ledgerReady.value = false; selectionFeedback.value = '';
      logsContent.value = ''; logsRefreshing.value = false; logsRefreshMessage.value = ''; messagesText.value = '';
      Object.assign(lastRunInfo, {time:'', summary:'', success:false});
      lastRunDetails.value = []; resultDetailsOpen.value = false;
      for (const key of Object.keys(config)) delete config[key];
      Object.assign(config, defaultConfig);
      for (const key of Object.keys(emailCfg)) delete emailCfg[key];
      Object.assign(emailCfg, defaultEmailConfig);
      savedEmailConfig.value = {...defaultEmailConfig};
      emailConfigStatus.value = '';
      Object.assign(status, defaultStatus);
      todaySummary.value = null;
      lockedConfigFields.value = []; configFeedback.value = '';
      savingLedger.value = false; savingConfig.value = false; savingEmail.value = false; testingEmail.value = false;
      ledgerWriteOwner = null; ledgerWriteBusy.value = false; ledgerWriteLabel.value = '';
      pendingRetryBusy.value = false;
      webhook.enabled = false; webhook.debounceSeconds = 600; webhook.deliveries = []; webhook.message = '';
      testingWebhook.value = false;
      Object.assign(assistant, defaultAssistantStatus());
      assistantReplyTo.value = ''; assistantReplyText.value = ''; assistant.replyMessage = '';
      assistantSending.value = false; assistantLoading.value = false;

      iosBanner.visible = false;
      if (iosBannerTimer !== null) clearTimeout(iosBannerTimer);
      iosBannerTimer = null;
      document.title = baseDocTitle();
    }
    const emailSubView = ref('');
    const playEmailEntrance = () => {
      nextTick(() => {
        const bar = document.getElementById('emailBar');
        if (!bar) return;
        [...bar.children].forEach((el, i) => {
          el.animate([{ opacity: 0 }, { opacity: 1 }],
            { duration: 1000, delay: 100 + i * 120, easing: 'cubic-bezier(0.4,0,0.2,1)', fill: 'both' });
        });
      });
    };
    const openTab = (name) => {
      tab.value = name;
      if (name === 'logs') refreshLogs();
      if (name === 'email') { playEmailEntrance(); }
      else { emailSubView.value = ''; resetEmailBar(); }
    };
    let emailAnims = [];
    const EMAIL_EASING = 'cubic-bezier(0.65,0,0.35,1)';
    const EMAIL_MS = 1200;

    // Width targets per state (flow layout, no transforms -> nothing can fly)
    const emailWidthTargets = (W, state) => {
      if (state === 'smtp') return [W - 64, 54];
      if (state === 'text') return [54, W - 64];
      const width = Math.min(165, Math.max(0, (W - 10) / 2));
      return [width, width];
    };

    const runEmailAnim = (state, duration) => {
      const bar = document.getElementById('emailBar');
      if (!bar) return;
      const W = bar.parentElement.clientWidth;
      const tg = emailWidthTargets(W, state);

      // 1) measure the CURRENT visual geometry BEFORE cancelling
      const cur = [];
      for (let i = 0; i < 2; i++) {
        cur.push(bar.children[i].getBoundingClientRect().width);
      }
      const barCur = bar.getBoundingClientRect().width;

      // 2) cancel, then freeze the measured geometry into base styles (no snap)
      emailAnims.forEach(a => { try { a.cancel(); } catch(_) {} });
      emailAnims = [];
      for (let i = 0; i < 2; i++) {
        bar.children[i].style.width = cur[i] + 'px';
      }
      bar.style.maxWidth = barCur + 'px';

      // 3) animate from the current visual state to targets
      for (let i = 0; i < 2; i++) {
        const el = bar.children[i];
        if (Math.abs(cur[i] - tg[i]) > 0.5) {
          emailAnims.push(el.animate(
            { width: [cur[i] + 'px', tg[i] + 'px'] },
            { duration, easing: EMAIL_EASING, fill: 'forwards' }
          ));
        }
      }
      const barTarget = state === '' ? Math.min(340, W) : W;
      if (Math.abs(barCur - barTarget) > 1) {
        emailAnims.push(bar.animate(
          { maxWidth: [barCur + 'px', barTarget + 'px'] },
          { duration, easing: EMAIL_EASING, fill: 'forwards' }
        ));
      }
    };

    const selectEmail = (name) => {
      const next = emailSubView.value === name ? '' : name;
      runEmailAnim(next, EMAIL_MS);
      emailSubView.value = next;
    };

    const resetEmailBar = () => {
      const bar = document.getElementById('emailBar');
      if (!bar) return;
      emailAnims.forEach(a => { try { a.cancel(); } catch(_) {} });
      emailAnims = [];
      bar.style.maxWidth = '340px';
      const btns = bar.children;
      for (let i = 0; i < btns.length; i++) {
        btns[i].style.width = '165px';
        btns[i].style.transform = '';
      }
    };

    window.addEventListener('resize', () => {
      if (emailSubView.value) runEmailAnim(emailSubView.value, 400);
    });

    const emailBtnStyle = (name) => {
      const active = emailSubView.value === name;
      const rgb = name === 'smtp' ? '0,122,255' : '255,122,0';
      return active
        ? 'background:rgba(' + rgb + ',0.12); box-shadow:0 4px 16px rgba(' + rgb + ',0.22);'
        : '';
    };
    const savingEmail = ref(false);
    const testingEmail = ref(false);

    const loadEmailConfig = async () => {
      const ticket = beginManagedRead('email');
      try {
        const res = await api.get('/api/email-config', ticket.options);
        if (!ticket.current()) return false;
        const preserveDraft = emailConfigDirty.value || savingEmail.value;
        savedEmailConfig.value = emailSnapshot(res.data);
        if (!preserveDraft) Object.assign(emailCfg, res.data, {smtp_pass:''});
        emailConfigStatus.value = preserveDraft ? '已读取后台邮箱配置，未保存的编辑内容已保留。' : '';
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        console.error('Failed to load email config', e);
        return false;
      } finally { finishManagedRead(ticket); }
    };

    const saveEmailForm = async () => {
      if (savingEmail.value) return false;
      const requestEpoch = managementEpoch;
      savingEmail.value = true;
      const submitted = emailFingerprint(emailCfg);
      try {
        const { has_pass: _ignored, ...payload } = emailCfg;
        const res = await api.post('/api/email-config', payload);
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok) {
          invalidateManagedRead('email');
          const preserveDraft = emailFingerprint(emailCfg) !== submitted;
          savedEmailConfig.value = emailSnapshot({...payload, ...res.data.config});
          ElementPlus.ElMessage.success('邮箱配置已保存！');
          if (!preserveDraft) Object.assign(emailCfg, res.data.config, {smtp_pass:''});
          else if (emailCfg.smtp_pass === payload.smtp_pass) emailCfg.smtp_pass = '';
          emailConfigStatus.value = preserveDraft ? '已保存刚才提交的邮箱配置；随后编辑的内容仍未保存。' : '邮箱配置已保存。';
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        ElementPlus.ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message));
      } finally {
        if (managementCurrent(requestEpoch)) savingEmail.value = false;
      }
    };

    const sendTestEmail = async () => {
      const requestEpoch = managementEpoch;
      testingEmail.value = true;
      try {
        const res = await api.post('/api/email-test');
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok) {
          ElementPlus.ElMessage.success('测试邮件已发送，请查收（注意垃圾箱）');
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        ElementPlus.ElMessage.error('测试邮件失败: ' + (e.response?.data?.detail || e.message));
      } finally {
        if (managementCurrent(requestEpoch)) testingEmail.value = false;
      }
    };

    const webhook = reactive({enabled:false, debounceSeconds:600, deliveries:[], message:''});
    const testingWebhook = ref(false);
    const webhookStatusLabel = computed(() => webhook.enabled ? '已启用' : '未配置');
    const webhookHint = computed(() => {
      if (!webhook.enabled) return '未设置 Webhook 地址时不会外发。在服务器 .env 填写后重启即可启用。此页不显示地址或密钥。';
      if (webhook.debounceSeconds <= 0) return '已启用。相同事件不会按时间间隔合并。任务卡死、测试发送和小号消息始终发出。地址和密钥不在此页显示。';
      const minutes = Math.max(1, Math.round(webhook.debounceSeconds / 60));
      return '已启用。相同事件约 ' + minutes + ' 分钟内只发送一次。任务卡死、测试发送和小号消息不受此限制。地址和密钥不在此页显示。';
    });
    const webhookResultLabel = result => result === 'ok' ? '成功' : result === 'fail' ? '失败' : result === 'suppressed' ? '已跳过' : '';
    const acceptWebhookDelivery = item => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
      if (!['ok', 'fail', 'suppressed'].includes(item.result)) return null;
      if (typeof item.event !== 'string' || !/^[a-z0-9_]{1,40}$/.test(item.event)) return null;
      if (typeof item.time !== 'string' || item.time.length > 40 || /https?:|webhook|key|token|secret/i.test(item.time)) return null;
      return {time: item.time, event: item.event, result: item.result};
    };
    const applyWebhookStatus = data => {
      const source = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
      webhook.enabled = source.enabled === true;
      const seconds = Number(source.debounce_seconds);
      webhook.debounceSeconds = Number.isFinite(seconds) && seconds >= 0 && seconds <= 86400 ? Math.floor(seconds) : 600;
      const rows = Array.isArray(source.deliveries) ? source.deliveries : [];
      webhook.deliveries = rows.map(acceptWebhookDelivery).filter(Boolean).slice(0, 20);
    };
    const loadWebhookStatus = async () => {
      const ticket = beginManagedRead('webhook');
      try {
        const res = await api.get('/api/webhook', ticket.options);
        if (!ticket.current()) return false;
        applyWebhookStatus(res.data);
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        console.error('Failed to load webhook status', e);
        return false;
      } finally { finishManagedRead(ticket); }
    };
    const sendTestWebhook = async () => {
      if (!webhook.enabled || testingWebhook.value) return false;
      const requestEpoch = managementEpoch;
      testingWebhook.value = true;
      try {
        const res = await api.post('/api/webhook/test');
        if (!managementCurrent(requestEpoch)) return false;
        applyWebhookStatus(res.data);
        webhook.message = res.data?.ok === true ? '测试 Webhook 已发送。' : '测试 Webhook 未送达。';
        if (res.data?.ok === true) ElementPlus.ElMessage.success(webhook.message);
        else ElementPlus.ElMessage.warning(webhook.message);
        return res.data?.ok === true;
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        webhook.message = '测试 Webhook 未送达。';
        ElementPlus.ElMessage.error(webhook.message);
        await loadWebhookStatus();
        return false;
      } finally {
        if (managementCurrent(requestEpoch)) testingWebhook.value = false;
      }
    };

    const defaultAssistantStatus = () => ({
      enabled:false, session_ok:null, session_file:false, last_poll:'', poll_seconds:90,
      allowlist:[], recent:[], pending_replies:0, last_error:'', replyMessage:'', qr_active:false,
    });
    const assistant = reactive(defaultAssistantStatus());
    const assistantReplyTo = ref('');
    const assistantReplyText = ref('');
    const assistantSending = ref(false);
    const assistantLoading = ref(false);
    const assistantQr = reactive({status:'idle', running:false, message:'', error:'', imageUrl:'', hasJob:false, canRefresh:false, verificationImageUrl:'', startUnconfirmed:false});
    let assistantQrController = null;
    const assistantStatusLabel = computed(() => {
      if (!assistant.enabled) return '未启用';
      if (assistant.session_ok === true) return '会话正常';
      if (assistant.session_ok === false) return '登录失效';
      return assistant.session_file ? '已有登录文件' : '尚未检查';
    });
    const assistantHint = computed(() => {
      if (!assistant.enabled) return '在 .env 设置 ASSISTANT_INBOX_ENABLED=1 并重启后才会轮询。此页不显示密钥。';
      if (!assistant.allowlist.length) return '白名单为空。服务不会打开小号浏览器，也不会转发或回复。';
      return '只处理白名单里的昵称或 ID。文字和分享卡片标题会转发；看不到视频内容。回复每 10 秒最多一条。';
    });
    const acceptAssistantRecent = item => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
      const messageId = typeof item.message_id === 'string' ? item.message_id.slice(0, 200) : '';
      if (!messageId) return null;
      return {
        message_id: messageId,
        conversation_name: typeof item.conversation_name === 'string' ? item.conversation_name.slice(0, 80) : '',
        message_time: typeof item.message_time === 'string' ? item.message_time.slice(0, 40) : '',
        kind: item.kind === 'share' ? 'share' : 'text',
        preview: typeof item.preview === 'string' ? item.preview.slice(0, 80) : '',
      };
    };
    const applyAssistantStatus = data => {
      const source = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
      assistant.enabled = source.enabled === true;
      assistant.session_ok = source.session_ok === true ? true : source.session_ok === false ? false : null;
      assistant.session_file = source.session_file === true;
      assistant.last_poll = typeof source.last_poll === 'string' ? source.last_poll.slice(0, 40) : '';
      const seconds = Number(source.poll_seconds);
      assistant.poll_seconds = Number.isFinite(seconds) ? Math.max(60, Math.min(3600, Math.floor(seconds))) : 90;
      assistant.allowlist = Array.isArray(source.allowlist) ? source.allowlist.filter(item => typeof item === 'string' && item.length <= 80).slice(0, 50) : [];
      assistant.recent = (Array.isArray(source.recent) ? source.recent : []).map(acceptAssistantRecent).filter(Boolean).slice(0, 30);
      const pending = Number(source.pending_replies);
      assistant.pending_replies = Number.isFinite(pending) && pending >= 0 ? Math.floor(pending) : 0;
      assistant.last_error = typeof source.last_error === 'string' ? source.last_error.slice(0, 180) : '';
      assistant.qr_active = source.qr_active === true;
    };
    const loadAssistantStatus = async () => {
      const ticket = beginManagedRead('assistant');
      assistantLoading.value = true;
      try {
        const res = await api.get('/api/assistant/status', ticket.options);
        if (!ticket.current()) return false;
        applyAssistantStatus(res.data);
        return true;
      } catch (e) {
        if (!ticket.current()) return false;
        console.error('Failed to load assistant status', e);
        return false;
      } finally {
        finishManagedRead(ticket);
        if (ticket.current()) assistantLoading.value = false;
      }
    };
    const sendAssistantReply = async () => {
      if (!assistant.enabled || assistantSending.value) return false;
      const to = assistantReplyTo.value.trim();
      const text = assistantReplyText.value.trim();
      if (!to || !text) {
        assistant.replyMessage = '请填写回复对象和内容。';
        return false;
      }
      const requestEpoch = managementEpoch;
      assistantSending.value = true;
      try {
        const res = await api.post('/api/assistant/reply', {to, text});
        if (!managementCurrent(requestEpoch)) return false;
        assistant.replyMessage = res.data?.queued ? '回复已排队，将由小号在下一轮发出。' : '回复未能排队。';
        if (res.data?.queued) {
          assistantReplyText.value = '';
          ElementPlus.ElMessage.success(assistant.replyMessage);
        }
        await loadAssistantStatus();
        return res.data?.queued === true;
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        assistant.replyMessage = e.response?.data?.detail || '回复未能排队。';
        ElementPlus.ElMessage.error(assistant.replyMessage);
        return false;
      } finally {
        if (managementCurrent(requestEpoch)) assistantSending.value = false;
      }
    };
    const uploadAssistantState = async event => {
      const input = event.target;
      const file = input.files && input.files[0];
      input.value = '';
      if (!file) return false;
      const requestEpoch = managementEpoch;
      const body = new FormData();
      body.append('file', file);
      try {
        await api.post('/api/assistant/state', body);
        if (!managementCurrent(requestEpoch)) return false;
        showIOSBanner('上传成功', '小号登录态已保存，主号凭证未改动');
        await loadAssistantStatus();
        return true;
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        showIOSBanner('上传失败', e.response?.data?.detail || '小号登录态未保存');
        return false;
      }
    };
    const updateAssistantQr = snapshot => { Object.assign(assistantQr, snapshot || {}); };
    function getAssistantQrController() {
      if (assistantQrController) return assistantQrController;
      if (!window.SparkQrLogin) throw new Error('二维码登录组件未加载，请刷新页面后重试。');
      assistantQrController = window.SparkQrLogin.create({
        api,
        base: '/api/assistant/qr',
        successMessage: '小号登录成功，已保存独立登录态，不影响主号。',
        onChange: updateAssistantQr,
        onSuccess: async () => {
          if (!authed.value) return;
          await loadAssistantStatus();
          if (authed.value) showIOSBanner('小号登录成功', '已保存小号登录态');
        },
      });
      return assistantQrController;
    }
    function startAssistantQr() {
      if (!assistant.enabled || assistantQr.running || !authed.value) return;
      try { return getAssistantQrController().start(); }
      catch (_) { Object.assign(assistantQr, {status:'failed', running:false, message:'二维码登录组件未加载，请刷新页面后重试。'}); }
    }
    function refreshAssistantQr() { if (assistantQrController && authed.value) return assistantQrController.refresh(); }
    function cancelAssistantQr() { if (assistantQrController) return assistantQrController.cancel(); }
    function assistantVerificationPointerDown(event) { if (assistantQrController && authed.value) return assistantQrController.verificationPointerDown(event); }
    function assistantVerificationPointerMove(event) { if (assistantQrController && authed.value) return assistantQrController.verificationPointerMove(event); }
    function assistantVerificationPointerUp(event) { if (assistantQrController && authed.value) return assistantQrController.verificationPointerUp(event); }
    function assistantVerificationPointerCancel(event) { if (assistantQrController) return assistantQrController.verificationPointerCancel(event); }
    function disposeAssistantQr() {
      if (assistantQrController) { assistantQrController.dispose(); assistantQrController = null; }
      Object.assign(assistantQr, {status:'idle', running:false, message:'', error:'', imageUrl:'', hasJob:false, canRefresh:false, verificationImageUrl:'', startUnconfirmed:false});
    }

    const loadAll = async () => {
      if (managementDisposed) return false;
      await Promise.all([loadStatus({reuse:true}), loadConfig(), loadLedger(), fetchLogs(), loadEmailConfig(), loadWebhookStatus(), loadAssistantStatus()]);
    };

    const pendingRetryBusy = ref(false);
    const canContinuePending = computed(() => authed.value && status.status_connected && !status.running && !status.stopping && !status.retry_dispatching && !ledgerWriteBusy.value && !pendingRetryBusy.value && status.retry_status === 'manual' && status.pending_retry_count > 0);
    const continuePending = async () => {
      if (!canContinuePending.value) return false;
      const requestEpoch = managementEpoch;
      pendingRetryBusy.value = true;
      try {
        const response = await api.post('/api/run/pending', {dry:false});
        if (!managementCurrent(requestEpoch)) return false;
        if (response.data?.ok !== true) throw new Error('未确认待完成任务启动');
        showIOSBanner('继续未完成', '只继续后台登记的安全待完成对象；待核对消息不会自动重发。');
        await loadStatus();
        return true;
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        showIOSBanner('无法继续', e.response?.data?.detail || e.message);
        return false;
      } finally { if (managementCurrent(requestEpoch)) pendingRetryBusy.value = false; }
    };
    const runStreak = async (dryRun = false) => {
      const requestEpoch = managementEpoch;
      if (!dryRun && status.ledger_recovery_required) {
        showIOSBanner('发送已阻止', '台账恢复后发送历史可能不完整，请先核对数据。');
        return false;
      }
      if (status.running || status.stopping || status.retry_dispatching) {
        ElementPlus.ElMessage.warning(status.stopping ? '后台任务尚未结束，结果待核对，请勿重复启动。' : status.running ? '当前任务仍在执行，请勿重复启动。' : '正在启动补发，请等待后台任务更新，勿重复启动。');
        return false;
      }
      if (ledgerWriteBusy.value || savingLedger.value || selectionDirty.value) {
        selectionFeedback.value = ledgerWriteBusy.value || savingLedger.value ? '正在更新好友台账，请等待确认后再执行。' : `草稿尚未保存，当前仍生效 ${savedFriendCount.value} 人。请先保存或放弃未保存变更，再执行。`;
        ElementPlus.ElMessage.warning(selectionFeedback.value);
        tab.value = 'friends';
        return false;
      }
      if (!ledgerReady.value) {
        selectionFeedback.value = '已生效名单尚未确认，请刷新好友台账后再执行。';
        ElementPlus.ElMessage.warning(selectionFeedback.value);
        tab.value = 'friends';
        return false;
      }
      if (savedFriendCount.value === 0) {
        ElementPlus.ElMessage.warning('您尚未保存任何生效好友！请在「好友与消息」中勾选并保存后再执行。');
        tab.value = 'friends';
        return false;
      }
      try {
        const res = await api.post('/api/run', { dry_run: dryRun, expected_ledger_version: ledgerVersion.value });
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok) {
          showIOSBanner('启动成功', '');
          loadAll();
        } else {
          showIOSBanner('启动失败', '');
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        if (e.response?.status === 409) {
          selectionFeedback.value = '已生效名单发生变化，本次未启动。请刷新并核对名单后再执行。';
          ledgerReady.value = false;
          showIOSBanner('名单需要重新确认', selectionFeedback.value);
        } else showIOSBanner('启动失败', e.response?.data?.detail || '请检查状态后重试。');
      }
    };

    function cancelContactSync() {
      ++contactsSyncGeneration;
      if (contactsSyncTimer !== null) clearTimeout(contactsSyncTimer);
      contactsSyncTimer = null;
      contactsSyncRequest?.abort();
      contactsSyncRequest = null;
      contactsSyncPolling = false;
      syncing.value = false;
      Object.assign(contactsSync, {status:'idle', jobId:'', message:'', error:'', updatedAt:''});
    }
    const contactSyncCurrent = (generation, epoch) => !contactsSyncDisposed && authed.value && generation === contactsSyncGeneration && epoch === managementEpoch;
    const pollContactSync = async (generation, epoch) => {
      if (!contactSyncCurrent(generation, epoch) || contactsSyncPolling) return false;
      contactsSyncPolling = true;
      contactsSyncChecks++;
      const ledgerReadSequence = readSequences.ledger || 0;
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      contactsSyncRequest = controller;
      try {
        const res = await api.get('/api/ledger', {timeout:15000, signal:controller?.signal});
        if (!contactSyncCurrent(generation, epoch)) return false;
        const data = res.data || {};
        const matched = data.contacts_job_id === contactsSync.jobId;
        if (matched) {
          contactsSync.updatedAt = data.contacts_at || '';
          contactsCapture.value = data.contacts_capture && typeof data.contacts_capture.complete === 'boolean' ? {...data.contacts_capture} : null;
          contactsSync.error = typeof data.contacts_error === 'string' ? data.contacts_error : '';
          if (data.contacts_job_status === 'failed' || contactsSync.error) {
            contactsSync.status = 'failed';
            contactsSync.message = '本次联系人同步失败，原台账与未保存草稿已保留。';
            syncing.value = false;
            showIOSBanner('同步失败', contactsSync.error || '后台任务未能完成，请检查后重试。');
            return false;
          }
          if (data.contacts_job_status === 'succeeded' && data.fetching !== true) {
            const rows = normalizeLedgerContacts(data.contacts || data.entries || []);
            // A later save/refresh may already have a newer authoritative
            // ledger; this task can finish without rolling that view back.
            if ((readSequences.ledger || 0) === ledgerReadSequence) applySavedLedger(rows, data.ledger_version, selectionDirty.value || savingLedger.value);
            contactsSync.status = 'succeeded';
            const scanned = captureSummary.value;
            contactsSync.message = `本次同步完成，${scanned ? `本轮读取 ${scanned.rows} 条会话记录` : '本轮读取数未记录'}；台账合计 ${rows.length} 位好友（含历史），其中当前 ${rows.filter(hasCurrentStreak).length} 位带火花。`;
            syncing.value = false;
            showIOSBanner('同步完成', contactsSync.message);
            return true;
          }
          contactsSync.status = 'running';
          contactsSync.message = '本次联系人仍在同步，请等待后台结果。';
        } else {
          contactsSync.status = 'running';
          contactsSync.message = '正在确认本次同步状态；旧台账不代表本次已完成。';
        }
        const stillRunning = matched && data.contacts_job_status === 'running' && data.fetching === true;
        if (contactsSyncChecks >= (stillRunning ? 120 : 25)) {
          contactsSync.status = 'unconfirmed';
          contactsSync.message = matched && data.fetching === true ? '本次联系人仍在同步，已暂停自动查询；请点击“刷新同步状态”继续查看。' : '本次同步结果未确认，已暂停自动查询；请点击“刷新同步状态”重试查询。';
          syncing.value = false;
          return false;
        }
        // 长任务继续低频串行查询，不在约40秒时让界面永久停留在旧结果。
        const pollDelay = contactsSyncChecks >= 25 ? 5000 : 1500;
        if (stillRunning && pollDelay === 5000) contactsSync.message = '本次联系人仍在同步，每5秒自动更新状态，无需重复启动。';
        contactsSyncTimer = setTimeout(() => { contactsSyncTimer = null; pollContactSync(generation, epoch); }, pollDelay);
        return false;
      } catch (e) {
        if (!contactSyncCurrent(generation, epoch)) return false;
        contactsSync.status = 'unconfirmed';
        contactsSync.message = '本次同步结果未确认，连接中断或查询超时；请点击“刷新同步状态”查询，不会重复启动任务。';
        syncing.value = false;
        return false;
      } finally {
        if (contactSyncCurrent(generation, epoch)) {
          contactsSyncPolling = false;
          if (contactsSyncRequest === controller) contactsSyncRequest = null;
        }
      }
    };
    const refreshContactSync = async () => {
      if (!authed.value || contactsSyncDisposed || contactsSyncPolling || !contactsSync.jobId || !['running','unconfirmed'].includes(contactsSync.status)) return false;
      if (contactsSyncTimer !== null) clearTimeout(contactsSyncTimer);
      contactsSyncTimer = null;
      contactsSyncChecks = 0;
      syncing.value = true;
      return pollContactSync(contactsSyncGeneration, managementEpoch);
    };
    const syncContacts = async () => {
      if (!authed.value || contactsSyncDisposed || syncing.value) return false;
      if (contactsSync.jobId && ['running','unconfirmed'].includes(contactsSync.status)) return refreshContactSync();
      const writeOwner = beginLedgerWrite('启动联系人同步');
      if (!writeOwner) return false;
      cancelContactSync();
      const generation = contactsSyncGeneration, epoch = managementEpoch;
      contactsSync.status = 'starting';
      contactsSync.message = '正在启动本次联系人同步…';
      syncing.value = true;
      contactsSyncChecks = 0;
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      contactsSyncRequest = controller;
      try {
        const res = await api.post('/api/sync', {}, {timeout:15000, signal:controller?.signal});
        if (!contactSyncCurrent(generation, epoch)) return false;
        if (res.data?.ok !== true || typeof res.data.job_id !== 'string' || !res.data.job_id) {
          contactsSync.status = 'unconfirmed';
          contactsSync.message = '启动结果未确认，未收到本次任务编号；请检查后台状态后再明确重试。';
          syncing.value = false;
          return false;
        }
        contactsSync.jobId = res.data.job_id;
        contactsSync.status = 'running';
        contactsSync.message = '本次联系人同步已启动，正在等待后台结果。';
        contactsSyncTimer = setTimeout(() => { contactsSyncTimer = null; pollContactSync(generation, epoch); }, 1500);
        return true;
      } catch (e) {
        if (!contactSyncCurrent(generation, epoch)) return false;
        const definite = [400,403,409,422].includes(e.response?.status);
        contactsSync.status = definite ? 'failed' : 'unconfirmed';
        contactsSync.error = definite ? e.response?.data?.detail || '后台拒绝启动本次同步。' : '';
        contactsSync.message = definite ? '本次同步未能启动，请检查错误后重试。' : '启动结果未确认，服务器可能已经启动同步；请检查后台状态后再明确重试。';
        syncing.value = false;
        return false;
      } finally {
        if (contactsSyncRequest === controller) contactsSyncRequest = null;
        finishLedgerWrite(writeOwner);
      }
    };

    const contactKey = row => row.entry_id || row.legacy_row_key || 'legacy:' + contacts.value.indexOf(row) + ':' + (row.display_name || row.nickname || '');
    const uniqueLegacyName = row => {
      const name = row.display_name || row.nickname || '';
      if (!name || row.identity_ambiguous || contacts.value.filter(c => (c.display_name || c.nickname) === name).length !== 1)
        throw new Error('好友身份存在歧义，请更新联系人后再操作；同名好友请设置唯一备注。');
      return name;
    };
    const toggleContactSelection = row => {
      if (identitySendBlocked(row) && !row.selected) { ElementPlus.ElMessage.warning('会话重名、本次未采集到或存在历史发送保护，暂不能勾选。'); return false; }
      if (!row.entry_id) { try { uniqueLegacyName(row); } catch (e) { ElementPlus.ElMessage.warning(e.message); return false; } }
      row.selected = !row.selected;
      selectionFeedback.value = '';
      return true;
    };

    const autoSelectSparkFriends = () => {
      selectionFeedback.value = '';
      let count = 0;
      contacts.value.forEach(c => {
        const pick = hasCurrentStreak(c) && !identitySendBlocked(c);
        c.selected = pick;
        if (pick) count++;
      });
      selectionFeedback.value = `已按当前火花勾选 ${count} 位好友（草稿）。请点击“保存勾选配置”后生效。`;
      ElementPlus.ElMessage.success(selectionFeedback.value);
    };

    const clearSelectedFriends = () => {
      selectionFeedback.value = '';
      contacts.value.forEach(c => c.selected = false);
      selectionFeedback.value = `草稿已清空，当前仍生效 ${savedFriendCount.value} 人。请点击“确认清空名单”生效，或放弃未保存变更。`;
      ElementPlus.ElMessage.info(selectionFeedback.value);
    };
    const confirmClearSelection = async () => {
      if (ledgerWriteBusy.value || savingLedger.value || selectedFriendCount.value !== 0) return false;
      if (!confirm(`确认清空已生效的 ${savedFriendCount.value} 位好友勾选吗？清空后，新任务将不再向任何好友发送。已启动任务仍按启动时名单处理。`)) return false;
      return saveLedgerSelection({confirmClear: true});
    };
    const discardSelectionChanges = () => {
      if (ledgerWriteBusy.value || savingLedger.value) return false;
      contacts.value = savedLedgerContacts.value.map(row => ({...row}));
      selectionFeedback.value = `已放弃未保存变更，当前生效 ${savedFriendCount.value} 人。`;
      return true;
    };
    const refreshLedgerSelection = async () => {
      if (ledgerWriteBusy.value || savingLedger.value) return false;
      const requestEpoch = managementEpoch;
      const loaded = await loadLedger();
      if (loaded === null) return false;
      if (loaded) {
        if (!managementCurrent(requestEpoch)) return false;
        selectionFeedback.value = !ledgerReady.value ? '名单版本尚未确认，请重试刷新后再执行。' : selectionDirty.value ? `已刷新生效名单 ${savedFriendCount.value} 人；草稿已保留。请先保存或放弃未保存变更，再执行。` : `已刷新，当前生效 ${savedFriendCount.value} 人。`;
        return ledgerReady.value;
      }
      if (!managementCurrent(requestEpoch)) return false;
      selectionFeedback.value = '名单刷新失败，原已生效信息和草稿已保留，请重试。';
      return false;
    };
    const removeSavedContacts = keys => {
      invalidateManagedRead('ledger');
      savedLedgerContacts.value = savedLedgerContacts.value.filter(row => !keys.has(contactKey(row)));
      savedSelection.value = savedSelection.value.filter(row => !keys.has(contactKey(row)));
      ledgerVersion.value = null;
      ledgerReady.value = false;
      selectionFeedback.value = '删除已完成，请刷新已生效名单后再执行；未保存草稿已保留。';
    };

    const deleteFriend = async (row) => {
      if (ledgerWriteBusy.value) return false;
      const requestEpoch = managementEpoch;
      const name = row.display_name || row.nickname;
      if (!confirm(`确定要删除好友「${name}」吗？删除后需重新同步联系人才能恢复。`)) return;
      const writeOwner = beginLedgerWrite('删除好友');
      if (!writeOwner) return false;
      try {
        const payload = row.entry_id ? {entry_id:row.entry_id} : {display_name:uniqueLegacyName(row)};
        await api.post('/api/ledger/delete', payload);
        if (!managementCurrent(requestEpoch)) return false;
        const key = contactKey(row);
        contacts.value = contacts.value.filter(c => contactKey(c) !== key);
        removeSavedContacts(new Set([key]));
        ElementPlus.ElMessage.success(`已删除「${name}」`);
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        ElementPlus.ElMessage.error('删除失败: ' + (e.response?.data?.detail || e.message));
      } finally { finishLedgerWrite(writeOwner); }
    };

    const batchDeleteSelected = async () => {
      if (ledgerWriteBusy.value) return false;
      const requestEpoch = managementEpoch;
      const selected = contacts.value.filter(c => c.selected);
      if (selected.length === 0) {
        ElementPlus.ElMessage.warning('请先勾选要删除的好友');
        return;
      }
      const names = selected.map(c => c.display_name || c.nickname);
      if (!confirm(`确定要删除选中的 ${names.length} 位好友吗？\n\n${names.slice(0, 5).join('、')}${names.length > 5 ? ' 等' : ''}\n\n删除后需重新同步联系人才能恢复。`)) return;
      const writeOwner = beginLedgerWrite('批量删除好友');
      if (!writeOwner) return false;
      try {
        const allIds = selected.every(row => row.entry_id);
        const payload = allIds ? {entry_ids:selected.map(row => row.entry_id)} : {display_names:selected.map(uniqueLegacyName)};
        const res = await api.post('/api/ledger/batch-delete', payload);
        if (!managementCurrent(requestEpoch)) return false;
        const deleted = res.data.deleted || 0;
        const keySet = new Set(selected.map(contactKey));
        contacts.value = contacts.value.filter(c => !keySet.has(contactKey(c)));
        removeSavedContacts(keySet);
        ElementPlus.ElMessage.success(`已成功删除 ${deleted} 位好友`);
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        ElementPlus.ElMessage.error('批量删除失败: ' + (e.response?.data?.detail || e.message));
      } finally { finishLedgerWrite(writeOwner); }
    };

    const reviewStreak = async (row, action) => {
      const current = savedLedgerContacts.value.find(item=>item.entry_id===row.entry_id && ['reset_pending','correction_pending'].includes(item.streak_status));
      if (!current || !identityDecisionAllowed.value || identitySendBlocked(current) || !['accept','keep'].includes(action)) {
        ElementPlus.ElMessage.warning('请先保存或放弃草稿，并等待当前任务结束、核对好友身份。');return false;
      }
      const requestEpoch=managementEpoch;
      const wording=action==='accept' ? `确认将“${current.display_name}”的火花读数从历史 ${current.streak_days} 天更新为 ${current.streak_suspect_value} 天？发送历史与待核对保护保留。` : `确认保持“${current.display_name}”的历史火花值？近期会暂停重复提示，旧值继续标为待核对。`;
      if (!confirm(wording) || !managementCurrent(requestEpoch)) return false;
      const owner=beginLedgerWrite('确认火花读数');if(!owner)return false;
      const draft=selectionFingerprint(draftSelection.value);
      try {
        const res=await api.post('/api/ledger/streak-decision',{entry_id:current.entry_id,action,expected_ledger_version:ledgerVersion.value});
        if(!managementCurrent(requestEpoch))return false;
        if(res.data?.ok!==true || !Array.isArray(res.data.entries) || typeof res.data.ledger_version!=='string') {
          ledgerReady.value=false;throw new Error('火花确认结果尚未确认，请刷新好友台账');
        }
        const changed=selectionFingerprint(draftSelection.value)!==draft;
        const edited=new Map(contacts.value.map(item=>[contactKey(item),item.selected]));
        applySavedLedger(normalizeLedgerContacts(res.data.entries),res.data.ledger_version,false);
        if(changed)for(const item of contacts.value)if(edited.has(contactKey(item)))item.selected=edited.get(contactKey(item));
        selectionFeedback.value=action==='accept'?'新火花读数已确认，发送历史与待核对保护已保留。':'已保持旧值，近期暂停重复提示。';
        ElementPlus.ElMessage.success(selectionFeedback.value);return true;
      } catch(error) {
        if(!managementCurrent(requestEpoch))return false;
        ElementPlus.ElMessage.error(error.response?.data?.detail || error.message || '火花确认未完成，请刷新核对');return false;
      } finally {finishLedgerWrite(owner);}
    };

    const reviewSend = async (row, action) => {
      const current = savedLedgerContacts.value.find(item=>item.entry_id===row.entry_id && item.send_review_status);
      if (!current || !identityDecisionAllowed.value || !['sent','not_sent'].includes(action)) {
        ElementPlus.ElMessage.warning('请先保存或放弃草稿，并等待当前任务结束后再核对。');return false;
      }
      const requestEpoch=managementEpoch;
      const wording=action==='sent'
        ? `确认已在抖音里看到发给“${current.display_name}”的这条消息？将记为已发送，当天不再重发。`
        : `确认发给“${current.display_name}”的消息没有发出？将允许程序重新发送。`;
      if (!confirm(wording) || !managementCurrent(requestEpoch)) return false;
      const owner=beginLedgerWrite('核对发送结果');if(!owner)return false;
      const draft=selectionFingerprint(draftSelection.value);
      try {
        const res=await api.post('/api/ledger/send-review',{entry_id:current.entry_id,action,expected_ledger_version:ledgerVersion.value});
        if(!managementCurrent(requestEpoch))return false;
        if(res.data?.ok!==true || !Array.isArray(res.data.entries) || typeof res.data.ledger_version!=='string') {
          ledgerReady.value=false;throw new Error('核对结果尚未确认，请刷新好友台账');
        }
        const changed=selectionFingerprint(draftSelection.value)!==draft;
        const edited=new Map(contacts.value.map(item=>[contactKey(item),item.selected]));
        applySavedLedger(normalizeLedgerContacts(res.data.entries),res.data.ledger_version,false);
        if(changed)for(const item of contacts.value)if(edited.has(contactKey(item)))item.selected=edited.get(contactKey(item));
        selectionFeedback.value=action==='sent'?'已记为已发送。':'已记为未发出，可以重新发送。';
        ElementPlus.ElMessage.success(selectionFeedback.value);return true;
      } catch(error) {
        if(!managementCurrent(requestEpoch))return false;
        ElementPlus.ElMessage.error(error.response?.data?.detail || error.message || '核对未完成，请刷新后重试');return false;
      } finally {finishLedgerWrite(owner);}
    };

    const clearRecovery = async () => {
      if (!confirm('请先在抖音里核对今天已经发过哪些好友。确认核对完毕，解除今天的发送保护？（明天也会自动解除）')) return false;
      try {
        const res=await api.post('/api/ledger/recovery-clear',{confirm:true});
        ElementPlus.ElMessage.success(res.data?.cleared ? '发送保护已解除。' : '当前没有需要解除的保护。');
        await loadStatus();
        return true;
      } catch(error) {
        ElementPlus.ElMessage.error(error.response?.data?.detail || error.message || '解除失败，请稍后重试');return false;
      }
    };

    const saveLedgerSelection = async (options = {}) => {
      if (ledgerWriteBusy.value || savingLedger.value) return false;
      const clearConfirmed = options?.confirmClear === true;
      if (selectedFriendCount.value === 0 && !clearConfirmed) {
        selectionFeedback.value = `当前仍生效 ${savedFriendCount.value} 人。保存空名单请点击“确认清空名单”，或放弃未保存变更。`;
        ElementPlus.ElMessage.warning(selectionFeedback.value);
        return false;
      }
      if (clearConfirmed && selectedFriendCount.value !== 0) return false;
      const writeOwner = beginLedgerWrite(clearConfirmed ? '确认清空名单' : '保存好友勾选');
      if (!writeOwner) return false;
      savingLedger.value = true;
      selectionFeedback.value = '正在保存好友勾选…';
      const requestEpoch = managementEpoch;
      try {
        const submittedRows = contacts.value.map(row => ({...row}));
        const selected = submittedRows.filter(c => c.selected);
        if (selected.some(row => row.identity_ambiguous)) throw new Error('同名好友待确认，请设置唯一备注后重新采集。');
        const allIds = selected.every(row => row.entry_id);
        const payload = allIds ? {selected_ids:selected.map(row => row.entry_id)} : {selected_names:selected.map(uniqueLegacyName)};
        if (clearConfirmed) payload.confirm_clear = true;
        const res = await api.post('/api/ledger/selection', payload);
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok !== true) throw new Error('服务器未确认保存成功');
        const draftChanged = selectionFingerprint(draftSelection.value) !== selectionFingerprint(selected);
        const confirmed = res.data.contacts || res.data.entries;
        applySavedLedger(Array.isArray(confirmed) ? normalizeLedgerContacts(confirmed) : submittedRows, res.data.ledger_version, draftChanged);
        selectionFeedback.value = ledgerReady.value ? `保存成功，当前生效 ${savedFriendCount.value} 人。${draftChanged ? '仍有新的未保存变更，请先保存或放弃后再执行。' : ''}` : '服务器已保存，名单版本尚未确认；请刷新好友台账后再执行。';
        ElementPlus.ElMessage.success(selectionFeedback.value);
        return true;
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        selectionFeedback.value = `保存失败，当前仍生效 ${savedFriendCount.value} 人；草稿已保留。`;
        ElementPlus.ElMessage.error(selectionFeedback.value + ' ' + (e.response?.data?.detail || e.message));
        return false;
      } finally {
        if (managementCurrent(requestEpoch)) savingLedger.value = false;
        finishLedgerWrite(writeOwner);
      }
    };

    const saveConfigForm = async () => {
      const requestEpoch = managementEpoch;
      savingConfig.value = true;
      try {
        const payload = Object.fromEntries(Object.keys(defaultConfig).map(key => [key, config[key]]));
        const res = await api.post('/api/config', payload);
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok) {
          invalidateManagedRead('config');
          applyConfigResponse(res.data);
          configFeedback.value = '';
          ElementPlus.ElMessage.success('定时与风控设置已保存！');
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        if (e.response?.status === 400 && e.response.data?.effective_config) {
          invalidateManagedRead('config');
          applyConfigResponse(e.response.data);
        }
        configFeedback.value = e.response?.data?.detail || e.message || '配置保存失败，请核对当前生效值。';
        ElementPlus.ElMessage.error('保存失败: ' + (e.response?.data?.detail || e.message));
      } finally {
        if (managementCurrent(requestEpoch)) savingConfig.value = false;
      }
    };

    const handleFileUpload = async (event) => {
      const requestEpoch = managementEpoch;
      if (qrLogin.running || qrLogin.hasJob) {
        showIOSBanner('请先结束扫码登录', '取消当前扫码流程后，再手动上传登录态文件');
        event.target.value = '';
        return;
      }
      const file = event.target.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append('file', file);
      try {
        showIOSBanner('上传中', '正在上传通行证凭据...');
        const res = await api.post('/api/upload-state', formData, {
          headers: { 'Content-Type': 'multipart/form-data' }
        });
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data.ok) {
          showIOSBanner('上传成功', '通行证凭证已就绪');
          await loadStatus();
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        showIOSBanner('上传失败', e.response?.data?.detail || e.message);
      }
    };

    // URL 令牌 + 密码验证：验证通过后设备30天免登录
    const doUrlPasswordLogin = async () => {
      if (logoutPending.value || logoutBusy.value) { loginError.value = '服务器退出尚未确认，请先点击“重试退出”。'; return; }
      const requestEpoch = managementEpoch;
      const pwd = (urlPassword.value || '').trim();
      if (!pwd) {
        loginError.value = '请输入访问密码';
        return;
      }
      urlPasswordLoading.value = true;
      loginError.value = '';
      try {
        const res = await api.post('/api/auth/url-token-login', {
          access_token: urlAccessToken.value,
          password: pwd
        });
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data && res.data.ok) {
          // 设备令牌由服务端写入 HttpOnly Cookie，前端脚本不可读取。
          localStorage.removeItem('dcs_auth_token');
          stripUrlToken();
          urlPassword.value = '';
          urlPasswordLoading.value = false;
          await enterSystem();
        } else {
          loginError.value = '验证响应异常，请重试';
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        if (e.response && e.response.status === 401) {
          loginError.value = (e.response.data && e.response.data.detail) || '访问令牌或密码不正确';
        } else {
          loginError.value = '无法连接服务器，请稍后重试';
        }
      } finally {
        if (managementCurrent(requestEpoch)) urlPasswordLoading.value = false;
      }
    };

    // 验证通过后的统一进入动作
    const enterSystem = async () => {
      if (logoutPending.value || managementDisposed) return;
      stopManagementReads();
      const enteredEpoch = ++managementEpoch;
      // Enter may remove the focused login input without dispatching blur.
      setFluidPaused(false);
      authed.value = true;
      loginError.value = '';
      try { await loadAll(); } catch (e) { /* 状态加载失败不拦截进入 */ }
      if (!authed.value || !managementCurrent(enteredEpoch) || logoutPending.value) return;
      statusPollingEnabled = true;
      scheduleStatusPoll();
      stabilizeCards();
    };

    // 兼容旧调用点；公开计算公式已经停用。
    const fetchChallenge = async () => {
      if (logoutPending.value) return;
      loginError.value = '';
    };

    // 点击「验证并进入」：用 AUTH_TOKEN 换取短期 HttpOnly 会话。
    const doLogin = async () => {
      if (logoutPending.value || logoutBusy.value) { loginError.value = '服务器退出尚未确认，请先点击“重试退出”。'; return; }
      const requestEpoch = managementEpoch;
      const val = (loginResult.value || '').trim();
      if (!val) {
        loginError.value = '请输入安全令牌';
        return;
      }
      loginLoading.value = true;
      loginError.value = '';
      try {
        const res = await api.post('/api/auth/login', {
          token: val
        });
        if (!managementCurrent(requestEpoch)) return false;
        if (res.data && res.data.ok) {
          localStorage.removeItem('dcs_auth_token');
          loginResult.value = '';
          loginLoading.value = false;
          await enterSystem();
        } else {
          loginError.value = '登录响应异常，请重试';
        }
      } catch (e) {
        if (!managementCurrent(requestEpoch)) return false;
        localStorage.removeItem('dcs_auth_token');
        if (e.response && e.response.status === 401) {
          loginError.value = '安全令牌不正确';
          loginResult.value = '';
        } else if (e.response && e.response.status === 400) {
          loginError.value = (e.response.data && e.response.data.detail) || '请求格式不正确';
        } else if (e.response && e.response.status === 403) {
          loginError.value = (e.response.data && e.response.data.detail) || '该 IP 已被锁定';
        } else {
          loginError.value = '无法连接服务器，请稍后重试';
        }
      } finally {
        if (managementCurrent(requestEpoch)) loginLoading.value = false;
      }
    };

    // 退出 / 锁定后台，回到登录门
    const logout = async () => {
      if (logoutBusy.value) return false;
      logoutBusy.value = true;
      ++managementEpoch;
      stopManagementReads();
      clearManagementData();
      cancelContactSync();
      try {
        // Cancel before server revocation while the HttpOnly session is available.
        if (qrLoginController) {
          const cancellingController = qrLoginController;
          await Promise.race([cancellingController.cancel(), new Promise(resolve => setTimeout(resolve, 1200))]);
          cancellingController.dispose();
          if (qrLoginController === cancellingController) qrLoginController = null;
        }
        disposeAssistantQr();
        rememberLogoutPending(true);
        authed.value = false;
        localStorage.removeItem('dcs_auth_token');
        loginResult.value = ''; urlPassword.value = '';
        loginError.value = '正在确认服务器退出…';
        const response = await api.post('/api/auth/logout', {}, {timeout:10000});
        if (response.data?.ok !== true) throw new Error('logout_not_confirmed');
        rememberLogoutPending(false);
        loginError.value = '';
        return true;
      } catch (_) {
        rememberLogoutPending(true);
        authed.value = false;
        loginError.value = '本页已锁定，但服务器退出尚未确认，原会话可能仍有效。请恢复连接后点击“重试退出”。';
        return false;
      } finally { logoutBusy.value = false; }
    };

    // 密码门通过后进入控制台（顶部「重新连接」沿用同一入口）
    const connectConsole = async () => {
      if (managementDisposed) return;
      stopManagementReads();
      const enteredEpoch = ++managementEpoch;
      setFluidPaused(false);
      authed.value = true;
      connectionError.value = '';
      try { await loadAll(); } catch (_) { status.status_connected = false; }
      if (!managementCurrent(enteredEpoch)) return;
      statusPollingEnabled = true;
      scheduleStatusPoll();
      stabilizeCards();
    };
    // Cancel stale in-flight work and clear browser-only snapshots. No server
    // account logout, password check or credential deletion is involved.
    const resetConsole = async () => {
      ++managementEpoch;
      stopManagementReads();
      cancelContactSync();
      clearManagementData();
      if (qrLoginController) { qrLoginController.dispose(); qrLoginController = null; }
      disposeAssistantQr();
      Object.assign(qrLogin, defaultQrLogin);
      qrStepMotion?.update(defaultQrLogin);
      authed.value = true;
      connectionError.value = '连接已重置，可重新连接本机服务。';
      return true;
    };

    // 导航栏滚动效果：透明 → 毛玻璃
    const initScrollEffect = () => {
      const topbar = document.querySelector('.topbar');
      if (!topbar) return;
      const onScroll = () => {
        if (window.scrollY > 4) {
          topbar.classList.add('scrolled');
        } else {
          topbar.classList.remove('scrolled');
        }
      };
      window.addEventListener('scroll', onScroll, { passive: true });
      onScroll();
    };

    // Bind reference pointer effects and preserve completed card entrances.
    const stabilizeCards = () => nextTick(() => {
      window.SparkReferenceMotion?.refresh();
      document.querySelectorAll('.main-view .stagger-item').forEach(card => {
        if (card._visibilityBound) return;
        card._visibilityBound = true;
        card.addEventListener('animationend', () => { card.style.opacity = '1'; });
      });
    });
    watch(tab, stabilizeCards);

    onMounted(async () => {
      // 确保 app 可见（防止任何异常导致白屏）
      const appEl = document.getElementById('app');
      if (appEl) appEl.style.display = '';
      const mountedEpoch = managementEpoch;
      const healthRead = beginManagedRead('health');
      try {
        const hv = await api.get('/api/health', healthRead.options);
        if (!healthRead.current()) return;
        if (hv.data.version) appVersion.value = hv.data.version;
        document.title = baseDocTitle();
      } catch (_) { if (!healthRead.current()) return; }
      finally { finishManagedRead(healthRead); }
      try {
      // An unfinished logout must not be bypassed by the still-valid Cookie after reload.
      if (logoutPending.value || !managementCurrent(mountedEpoch)) return;
      // HttpOnly Cookie 会由浏览器自动携带；先探测会话或白名单旁路。
      initScrollEffect();
      stabilizeCards();
      // 清理 1.5 及更早版本遗留在 localStorage 的明文令牌。
      localStorage.removeItem('dcs_auth_token');
      loginLoading.value = true;
      const probe = beginManagedRead('session-probe');
      try {
        await api.get('/api/status', probe.options);
        if (!probe.current()) return;
        loginLoading.value = false;
        await enterSystem();
      } catch (e) {
        if (!probe.current()) return;
        await fetchChallenge();
      } finally {
        finishManagedRead(probe);
        if (managementCurrent(mountedEpoch)) loginLoading.value = false;
      }
      } catch (e) {
        if (!managementCurrent(mountedEpoch)) return;
        console.error('onMounted error:', e);
        // 出错时确保显示登录门
        authed.value = false;
        try { await fetchChallenge(); } catch (e2) {}
      }
    });


    function getQrLoginController() {
      if (qrLoginController) return qrLoginController;
      if (!window.SparkQrLogin) throw new Error('二维码登录组件未加载，请刷新页面后重试。');
      qrLoginController = window.SparkQrLogin.create({
        api,
        onChange: updateQrPresentation,
        onSuccess: async () => {
          if (!authed.value) return;
          const requestEpoch = managementEpoch;
          const synced = await loadStatus();
          if (!managementCurrent(requestEpoch)) return;
          if (!synced) throw new Error('status_refresh_failed');
          if (authed.value) showIOSBanner('登录成功', 'state.json 已自动更新，无需手动上传');
        }
      });
      return qrLoginController;
    }
    function startQrLogin() {
      if (qrLogin.running || !authed.value) return;
      try { return getQrLoginController().start(); }
      catch (_) { Object.assign(qrLogin,{status:'failed',running:false,message:'二维码登录组件未加载，请刷新页面后重试。'}); }
    }
    function refreshQrLogin() { if (qrLoginController && authed.value) return qrLoginController.refresh(); }
    function cancelQrLogin() { if (qrLoginController) return qrLoginController.cancel(); }
    function retryQrStart() { if (qrLoginController && authed.value) return qrLoginController.retryStart(); }
    function retryQrSync() { if (qrLoginController && authed.value) return qrLoginController.retrySync(); }
    function retryQrImage() { if (qrLoginController && authed.value) return qrLoginController.retryImage(); }
    function retryQrCleanup() { if (qrLoginController && authed.value) return qrLoginController.retryCleanup(); }
    function qrVerificationPointerDown(event) { if (qrLoginController && authed.value) return qrLoginController.verificationPointerDown(event); }
    function qrVerificationPointerMove(event) { if (qrLoginController && authed.value) return qrLoginController.verificationPointerMove(event); }
    function qrVerificationPointerUp(event) { if (qrLoginController && authed.value) return qrLoginController.verificationPointerUp(event); }
    function qrVerificationPointerCancel(event) { if (qrLoginController) return qrLoginController.verificationPointerCancel(event); }
    watch(authed, value => {
      if (!value) {
        stopManagementReads();
        clearManagementData();
        cancelContactSync();
        if (qrLoginController) { qrLoginController.dispose(); qrLoginController=null; }
        disposeAssistantQr();
        if (qrStepMotion) qrStepMotion.update({status:'idle',running:false,hasJob:false});
        Object.assign(qrLogin,{status:'idle',running:false,message:'请先验证网站访问权限。',error:'',imageUrl:'',deadline:0,hasJob:false,canRefresh:false,canRetrySync:false,canRetryImage:false,canRetryCleanup:false,canRetryStart:false,startUnconfirmed:false,saved:false,imageError:'',verificationImageError:'',verificationImageUrl:'',verificationReady:false,verificationRevision:null,verificationWidth:0,verificationHeight:0,verificationBusy:false,interactionPending:false,gestureActive:false});
      }
    });

    onBeforeUnmount(() => {
      managementDisposed = true;
      document.removeEventListener?.('visibilitychange', onConsoleVisibility);
      stopManagementReads();
      contactsSyncDisposed = true;
      cancelContactSync();
      if (qrClockTimer !== null) clearInterval(qrClockTimer);
      qrClockTimer = null;
      window.removeEventListener('spark-silk-change', updateBackgroundMotion);
      window.SparkReferenceMotion?.destroy();
      if (qrStepMotion) qrStepMotion.dispose();
      if (qrLoginController) { qrLoginController.dispose(); qrLoginController=null; }
      disposeAssistantQr();
    });
    return {
      tab,
      navItems,
      status,
      todaySummary,
      todayResultMessage,
      todayResultDetail,
      streakWarningMessage,
      activeDiagnosticMessage,
      backgroundPauseMessage,
      credentialDisplay,
      sessionCheckedLabel,
      taskDisplay,
      loadStatus,
      progressDisplay,
      taskStatusAnnouncement,
      config,
      lockedConfigFields,
      configFeedback,
      configLockMessage,
      messagesText,
      contacts,
      contactKey,
      toggleContactSelection,
      lastRunInfo,
      lastRunDetails,
      resultDetailsOpen,
      totalContactCount,
      selectedFriendCount,
      savedSelection,
      draftSelection,
      savedFriendCount,
      savedSelectionLabel,
      selectionDirty,
      selectionStatusMessage,
      ledgerVersion,
      ledgerReady,
      sparkFriendCount,
      contactsCapture,
      captureSummary,
      captureMessage,
      identityDescription,
      identityDecisionAllowed,
      identitySendBlocked,
      streakResetCandidates,
      reviewStreak,
      sendReviewCandidates,
      reviewSend,
      clearRecovery,
      streakDisplay,
      streakDetail,
      streakStale,
      streakExpiring,
      verificationLabel,
      streakBadge,
      filteredContacts,
      searchKeyword,
      showHistoricalContacts,
      showOnlySparkFriends,
      hasCurrentStreak,
      historicalContactCount,
      logsContent,
      logsRefreshing,
      logsRefreshMessage,
      refreshLogs,
      qrLogin,
      qrSteps,
      qrVisualStep,
      qrStageIndex,
      qrRemainingSeconds,
      qrRemainingLabel,
      retryQrStart,
      retryQrSync,
      retryQrImage,
      retryQrCleanup,
      startQrLogin,
      refreshQrLogin,
      cancelQrLogin,
      qrVerificationPointerDown,
      qrVerificationPointerMove,
      qrVerificationPointerUp,
      qrVerificationPointerCancel,
      syncing,
      savingLedger,
      ledgerWriteBusy,
      ledgerWriteLabel,
      savingConfig,
      ICONS,
      formatTime,
      contactTimeDisplay,
      loadAll,
      runStreak,
      pendingRetryBusy,
      canContinuePending,
      continuePending,
      syncContacts,
      contactsSync,
      contactsSyncTimeLabel,
      refreshContactSync,
      autoSelectSparkFriends,
      clearSelectedFriends,
      confirmClearSelection,
      discardSelectionChanges,
      refreshLedgerSelection,
      saveLedgerSelection,
      saveConfigForm,
      fetchLogs,
      clearLogs,
      handleFileUpload,
      authed,
      isMobile,
      loginResult,
      loginError,
      loginLoading,
      logoutBusy,
      logoutPending,
      urlAccessToken,
      publicPasswordMode,
      showAdminGate,
      switchLoginGate,
      urlPassword,
      urlPasswordLoading,
      doUrlPasswordLogin,
      fetchChallenge,
      doLogin,
      logout,
      setFluidPaused,
      backgroundMotion,
      backgroundMotionLabel,
      backgroundMotionTitle,
      toggleBackgroundMotion,
      appVersion,
      connectionError,
      connectConsole,
      resetConsole,
      iosBanner,
      showIOSBanner,
      avatarProxy,
      avatarErrors,
      deleteFriend,
      batchDeleteSelected,
      emailCfg,
      emailConfigDirty,
      emailConfigStatus,
      savingEmail,
      testingEmail,
      loadEmailConfig,
      saveEmailForm,
      sendTestEmail,
      webhook,
      webhookStatusLabel,
      webhookHint,
      webhookResultLabel,
      testingWebhook,
      sendTestWebhook,
      loadWebhookStatus,
      applyWebhookStatus,
      assistant,
      assistantStatusLabel,
      assistantHint,
      assistantReplyTo,
      assistantReplyText,
      assistantSending,
      assistantLoading,
      assistantQr,
      loadAssistantStatus,
      sendAssistantReply,
      uploadAssistantState,
      startAssistantQr,
      refreshAssistantQr,
      cancelAssistantQr,
      assistantVerificationPointerDown,
      assistantVerificationPointerMove,
      assistantVerificationPointerUp,
      assistantVerificationPointerCancel,
      emailSubView,
      openTab,
      selectEmail,
      emailBtnStyle
    };
  }
});

app.use(ElementPlus);
// 挂载前先显示 app（防止 v-cloak 类问题导致白屏）
const appEl = document.getElementById('app');
if (appEl) appEl.style.display = '';
app.mount('#app');
// 挂载后再次确保显示
setTimeout(() => {
  const el = document.getElementById('app');
  if (el) el.style.display = '';
}, 100);
