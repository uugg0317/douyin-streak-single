"""Pure streak value rules; no account configuration or filesystem access."""
from __future__ import annotations

import re
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

BUSINESS_ZONE = ZoneInfo('Asia/Shanghai')
MIN_GROWTH_WINDOW_DAYS = 1  # Same-day observations may straddle a platform update.
logger = logging.getLogger('douyin-cloud-streak')
RESET_DISMISS_BUSINESS_DAYS = 3  # User-kept values do not prompt again for the next three business dates.
STREAK_STALE_AFTER_DAYS = 3  # UI warning policy, not a claim about platform expiry rules.
_RESET_KEYS = ('streak_suspect_value','streak_suspect_since','streak_suspect_count',
               'streak_suspect_last_at','streak_suspect_last_sync','streak_suspect_source',
               'streak_suspect_direction')
_GROWTH_KEYS = ('streak_growth_base_days', 'streak_growth_base_at', 'streak_growth_changed_at')
_HISTORY_STATUSES = {'confirmed', 'none', 'unknown', 'suspect', 'reset_pending', 'correction_pending'}


def _clear_reset_evidence(entry):
    for key in _RESET_KEYS:entry.pop(key,None)


def _confirmed_time(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _trusted_history(entry):
    value = entry.get('streak_days')
    status, source = entry.get('streak_status'), entry.get('streak_source')
    return (not entry.get('streak_history_unverified')
            and isinstance(status, str) and status in _HISTORY_STATUSES
            and type(value) is int and 0 <= value <= 999
            and not (status == 'confirmed' and value == 0 or status == 'none' and value != 0)
            and isinstance(source, str) and source in {'class', 'emoji'}
            and _confirmed_time(entry.get('streak_confirmed_at')) is not None)


def normalize_streak_record(entry):
    """Copy legacy rows without inventing proof, modifying identity or send state."""
    result = dict(entry)
    if not _trusted_history(result):
        result.update(streak_status='unknown', streak_history_unverified=True,
                      streak_reason='unverified_history')
        for key in _GROWTH_KEYS:
            result.pop(key, None)
        _clear_reset_evidence(result)
    elif not any(key in result for key in _GROWTH_KEYS):
        result.update(streak_growth_base_days=result['streak_days'],
                      streak_growth_base_at=result['streak_confirmed_at'],
                      streak_growth_changed_at=result['streak_confirmed_at'])
    elif _trusted_history(result) and 'streak_growth_changed_at' not in result:
        result['streak_growth_changed_at'] = result['streak_confirmed_at']
    return result


def confirmation_age(entry,now):
    view={'streak_confirmed_date':None,'streak_unconfirmed_days':None,'streak_may_be_stale':False}
    try:
        when=datetime.fromisoformat(str(entry.get('streak_confirmed_at','')).replace('Z','+00:00'))
        if when.tzinfo is None or when>now:return view
        confirmed=when.astimezone(BUSINESS_ZONE).date()
        days=(now.astimezone(BUSINESS_ZONE).date()-confirmed).days
    except (ValueError,TypeError):return view
    view.update(streak_confirmed_date=confirmed.isoformat(),streak_unconfirmed_days=days,
        streak_may_be_stale=entry.get('streak_status') in {'unknown','suspect','reset_pending','correction_pending'} and days>STREAK_STALE_AFTER_DAYS)
    return view


def _candidate_ready(entry, now, direction):
    try:
        since=date.fromisoformat(entry['streak_suspect_since'])
        last=datetime.fromisoformat(entry['streak_suspect_last_at'])
        count=entry['streak_suspect_count']
        # Old persisted reset candidates predate the direction field and are downward only.
        recorded_direction = entry.get('streak_suspect_direction', 'down')
        minimum_days = 1 if direction == 'down' else 0
        return (recorded_direction == direction and type(count) is int and count>=2 and last.tzinfo is not None and last<=now
                and (last.astimezone(BUSINESS_ZONE).date()-since).days>=minimum_days
                and entry.get('streak_suspect_source') in {'class','emoji'}
                and parse_streak(entry.get('streak_suspect_value')) is not None)
    except (ValueError,TypeError,KeyError):return False


def _reset_ready(entry, now):
    return _candidate_ready(entry, now, 'down')


def _record_reset_evidence(entry,value,source,now,sync_id, *, direction='down'):
    if not isinstance(sync_id,str) or not sync_id:return False
    today=now.astimezone(BUSINESS_ZONE).date()
    try:
        if date.fromisoformat(entry.get('streak_reset_dismissed_until',''))>=today:
            _clear_reset_evidence(entry);return False
    except (ValueError,TypeError):pass
    if entry.get('streak_suspect_last_sync')==sync_id:
        return entry.get('streak_suspect_value')==value and _candidate_ready(entry,now,direction)
    continuing=False
    try:
        previous=datetime.fromisoformat(entry['streak_suspect_last_at'])
        days=(today-previous.astimezone(BUSINESS_ZONE).date()).days
        continuing=(entry.get('streak_suspect_direction', 'down') == direction and
                    previous.tzinfo is not None and previous<=now and
                    0<=value-entry['streak_suspect_value']<=days and type(entry['streak_suspect_count']) is int)
    except (ValueError,TypeError,KeyError):pass
    if not continuing:
        _clear_reset_evidence(entry)
        entry.update(streak_suspect_since=today.isoformat(),streak_suspect_count=0)
    entry.update(streak_suspect_value=value,streak_suspect_count=entry['streak_suspect_count']+1,
                 streak_suspect_last_at=now.isoformat(timespec='seconds'),streak_suspect_last_sync=sync_id,
                 streak_suspect_source=source, streak_suspect_direction=direction)
    return _candidate_ready(entry,now,direction)


def apply_reset_decision(entry,action,now):
    direction = {'reset_pending':'down', 'correction_pending':'up'}.get(entry.get('streak_status'))
    if direction is None or not _candidate_ready(entry,now,direction):
        raise ValueError('当前没有可确认的火花读数证据，请重新同步核对')
    candidate=entry['streak_suspect_value'];old=entry.get('streak_days',0)
    if type(old) is not int or (candidate>=old if direction=='down' else candidate<=old):
        raise ValueError('候选读数不再适用，请刷新核对')
    when=now.isoformat(timespec='seconds')
    if action=='accept':
        entry.update(streak_previous_days=old,streak_days=candidate,
                     streak_status='confirmed',streak_confirmed_at=entry['streak_suspect_last_at'],
                     streak_source=entry['streak_suspect_source'],streak_reason=None,
                     streak_growth_base_days=candidate, streak_growth_base_at=entry['streak_suspect_last_at'],
                     streak_growth_changed_at=entry['streak_suspect_last_at'])
        entry['streak_reset_accepted_at' if direction=='down' else 'streak_correction_accepted_at'] = when
        entry.pop('streak_history_unverified', None)
        entry.pop('streak_reset_dismissed_until',None)
    elif action=='keep':
        entry.update(streak_status='suspect',streak_reason='reset_kept' if direction=='down' else 'correction_kept',streak_reset_kept_at=when,
                     streak_reset_dismissed_until=(now.astimezone(BUSINESS_ZONE).date()+timedelta(days=RESET_DISMISS_BUSINESS_DAYS)).isoformat())
    else:raise ValueError('未知火花确认动作')
    _clear_reset_evidence(entry)


def _growth_window(entry, now):
    """A numeric change may anchor a later date, never another read on the same date."""
    old = entry['streak_days']
    previous = _confirmed_time(entry.get('streak_growth_base_at', entry['streak_confirmed_at']))
    baseline = entry.get('streak_growth_base_days', old)
    confirmed = _confirmed_time(entry['streak_confirmed_at'])
    changed = _confirmed_time(entry.get('streak_growth_changed_at', entry['streak_confirmed_at']))
    if (previous is None or changed is None or previous > now or confirmed > now
            or changed < previous or changed > confirmed):
        return None, None, 'invalid_confirmation_time'
    if type(baseline) is not int or baseline < 0 or baseline > old:
        return None, None, 'invalid_history'
    if changed.astimezone(BUSINESS_ZONE).date() < now.astimezone(BUSINESS_ZONE).date() and old > baseline:
        baseline, previous = old, changed
    return baseline, previous, None


def _growth_problem(entry, value, now):
    if not _trusted_history(entry):
        return None  # A legacy number without evidence cannot reject a first trusted read.
    baseline, previous, problem = _growth_window(entry, now)
    if problem:
        return problem
    elapsed = max(MIN_GROWTH_WINDOW_DAYS, (now.astimezone(BUSINESS_ZONE).date() - previous.astimezone(BUSINESS_ZONE).date()).days)
    return 'unexpected_jump' if value < entry['streak_days'] or value - baseline > elapsed else None


def parse_streak(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    matched = re.fullmatch(r'(?:🔥\s*)?([1-9][0-9]{0,2})(?:\s*天)?', str(value).strip())
    return int(matched[1]) if matched else None


def observe_streak(entry: dict, observed: dict, now, *, sync_id: str | None = None) -> None:
    """A failed observation must not rewrite the last positively confirmed value."""
    entry['streak_seen_at'] = now.isoformat(timespec='seconds')
    status = observed.get('streak_status', 'unknown')
    # 特殊提示只作为状态说明保存，不当作天数，也不清空历史读数。
    label = observed.get('streak_label')
    entry['streak_label'] = label if isinstance(label, str) and len(label) <= 32 and re.fullmatch(r'(?:重燃中\s*[0-9]+\s*/\s*[0-9]+|[0-9]+\s*天后消失)', label) else None
    value = parse_streak(observed.get('streak'))
    source = observed.get('streak_source')
    reason = None
    trusted_history = _trusted_history(entry)
    if trusted_history and not any(key in entry for key in _GROWTH_KEYS):
        entry.update(streak_growth_base_days=entry['streak_days'], streak_growth_base_at=entry['streak_confirmed_at'],
                     streak_growth_changed_at=entry['streak_confirmed_at'])
    if status in {'confirmed','none'} and source not in {'class','emoji'}:
        status, reason = 'unknown', 'unsupported_source'
    if status == 'confirmed' and value is not None:
        reason = _growth_problem(entry, value, now)
        if reason:
            status = 'suspect'
            if reason=='unexpected_jump' and value != entry.get('streak_days',0):
                direction = 'down' if value < entry['streak_days'] else 'up'
                if _record_reset_evidence(entry,value,source,now,sync_id,direction=direction):
                    status='reset_pending' if direction=='down' else 'correction_pending'
            else:
                _clear_reset_evidence(entry)
            logger.warning('火花读数未更新：%s', reason)
    if status == 'confirmed' and value is None:
        reason = reason or 'invalid_value'
    elif status == 'suspect':
        reason = reason or 'inconsistent_observation'
    elif status not in {'confirmed', 'suspect', 'reset_pending', 'correction_pending'} and not (
            status == 'none' and observed.get('streak_none_evidence') is True):
        reason = reason or 'unreadable'
    entry.update(streak_reason=reason, streak_observed_value=0 if status=='none' else value,
                 streak_observed_at=entry['streak_seen_at'],
                 streak_observed_source=source if source in {'class','emoji'} else None,
                 streak_observed_reason=reason)
    if status == 'confirmed' and value is not None:
        _clear_reset_evidence(entry)
        if not trusted_history:
            entry.update(streak_growth_base_days=value, streak_growth_base_at=entry['streak_seen_at'],
                         streak_growth_changed_at=entry['streak_seen_at'])
        else:
            baseline, baseline_at, _ = _growth_window(entry, now)
            entry.update(streak_growth_base_days=baseline, streak_growth_base_at=baseline_at.isoformat())
            if value != entry['streak_days']:
                entry['streak_growth_changed_at'] = entry['streak_seen_at']
            else:
                entry.setdefault('streak_growth_changed_at', entry['streak_confirmed_at'])
        entry.pop('streak_history_unverified', None)
        entry.update(streak_days=value, streak_status='confirmed',
                     streak_confirmed_at=entry['streak_seen_at'], streak_source=observed.get('streak_source'))
    elif status == 'none' and observed.get('streak_none_evidence') is True:
        _clear_reset_evidence(entry)
        if not trusted_history or entry['streak_days'] != 0:
            entry.update(streak_growth_base_days=0, streak_growth_base_at=entry['streak_seen_at'],
                         streak_growth_changed_at=entry['streak_seen_at'])
        entry.update(streak_days=0, streak_status='none',
                     streak_confirmed_at=entry['streak_seen_at'], streak_source=observed.get('streak_source'))
        entry.pop('streak_history_unverified', None)
    else:
        entry['streak_status'] = status if status in {'suspect','reset_pending','correction_pending'} else 'unknown'
