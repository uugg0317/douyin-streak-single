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
               'streak_suspect_last_at','streak_suspect_last_sync','streak_suspect_source')


def _clear_reset_evidence(entry):
    for key in _RESET_KEYS:entry.pop(key,None)


def confirmation_age(entry,now):
    view={'streak_confirmed_date':None,'streak_unconfirmed_days':None,'streak_may_be_stale':False}
    try:
        when=datetime.fromisoformat(str(entry.get('streak_confirmed_at','')).replace('Z','+00:00'))
        if when.tzinfo is None or when>now:return view
        confirmed=when.astimezone(BUSINESS_ZONE).date()
        days=(now.astimezone(BUSINESS_ZONE).date()-confirmed).days
    except (ValueError,TypeError):return view
    view.update(streak_confirmed_date=confirmed.isoformat(),streak_unconfirmed_days=days,
        streak_may_be_stale=entry.get('streak_status') in {'unknown','suspect','reset_pending'} and days>STREAK_STALE_AFTER_DAYS)
    return view


def _reset_ready(entry,now):
    try:
        since=date.fromisoformat(entry['streak_suspect_since'])
        last=datetime.fromisoformat(entry['streak_suspect_last_at'])
        count=entry['streak_suspect_count']
        return (type(count) is int and count>=2 and last.tzinfo is not None and last<=now
                and (last.astimezone(BUSINESS_ZONE).date()-since).days>=1
                and entry.get('streak_suspect_source') in {'class','emoji'}
                and parse_streak(entry.get('streak_suspect_value')) is not None)
    except (ValueError,TypeError,KeyError):return False


def _record_reset_evidence(entry,value,source,now,sync_id):
    if not isinstance(sync_id,str) or not sync_id:return False
    today=now.astimezone(BUSINESS_ZONE).date()
    try:
        if date.fromisoformat(entry.get('streak_reset_dismissed_until',''))>=today:
            _clear_reset_evidence(entry);return False
    except (ValueError,TypeError):pass
    if entry.get('streak_suspect_last_sync')==sync_id:
        return entry.get('streak_suspect_value')==value and _reset_ready(entry,now)
    continuing=False
    try:
        previous=datetime.fromisoformat(entry['streak_suspect_last_at'])
        days=(today-previous.astimezone(BUSINESS_ZONE).date()).days
        continuing=(previous.tzinfo is not None and previous<=now and
                    0<=value-entry['streak_suspect_value']<=days and type(entry['streak_suspect_count']) is int)
    except (ValueError,TypeError,KeyError):pass
    if not continuing:
        _clear_reset_evidence(entry)
        entry.update(streak_suspect_since=today.isoformat(),streak_suspect_count=0)
    entry.update(streak_suspect_value=value,streak_suspect_count=entry['streak_suspect_count']+1,
                 streak_suspect_last_at=now.isoformat(timespec='seconds'),streak_suspect_last_sync=sync_id,
                 streak_suspect_source=source)
    return _reset_ready(entry,now)


def apply_reset_decision(entry,action,now):
    if entry.get('streak_status')!='reset_pending' or not _reset_ready(entry,now):
        raise ValueError('当前没有可确认的火花重置证据，请重新同步核对')
    candidate=entry['streak_suspect_value'];old=entry.get('streak_days',0)
    if type(old) is not int or candidate>=old:
        raise ValueError('重置读数不再适用，请刷新核对')
    when=now.isoformat(timespec='seconds')
    if action=='accept':
        entry.update(streak_previous_days=old,streak_reset_accepted_at=when,streak_days=candidate,
                     streak_status='confirmed',streak_confirmed_at=entry['streak_suspect_last_at'],
                     streak_source=entry['streak_suspect_source'],streak_reason=None)
        entry.pop('streak_reset_dismissed_until',None)
    elif action=='keep':
        entry.update(streak_status='suspect',streak_reason='reset_kept',streak_reset_kept_at=when,
                     streak_reset_dismissed_until=(now.astimezone(BUSINESS_ZONE).date()+timedelta(days=RESET_DISMISS_BUSINESS_DAYS)).isoformat())
    else:raise ValueError('未知火花确认动作')
    _clear_reset_evidence(entry)


def _growth_problem(entry, value, now):
    old = entry.get('streak_days') or 0
    if not isinstance(old, int) or isinstance(old, bool):
        return 'invalid_history'
    when = entry.get('streak_confirmed_at')
    elapsed = MIN_GROWTH_WINDOW_DAYS
    if when:
        try:
            previous = datetime.fromisoformat(str(when).replace('Z','+00:00'))
            if previous.tzinfo is None or previous > now:
                return 'invalid_confirmation_time'
            elapsed = max(MIN_GROWTH_WINDOW_DAYS, (now.astimezone(BUSINESS_ZONE).date() - previous.astimezone(BUSINESS_ZONE).date()).days)
        except (ValueError, TypeError):
            return 'invalid_confirmation_time'
    if not when and old == 0:
        return None  # First trusted observation, no invented history timestamp.
    return 'unexpected_jump' if value < old or value - old > elapsed else None


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
    if status in {'confirmed','none'} and source not in {'class','emoji'}:
        status, reason = 'unknown', 'unsupported_source'
    if status == 'confirmed' and value is not None:
        reason = _growth_problem(entry, value, now)
        if reason:
            status = 'suspect'
            if reason=='unexpected_jump' and value<entry.get('streak_days',0):
                if _record_reset_evidence(entry,value,source,now,sync_id):
                    status='reset_pending'
            else:
                _clear_reset_evidence(entry)
            logger.warning('火花读数未更新：%s', reason)
    entry['streak_reason'] = reason
    if status == 'confirmed' and value is not None:
        _clear_reset_evidence(entry)
        entry.update(streak_days=value, streak_status='confirmed',
                     streak_confirmed_at=entry['streak_seen_at'], streak_source=observed.get('streak_source'))
    elif status == 'none' and observed.get('streak_none_evidence') is True:
        _clear_reset_evidence(entry)
        entry.update(streak_days=0, streak_status='none',
                     streak_confirmed_at=entry['streak_seen_at'], streak_source=observed.get('streak_source'))
    else:
        entry['streak_status'] = status if status in {'suspect','reset_pending'} else 'unknown'
