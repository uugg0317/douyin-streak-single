"""保守整理重复观察记录；纯数据转换，不读取账号或写入文件。"""
from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from .contact_identity import avatar_key, unresolved_send


def _stamp(value):
    try:
        d=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError,ValueError,OverflowError):return float('-inf')


def _name(entry):return str(entry.get('display_name') or '').replace('\u00a0',' ').strip()


def consolidate_duplicates(entries):
    """仅合并同名同头像资源的重复观察；不猜测真实同名、多平台ID或未决发送。"""
    work=deepcopy(entries);groups=defaultdict(list)
    for index,e in enumerate(work):
        resource=avatar_key(e.get('avatar'))
        if resource.startswith('douyin-avatar:') and (e.get('has_conversation') or e.get('source',{}).get('consumer')):
            groups[(_name(e),resource)].append((index,e))
    replacements={};removed=set();aliases={};skipped=0;merged=0
    history_fields=('entry_id','selected','selected_order','last_status','last_sent_at','last_attempt_at',
                    'last_unconfirmed_at','last_ok','last_msg','send_attempt','identity_history')
    for group in groups.values():
        if len(group)<2:continue
        values=[e for _,e in group]
        # 同屏出现的第2个同名同头像条目不能按展示资源猜成同一人。
        multiple_rows=any(str(e.get('consumer_key') or '').rsplit('|',1)[-1].isdigit()
                          and int(str(e.get('consumer_key')).rsplit('|',1)[-1])>1 for e in values)
        platform_ids={e.get('user_id') for e in values if e.get('user_id')}
        if multiple_rows or len(platform_ids)>1 or any(e.get('identity_conflict') or unresolved_send(e) or e.get('send_attempt') for e in values):
            skipped+=1;continue
        keep_index,base=next(((i,e) for i,e in group if e.get('selected')),group[0])
        keeper=deepcopy(base)
        fresh=max(group,key=lambda pair:(pair[1].get('observation_status')=='seen',_stamp(pair[1].get('observation_at')),pair[0]))[1]
        for key,value in fresh.items():
            if key.startswith('streak_') or key in {'display_name','nickname','avatar','consumer_key','has_conversation','channel',
                                                  'observation_status','observation_at','last_observed_at','last_capture_complete'}:
                keeper[key]=deepcopy(value)
        keeper['selected']=any(e.get('selected') for e in values)
        orders=[e.get('selected_order') for e in values if e.get('selected') and isinstance(e.get('selected_order'),(int,float))]
        keeper['selected_order']=min(orders) if orders else base.get('selected_order')
        keeper['source']={kind:any(e.get('source',{}).get(kind) for e in values) for kind in ('consumer','creator')}
        if platform_ids:
            keeper['user_id']=next(iter(platform_ids));keeper['platform_id_verified']=any(e.get('platform_id_verified') for e in values)
        for key in ('last_sent_at','last_unconfirmed_at'):
            present=[e[key] for e in values if e.get(key)]
            if present:keeper[key]=max(present,key=_stamp)
        attempts=[e for e in values if e.get('last_attempt_at') or e.get('last_sent_at')]
        if attempts:
            latest=max(attempts,key=lambda e:_stamp(e.get('last_attempt_at') or e.get('last_sent_at')))
            for key in ('last_attempt_at','last_status','last_ok','last_msg'):
                if key in latest:keeper[key]=deepcopy(latest[key])
        if fresh.get('streak_status') not in {'confirmed','none'}:
            past=[e for e in values if e.get('streak_confirmed_at')]
            if past:
                latest=max(past,key=lambda e:_stamp(e.get('streak_confirmed_at')))
                for key in ('streak_days','streak_confirmed_at','streak_source'):
                    if key in latest:keeper[key]=deepcopy(latest[key])
        history=[];old_ids=[];candidates=[];dismissed=[]
        for _,e in group:
            history.extend(deepcopy(e.get('merged_record_history') or []))
            history.append({key:deepcopy(e[key]) for key in history_fields if key in e})
            old_ids.extend(e.get('merged_entry_ids') or [])
            candidates.extend(deepcopy(e.get('rename_candidates') or []))
            dismissed.extend(e.get('rename_dismissed_from') or [])
            if e['entry_id']!=keeper['entry_id']:
                aliases[e['entry_id']]=keeper['entry_id'];old_ids.append(e['entry_id'])
        keeper['merged_entry_ids']=sorted(set(old_ids)-{keeper['entry_id']})
        keeper['merged_record_history']=history
        keeper['rename_candidates']=candidates;keeper['rename_dismissed_from']=dismissed
        replacements[keep_index]=keeper
        removed.update(i for i,_ in group if i!=keep_index);merged+=1
    result=[replacements.get(i,e) for i,e in enumerate(work) if i not in removed]
    for e in result:
        candidates={}
        for item in e.get('rename_candidates') or []:
            item=deepcopy(item);old=aliases.get(item.get('old_entry_id'),item.get('old_entry_id'))
            if old==e['entry_id']:continue
            item['old_entry_id']=old
            if old not in candidates or item.get('unresolved_send'):candidates[old]=item
        if 'rename_candidates' in e:
            e['rename_candidates']=list(candidates.values());e['identity_review_pending']=bool(candidates)
        if 'rename_dismissed_from' in e:
            e['rename_dismissed_from']=sorted({aliases.get(key,key) for key in e['rename_dismissed_from']} - {e['entry_id']})
    return result,{'before':len(entries),'after':len(result),'merged_groups':merged,'removed_duplicates':len(removed),'skipped_groups':skipped,'aliases':aliases}
