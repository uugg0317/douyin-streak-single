"""T09: offline semantic-layer fixtures; no browser, config, or account reads."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType
import unittest


# Import just the guard and its pure helper without executing core/__init__.py,
# which bootstraps deployment configuration. These fixtures never need it.
_PACKAGE = "_guard_scope_fixture"
if _PACKAGE not in sys.modules:
    package = ModuleType(_PACKAGE)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "core")]
    sys.modules[_PACKAGE] = package
guard = importlib.import_module(f"{_PACKAGE}.guard")
browser_errors = importlib.import_module(f"{_PACKAGE}.browser_errors")


class Node:
    def __init__(self, text="", *, visible=True, children=(), error=None):
        self.text = text
        self.visible = visible
        self.children = list(children)
        self.error = error

    def bounding_box(self):
        if self.error:
            raise self.error
        return {"x": 0, "y": 0, "width": 100, "height": 40} if self.visible else None

    def descendants(self):
        yield self
        for child in self.children:
            yield from child.descendants()

    def get_by_text(self, keyword, exact=False):
        return Matches([node for node in self.descendants() if keyword in node.text])


class Matches:
    def __init__(self, nodes=(), error=None):
        self.nodes = list(nodes)
        self.error = error

    def count(self):
        if self.error:
            raise self.error
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class Page(Node):
    def __init__(self, *, previews=(), layers=(), locator_error=None, text_error=None):
        super().__init__(children=[*previews, *layers])
        self.layers = list(layers)
        self.locator_error = locator_error
        self.text_error = text_error
        self.locator_calls = []

    def locator(self, selector):
        self.locator_calls.append(selector)
        if self.locator_error:
            raise self.locator_error
        return Matches(self.layers)

    def get_by_text(self, keyword, exact=False):
        if self.text_error:
            raise self.text_error
        return super().get_by_text(keyword, exact=exact)


class GuardSignalScopeTests(unittest.TestCase):
    def test_ordinary_message_previews_do_not_trigger_on_weak_words(self):
        for text in ("把验证码发给我", "请稍后回复我", "稍后再试这个功能", "请稍后再试", "网络异常，刚刚掉线了"):
            with self.subTest(text=text):
                self.assertIsNone(guard.detect_rate_limit(Page(previews=[Node(text)])))

    def test_network_or_wait_alert_alone_is_not_confirmed_platform_block(self):
        for text in ("网络异常，请稍后再试", "验证码", "请稍后"):
            with self.subTest(text=text):
                self.assertIsNone(guard.detect_rate_limit(Page(layers=[Node(text)])))

    def test_visible_strong_layer_still_stops(self):
        for keyword in ("安全验证", "滑动验证", "人机验证", "验证中心", "操作太频繁", "操作频繁", "发送过于频繁", "请勿频繁"):
            with self.subTest(keyword=keyword):
                self.assertEqual(guard.detect_rate_limit(Page(layers=[Node(keyword)])), keyword)

    def test_strong_layer_has_priority_over_conversation_preview(self):
        page = Page(previews=[Node("朋友说操作频繁是什么")], layers=[Node("请完成安全验证")])
        self.assertEqual(guard.detect_rate_limit(page), "安全验证")
        self.assertTrue(page.locator_calls)

    def test_visible_nonblocking_layer_does_not_scan_conversation_previews(self):
        page = Page(previews=[Node("帮我看看这个人机验证截图")], layers=[Node("网络异常，请稍后再试")])
        self.assertIsNone(guard.detect_rate_limit(page))

    def test_hidden_strong_layer_does_not_trigger(self):
        self.assertIsNone(guard.detect_rate_limit(Page(layers=[Node("安全验证", visible=False)])))

    def test_nested_strong_signal_inside_second_visible_layer_still_stops(self):
        page = Page(layers=[Node("操作频繁", visible=False), Node("普通提示"),
                            Node(children=[Node("请完成人机验证")])])
        self.assertEqual(guard.detect_rate_limit(page), "人机验证")

    def test_no_visible_layer_keeps_explicit_strong_signal_fallback(self):
        page = Page(previews=[Node("操作太频繁，请稍后再试")], layers=[Node("旧安全验证", visible=False)])
        self.assertEqual(guard.detect_rate_limit(page), "操作太频繁")

    def test_text_only_legacy_page_fixture_uses_strong_fallback(self):
        page = Node(children=[Node("发送过于频繁")])
        self.assertEqual(guard.detect_rate_limit(page), "发送过于频繁")

    def test_unsupported_scope_locator_uses_strong_fallback(self):
        page = Page(previews=[Node("请完成安全验证")], locator_error=AssertionError("fixture has no layer selector"))
        self.assertEqual(guard.detect_rate_limit(page), "安全验证")

    def test_detached_layer_query_uses_strong_fallback(self):
        page = Page(previews=[Node("请完成安全验证")], layers=[Node("", error=RuntimeError("Element is not attached to the DOM"))])
        self.assertEqual(guard.detect_rate_limit(page), "安全验证")

    def test_unreadable_visible_layer_retains_strong_fallback(self):
        class UnreadableLayer(Node):
            def get_by_text(self, keyword, exact=False):
                raise RuntimeError("Element is not attached to the DOM")

        page = Page(previews=[Node("请完成安全验证")], layers=[UnreadableLayer()])
        self.assertEqual(guard.detect_rate_limit(page), "安全验证")

    def test_no_scope_fallback_can_still_stop_on_quoted_strong_phrase(self):
        # This retained fail-closed fallback is a documented limitation, not
        # evidence that a real platform challenge was positively identified.
        page = Page(previews=[Node("朋友问安全验证应该怎么办")])
        self.assertEqual(guard.detect_rate_limit(page), "安全验证")

    def test_closed_browser_during_scope_lookup_is_not_swallowed(self):
        page = Page(locator_error=RuntimeError("Target page, context or browser has been closed"))
        with self.assertRaises(browser_errors.BrowserSessionUnavailable):
            guard.detect_rate_limit(page)

    def test_closed_browser_during_scope_visibility_is_not_swallowed(self):
        page = Page(layers=[Node("安全验证", error=RuntimeError("Event loop is closed! Is Playwright already stopped?"))])
        with self.assertRaises(browser_errors.BrowserSessionUnavailable):
            guard.detect_rate_limit(page)

    def test_closed_browser_during_fallback_text_lookup_is_not_swallowed(self):
        page = Page(text_error=RuntimeError("Connection closed while reading from the driver"))
        with self.assertRaises(browser_errors.BrowserSessionUnavailable):
            guard.detect_rate_limit(page)

    def test_closed_browser_during_fallback_visibility_is_not_swallowed(self):
        page = Page(previews=[Node("安全验证", error=RuntimeError("Browser has been closed"))])
        with self.assertRaises(browser_errors.BrowserSessionUnavailable):
            guard.detect_rate_limit(page)

    def test_recoverable_text_query_error_does_not_masquerade_as_rate_limit(self):
        self.assertIsNone(guard.detect_rate_limit(Page(text_error=RuntimeError("Element is not attached to the DOM"))))


if __name__ == "__main__":
    unittest.main()
