/* Pure run-result descriptions. Loaded before console-app.js. */
(function(root) {
  function describeLastRun(run) {
    if (!run || typeof run !== 'object') return [];
    const details = [], reasons = {
      already_sent_today:'今天已发送，已跳过', unconfirmed_today:'发送结果待核对，不会自动重发',
      delivery_unconfirmed:'发送结果待核对，不会自动重发', delivery_rejected:'页面明确拒绝发送',
      ledger_recovery_required:'台账恢复保护：发送历史可能不完整，请先核对数据',
      identity_ambiguous:'会话存在歧义，未提交消息', not_observed:'本轮未观察到该会话',
      related_send_unconfirmed:'关联历史记录有待核对消息', related_sent_today:'关联历史记录今天已发送',
      contact_not_found:'未能定位到目标会话', browser_session_closed:'浏览器会话中断',
    };
    for (const [field, fallback] of [['failed','未完成，具体情况请查看日志'],['skipped','本轮跳过'],['deferred','本轮尚未处理']]) {
      for (const row of Array.isArray(run[field]) ? run[field] : []) {
        const name = typeof row === 'string' ? row : row?.name || row?.display_name;
        if (typeof name !== 'string' || !name.trim()) continue;
        details.push({name:name === '_system' ? '本轮任务' : name.slice(0,120), detail:Object.hasOwn(reasons, row?.code) ? reasons[row.code] : fallback});
      }
    }
    const checks = {historical_markers_observed:'已观察到历史己方消息标记；本次演练未发送，不能据此确认真实投递',
      historical_text_observed:'在己方一侧看到了与本次文案相同的历史消息，正式发送时可据此确认；本次演练未发送',
      no_observed_outgoing_markers:'尚未观察到可用的己方消息标记，空会话也可能出现此情况',
      snapshot_unavailable:'当前页面确认依据尚不可读取', snapshot_failed:'页面确认依据读取未完成',
      visible_failure_signal:'观察到历史失败提示，请先查看会话', pending_message_visible:'观察到尚未完成的消息状态',
      not_reached:'尚未执行到发送判据检查'};
    if (run.dry_run === true) for (const row of Array.isArray(run.delivery_checks) ? run.delivery_checks : []) {
      if (!row || typeof row.name !== 'string') continue;
      details.push({name:row.name.slice(0,120), detail:'演练诊断：'+(Object.hasOwn(checks, row.reason) ? checks[row.reason] : '确认依据尚未验证')});
    }
    return details;
  }

  function summarizeLastRun(run) {
    const empty = {time:'',summary:'',success:false};
    if (!run || typeof run !== 'object' || Array.isArray(run)) return empty;
    const keys = ['ok','failed','deferred','skipped'];
    const hasResults = keys.some(key => Array.isArray(run[key]) && run[key].length > 0);
    const hasMode = typeof run.dry_run === 'boolean';
    let date = null;
    if ((typeof run.at === 'string' && run.at.trim()) || (typeof run.at === 'number' && Number.isFinite(run.at) && run.at > 0)) {
      const value = typeof run.at === 'number' && run.at < 1e12 ? run.at * 1000 : run.at;
      const parsed = new Date(value);
      if (Number.isFinite(parsed.getTime())) date = parsed;
    }
    if (!date && !hasMode && !hasResults) return empty;
    const time = date ? date.toLocaleString('zh-CN',{hour12:false}) : '时间未记录';
    const mode = run.dry_run === true ? '模拟演练（未发送消息）' : run.dry_run === false ? '实际发送' : '执行记录（模式未标明）';
    if (keys.some(key => run[key] !== undefined && !Array.isArray(run[key]))) {
      return {time,summary:mode+' · 结果记录不完整，暂无法统计',success:false};
    }
    const names = {ok:new Set(),unconfirmed:new Set(),failed:new Set(),deferred:new Set(),skipped:new Set()};
    const systemErrors = [];
    const alreadySentToday = new Set();
    const alreadySentPeople = new Map();
    let incomplete = false;
    for (const key of keys) {
      for (const item of Array.isArray(run[key]) ? run[key] : []) {
        const value = typeof item === 'string' ? item : item && typeof item === 'object' ? item.name ?? item.display_name ?? item.nickname : null;
        if (typeof value !== 'string' || !value.trim()) {incomplete=true;continue;}
        const name = value.trim().normalize('NFC');
        if (name === '_system') {
          if (key === 'failed' || key === 'deferred') systemErrors.push(item);
          continue;
        }
        if (key === 'skipped' && item && typeof item === 'object' && ['delivery_unconfirmed','unconfirmed_today'].includes(item.code)) names.unconfirmed.add(name);
        else {
          names[key].add(name);
          if (key === 'skipped' && run.dry_run === false && item && typeof item === 'object' && item.code === 'already_sent_today') {
            alreadySentToday.add(name);
            const identity = typeof item.entry_id === 'string' && item.entry_id.trim() ? 'id:'+item.entry_id : 'name:'+name;
            alreadySentPeople.set(identity, name);
          }
        }
      }
    }
    // One person has one displayed outcome. An unconfirmed send must
    // never be classified as an ordinary retry candidate or a skip.
    for (const name of names.ok) { alreadySentToday.delete(name); names.unconfirmed.delete(name); names.failed.delete(name); names.deferred.delete(name); names.skipped.delete(name); }
    for (const name of names.unconfirmed) { alreadySentToday.delete(name); names.failed.delete(name); names.deferred.delete(name); names.skipped.delete(name); }
    for (const name of names.failed) { alreadySentToday.delete(name); names.deferred.delete(name); names.skipped.delete(name); }
    for (const name of names.deferred) { alreadySentToday.delete(name); names.skipped.delete(name); }
    const parts = [mode, (run.dry_run === true ? '演练通过 ' : hasMode ? '成功 ' : '已完成 ') + names.ok.size,
      '失败 '+names.failed.size, '待处理 '+names.deferred.size];
    const otherSkipped = names.skipped.size - alreadySentToday.size;
    const alreadySentCount = [...alreadySentPeople.values()].filter(name => alreadySentToday.has(name)).length;
    if (otherSkipped) parts.push('跳过 '+otherSkipped);
    if (alreadySentCount) parts.push('今天已发 '+alreadySentCount+' 人，已自动跳过');
    const hasUnconfirmed = run.delivery_unconfirmed === true || names.unconfirmed.size > 0;
    if (hasUnconfirmed) parts.push((names.unconfirmed.size ? '待核对 '+names.unconfirmed.size : '有待核对结果')+'（请在抖音核对，勿直接重发）');
    if (incomplete) parts.push('部分结果记录不完整，请查看运行日志');
    if (systemErrors.length) {
      const closed = systemErrors.some(item => item && typeof item === 'object' &&
        (item.code === 'browser_session_closed' || typeof item.reason === 'string' &&
         /event loop.*closed|(?:browser|context|page|target).*closed|浏览器.*关闭|事件循环.*关闭/i.test(item.reason)));
      // Only fixed explanations leave the formatter: arbitrary exception
      // strings can contain URLs, private paths or account identifiers.
      parts.push('已中断：'+(closed ? '浏览器会话已关闭，请重新运行模拟演练检查' : '后台执行异常，请查看运行日志'));
    }
    // 当全部对象今天已经成功发送，本轮安全跳过仍是健康运行。
    const completedToday = run.dry_run === false && alreadySentCount > 0 && otherSkipped === 0;
    return {time,summary:parts.join(' · '),success:hasMode && (names.ok.size > 0 || completedToday) &&
      names.failed.size === 0 && names.deferred.size === 0 && systemErrors.length === 0 && !incomplete && !hasUnconfirmed};
  }

  root.SparkConsoleResults = Object.freeze({describeLastRun, summarizeLastRun});
})(window);
