"""Playwright session for the assistant account only.

Cookie file: data/assistant_state.json
Persistent profile: data/assistant-browser-profile
QR capture profile (separate): data/assistant-session/qr-login-profile

This module never reads data/state.json, never calls the streak sender, and
never launches the main account browser. Selector names below match the
classes this project already uses for www.douyin.com/chat. They are
assumptions about the live site: data-message-id, data-user-id and share-card
links are read when present, and a missing id falls back to a content hash.
A conversation that is not an exact, unique title match is not opened and is
not sent a reply.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager

from assistant_inbox import profile_dir, redact, state_path
from core.browser import _COMMON_ARGS
from core.config import atomic_write_bytes
from state_validation import validate_storage_state

logger = logging.getLogger("douyin-cloud-streak")

CHAT_URL = "https://www.douyin.com/chat"
TITLE_SELECTOR = ".conversationConversationItemtitle, [data-contact-title]"
ROW_SELECTOR = ".conversationConversationItemwrapper, [data-conversation-row]"
ACTIVE_TITLE_SELECTOR = ".conversationConversationItemcurConversation .conversationConversationItemtitle"
INPUT_SELECTOR = 'div[contenteditable="true"]'
MAX_CONVERSATIONS = 8
MAX_SCROLLS = 6

profile_lock = threading.Lock()

# List rows. Class names are the same ones the streak contact reader uses.
# data-user-id / data-sec-uid / data-conversation-id are optional; the live
# site may not expose them, in which case only the visible nickname matches.
_LIST_JS = r"""() => {
  const titleSel = '.conversationConversationItemtitle, [data-contact-title]';
  const rowSel = '.conversationConversationItemwrapper, [data-conversation-row], [role="listitem"], li';
  const norm = text => (text || '').replace(/\u00a0/g, ' ').trim();
  const visible = el => !!(el && el.isConnected && el.getClientRects().length);
  const rows = [];
  const seen = new Set();
  for (const title of document.querySelectorAll(titleSel)) {
    if (!visible(title)) continue;
    const name = norm(title.textContent);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const row = title.closest(rowSel) || title.parentElement;
    const attr = node => {
      if (!node) return '';
      return node.getAttribute('data-user-id')
        || node.getAttribute('data-sec-uid')
        || node.getAttribute('data-uid')
        || node.getAttribute('data-conversation-id')
        || '';
    };
    rows.push({
      title: name.slice(0, 80),
      sender_id: norm(attr(row) || attr(title)).slice(0, 80),
    });
    if (rows.length >= 80) break;
  }
  return rows;
}"""

# Messages in the open conversation. Unknown direction is dropped by Python.
_MESSAGES_JS = r"""() => {
  const norm = text => (text || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el && el.isConnected && el.getClientRects().length);
  const header = document.querySelector('[data-conversation-title]')
    || document.querySelector('.conversationConversationItemcurConversation .conversationConversationItemtitle');
  const conversation = norm(header ? header.textContent : '').slice(0, 80);
  const scope = document.querySelector('[data-conversation-active="true"]')
    || document.querySelector('[data-message-list]')
    || document.body;
  const nodes = scope.querySelectorAll(
    '[data-message-id], [data-e2e="msg-item"], [data-message-text], .message-item, .msg-item'
  );
  const out = [];
  const seen = new Set();
  for (const node of nodes) {
    if (!visible(node) || seen.has(node)) continue;
    const host = node.closest('[data-message-id], [data-e2e="msg-item"], .message-item, .msg-item') || node;
    if (seen.has(host)) continue;
    seen.add(host);
    const flag = [
      host.getAttribute('data-is-self') || '',
      host.getAttribute('data-sender') || '',
      host.getAttribute('data-direction') || '',
      host.className || '',
    ].join(' ');
    let direction = 'unknown';
    if (/true|self|outgoing|fromMe|isMe|message-right|msg-self/i.test(flag)) direction = 'out';
    else if (/false|incoming|fromOther|message-left|msg-other/i.test(flag)) direction = 'in';
    const textNode = host.querySelector('[data-message-text], [data-testid="message-text"]');
    const link = host.querySelector('a[href*="/video/"], a[href*="v.douyin.com"]');
    const titleNode = host.querySelector('[data-share-title], [data-video-title], .share-title, .video-title');
    const text = norm(textNode ? textNode.textContent : (link ? '' : host.textContent)).slice(0, 2000);
    const shareUrl = link && link.href ? String(link.href).slice(0, 800) : (host.getAttribute('data-share-url') || '');
    const shareTitle = norm(titleNode ? titleNode.textContent : '').slice(0, 200);
    const messageId = host.getAttribute('data-message-id') || host.getAttribute('data-id') || '';
    const messageTime = host.getAttribute('data-time') || host.getAttribute('datetime') || '';
    const senderId = host.getAttribute('data-sender-id') || host.getAttribute('data-user-id') || '';
    out.push({
      id: messageId.slice(0, 200),
      direction,
      text,
      share_title: shareTitle,
      share_url: shareUrl,
      message_time: String(messageTime).slice(0, 40),
      sender_id: norm(senderId).slice(0, 80),
      sender_name: conversation,
      conversation_name: conversation,
    });
    if (out.length >= 40) break;
  }
  return {conversation, messages: out};
}"""


class AssistantBrowserError(RuntimeError):
    pass


def _assert_isolated(path) -> None:
    if path.name in {"state.json", "qr-login-profile"}:
        raise AssistantBrowserError("refusing the main account session path")


@contextmanager
def _page():
    """Open the assistant persistent profile. Caller must hold profile_lock."""
    from playwright.sync_api import sync_playwright

    profile = profile_dir()
    state = state_path()
    _assert_isolated(profile)
    _assert_isolated(state)
    profile.mkdir(parents=True, exist_ok=True)
    driver = sync_playwright().start()
    context = None
    try:
        context = driver.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            args=list(_COMMON_ARGS),
            viewport={"width": 1366, "height": 768},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            ignore_https_errors=False,
        )
        _install_cookies(context, state)
        page = context.pages[0] if context.pages else context.new_page()
        yield page, context
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            driver.stop()


def _install_cookies(context, state) -> None:
    if not state.exists():
        return
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("小号登录态文件无法读取，将只使用独立浏览器目录里的会话")
        return
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return
    safe = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if "douyin.com" not in domain and "bytedance.com" not in domain:
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or not value:
            continue
        item = {"name": name, "value": value, "domain": domain, "path": str(cookie.get("path") or "/")}
        expires = cookie.get("expires", -1)
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            item["expires"] = float(expires)
        if cookie.get("httpOnly") is True:
            item["httpOnly"] = True
        if cookie.get("secure") is True:
            item["secure"] = True
        if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
            item["sameSite"] = cookie["sameSite"]
        safe.append(item)
    if safe:
        context.add_cookies(safe)


def _save_state(context, state) -> None:
    raw = context.storage_state()
    encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    validate_storage_state(encoded)
    atomic_write_bytes(state, encoded)


def _open_chat(page) -> None:
    last_error = "无法打开抖音私信页"
    for attempt in range(2):
        try:
            page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
            return
        except Exception as exc:
            last_error = redact(exc)
            logger.info("小号私信页打开失败（第 %s 次）：%s", attempt + 1, last_error)
    raise AssistantBrowserError(last_error)


def _login_ok(page) -> tuple[bool, str]:
    from core.automation import check_login
    return check_login(page)


def _scroll(page, to_top: bool) -> bool:
    script = r"""(toTop) => {
      const item = document.querySelector('.conversationConversationItemwrapper, [data-conversation-row]');
      if (!item) return false;
      let node = item;
      while (node) {
        const style = getComputedStyle(node);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && node.scrollHeight > node.clientHeight + 20) {
          const before = node.scrollTop;
          node.scrollTop = toTop ? 0 : before + node.clientHeight * 0.85;
          return node.scrollTop !== before || toTop;
        }
        node = node.parentElement;
      }
      return false;
    }"""
    try:
        return bool(page.evaluate(script, to_top))
    except Exception as exc:
        logger.info("小号会话列表滚动失败：%s", redact(exc))
        return False


def _listed_rows(page) -> list[dict]:
    try:
        rows = page.evaluate(_LIST_JS)
    except Exception as exc:
        logger.info("小号会话列表读取失败：%s", redact(exc))
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and str(row.get("title") or "").strip()]


def _matches(row: dict, allowed: set[str]) -> bool:
    title = str(row.get("title") or "").strip()
    sender_id = str(row.get("sender_id") or "").strip()
    return title in allowed or (sender_id and sender_id in allowed)


def _collect_targets(page, allowed: list[str]) -> list[dict]:
    wanted = set(allowed)
    found: dict[str, dict] = {}
    _scroll(page, True)
    stalled = 0
    for _ in range(MAX_SCROLLS):
        for row in _listed_rows(page):
            title = str(row.get("title") or "").strip()
            if title and _matches(row, wanted) and title not in found:
                found[title] = row
            if len(found) >= MAX_CONVERSATIONS:
                return list(found.values())
        if not _scroll(page, False):
            stalled += 1
            if stalled >= 2:
                break
        else:
            stalled = 0
    return list(found.values())


def _open_exact(page, title: str) -> bool:
    from core.automation import _click_and_verify, _find_contact

    target = _find_contact(page, title)
    if target is None:
        logger.info("小号会话无法唯一定位，已跳过")
        return False
    try:
        return bool(_click_and_verify(page, target, title))
    except Exception as exc:
        logger.info("小号会话切换失败：%s", redact(exc))
        return False


def _read_open_thread(page) -> list[dict]:
    try:
        payload = page.evaluate(_MESSAGES_JS)
    except Exception as exc:
        logger.info("小号消息读取失败：%s", redact(exc))
        return []
    if not isinstance(payload, dict):
        return []
    messages = payload.get("messages")
    return [item for item in messages if isinstance(item, dict)] if isinstance(messages, list) else []


def _send_in_open_thread(page, title: str, text: str) -> dict:
    from core.automation import _verify_in_conversation, _verify_item_active

    try:
        if not (_verify_item_active(page, title) and _verify_in_conversation(page, title)):
            return {"ok": False, "permanent": True, "reason": "当前会话不是目标好友，已取消发送"}
        box = page.locator(INPUT_SELECTOR)
        count = box.count()
        visible = [box.nth(index) for index in range(min(count, 5)) if box.nth(index).is_visible()]
        if len(visible) != 1:
            return {"ok": False, "permanent": False, "reason": "没有唯一切入点，已取消发送"}
        field = visible[0]
        field.click(timeout=5000)
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.type(text, delay=30)
        page.keyboard.press("Enter")
        return {"ok": True}
    except Exception as exc:
        logger.info("小号回复输入失败：%s", redact(exc))
        return {"ok": False, "permanent": False, "reason": "回复未能输入，下轮再试"}


def run_browser_cycle(allowed: list[str], pending: dict | None) -> dict:
    """Read allowlisted threads and optionally send one reply in the same session."""
    if not profile_lock.acquire(blocking=False):
        return {"session_ok": None, "reason": "小号浏览器正忙", "messages": [], "send": None}
    try:
        with _page() as (page, context):
            _open_chat(page)
            try:
                page.wait_for_selector(".conversationConversationItemtitle, [data-contact-title]", timeout=20000)
            except Exception:
                logger.info("小号会话列表在 20 秒内未出现，继续检查登录态")
            logged_in, why = _login_ok(page)
            if not logged_in:
                return {"session_ok": False, "reason": why, "messages": [], "send": None}
            messages: list[dict] = []
            targets = _collect_targets(page, allowed)
            opened_titles = []
            for row in targets:
                title = str(row.get("title") or "").strip()
                if not title or not _open_exact(page, title):
                    continue
                opened_titles.append(title)
                sender_id = str(row.get("sender_id") or "").strip()
                for item in _read_open_thread(page):
                    if not item.get("conversation_name"):
                        item["conversation_name"] = title
                    if not item.get("sender_name"):
                        item["sender_name"] = title
                    if sender_id and not item.get("sender_id"):
                        item["sender_id"] = sender_id
                    messages.append(item)
            send = None
            if pending:
                send = _send_pending(page, allowed, pending, opened_titles)
            try:
                _save_state(context, state_path())
            except Exception as exc:
                logger.warning("小号登录态回写失败：%s", redact(exc))
            return {"session_ok": True, "reason": "", "messages": messages, "send": send}
    except AssistantBrowserError as exc:
        return {"session_ok": None, "reason": redact(exc), "messages": [], "send": None}
    except Exception as exc:
        logger.warning("小号浏览器异常：%s", redact(exc))
        return {"session_ok": None, "reason": redact(exc), "messages": [], "send": None}
    finally:
        profile_lock.release()


def _send_pending(page, allowed: list[str], pending: dict, opened_titles: list[str]) -> dict:
    recipient = str(pending.get("to") or "").strip()
    text = str(pending.get("text") or "")
    if recipient not in set(allowed):
        return {"ok": False, "permanent": True, "reason": "收件人不在白名单，已取消发送"}
    title = recipient if recipient in opened_titles else ""
    if not title:
        rows = _collect_targets(page, [recipient])
        exact = [row for row in rows if str(row.get("title") or "").strip() == recipient or str(row.get("sender_id") or "").strip() == recipient]
        if len(exact) != 1:
            return {"ok": False, "permanent": True, "reason": "找不到唯一的白名单会话，已取消发送"}
        title = str(exact[0].get("title") or "").strip()
    if not title or not _open_exact(page, title):
        return {"ok": False, "permanent": True, "reason": "无法确认目标会话，已取消发送"}
    return _send_in_open_thread(page, title, text)
