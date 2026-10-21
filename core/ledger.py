"""好友台账：本地 entry_id 为主键。显示名与头像仅用于显示/保守匹配，不能断言平台身份。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from .config import DATA_DIR, atomic_write_text
from .send_day import business_now, daily_skip_code
from .streaks import parse_streak, observe_streak, apply_reset_decision, confirmation_age
from .contact_identity import identity_block_reason, mark_observations, unresolved_send, avatar_key

LEDGER_PATH = DATA_DIR / "ledger.json"
LEDGER_BAK_PATH = DATA_DIR / "ledger.json.bak"

logger = logging.getLogger("douyin-cloud-streak")

_lock = threading.RLock()


def _serialized_mutation(func):
    """好友台账的读取-修改-写入必须是同一个临界区。"""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with _lock:
            return func(*args, **kwargs)
    return wrapped


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_entry(display_name: str) -> dict:
    """创建本地身份；抖音号/昵称均可变，不作为本地主键。"""
    return {
        "entry_id": uuid.uuid4().hex,
        "identity_ambiguous": False,
        "platform_id_verified": False,
        "display_name": display_name,
        "avatar": "",
        "nickname": "",
        "short_id": "",
        "user_id": "",
        "streak_days": 0,
        "has_conversation": False,
        "selected": False,
        "selected_order": None,  # 勾选顺序（越小越靠前，仅勾选时有效；持久化保证重启后置顶顺序不丢）
        "last_sent_at": None,     # 仅在发送成功时写入（界面用它表示「成功」）
        "last_unconfirmed_at": None,  # 可选：最近一次发送结果待核对的时间
        "last_attempt_at": None,  # 最近一次尝试时间，无论成败
        "last_status": "pending",  # pending | success | failed
        "last_ok": None,          # 最近一次尝试是否成功（None = 从未尝试）
        "last_msg": "",           # 最近一次成功发送的文案，供 build_message 避免连续重复
        "channel": "none",        # consumer | creator | none
        "join_confidence": "low",  # high: display_name 与 nickname 已对上；low: 未确认
        "source": {"creator": False, "consumer": False},
    }


def _parse_streak(text) -> int | None:
    """Accept only a whole streak representation; failure is not zero."""
    return parse_streak(text)


def _norm_ws(s) -> str:
    """空白归一化（仅用于 join 匹配，不改存储原样值）：
    consumer 页显示名可能含不间断空格（U+00A0），creator 接口返回普通空格。"""
    return str(s or "").replace("\u00a0", " ").strip()



class IdentityConflict(ValueError):
    """Identity ambiguity must be handled as a conflict, never first-match selection."""


class LedgerVersionConflict(ValueError):
    """The saved ledger changed since the caller reviewed its selection."""


def _mark_ambiguity(entries: list[dict]) -> None:
    # 未出现在本次列表中的历史行不再把唯一当前会话判为同名冲突。
    current = [e for e in entries if e.get('observation_status') != 'not_observed']
    by_id = {e.get('entry_id'):e for e in entries}
    all_counts = Counter(_norm_ws(e.get("display_name")) for e in current)
    consumer_counts = Counter(_norm_ws(e.get("display_name")) for e in current
                              if e.get("has_conversation") or e.get("source", {}).get("consumer"))
    for e in entries:
        is_consumer = bool(e.get("has_conversation") or e.get("source", {}).get("consumer"))
        # A separate creator observation is not a second consumer conversation.
        # Its unresolved identity must not disable an otherwise unique consumer.
        duplicate = consumer_counts[_norm_ws(e.get("display_name"))] > 1 if is_consumer else all_counts[_norm_ws(e.get("display_name"))] > 1
        e["identity_ambiguous"] = bool(e.get("identity_conflict") or duplicate)
        for candidate in e.get('rename_candidates') or []:
            old = by_id.get(candidate.get('old_entry_id'))
            if old is not None:
                protected = daily_skip_code(old)
                candidate['unresolved_send'] = unresolved_send(old) or protected == 'unconfirmed_today'
                candidate['protected_day'] = business_now().date().isoformat() if protected == 'already_sent_today' else None
        e['send_block_reason'] = identity_block_reason(e)


def resolve_entry(entries: list[dict], *, entry_id: str = "", display_name: str = "") -> dict | None:
    if entry_id:
        matches = [e for e in entries if e.get("entry_id") == entry_id]
    else:
        name = _norm_ws(display_name)
        matches = [e for e in entries if _norm_ws(e.get("display_name")) == name] if name else []
    if len(matches) > 1:
        raise IdentityConflict("存在同名好友，请使用 entry_id，不能按名字猜测身份")
    return matches[0] if matches else None


def _parse_ledger_text(text: str) -> list[dict]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("ledger 不是数组")
    entries = [dict(e) for e in data if isinstance(e, dict) and e.get("display_name")]
    used = set()
    for index, e in enumerate(entries):
        key = str(e.get("entry_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", key) or key in used:
            # Deterministic fallback also preserves identity when loading the backup.
            seed = json.dumps([index, e.get("display_name"), e.get("avatar"), e.get("user_id")], ensure_ascii=False)
            key = uuid.uuid5(uuid.NAMESPACE_URL, "sparkkeeper-legacy:" + seed).hex
            while key in used:
                key = uuid.uuid4().hex
            e["entry_id"] = key
            e["_identity_migrated"] = True
            e["platform_id_verified"] = False
        used.add(key)
        e["no_consumer_conversation"] = not e.get("has_conversation")
        e["creator_only"] = e.get("channel") == "creator" and e["no_consumer_conversation"]
    _mark_ambiguity(entries)
    return entries


def _load_ledger_unlocked() -> list[dict]:
    """读台账。文件不存在返回空列表（首次运行正常）；文件存在但解析失败时，
    绝不静默返回空列表——那会导致 run_send 看到 0 个好友，一整晚不发却显示「正常」。
    此时尝试上一份 .bak，仍失败才告警并返回空。"""
    if not LEDGER_PATH.exists():
        # 进程若恰好在「主文件改名为 .bak」后、写入新主文件前退出，主文件会
        # 缺失但备份完整。此时不能把台账误判为空。
        if LEDGER_BAK_PATH.exists():
            try:
                entries = _parse_ledger_text(LEDGER_BAK_PATH.read_text(encoding="utf-8"))
                logger.warning("ledger.json 缺失，已从 ledger.json.bak 恢复 %d 条好友", len(entries))
                return entries
            except Exception as e:
                logger.error("ledger.json.bak 解析失败（%s），按空台账处理", e)
        return []
    try:
        return _parse_ledger_text(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("ledger.json 解析失败（%s），尝试读取备份 ledger.json.bak", e)
    if LEDGER_BAK_PATH.exists():
        try:
            entries = _parse_ledger_text(LEDGER_BAK_PATH.read_text(encoding="utf-8"))
            logger.warning("已从 ledger.json.bak 恢复 %d 条好友", len(entries))
            return entries
        except Exception as e:
            logger.error("ledger.json.bak 也解析失败（%s），本轮按空台账处理", e)
    return []


def load_ledger() -> list[dict]:
    with _lock:
        entries = _load_ledger_unlocked()
        if any(e.pop("_identity_migrated", False) for e in entries):
            # Persist local IDs without dropping any legacy entry or its selection/history.
            for e in entries:
                e.pop("_identity_migrated", None)
            _save(entries)
        return entries


def _save(entries: list[dict]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 写新内容前把当前有效文件留作 .bak，下次主文件损坏可回滚
        try:
            if LEDGER_PATH.exists() and LEDGER_PATH.stat().st_size > 0:
                previous = LEDGER_PATH.read_text(encoding='utf-8')
                _parse_ledger_text(previous)  # Never replace a good recovery file with a corrupt primary.
                atomic_write_text(LEDGER_BAK_PATH, previous)
        except (OSError, ValueError):
            pass
        atomic_write_text(LEDGER_PATH, json.dumps(entries, ensure_ascii=False, indent=2))


def _upsert(entries: list[dict], entry: dict) -> dict:
    target = resolve_entry(entries, entry_id=entry.get("entry_id", ""), display_name=entry["display_name"])
    if target is None:
        target = _default_entry(entry["display_name"])
        entries.append(target)
    old_uid = target.get("user_id")
    new_uid = entry.get("user_id")
    if old_uid and new_uid and old_uid != new_uid:
        raise IdentityConflict("平台身份冲突，不能覆盖现有好友")
    preserve = {k: target.get(k) for k in ("entry_id", "selected", "selected_order", "last_sent_at")}
    target.update(entry)
    target.update(preserve)
    return target


@_serialized_mutation
def merge_consumer_contacts(contacts: list[dict], *, capture: dict | None = None) -> dict:
    entries = load_ledger()
    seen, new_ids = set(), set()
    sync_id = uuid.uuid4().hex
    added = updated = 0
    observed = Counter()
    name_counts = Counter(_norm_ws(c.get("name")) for c in contacts or [] if c.get("name"))
    avatar_counts = Counter((_norm_ws(c.get("name")), str(c.get("avatar") or "")) for c in contacts or [] if c.get("name"))
    for c in contacts or []:
        name = str(c.get("name", "")).strip()
        if not name:
            continue
        avatar = str(c.get("avatar") or "")
        uid = str(c.get("user_id") or "")
        verified = bool(uid and c.get("identity_verified"))
        observed[(name, avatar)] += 1
        ordinal = observed[(name, avatar)]
        key = str(c.get("row_key") or f"{_norm_ws(name)}|{avatar}|{ordinal}")
        target = None
        if verified:
            candidates = [e for e in entries if e.get("platform_id_verified") and e.get("user_id") == uid]
            if len(candidates) == 1:
                target = candidates[0]
        if target is None:
            candidates = [e for e in entries if e.get("source", {}).get("consumer")
                          and e.get("consumer_key") == key]
            if len(candidates) == 1:
                target = candidates[0]
        if target is None and (name_counts[_norm_ws(name)] == 1 or avatar and avatar_counts[(_norm_ws(name), avatar)] == 1):
            candidates = [e for e in entries if _norm_ws(e.get("display_name")) == _norm_ws(name)
                          and (e.get("has_conversation") or e.get("source", {}).get("consumer") or not e.get("source", {}).get("creator"))
                          and (avatar_key(e.get("avatar")) == avatar_key(avatar) or name_counts[_norm_ws(name)] == 1 and not e.get("avatar"))]
            if len(candidates) == 1:
                target = candidates[0]
        if target is not None and verified and target.get("user_id") and target.get("user_id") != uid:
            target["identity_conflict"] = True
            target = None
        if target is not None and target['entry_id'] in seen:
            target['identity_conflict'] = True
            target = None
        if target is None:
            target = _default_entry(name)
            entries.append(target)
            added += 1
            new_ids.add(target['entry_id'])
        else:
            updated += 1
        target.update(display_name=name, avatar=avatar, consumer_key=key,
                      has_conversation=True, channel="consumer")
        observe_streak(target, c, business_now(), sync_id=sync_id)
        target.setdefault("source", {})["consumer"] = True
        if verified:
            target["user_id"] = uid
            target["platform_id_verified"] = True
        if c.get("identity_ambiguous"):
            target["identity_conflict"] = True
        seen.add(target['entry_id'])
    if capture is not None:
        mark_observations(entries, seen, new_ids, capture, business_now())
    _mark_ambiguity(entries)
    if added or updated or capture is not None:
        _save(entries)
    return {"added": added, "updated": updated, "total": len(entries)}


def legacy_import_allowed() -> bool:
    """Only a first-run ledger may inherit the legacy config selection."""
    with _lock:
        return not LEDGER_PATH.exists() and not LEDGER_BAK_PATH.exists()


@_serialized_mutation
def import_config_friends(friends: list[str], *, only_if_missing: bool = False) -> dict:
    # Automatic migration and explicit clearing share the same lock; a saved
    # empty selection must never be mistaken for an uninitialized ledger.
    if only_if_missing and not legacy_import_allowed():
        return {"added": 0, "selected": 0, "ambiguous": 0}
    entries = load_ledger()
    added = selected = ambiguous = 0
    for name in friends or []:
        name = str(name).strip()
        if not name:
            continue
        try:
            target = resolve_entry(entries, display_name=name)
        except IdentityConflict:
            ambiguous += 1
            continue
        if target is None:
            target = _default_entry(name)
            entries.append(target)
            added += 1
        if not target.get("selected"):
            target["selected"] = True
            selected += 1
    if added or selected:
        _save(entries)
    return {"added": added, "selected": selected, "ambiguous": ambiguous}


def get_selected() -> list[dict]:
    return [e for e in load_ledger() if e.get("selected")]


def streak_confirmation_age(entry):
    return confirmation_age(entry,business_now())


def ledger_snapshot(expected_version: str | None = None) -> dict:
    """Read, fingerprint and validate one stable ledger view in the same lock epoch."""
    with _lock:
        entries = load_ledger()
        canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if expected_version is not None and expected_version != version:
            raise LedgerVersionConflict("已生效好友名单已变化，请刷新并确认后重试")
        return {"entries": entries, "ledger_version": version}


@_serialized_mutation
def remove_entry(display_name: str = "", *, entry_id: str = "") -> bool:
    entries = load_ledger()
    target = resolve_entry(entries, entry_id=entry_id, display_name=display_name)
    if target is None:
        return False
    _check_deletion_safe(target)
    entries.remove(target)
    _save(entries)
    return True


def _check_deletion_safe(entry: dict) -> None:
    attempt = entry.get("send_attempt") or {}
    if (attempt.get("status") in {"sending", "unconfirmed"}
            or entry.get("last_status") in {"sending", "unconfirmed"}):
        raise IdentityConflict("该好友存在发送中或待核对记录，不能删除；请保留台账并先人工核对发送结果")


@_serialized_mutation
def remove_entries(*, entry_ids=(), display_names=()) -> dict:
    """Validate every target and its intent before atomically deleting a batch."""
    entries = load_ledger()
    keys = set(entry_ids)
    for name in display_names:
        target = resolve_entry(entries, display_name=name)
        if target is not None:
            keys.add(target["entry_id"])
    targets = [entry for entry in entries if entry.get("entry_id") in keys]
    for target in targets:
        _check_deletion_safe(target)
    if targets:
        _save([entry for entry in entries if entry.get("entry_id") not in keys])
    return {"deleted": len(targets), "requested": len(keys)}


@_serialized_mutation
def merge_creator_map(mapping: dict) -> dict:
    entries = load_ledger()
    joined = added = updated = 0
    for sid, info in (mapping or {}).items():
        sid = str(sid).strip()
        uid = str(info.get("user_id") or "")
        nick = str(info.get("nickname") or sid).strip()
        if not sid:
            continue
        # Creator API user_id is evidence; a nickname or short_id is not.
        candidates = [e for e in entries if uid and e.get("platform_id_verified")
                      and e.get("user_id") == uid]
        target = candidates[0] if len(candidates) == 1 else None
        if target is None:
            # A repeated observation without a user ID may only reuse its isolated creator stub.
            candidates = [e for e in entries if not uid and not e.get("has_conversation")
                          and e.get("source", {}).get("creator") and e.get("short_id") == sid]
            target = candidates[0] if len(candidates) == 1 else None
        if target is None:
            target = _default_entry(nick)
            target["channel"] = "creator"
            entries.append(target)
            added += 1
        else:
            updated += 1
            joined += int(bool(target.get("source", {}).get("consumer")))
        if target.get("user_id") and uid and target["user_id"] != uid:
            raise IdentityConflict("creator 平台身份冲突，不能覆盖")
        target.update(nickname=nick, short_id=sid)
        if not target.get("has_conversation"):
            target["display_name"] = nick
        if uid:
            target["user_id"] = uid
            target["platform_id_verified"] = True
        target.setdefault("source", {})["creator"] = True
        target["join_confidence"] = "high" if target.get("source", {}).get("consumer") and target.get("platform_id_verified") else "low"
    _mark_ambiguity(entries)
    if added or updated:
        _save(entries)
    return {"joined": joined, "added": added, "updated": updated, "total": len(entries)}


@_serialized_mutation
def confirm_join(display_name: str = "", *, entry_id: str = "") -> None:
    entries = load_ledger()
    target = resolve_entry(entries, entry_id=entry_id, display_name=display_name)
    if target is not None:
        target.setdefault("source", {})["consumer"] = True
        target["channel"] = "consumer"
        # Locating an exact display name does not establish a creator/platform identity join.
        _save(entries)


@_serialized_mutation
def clear_selection() -> dict:
    """Persist an explicitly confirmed empty selection in one ledger mutation."""
    entries = load_ledger()
    updated = sum(bool(e.get("selected")) or e.get("selected_order") is not None for e in entries)
    for entry in entries:
        entry.update(selected=False, selected_order=None)
    # Save even a first-run empty ledger: its existence records that the user
    # already made a selection decision, preventing automatic legacy import.
    _save(entries)
    return {"updated": updated, "added": 0, **ledger_snapshot()}


@_serialized_mutation
def review_identity(old_entry_id: str, new_entry_id: str, action: str, expected_version: str) -> dict:
    """Caller holds run_lock. One versioned ledger transaction owns the decision."""
    snapshot = ledger_snapshot(expected_version)
    entries = snapshot['entries']
    old = resolve_entry(entries, entry_id=old_entry_id)
    new = resolve_entry(entries, entry_id=new_entry_id)
    if old is None or new is None or old is new:
        raise IdentityConflict('关联对象已变化，请刷新')
    candidates = new.get('rename_candidates') or []
    if not any(row.get('old_entry_id') == old_entry_id for row in candidates):
        raise IdentityConflict('该候选关系已失效，请刷新后核对')
    if action == 'different':
        new['rename_candidates'] = [row for row in candidates if row.get('old_entry_id') != old_entry_id]
        new['identity_review_pending'] = bool(new['rename_candidates'])
        new['rename_dismissed_from'] = sorted(set(new.get('rename_dismissed_from') or []) | {old_entry_id})
    elif action == 'confirm':
        if old.get('observation_status') != 'not_observed' or new.get('observation_status') != 'seen':
            raise IdentityConflict('观察状态已变化，不能确认改名；请重新同步核对')
        owners = [entry for entry in entries if any(row.get('old_entry_id') == old_entry_id for row in entry.get('rename_candidates',[]))]
        if len(candidates) != 1 or len(owners) != 1:
            raise IdentityConflict('存在多个候选，请先明确排除无关关系，不能自动合并')
        if (old.get('identity_conflict') or new.get('identity_conflict') or
                old.get('user_id') and new.get('user_id') and old['user_id'] != new['user_id']):
            raise IdentityConflict('存在平台身份冲突，不能合并')
        evidence_fields = ('last_sent_at','last_unconfirmed_at','last_attempt_at','send_attempt','identity_history')
        if any(new.get(key) for key in evidence_fields) or new.get('last_status','pending') != 'pending':
            raise IdentityConflict('新记录已经有独立发送或身份历史，不能自动移除；请保留两份记录核对')
        before_name = old['display_name']
        for key in ('display_name','avatar','consumer_key','has_conversation','channel','observation_status',
                    'observation_at','last_observed_at','last_capture_complete'):
            if key in new:
                old[key] = new[key]
        old.setdefault('source',{})['consumer'] = True
        if new.get('platform_id_verified') and new.get('user_id'):
            old.update(user_id=new['user_id'],platform_id_verified=True)
        observe_streak(old, {'streak':new.get('streak_days'),'streak_status':new.get('streak_status','unknown'),
                            'streak_source':new.get('streak_source'),'streak_none_evidence':new.get('streak_status')=='none'}, business_now())
        old.setdefault('identity_history',[]).append({'at':business_now().isoformat(timespec='seconds'),
            'action':'user_confirmed_rename','old_name':before_name,'new_name':old['display_name'],
            'absorbed_entry_id':new_entry_id})
        old['rename_candidates'] = []
        old['identity_review_pending'] = False
        entries.remove(new)
    else:
        raise IdentityConflict('未知身份确认动作')
    _mark_ambiguity(entries)
    _save(entries)
    return {'ok':True,'action':action,**ledger_snapshot()}


@_serialized_mutation
def review_streak(entry_id: str, action: str, expected_version: str) -> dict:
    snapshot=ledger_snapshot(expected_version)
    entry=resolve_entry(snapshot['entries'],entry_id=entry_id)
    if entry is None or identity_block_reason(entry):
        raise IdentityConflict('好友身份或观察状态待核对，暂不能确认火花重置')
    try:apply_reset_decision(entry,action,business_now())
    except ValueError as error:raise IdentityConflict(str(error)) from None
    _save(snapshot['entries'])
    return {'ok':True,'action':action,**ledger_snapshot()}


@_serialized_mutation
def set_selected(entries_in: list[dict]) -> dict:
    entries = load_ledger()
    updated = added = 0
    # Validate the entire batch before persisting any change.
    resolved = []
    for item in entries_in:
        name = str(item.get("display_name") or "").strip()
        entry_id = str(item.get("entry_id") or "").strip()
        if not name and not entry_id:
            continue
        target = resolve_entry(entries, entry_id=entry_id, display_name=name)
        if target is None:
            if entry_id:
                raise IdentityConflict("好友编号不存在，请刷新好友列表")
            target = _default_entry(name)
            entries.append(target)
            added += 1
        resolved.append((target, item))
    for target, item in resolved:
        sel = bool(item.get("selected"))
        order = item.get("selected_order") if sel else None
        if target.get("selected") != sel or target.get("selected_order") != order:
            updated += 1
        target.update(selected=sel, selected_order=order)
    if updated or added:
        _save(entries)
    return {"updated": updated, "added": added, **ledger_snapshot()}


@_serialized_mutation
def begin_send_attempt(entry_id: str, *, force: bool = False, expected_display_name: str | None = None) -> str:
    """Durably claim a stable identity before the only message submission point."""
    entries = load_ledger()
    target = resolve_entry(entries, entry_id=entry_id)
    if target is None or identity_block_reason(target):
        raise IdentityConflict("发送目标不存在或身份不明确，未提交消息")
    if expected_display_name is not None and target.get('display_name') != expected_display_name:
        raise IdentityConflict('好友名称已变更，拒绝旧任务提交；请刷新并确认身份')
    previous = target.get("send_attempt") or {}
    if previous.get("status") in {"sending", "unconfirmed"} and not force:
        raise IdentityConflict("该好友有未核对的发送意图，不能自动重试")
    when = business_now()
    attempt_id = uuid.uuid4().hex
    target["send_attempt"] = {"attempt_id": attempt_id, "entry_id": entry_id,
                              "business_date": when.date().isoformat(), "status": "sending",
                              "started_at": when.isoformat(timespec="seconds")}
    target.update(last_status="sending", last_ok=None, last_attempt_at=target["send_attempt"]["started_at"])
    _save(entries)
    return attempt_id


@_serialized_mutation
def recover_interrupted_attempts() -> int:
    """Startup only, after the instance lock: never infer that a lost attempt was unsent."""
    entries = load_ledger()
    recovered = 0
    for entry in entries:
        attempt = entry.get("send_attempt") or {}
        if attempt.get("status") != "sending":
            continue
        attempt.update(status="unconfirmed", recovered_at=business_now().isoformat(timespec="seconds"))
        entry.update(last_status="unconfirmed", last_ok=None,
                     last_unconfirmed_at=attempt.get("started_at") or entry.get("last_attempt_at") or _now())
        recovered += 1
    if recovered:
        _save(entries)
    return recovered


@_serialized_mutation
def update_send_result(display_name: str, ok: bool, at: str | None = None,
                       via_creator: bool = False, msg: str | None = None,
                       *, entry_id: str = "", status: str | None = None, attempt_id: str = "") -> None:
    entries = load_ledger()
    target = resolve_entry(entries, entry_id=entry_id, display_name=display_name)
    if attempt_id and (target is None or (target.get("send_attempt") or {}).get("attempt_id") != attempt_id):
        raise IdentityConflict("发送意图已变更，拒绝旧任务覆盖当前结果")
    if target is not None:
        when = at or _now()
        outcome = status or ("success" if ok else "failed")
        if outcome not in {"success", "failed", "unconfirmed", "rejected"}:
            raise ValueError("无效发送状态")
        if attempt_id:
            target["send_attempt"].update(status="accepted" if outcome == "success" else "unconfirmed", finished_at=when)
        target.update(last_attempt_at=when, last_ok=True if outcome == "success" else None if outcome == "unconfirmed" else False,
                      last_status=outcome)
        if outcome == "unconfirmed":
            target["last_unconfirmed_at"] = when
        if outcome == "success":
            target["last_sent_at"] = when
            if attempt_id:
                target["last_unconfirmed_at"] = None
            if msg:
                target["last_msg"] = msg
            if not via_creator:
                target["has_conversation"] = True
        _save(entries)


@_serialized_mutation
def mark_no_consumer_conversation(display_name: str = "", *, entry_id: str = "") -> None:
    entries = load_ledger()
    target = resolve_entry(entries, entry_id=entry_id, display_name=display_name)
    if target is not None and target.get("channel") == "creator" and target.get("has_conversation"):
        target["has_conversation"] = False
        _save(entries)


def stats() -> dict:
    """台账自愈报表：分布统计、连续成功 Top、low 待确认、近 7 天发送。"""
    entries = load_ledger()
    now = datetime.now().astimezone()
    week_ago = (now - timedelta(days=7)).isoformat()
    high = sum(1 for e in entries if e.get("join_confidence") == "high")
    no_conv = [
        {"display_name": e["display_name"], "channel": e.get("channel")}
        for e in entries
        if not e.get("has_conversation")
    ]
    low_pending = [
        e["display_name"]
        for e in entries
        if e.get("join_confidence") == "low" and e.get("has_conversation")
    ]
    recent_sent = [
        e["display_name"]
        for e in entries
        if str(e.get("last_sent_at") or "") >= week_ago
    ]
    top_streak = sorted((e for e in entries if e.get('streak_status') not in {'unknown','suspect','reset_pending'}),
                        key=lambda e: -(e.get("streak_days") or 0))[:10]
    return {
        "total": len(entries),
        "selected": sum(1 for e in entries if e.get("selected")),
        "confidence": {"high": high, "low": len(entries) - high},
        "with_short_id": sum(1 for e in entries if e.get("short_id")),
        "no_conversation": no_conv,
        "low_pending": low_pending,
        "recent_sent_7d": recent_sent,
        "top_streak": [
            {"display_name": e["display_name"], "streak_days": e.get("streak_days")}
            for e in top_streak
        ],
    }


# 别名兼容
update_from_sync = merge_consumer_contacts
