"""发送后只读会话证据。本模块不输入文字、不按发送键、不重发。

选择器分两层：
- 本地合成页能测到解析逻辑，不能证明抖音线上 DOM 使用同名节点。
- 标 needs_live_verification 的选择器必须在真实抖音私信页再核对一次。

LIVE_DOM_SELECTORS 是这份清单的单一来源。
"""
from __future__ import annotations

import re
from datetime import date

from .send_day import timestamp_date
from .streaks import parse_streak

# 发送后等待再打开会话。测试可把模块常量改成 0。
POST_SEND_VERIFY_SECONDS = 3.0
EXPIRING_WITHIN_DAYS = 3

LIVE_DOM_SELECTORS = (
    {"id": "conversation-active", "selector": "[data-conversation-active='true']",
     "purpose": "当前会话容器", "needs_live_verification": True},
    {"id": "conversation-title", "selector": "[data-conversation-title]",
     "purpose": "会话标题，必须与好友名精确一致才采纳消息", "needs_live_verification": True},
    {"id": "structural-header", "selector": "右侧顶部 header / 精确标题文本",
     "purpose": "没有 data-conversation-title 时的标题回退", "needs_live_verification": True},
    {"id": "active-row-title", "selector": ".conversationConversationItemcurConversation .conversationConversationItemtitle",
     "purpose": "左侧当前会话标题，与右侧标题不一致时拒绝确认", "needs_live_verification": True},
    {"id": "message-list", "selector": "[data-message-list]",
     "purpose": "消息列表边界", "needs_live_verification": True},
    {"id": "own-data-is-self", "selector": "[data-is-self='true']",
     "purpose": "己方消息显式属性", "needs_live_verification": True},
    {"id": "own-data-sender", "selector": "[data-sender='self']",
     "purpose": "己方消息发送者属性", "needs_live_verification": True},
    {"id": "own-data-direction", "selector": "[data-direction='outgoing']",
     "purpose": "己方消息方向属性", "needs_live_verification": True},
    {"id": "own-isFromMe", "selector": "[class*='isFromMe']",
     "purpose": "己方消息类名（例如 isFromMe）", "needs_live_verification": True},
    {"id": "right-side-bubble", "selector": "气泡中心位于消息区域宽度 62% 以右",
     "purpose": "没有任何己方标记时，才用右侧气泡几何位置作回退", "needs_live_verification": True},
    {"id": "message-text", "selector": "[data-message-text], img[alt], [aria-label]",
     "purpose": "正文或表情文案，例如 [续火花吧]", "needs_live_verification": True},
    {"id": "message-time", "selector": "time[datetime], [data-message-time], [data-message-time-text]",
     "purpose": "消息时间；仅有 HH:MM 时按当天处理，这个假设需要实页确认", "needs_live_verification": True},
    {"id": "failure-mark", "selector": "[data-send-status='failed'], [data-send-status='rejected'], [aria-label='发送失败'], [aria-label='重新发送'], [aria-label='重发'], [title='发送失败'], [title='重新发送'], [title='重发']",
     "purpose": "失败红标或重发入口；命中后只记失败，不自动重发", "needs_live_verification": True},
    {"id": "streak-container", "selector": ".commonStreakstreakContainer",
     "purpose": "会话内火花容器；列表同步已使用同类名，打开的会话顶部仍需实页确认", "needs_live_verification": True},
    {"id": "streak-text", "selector": ".commonStreaknormalText",
     "purpose": "火花天数，或「X 天后消失」", "needs_live_verification": True},
    {"id": "streak-icon", "selector": "img.commonStreakicon",
     "purpose": "火花图标；灰色或未知图标不写成正常天数", "needs_live_verification": True},
    {"id": "streak-label", "selector": ".commonStreaklabelText",
     "purpose": "「X 天后消失」「重燃中」标签态", "needs_live_verification": True},
    {"id": "streak-badge-fallback", "selector": "[data-streak-badge]",
     "purpose": "无 commonStreak 容器时的天数回退", "needs_live_verification": True},
)


def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u00a0", " ").replace("\u200b", "")).strip()


def text_matches(actual, expected) -> bool:
    left, right = _norm(actual), _norm(expected)
    return bool(left) and left == right


def expiry_days(label) -> int | None:
    matched = re.fullmatch(r"([0-9]+)\s*天后消失", _norm(label))
    if not matched:
        return None
    return int(matched.group(1))


def expiring_label(label, *, within: int = EXPIRING_WITHIN_DAYS) -> bool:
    days = expiry_days(label)
    return days is not None and days <= within


def interpret_streak_badge(badge) -> dict | None:
    """把一次火花读数变成 observe_streak 能接收的观察；不确定时返回 None，避免冲掉缓存。"""
    if not isinstance(badge, dict):
        return None
    text = _norm(badge.get("text"))
    if not text or len(text) > 32:
        return None
    if re.fullmatch(r"(?:重燃中\s*[0-9]+\s*/\s*[0-9]+|[0-9]+\s*天后消失)", text):
        return {"streak": None, "streak_status": "unknown", "streak_source": None, "streak_label": text}
    value = parse_streak(text)
    if value is None:
        return None
    icon = str(badge.get("icon") or "")
    icon_path = icon.split("/flame_icon/", 1)[1] if "/flame_icon/" in icon else icon
    normal = re.fullmatch(r"[a-z_]+/normal_[a-z_]+\.png(?:[?#].*)?", icon_path)
    if badge.get("label") is True or (icon_path and not normal):
        return None
    return {
        "streak": value,
        "streak_status": "confirmed",
        "streak_source": "emoji" if "🔥" in text else "class",
        "streak_label": None,
    }


def _message_matches(message: dict, expected: str) -> bool:
    if text_matches(message.get("text"), expected):
        return True
    texts = message.get("texts")
    return isinstance(texts, list) and any(text_matches(item, expected) for item in texts)


def _clock_is_today(text: str) -> bool:
    return re.fullmatch(r"(?:今天|今日|刚刚)", text) is not None or re.fullmatch(
        r"(?:今天|今日)?\s*(?:[01]?\d|2[0-3])[:：][0-5]\d(?::[0-5]\d)?", text) is not None


def _text_day(text: str, today: date) -> str | None:
    if not text:
        return None
    if any(token in text for token in ("昨天", "前天", "前日")):
        return "other"
    if text in {"刚刚", "今天", "今日"} or text.startswith("今天") or text.startswith("今日"):
        return "today"
    month_day = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if month_day:
        try:
            found = date(today.year, int(month_day.group(1)), int(month_day.group(2)))
        except ValueError:
            return "unknown"
        return "today" if found == today else "other"
    full = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if full:
        try:
            found = date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
        except ValueError:
            return "unknown"
        return "today" if found == today else "other"
    if _clock_is_today(text):
        return "today"
    return None


def message_day(message: dict, today: date) -> str:
    """返回 today / other / unknown。日期来源互相矛盾时保持 unknown。"""
    verdicts = set()
    iso_day = timestamp_date(message.get("time_iso"))
    if iso_day is not None:
        verdicts.add("today" if iso_day == today else "other")
    text_day = _text_day(_norm(message.get("time_text")), today)
    if text_day:
        verdicts.add(text_day)
    if "unknown" in verdicts or {"today", "other"} <= verdicts:
        return "unknown"
    if "today" in verdicts:
        return "today"
    if "other" in verdicts:
        return "other"
    return "unknown"


def _own_messages(raw: dict) -> list[dict]:
    messages = raw.get("messages")
    if not isinstance(messages, list):
        return []
    explicit = [item for item in messages if isinstance(item, dict) and item.get("explicit") is True]
    if explicit:
        return explicit
    return [item for item in messages if isinstance(item, dict) and item.get("right_side") is True]


def classify_chat_evidence(raw, expected_text: str, today: date, friend_name: str = "") -> dict:
    """confirmed / failed / pending_review。没有任何返回值表示可以重发。"""
    streak = interpret_streak_badge(raw.get("badge")) if isinstance(raw, dict) else None
    result = {"status": "pending_review", "reason": "unreadable", "message_time": "", "message_text": "", "streak": streak}
    if not isinstance(raw, dict):
        return result
    if raw.get("opened") is False:
        result["reason"] = "not_opened"
        return result
    title = _norm(raw.get("title"))
    expected_name = _norm(friend_name)
    if raw.get("title_matches") is not True or not title or (expected_name and title != expected_name):
        result["reason"] = "scope_mismatch"
        return result
    own = _own_messages(raw)
    if not own:
        result["reason"] = "no_own_message"
        return result
    latest = own[-1]
    result["message_text"] = _norm(latest.get("text"))[:200]
    result["message_time"] = _norm(latest.get("time_text") or latest.get("time_iso"))[:80]
    matched = _message_matches(latest, expected_text)
    if latest.get("failed") is True:
        result["reason"] = "failure_mark" if matched else "failure_mark_unmatched"
        if matched:
            result["status"] = "failed"
        return result
    if not matched:
        result["reason"] = "text_mismatch"
        return result
    day = message_day(latest, today)
    if day == "today":
        result["status"] = "confirmed"
        result["reason"] = "matched_today"
        return result
    result["reason"] = "time_not_today" if day == "other" else "time_unknown"
    return result


READ_CHAT_EVIDENCE_JS = r"""({target}) => {
  const visible = el => !!(el && el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility)
    && getComputedStyle(el).display !== 'none');
  const norm = text => (text || '').replace(/\u00a0/g, ' ').replace(/\u200b/g, '').trim();
  const OWN = "[data-is-self='true'], [data-sender='self'], [data-direction='outgoing'], [class*='isFromMe']";
  const FAILURE = "[data-send-status='failed'], [data-send-status='rejected'], [aria-label='发送失败'], [aria-label='重新发送'], [aria-label='重发'], [title='发送失败'], [title='重新发送'], [title='重发']";
  const active = document.querySelector("[data-conversation-active='true']");
  const activeTitle = active ? Array.from(active.querySelectorAll('[data-conversation-title]')).filter(visible) : [];
  const titles = activeTitle.length ? activeTitle : Array.from(document.querySelectorAll('[data-conversation-title]')).filter(visible);
  let title = titles.length === 1 ? norm(titles[0].textContent) : '';
  if (!title) {
    const headers = Array.from(document.querySelectorAll('header, [data-conversation-header]')).filter(visible);
    const hit = headers.filter(el => {
      const box = el.getBoundingClientRect();
      return box.x > 240 && box.y < 160 && norm(el.textContent) === target;
    });
    if (hit.length === 1) title = target;
  }
  const rowTitle = document.querySelector('.conversationConversationItemcurConversation .conversationConversationItemtitle');
  const rowName = rowTitle && visible(rowTitle) ? norm(rowTitle.textContent) : '';
  if (rowName && rowName !== target) title = '';
  if (!title && rowName === target) title = target;
  const titleMatches = !!target && title === target;
  const list = (active && active.querySelector('[data-message-list]')) || document.querySelector('[data-message-list]') || active;
  const failed = el => {
    const status = el.getAttribute('data-send-status') || '';
    if (status === 'failed' || status === 'rejected') return true;
    const label = norm(el.getAttribute('aria-label') || el.getAttribute('title') || '');
    if (/^(?:发送失败|消息发送失败|重新发送|点击重发|重发)$/.test(label)) return true;
    return Array.from(el.querySelectorAll(FAILURE)).some(visible);
  };
  const textsOf = el => {
    const values = [];
    const push = value => {
      const text = norm(value);
      if (text && text.length <= 200 && !values.includes(text)) values.push(text);
    };
    push(el.getAttribute('aria-label'));
    const marked = el.querySelector('[data-message-text]');
    if (marked) push(marked.textContent);
    for (const image of el.querySelectorAll('img[alt]')) push(image.getAttribute('alt'));
    push(el.textContent);
    return values;
  };
  const timeOf = el => {
    const node = el.querySelector('time, [data-message-time-text]');
    return {
      time_iso: el.getAttribute('data-message-time') || (node && node.getAttribute('datetime')) || '',
      time_text: norm((node && (node.getAttribute('data-message-time-text') || node.textContent)) || el.getAttribute('data-time-text') || ''),
    };
  };
  let messages = [];
  if (list && titleMatches) {
    const explicit = Array.from(list.querySelectorAll(OWN)).filter(el => visible(el) && (!active || el.closest("[data-conversation-active='true']") === active || !el.closest("[data-conversation-active='true']")));
    const innermost = explicit.filter(el => !explicit.some(other => other !== el && el.contains(other)));
    const useRight = innermost.length === 0;
    const candidates = useRight ? Array.from(list.querySelectorAll('[data-message-id], [data-message-text]')).map(el => el.closest('[data-message-id]') || el).filter(visible) : innermost;
    const unique = [];
    for (const el of candidates) if (!unique.includes(el)) unique.push(el);
    const bounds = list.getBoundingClientRect();
    messages = unique.filter(el => {
      if (!useRight) return true;
      const box = el.getBoundingClientRect();
      return box.width > 1 && bounds.width > 40 && (box.left + box.right) / 2 > bounds.left + bounds.width * 0.62;
    }).map(el => {
      const texts = textsOf(el);
      return {explicit: !useRight, right_side: useRight, text: texts[0] || '', texts, failed: failed(el), ...timeOf(el)};
    });
  }
  function readBadge(scope) {
    if (!scope) return null;
    const boxes = Array.from(scope.querySelectorAll('.commonStreakstreakContainer')).filter(el => visible(el) && scope.contains(el));
    if (boxes.length > 1) return null;
    if (boxes.length === 1) {
      const box = boxes[0];
      const texts = Array.from(box.querySelectorAll('.commonStreaknormalText')).filter(visible);
      const icons = Array.from(box.querySelectorAll('img.commonStreakicon')).filter(visible);
      if (texts.length !== 1) return null;
      return {text: norm(texts[0].textContent), icon: icons[0] ? (icons[0].getAttribute('src') || '') : '', label: texts[0].classList.contains('commonStreaklabelText')};
    }
    const badges = Array.from(scope.querySelectorAll('[data-streak-badge]')).filter(el => visible(el) && scope.contains(el));
    if (badges.length === 1) return {text: norm(badges[0].textContent), icon: '', label: false};
    return null;
  }
  const row = document.querySelector('.conversationConversationItemcurConversation');
  const badge = readBadge(active) || readBadge(row);
  return {title, title_matches: titleMatches, messages, badge};
}"""


def verify_recent_outgoing(page, friend_name: str, expected_text: str, today: date, *, locate, sleep, delay: float | None = None) -> dict:
    """重新打开会话并只读最近一条己方消息。locate 只能点击已有会话，不能发送。"""
    wait = POST_SEND_VERIFY_SECONDS if delay is None else max(0.0, float(delay))
    if wait:
        sleep(wait)
    opened = False
    try:
        opened = bool(locate(page, friend_name))
    except Exception:
        raise
    if not opened:
        return classify_chat_evidence({"opened": False}, expected_text, today, friend_name)
    try:
        raw = page.evaluate(READ_CHAT_EVIDENCE_JS, {"target": _norm(friend_name)})
    except Exception as error:
        from .browser_errors import raise_if_browser_unavailable
        raise_if_browser_unavailable(error)
        return classify_chat_evidence({"opened": True, "title_matches": False}, expected_text, today, friend_name)
    if isinstance(raw, dict):
        raw = {**raw, "opened": True}
    return classify_chat_evidence(raw, expected_text, today, friend_name)
