"""Read-only DOM contract. Synthetic markers do not establish real-site support."""
import json
import re
import time
from .contact_identity import avatar_key
from .streaks import parse_streak

# Reserved for U01 B. Merely changing this flag does not implement unreviewed
# absence inference; real fixtures and all relative-evidence checks are required.
NONE_BY_ABSENCE_ENABLED = False
EXPLICIT_NONE_SELECTOR = '[data-streak-region][data-streak-state="none"]'  # Synthetic fixture evidence only.

EXTRACT_JS = r"""() => {
    const TITLE = '.conversationConversationItemtitle, [data-contact-title]';
    const ROW = '.conversationConversationItemwrapper, [data-conversation-row], [role="listitem"], li';
    const BADGE = '.commonStreakstreakContainer, .commonStreak, [data-streak-badge]';
    const NONE_EVIDENCE_SELECTOR = __NONE_EVIDENCE_SELECTOR__;
    const EXCLUDED = '[data-message-preview], [data-unread-count], [data-unread], .unread-count, .message-preview';
    const visible = el => {
        if (!el.getClientRects().length) return false;
        for (let p=el;p;p=p.parentElement) {
            const style=getComputedStyle(p);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0 || p.getAttribute('aria-hidden') === 'true') return false;
        }
        return true;
    };
    const parse = text => {
        const match = (text || '').trim().match(/^(?:🔥\s*)?([1-9][0-9]{0,2})(?:\s*天)?$/u);
        return match ? Number(match[1]) : null;
    };
    function explicitNoStreak(row) {
        // Missing nodes are unknown. This selector only has synthetic support.
        return Array.from(row.querySelectorAll(NONE_EVIDENCE_SELECTOR)).some(visible);
    }
    function extractStreak(row) {
        const values = [];
        const labels = [];
        let uncertain = false;
        for (const badge of row.querySelectorAll(BADGE)) {
            if (!visible(badge) || badge.closest(EXCLUDED) || badge.closest(ROW) !== row) continue;
            let text = (badge.textContent || '').trim();
            if (badge.matches('.commonStreakstreakContainer')) {
                // 真实网页：图标与天数必须同属一个火花容器，不读取整行数字。
                const texts = Array.from(badge.querySelectorAll('.commonStreaknormalText'))
                    .filter(el => visible(el) && el.closest('.commonStreakstreakContainer') === badge);
                const icons = Array.from(badge.querySelectorAll('img.commonStreakicon'))
                    .filter(el => visible(el) && el.closest('.commonStreakstreakContainer') === badge);
                if (texts.length !== 1 || icons.length !== 1) { uncertain = true; continue; }
                text = (texts[0].textContent || '').trim();
                const iconPath = (icons[0].getAttribute('src') || '').split('/flame_icon/')[1] || '';
                if (/^重燃中\s*[0-9]+\s*\/\s*[0-9]+$/.test(text) || /^[0-9]+\s*天后消失$/.test(text)) {
                    labels.push(text); continue;
                }
                // 灰色、标签态或未知图标不能仅凭一个数字确认为正常火花。
                if (!/^[a-z_]+\/normal_[a-z_]+\.png(?:[?#].*)?$/.test(iconPath)
                    || texts[0].classList.contains('commonStreaklabelText')) { uncertain = true; continue; }
            }
            const value = parse(text);
            if (value !== null) values.push({streak:value, streak_source:text.includes('🔥') ? 'emoji':'class'});
        }
        const none = explicitNoStreak(row);
        if (labels.length) return {streak:null, streak_status:values.length || none ? 'suspect':'unknown',
            streak_source:null, streak_label:labels.length === 1 ? labels[0]:null};
        if (uncertain) return {streak:null, streak_status:'unknown', streak_source:null};
        if (values.length && (none || new Set(values.map(x => x.streak)).size !== 1))
            return {streak:null, streak_status:'suspect', streak_source:null};
        if (values.length) return {...values[0], streak_status:'confirmed'};
        if (none) return {streak:0, streak_status:'none', streak_source:'class', streak_none_evidence:true};
        return {streak:null, streak_status:'unknown', streak_source:null};
    }
    // 只规范化已确认的抖音头像资源路径，不把 CDN 节点/尺寸算作新好友。
    function avatarIdentity(value) {
        try {
            const url = new URL(value);
            const trusted = /^(?:p[0-9]+(?:-pc)?\.douyinpic\.com|p[0-9]+\.huoshanimg\.com)$/.test(url.hostname);
            const resource = url.pathname.match(/^(\/img\/aweme-avatar\/[A-Za-z0-9_/-]+(?:\.(?:png|jpe?g|webp))?)~[A-Za-z0-9_.-]+$/);
            if (url.protocol === 'https:' && trusted && resource && !url.username && !url.password) return 'douyin-avatar:' + resource[1];
            return value.split('#')[0];
        } catch (_) { return value; }
    }
    const counts = new Map();
    const out = [];
    for (const title of document.querySelectorAll(TITLE)) {
        if (!visible(title)) continue;
        const name = (title.textContent || '').trim();
        if (!name || name.length > 40 || ['消息','私信','朋友私信','通知'].includes(name)) continue;
        const row = title.closest(ROW);
        const valid = row && row.querySelectorAll(TITLE).length === 1;
        let read = {streak:null, streak_status:'unknown', streak_source:null};
        let avatar = '';
        if (valid) {
            read = extractStreak(row);
            const image = Array.from(row.querySelectorAll('img')).find(image => visible(image) && !image.closest(EXCLUDED) && !image.closest(BADGE) && !image.matches('img.commonStreakicon'));
            if (image) avatar = image.src || '';
        }
        const group = name.replace(/\u00a0/g,' ').trim() + '|' + avatarIdentity(avatar);
        const ordinal = (counts.get(group) || 0) + 1;
        counts.set(group, ordinal);
        // 数字临时昵称配内嵌占位图时先等待资料，不把临时行写成新好友。
        const profilePending = /^[0-9]{6,}$/.test(name) && avatar.startsWith('data:image/png');
        out.push({name, avatar, ...read, profile_pending:profilePending, row_key:group+'|'+ordinal});
    }
    return out;
}""".replace('__NONE_EVIDENCE_SELECTOR__',json.dumps(EXPLICIT_NONE_SELECTOR))

SCROLL_STATE_JS = r"""() => {
    // capture-state: a guessed viewport is never proof of list completeness.
    const item = document.querySelector('.conversationConversationItemwrapper, [data-conversation-row], [role="listitem"]');
    let list = item;
    while (list && !['auto','scroll'].includes(getComputedStyle(list).overflowY)) list = list.parentElement;
    list = list || document.querySelector('[role="list"]');
    const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const loading = Array.from(document.querySelectorAll('[aria-busy="true"], [role="progressbar"], [data-loading="true"]')).some(visible);
    const empty = Array.from(document.querySelectorAll('[data-contacts-empty]')).some(visible);
    return {known:!!list || empty, height:list ? list.scrollHeight:0,
            bottom:empty || !!list && list.scrollTop + list.clientHeight >= list.scrollHeight - 2, loading, empty};
}"""

SCROLL_NEXT_JS = r"""() => {
    const item = document.querySelector('.conversationConversationItemwrapper, [data-conversation-row], [role="listitem"]');
    let list = item;
    while (list && !['auto','scroll'].includes(getComputedStyle(list).overflowY)) list = list.parentElement;
    if (list) list.scrollTop += Math.max(100, Math.floor(list.clientHeight * 0.8));
}"""

READ_PAIR_INTERVAL_MS = 180
SCROLL_SETTLE_MS = 800
BOTTOM_SETTLE_MS = 2500  # 到底后给延迟分页/资料加载留出稳定窗口。
STABLE_ROUNDS = 2


def summarize_streaks(rows):
    """Partition this scan's rows; special badges are not missing day readings.

    These counts describe captured DOM evidence, not historical ledger values,
    unique platform accounts, or proof that sending is currently possible.
    """
    counts = dict.fromkeys(('normal', 'rekindling', 'expiring', 'none', 'unknown'), 0)
    for row in rows:
        kind = 'unknown'
        status, label = row.get('streak_status'), row.get('streak_label')
        if status == 'confirmed' and not label and row.get('streak_source') in {'class', 'emoji'} and (parse_streak(row.get('streak')) or 0) > 0:
            kind = 'normal'
        elif status == 'none' and not label and row.get('streak_none_evidence') is True and row.get('streak_source') in {'class', 'emoji'}:
            kind = 'none'
        elif status == 'unknown' and row.get('streak') is None and isinstance(label, str):
            if re.fullmatch(r'重燃中\s*[0-9]+\s*/\s*[0-9]+', label):
                kind = 'rekindling'
            elif re.fullmatch(r'[0-9]+\s*天后消失', label):
                kind = 'expiring'
        counts[kind] += 1
    return counts


def _indexed(rows):
    counts = {}
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get('name') or row.get('profile_pending'):
            continue
        group = (str(row['name']).replace('\u00a0',' ').strip(), avatar_key(row.get('avatar')))
        counts[group] = counts.get(group, 0) + 1
        key = str(row.get('row_key') or repr((*group,counts[group])))
        indexed[key] = dict(row)
    return indexed


def scan_contacts(page, collected, *, extract_js=EXTRACT_JS, max_rounds=20, progress=lambda:None, clock=None):
    """Measure this scan without shortening any DOM or pagination settling wait.

    read includes extraction and scroll-state queries; stabilize includes all
    existing waits; scroll measures only the scroll command. Total also includes
    Python merging, callbacks and summary work, so buckets need not sum to total.
    """
    now = time.perf_counter if clock is None else clock
    started = now()
    elapsed = dict.fromkeys(('read', 'stabilize', 'scroll'), 0.0)

    def measured(bucket, operation, *args):
        before = now()
        try:
            return operation(*args)
        finally:
            elapsed[bucket] += max(0.0, now() - before)

    merged = _indexed(collected)
    stable, previous_height, complete = 0, None, False
    reason, rounds = 'round_limit', 0
    profiles_unresolved = False
    for rounds in range(1, max_rounds + 1):
        first_rows = measured('read', page.evaluate, extract_js) or []
        measured('stabilize', page.wait_for_timeout, READ_PAIR_INTERVAL_MS)
        second_rows = measured('read', page.evaluate, extract_js) or []
        for _ in range(6):
            if not any(row.get('profile_pending') for row in first_rows + second_rows if isinstance(row,dict)):
                break
            measured('stabilize', page.wait_for_timeout, 500)
            first_rows = measured('read', page.evaluate, extract_js) or []
            measured('stabilize', page.wait_for_timeout, READ_PAIR_INTERVAL_MS)
            second_rows = measured('read', page.evaluate, extract_js) or []
        pending = any(row.get('profile_pending') for row in first_rows + second_rows if isinstance(row,dict))
        profiles_unresolved = profiles_unresolved or pending
        first, second = _indexed(first_rows), _indexed(second_rows)
        fresh = bool((first.keys() | second.keys()) - merged.keys())
        consistent = first.keys() == second.keys() and not pending
        for key in dict.fromkeys([*first, *second]):
            value = dict(second.get(key) or first[key])
            a, b = first.get(key), second.get(key)
            fields = ('name','avatar','streak','streak_status','streak_source','streak_none_evidence','streak_label')
            if a is None or b is None or any((avatar_key(a.get(field)) != avatar_key(b.get(field)) if field == 'avatar' else a.get(field) != b.get(field)) for field in fields):
                consistent = False
                value.update(streak=None, streak_status='unknown', streak_source=None, streak_none_evidence=False, streak_label=None)
            merged[key] = value
        if fresh:
            progress()
        state = measured('read', page.evaluate, SCROLL_STATE_JS) or {}
        settled = (state.get('known') is True and state.get('bottom') is True
                   and state.get('loading') is False and state.get('height') == previous_height)
        stable = stable + 1 if settled and consistent and not fresh else 0
        previous_height = state.get('height')
        if stable >= STABLE_ROUNDS:
            complete, reason = not profiles_unresolved, 'profiles_unresolved' if profiles_unresolved else 'stable_bottom'
            break
        if not state.get('known'):
            reason = 'unknown_scroll_container'
        elif state.get('loading'):
            reason = 'loading'
        measured('scroll', page.evaluate, SCROLL_NEXT_JS)
        measured('stabilize', page.wait_for_timeout, BOTTOM_SETTLE_MS if state.get('bottom') else SCROLL_SETTLE_MS)
    collected[:] = list(merged.values())
    result = {'complete':complete, 'reason':reason, 'rounds':rounds, 'rows':len(collected),
            'streak_counts':summarize_streaks(collected),
            'confirmed':sum(row.get('streak_status')=='confirmed' for row in collected),
            'unknown':sum(row.get('streak_status') in {'unknown','suspect'} for row in collected),
            'profiles_unresolved':profiles_unresolved, 'scroll_height':previous_height, 'platform_dom_verified':False}
    result['timings_ms'] = {key: round(value * 1000, 3) for key, value in elapsed.items()}
    result['timings_ms']['total'] = round(max(0.0, now() - started) * 1000, 3)
    return result
