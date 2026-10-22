"""Conservative observation/candidate rules. Similarity never authorizes a send."""
import re
from urllib.parse import urldefrag, urlsplit
from .send_day import business_now


def avatar_key(value):
    """同一抖音头像资源的 CDN/尺寸不是身份变化；其它地址严格保留。"""
    raw = urldefrag(str(value or '').strip())[0]
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ''
        trusted = re.fullmatch(r'p[0-9]+(?:-pc)?\.douyinpic\.com|p[0-9]+\.huoshanimg\.com', host)
        resource = re.fullmatch(r'(/img/aweme-avatar/[A-Za-z0-9_/-]+(?:\.(?:png|jpe?g|webp))?)~[A-Za-z0-9_.-]+', parsed.path)
        if parsed.scheme == 'https' and trusted and resource and not parsed.username and not parsed.password:
            return 'douyin-avatar:' + resource.group(1)
    except ValueError:
        pass
    return raw


def unresolved_send(entry):
    return ((entry.get('send_attempt') or {}).get('status') in {'sending','unconfirmed'}
            or entry.get('last_status') in {'sending','unconfirmed'})


def identity_block_reason(entry):
    if entry.get('identity_ambiguous'):
        return 'identity_ambiguous'
    # 旧候选只用于防止重复发送，不再要求用户确认是不是同一个人。
    for candidate in entry.get('rename_candidates') or []:
        if candidate.get('unresolved_send'):
            return 'related_send_unconfirmed'
        if candidate.get('protected_day') == business_now().date().isoformat():
            return 'related_sent_today'
    if entry.get('observation_status') == 'not_observed':
        return 'not_observed'
    return None


def mark_observations(entries, seen, new_ids, capture, now):
    timestamp = now.isoformat(timespec='seconds')
    for entry in entries:
        if not (entry.get('has_conversation') or entry.get('source',{}).get('consumer')):
            continue
        entry.update(observation_status='seen' if entry['entry_id'] in seen else 'not_observed',
                     observation_at=timestamp, last_capture_complete=capture.get('complete') is True)
        if entry['entry_id'] in seen:
            entry['last_observed_at'] = timestamp
        else:
            entry.update(streak_status='unknown',streak_seen_at=timestamp,streak_reason='not_observed')
    missing = [entry for entry in entries if entry.get('observation_status')=='not_observed']
    for entry in entries:
        if entry['entry_id'] not in seen or not (entry['entry_id'] in new_ids or entry.get('identity_review_pending')):
            continue
        candidates = {row['old_entry_id']:row for row in entry.get('rename_candidates',[])
                      if any(old['entry_id']==row.get('old_entry_id') for old in entries)}
        dismissed = set(entry.get('rename_dismissed_from') or [])
        for old in missing:
            if old['entry_id'] in dismissed:
                continue
            evidence = []
            if entry.get('display_name') == old.get('display_name'):
                evidence.append('same_name')
            if avatar_key(entry.get('avatar')) and avatar_key(entry.get('avatar')) == avatar_key(old.get('avatar')):
                evidence.append('same_avatar')
            unresolved = unresolved_send(old)
            selected_identity = bool(old.get('selected'))
            if not evidence and not unresolved and not selected_identity:
                continue
            if selected_identity and not evidence:
                evidence.append('selected_identity_needs_exclusion')
            if unresolved:
                evidence.append('unresolved_send_needs_exclusion')
            candidates[old['entry_id']] = {'old_entry_id':old['entry_id'],'old_name':old['display_name'],
                'new_name':entry['display_name'],'evidence':evidence,'unresolved_send':unresolved}
        entry['rename_candidates'] = list(candidates.values())
        entry['identity_review_pending'] = bool(candidates)
