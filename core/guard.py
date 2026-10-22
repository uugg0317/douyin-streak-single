"""共享防护：限流关键词与安全验证浮层检测。"""

from __future__ import annotations

from .browser_errors import raise_if_browser_unavailable

# Only semantic containers are assumed here. Site-specific class names require
# real-page evidence; do not invent selectors for an unverified platform DOM.
VERIFICATION_LAYER_SELECTORS = (
    '[role="dialog"]',
    '[role="alertdialog"]',
    '[aria-modal="true"]',
    '[role="alert"]',
    "dialog",
)
VERIFICATION_LAYER_SELECTOR = ", ".join(VERIFICATION_LAYER_SELECTORS)

# Generic waiting/network text and the bare word "验证码" occur in ordinary
# conversations too. They never independently establish a platform block.
RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "安全验证",
    "滑动验证",
    "验证中心",
    "人机验证",
    "请勿频繁",
]


def _visible_strong_signal(scope, *, propagate_lookup_errors: bool = False) -> str | None:
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = scope.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                box = loc.nth(i).bounding_box()
                if box:  # 可见才计，避免匹配到隐藏节点
                    return kw
        except Exception as error:
            # A missing/detached element may be inspected again on the next
            # call; a dead browser must reach the caller's stop/recovery path.
            raise_if_browser_unavailable(error)
            if propagate_lookup_errors:
                raise
            continue
    return None


def detect_rate_limit(page) -> str | None:
    """优先核验可见验证/限流层；无可见层时保留明确强信号回退。

    可见语义层存在时只检查层内文案，避免会话预览抢先命中。平台未提供
    预期容器或暂时无法查询时，保守保留现有明确强词的全页停止规则；该
    回退仍可能因普通消息引用强词而停止，不冒充已验证的真实平台识别。
    本函数只报告信号，不处理、点击或绕过验证。
    """
    visible_layer_found = False
    try:
        layers = page.locator(VERIFICATION_LAYER_SELECTOR)
        for index in range(layers.count()):
            layer = layers.nth(index)
            if not layer.bounding_box():
                continue
            visible_layer_found = True
            signal = _visible_strong_signal(layer, propagate_lookup_errors=True)
            if signal:
                return signal
        if visible_layer_found:
            return None
    except Exception as error:
        # Older page fixtures can lack locator(); temporary DOM lookup errors
        # retain the same conservative strong-signal fallback as an absent layer.
        raise_if_browser_unavailable(error)
    return _visible_strong_signal(page)
