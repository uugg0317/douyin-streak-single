"""Thin adapter contract tests with async fakes; no browser/network/real state.

The Runner fake drives only immediately completing fixture coroutines. It lets
this suite run even in a Windows sandbox that blocks asyncio's socketpair.
"""
from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.sparkflow_login import desktop as vendor
from core.sparkflow_login import adapter


PNG = b"\x89PNG\r\n\x1a\nsynthetic-qr-image"
STATE = {"cookies": [{"name": "sessionid", "value": "fixture-secret-not-public", "domain": ".douyin.com", "path": "/", "expires": -1}], "origins": []}
ORIGINAL_MANAGER = vendor.LoginDesktopManager


class ImmediateRunner:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def run(self, coroutine, timeout=45):
        if self.closed:
            coroutine.close()
            raise RuntimeError("runner already closed")
        self.events.append("runner.run")
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        finally:
            coroutine.close()
        raise AssertionError("fixture unexpectedly awaited real asynchronous work")

    def close(self):
        self.events.append("runner.close")
        self.closed = True


class AsyncLock:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


class FakePage:
    def __init__(self, events):
        self.events = events
        self.url = "https://creator.douyin.com/"
        self.frames = []
        self.closed = False
        self.viewport_size = {"width": 1600, "height": 1000}

    def is_closed(self): return self.closed

    async def title(self): return "Creator fixture"

    async def close(self):
        self.events.append("page.close")
        self.closed = True

    def locator(self, _selector):
        locator = Mock()
        locator.first = locator
        async def empty_count(): return 0
        async def invisible(): return False
        locator.count = empty_count
        locator.is_visible = invisible
        return locator


class FakeContext:
    def __init__(self, events):
        self.events = events
        self.handlers = {}
        self.close_error = False
        self.emit_close = True
        self.storage_calls = 0

    def on(self, event, callback): self.handlers.setdefault(event, []).append(callback)

    async def close(self):
        self.events.append("context.close")
        if self.close_error: raise RuntimeError("fixture close failed")
        if self.emit_close:
            for callback in self.handlers.get("close", []): callback(self)

    async def storage_state(self):
        self.storage_calls += 1
        self.events.append("context.storage_state")
        return copy.deepcopy(STATE)


class FakeQr:
    def __init__(self, events): self.events = events

    async def screenshot(self, **kwargs):
        self.events.append(("qr.screenshot", kwargs))
        return PNG


class FakeManager:
    def __init__(self, events):
        self.events = events
        self.context = None
        self.page = None
        self.fixture_context = FakeContext(events)
        self.fixture_page = FakePage(events)
        self.qr = FakeQr(events)
        self.ready = False
        self.ready_error = None
        self.open_error = None
        self.refresh_error = None
        self._lock = AsyncLock()
        self.playwright = None

    async def open_login(self):
        self.events.append("manager.open_login")
        if self.open_error: raise self.open_error
        self.context = self.fixture_context
        self.page = self.fixture_page
        return {"ok": True, "url": self.page.url}

    async def export_if_ready(self):
        self.events.append("manager.export_if_ready")
        if self.ready_error: raise self.ready_error
        return {"ok": True, "state": "ready" if self.ready else "waiting",
                "result": {"unique_id": "fixture-id", "username": "Fixture", "cookies": STATE["cookies"]}} if self.ready else {"ok": True, "state": "waiting"}

    async def _get_active_page(self):
        self.events.append("manager._get_active_page")
        return self.page

    async def _extract_login_qr(self, page):
        assert page is self.page
        self.events.append("manager._extract_login_qr")
        return self.qr

    async def reduce_page_activity(self, page):
        assert page is self.page
        self.events.append("manager.reduce_page_activity")

    async def refresh_login_qr(self):
        self.events.append("manager.refresh_login_qr")
        if self.refresh_error: raise self.refresh_error
        return {"ok": True, "state": "qr_ready"}

    async def stop(self, clear_profile=False):
        self.events.append(("manager.stop", clear_profile))
        # Use the exact copied stop implementation, including its swallowed
        # context-close errors and internal purge. The adapter must guard it.
        return await ORIGINAL_MANAGER.stop(self, clear_profile=clear_profile)


class SparkFlowLoginAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sparkflow-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.profile = self.data / "qr-login-profile"
        self.events = []
        self.manager = FakeManager(self.events)
        self.runner = ImmediateRunner(self.events)
        self.patches = []
        self.session = None
        # Keep mutation of vendor configuration local to each fixture.
        for name in ("PROFILE_DIR", "LOGIN_NETWORK_MODE", "LOGIN_PROXY_SERVER"):
            self.start_patch(patch.object(vendor, name, getattr(vendor, name)))
        self.start_patch(patch.dict("os.environ", {"SPARKFLOW_BROWSER_CHANNEL": "msedge", "LOGIN_DESKTOP_PROXY_MODE": "direct", "QR_LOGIN_NETWORK_MODE": "direct"}))
        self.start_patch(patch.object(vendor, "async_playwright", side_effect=AssertionError("real browser startup forbidden")))
        self.manager_factory = self.patch_reference(ORIGINAL_MANAGER, "LoginDesktopManager", vendor, return_value=self.manager)
        import asyncio
        async def immediate_thread(function, *args, **kwargs): return function(*args, **kwargs)
        self.start_patch(patch.object(asyncio, "to_thread", side_effect=immediate_thread))
        self.original_purge = vendor._purge_login_data
        self.start_patch(patch.object(vendor, "_purge_login_data", self.original_purge))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def patch_reference(self, original, name, module, **kwargs):
        # Cover both module-qualified imports and direct aliases in the adapter.
        replacement = Mock(**kwargs)
        for alias, value in tuple(vars(adapter).items()):
            if value is original:
                self.start_patch(patch.object(adapter, alias, replacement))
        self.start_patch(patch.object(module, name, replacement))
        return replacement

    def create(self):
        self.session = adapter.SparkFlowLoginBrowser(profile_data_dir=self.data, channel="msedge",
            manager_factory=self.manager_factory, runner_factory=lambda: self.runner)
        self.addCleanup(self.safe_close)
        return self.session

    def safe_close(self):
        if self.session is not None:
            try: self.session.close()
            except Exception: pass

    def test_open_delegates_to_original_manager_interface_and_owned_profile(self):
        session = self.create()
        self.assertIs(session._manager, self.manager)
        session.open()
        self.assertEqual(self.events.count("manager.open_login"), 1)
        self.assertEqual(Path(vendor.PROFILE_DIR).resolve(), self.profile.resolve())
        self.assertEqual(self.manager.fixture_context.storage_calls, 0)

    def test_waiting_uses_original_qr_extraction_reduction_and_png_screenshot(self):
        session = self.create(); session.open()
        result = session.observe()
        self.assertEqual(result.qr_png, PNG)
        self.assertFalse(result.authenticated)
        extraction = self.events.index("manager._extract_login_qr")
        reduction = self.events.index("manager.reduce_page_activity")
        screenshot = next(i for i, event in enumerate(self.events) if isinstance(event, tuple) and event[0] == "qr.screenshot")
        self.assertLess(extraction, reduction)
        self.assertLess(reduction, screenshot)
        self.assertNotIn("fixture-secret-not-public", repr(result))
        self.assertNotIn("synthetic-qr-image", repr(result))

    def test_confirmed_identity_exposes_only_signal_until_storage_is_requested(self):
        session = self.create(); session.open(); self.manager.ready = True
        result = session.observe()
        self.assertTrue(result.authenticated)
        self.assertEqual(result.login_signal, "creator_identity")
        self.assertIsNone(result.qr_png)
        self.assertEqual(self.manager.fixture_context.storage_calls, 0)
        self.assertNotIn("fixture-secret-not-public", repr(result))
        self.assertNotIn("fixture-id", repr(result))
        self.assertEqual(session.storage_state(), STATE)
        self.assertEqual(self.manager.fixture_context.storage_calls, 1)

    def test_refresh_delegates_without_opening_another_context_or_exporting(self):
        session = self.create(); session.open(); session.refresh()
        self.assertEqual(self.events.count("manager.refresh_login_qr"), 1)
        self.assertEqual(self.events.count("manager.open_login"), 1)
        self.assertEqual(self.manager.fixture_context.storage_calls, 0)

    def test_missing_qr_returns_waiting_without_inventing_an_image(self):
        session = self.create(); session.open(); self.manager.qr = None
        result = session.observe()
        self.assertFalse(result.authenticated)
        self.assertIsNone(result.qr_png)

    def test_close_uses_stop_without_full_profile_deletion_then_closes_runner(self):
        session = self.create(); session.open()
        cache = self.profile / "Default" / "Cache" / "cached-resource"
        cookie = self.profile / "Default" / "Cookies"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"public-cache"); cookie.write_bytes(b"fixture-cookie")
        session.close()
        self.assertIn(("manager.stop", False), self.events)
        self.assertLess(self.events.index("context.close"), self.events.index("runner.close"))
        self.assertTrue(self.runner.closed)
        self.assertFalse(cookie.exists())
        self.assertEqual(cache.read_bytes(), b"public-cache")

    def test_context_close_failure_does_not_purge_still_active_profile(self):
        session = self.create(); session.open()
        cookie = self.profile / "Default" / "Cookies"
        cookie.parent.mkdir(parents=True, exist_ok=True); cookie.write_bytes(b"still-active-cookie")
        self.manager.fixture_context.close_error = True
        with self.assertRaises(Exception): session.close()
        self.assertEqual(cookie.read_bytes(), b"still-active-cookie")
        self.assertFalse(self.runner.closed)
        self.assertFalse(session._closed)
        self.manager.fixture_context.close_error = False
        session.close()
        self.assertTrue(self.runner.closed)
        self.assertTrue(session._closed)
        self.assertFalse(cookie.exists())

    def test_missing_close_event_is_not_treated_as_confirmed_cleanup(self):
        session = self.create(); session.open()
        cookie = self.profile / "Default" / "Cookies"
        cookie.parent.mkdir(parents=True, exist_ok=True); cookie.write_bytes(b"unconfirmed-close-cookie")
        self.manager.fixture_context.emit_close = False
        with self.assertRaises(Exception): session.close()
        self.assertEqual(cookie.read_bytes(), b"unconfirmed-close-cookie")
        self.assertFalse(self.runner.closed)
        self.manager.fixture_context.emit_close = True
        session.close()
        self.assertFalse(cookie.exists())

    def test_runner_close_failure_is_retryable_without_repeating_profile_purge(self):
        session=self.create();session.open()
        attempts=[];original=self.runner.close
        def close():
            attempts.append(1)
            if len(attempts)==1:raise RuntimeError('fixture runner close failed')
            original()
        self.runner.close=close
        with self.assertRaises(Exception):session.close()
        self.assertFalse(session._closed)
        stops=self.events.count(('manager.stop',False))
        session.close()
        self.assertTrue(session._closed)
        self.assertEqual(self.events.count(('manager.stop',False)),stops)

    def test_driver_close_failure_keeps_owned_reference_for_retry(self):
        session=self.create();session.open()
        class Driver:
            calls=0
            async def stop(driver):
                driver.calls+=1
                if driver.calls==1:raise RuntimeError('fixture driver close failed')
        driver=Driver();self.manager.playwright=driver
        with self.assertRaises(Exception):session.close()
        self.assertFalse(self.runner.closed);self.assertFalse(session._closed)
        self.assertIs(session._playwright_ref,driver)
        session.close()
        self.assertEqual(driver.calls,2)
        self.assertTrue(self.runner.closed);self.assertTrue(session._closed)

    def test_async_worker_close_is_safe_when_worker_already_stopped(self):
        worker=object.__new__(adapter._AsyncWorker)
        worker._thread=Mock();worker._thread.is_alive.return_value=False
        worker._loop=Mock()
        worker.close();worker.close()
        worker._loop.call_soon_threadsafe.assert_not_called()

    def test_open_failure_propagates_and_close_still_releases_runner(self):
        session = self.create(); self.manager.open_error = RuntimeError("fixture open failed")
        with self.assertRaises(Exception): session.open()
        try: session.close()
        except Exception: pass
        self.assertTrue(self.runner.closed)

    def test_refresh_failure_is_not_reported_as_a_successful_refresh(self):
        session = self.create(); session.open(); self.manager.refresh_error = RuntimeError("fixture refresh failed")
        with self.assertRaises(Exception): session.refresh()


if __name__ == "__main__":
    unittest.main()
