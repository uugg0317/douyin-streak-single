"""发送后只读会话证据。本模块不输入文字、不按发送键、不重发。

2026-11-08 在真实抖音私信页核对过一轮。主选择器用当时命中的类名；
旧的 data-* 选择器只作回退。失败红标当天没有出现，仍待实页证实。

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

def _selector(selector_id, selector, purpose, *, role, needs_live_verification, live_note):
    return {
        "id": selector_id,
        "selector": selector,
        "purpose": purpose,
        "role": role,
        "needs_live_verification": needs_live_verification,
        "live_note": live_note,
    }


_VERIFIED = "2026-11-08 真实私信页 11/11 命中"
_ABSENT = "2026-11-08 真实私信页不存在，只作回退"
_UNPROVEN = "2026-11-08 当天没有失败消息，选择器尚未证实"

LIVE_DOM_SELECTORS = (
    _selector("active-row-title", ".conversationConversationItemcurConversation .conversationConversationItemtitle",
              "左侧当前会话标题", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("right-header-title", ".RightPanelHeadertitle",
              "右侧会话标题；与左侧不一致时不确认", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("message-list", ".messageMessageListlist",
              "当前消息列表", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("message-list-wrapper", ".messageMessageListwrapper",
              "消息列表外层；列表类名缺失时回退到这里", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("own-message-box", ".messageMessageBoxisFromMe",
              "己方气泡最外层，时间和正文都在里面", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("own-isFromMe", "[class*='isFromMe']",
              "己方标记；没有最外层气泡类名时，取最内层", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("message-plain-text", ".TextMessageTextpureText",
              "纯文本正文", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("message-emoji-title", ".TextMessageTextemoji img[title]",
              "表情文案，例如 [续火花吧]；读 title，不读空的 alt", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("message-time", ".MessageBoxTimetimeLayout",
              "消息时间。00:00、11:04 按当天；昨天、前天按其他日期", role="primary", needs_live_verification=False, live_note=_VERIFIED + "；裸 HH:MM 按当天已在实页对照"),
    _selector("streak-container", ".commonStreakstreakContainer",
              "会话标题旁的火花容器", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("streak-text", ".commonStreaknormalText",
              "火花天数，或「X 天后消失」", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("streak-icon", "img.commonStreakicon",
              "彩色 normal_* 为普通读数；gray_* 仍记下数字并标灰色", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("streak-label", ".commonStreaklabelText",
              "「X 天后消失」「重燃中」", role="primary", needs_live_verification=False, live_note=_VERIFIED),
    _selector("failure-mark", "[data-send-status='failed'], [data-send-status='rejected'], [aria-label='发送失败'], [aria-label='重新发送'], [aria-label='重发'], [title='发送失败'], [title='重新发送'], [title='重发']",
              "失败红标或重发入口；命中后只记失败，不自动重发", role="primary", needs_live_verification=True, live_note=_UNPROVEN),
    _selector("right-side-bubble", "气泡中心位于消息区域宽度 62% 以右",
              "没有任何己方标记时的几何回退", role="fallback", needs_live_verification=True, live_note="2026-11-08 实页已有 isFromMe，这条几何回退没有走到"),
    _selector("conversation-active", "[data-conversation-active='true']",
              "旧的当前会话容器", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("conversation-title", "[data-conversation-title]",
              "旧的会话标题属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("structural-header", "header, [data-conversation-header]",
              "旧的右侧标题回退", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("legacy-message-list", "[data-message-list]",
              "旧的消息列表属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("own-data-is-self", "[data-is-self='true']",
              "旧的己方属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("own-data-sender", "[data-sender='self']",
              "旧的发送者属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("own-data-direction", "[data-direction='outgoing']",
              "旧的方向属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("legacy-message-text", "[data-message-text], img[alt], [aria-label]",
              "旧的正文属性；表情 alt 在实页是空的", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("legacy-message-time", "time[datetime], [data-message-time], [data-message-time-text]",
              "旧的时间属性", role="fallback", needs_live_verification=False, live_note=_ABSENT),
    _selector("streak-badge-fallback", "[data-streak-badge]",
              "无 commonStreak 容器时的天数回退", role="fallback", needs_live_verification=False, live_note=_ABSENT),
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
    gray = re.fullmatch(r"(?:[a-z_]+/)?gray_[a-z_]+\.png(?:[?#].*)?", icon_path)
    if icon_path and not normal and not gray:
        return None
    return {
        "streak": value,
        "streak_status": "confirmed",
        "streak_source": "emoji" if "🔥" in text else "class",
        "streak_label": None,
        "streak_at_risk": bool(gray),
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


def _describe_message(result: dict, message: dict) -> None:
    result["message_text"] = _norm(message.get("text"))[:200]
    result["message_time"] = _norm(message.get("time_text") or message.get("time_iso"))[:80]


def classify_chat_evidence(raw, expected_text: str, today: date, friend_name: str = "") -> dict:
    """confirmed / failed / pending_review。在当天全部己方消息里找目标文案，不因为后面又发了别的而判失败。没有任何返回值表示可以重发。"""
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
    matches = [item for item in own if _message_matches(item, expected_text)]
    today_matches = [item for item in matches if message_day(item, today) == "today"]
    if today_matches:
        failed = [item for item in today_matches if item.get("failed") is True]
        chosen = failed[-1] if failed else today_matches[-1]
        _describe_message(result, chosen)
        if failed:
            result["status"] = "failed"
            result["reason"] = "failure_mark"
            return result
        result["status"] = "confirmed"
        result["reason"] = "matched_today"
        return result
    if matches:
        chosen = matches[-1]
        _describe_message(result, chosen)
        day = message_day(chosen, today)
        result["reason"] = "time_not_today" if day == "other" else "time_unknown"
        return result
    latest = own[-1]
    _describe_message(result, latest)
    if latest.get("failed") is True:
        result["reason"] = "failure_mark_unmatched"
        return result
    result["reason"] = "text_mismatch"
    return result


READ_CHAT_EVIDENCE_JS = r"""({target}) => {
  const visible = el => !!(el && el.isConnected && el.getClientRects().length
    && !['hidden', 'collapse'].includes(getComputedStyle(el).visibility)
    && getComputedStyle(el).display !== 'none');
  const norm = text => (text || '').replace(/\u00a0/g, ' ').replace(/\u200b/g, '').trim();
  const OWN = "[data-is-self='true'], [data-sender='self'], [data-direction='outgoing'], [class*='isFromMe']";
  const FAILURE = "[data-send-status='failed'], [data-send-status='rejected'], [aria-label='发送失败'], [aria-label='重新发送'], [aria-label='重发'], [title='发送失败'], [title='重新发送'], [title='重发']";
  const active = document.querySelector("[data-conversation-active='true']");
  const rowTitle = document.querySelector('.conversationConversationItemcurConversation .conversationConversationItemtitle');
  const headerTitle = document.querySelector('.RightPanelHeadertitle');
  const rowName = rowTitle && visible(rowTitle) ? norm(rowTitle.textContent) : '';
  const headerName = headerTitle && visible(headerTitle) ? norm(headerTitle.textContent) : '';
  let title = '';
  if (rowName && headerName) title = rowName === headerName ? rowName : '';
  else title = rowName || headerName;
  if (!title) {
    const titled = active
      ? Array.from(active.querySelectorAll('[data-conversation-title]')).filter(visible)
      : Array.from(document.querySelectorAll('[data-conversation-title]')).filter(visible);
    if (titled.length === 1) title = norm(titled[0].textContent);
  }
  if (!title) {
    const headers = Array.from(document.querySelectorAll('header, [data-conversation-header]')).filter(visible);
    const hit = headers.filter(el => {
      const box = el.getBoundingClientRect();
      return box.x > 240 && box.y < 160 && norm(el.textContent) === target;
    });
    if (hit.length === 1) title = target;
  }
  if ((rowName && rowName !== target) || (headerName && headerName !== target)) title = '';
  const titleMatches = !!target && title === target;
  const oneVisible = (selector, root) => {
    const found = Array.from((root || document).querySelectorAll(selector)).filter(visible);
    return found.length === 1 ? found[0] : null;
  };
  let list = null;
  if (active) {
    list = ['.messageMessageListlist', '[data-message-list]', '.messageMessageListwrapper']
      .map(selector => active.querySelector(selector)).find(el => visible(el)) || null;
  }
  if (!list) list = oneVisible('.messageMessageListlist') || oneVisible('[data-message-list]') || oneVisible('.messageMessageListwrapper');
  const failed = el => {
    const status = el.getAttribute('data-send-status') || '';
    if (status === 'failed' || status === 'rejected') return true;
    const label = norm(el.getAttribute('aria-label') || '');
    if (/^(?:发送失败|消息发送失败|重新发送|点击重发|重发)$/.test(label)) return true;
    return Array.from(el.querySelectorAll(FAILURE)).some(visible);
  };
  const textsOf = el => {
    const values = [];
    const push = value => {
      const text = norm(value);
      if (text && text.length <= 200 && !values.includes(text)) values.push(text);
    };
    for (const node of el.querySelectorAll('.TextMessageTextpureText')) push(node.textContent);
    for (const image of el.querySelectorAll('.TextMessageTextemoji img[title], img[title]')) push(image.getAttribute('title'));
    const marked = el.querySelector('[data-message-text]');
    if (marked) push(marked.textContent);
    for (const image of el.querySelectorAll('img[alt]')) push(image.getAttribute('alt'));
    if (!values.length) push(el.textContent);
    return values;
  };
  const timeOf = el => {
    const layout = el.querySelector('.MessageBoxTimetimeLayout');
    const node = layout || el.querySelector('time, [data-message-time-text]');
    return {
      time_iso: el.getAttribute('data-message-time') || (node && node.getAttribute('datetime')) || '',
      time_text: norm(layout ? layout.textContent : ((node && (node.getAttribute('data-message-time-text') || node.textContent)) || el.getAttribute('data-time-text') || '')),
    };
  };
  let messages = [];
  if (list && titleMatches) {
    const boxes = Array.from(list.querySelectorAll('.messageMessageBoxisFromMe')).filter(visible);
    let nodes = boxes;
    let explicit = boxes.length > 0;
    if (!nodes.length) {
      const marked = Array.from(list.querySelectorAll(OWN)).filter(visible);
      nodes = marked.filter(el => !marked.some(other => other !== el && el.contains(other)));
      explicit = nodes.length > 0;
    }
    const useRight = !explicit;
    const candidates = useRight ? Array.from(list.querySelectorAll('[data-message-id], [data-message-text]')).map(el => el.closest('[data-message-id]') || el).filter(visible) : nodes;
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
  const headerScope = document.querySelector('.RightPanelHeaderuser') || document.querySelector('.RightPanelHeadertitleContainer');
  const row = document.querySelector('.conversationConversationItemcurConversation');
  const badge = readBadge(headerScope) || readBadge(row) || readBadge(active);
  return {title, title_matches: titleMatches, messages, badge};
}"""


def verify_recent_outgoing(page, friend_name: str, expected_text: str, today: date, *, locate, sleep, delay: float | None = None) -> dict:
    """重新打开会话，在当天的己方消息里查找目标文案。locate 只能点击已有会话，不能发送。"""
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
