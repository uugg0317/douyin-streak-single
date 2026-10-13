"""不访问抖音、不读取真实 .env 的本地回归测试。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path


_SANDBOX = tempfile.TemporaryDirectory(prefix="douyin-streak-tests-")
_ROOT = Path(_SANDBOX.name)
os.environ["DATA_DIR"] = str(_ROOT / "data")
os.environ["ENV_FILE_PATH"] = str(_ROOT / "missing.env")
os.environ["INSTANCE_LOCK_PATH"] = str(_ROOT / "server.pid")
os.environ["HOST"] = "127.0.0.1"

import app_common  # noqa: E402
from core import ledger  # noqa: E402
from core.config import load_config, save_config  # noqa: E402
from core.runtime import load_runtime, update_runtime  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from middleware import cache_policy  # noqa: E402
from routers.contacts_ledger import api_avatar  # noqa: E402
from routers.run import api_reset_running  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402
from tasks_service import _collect_retry_names  # noqa: E402
import session_service  # noqa: E402
from core import config as cfg_mod  # noqa: E402


def tearDownModule() -> None:  # noqa: N802 - unittest 固定钩子名
    # Windows 不允许删除仍被 RotatingFileHandler 占用的临时日志。
    for handler in list(app_common.logger.handlers):
        handler.close()
        app_common.logger.removeHandler(handler)
    _SANDBOX.cleanup()


def _request(path: str, method: str = "GET", scheme: str = "http") -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "scheme": scheme,
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    })


class LocalConsoleBoundaryTests(unittest.TestCase):
    def test_local_bind_accepts_loopback_and_rejects_public_interfaces(self) -> None:
        from ip_guard import _check_local_bind
        for host in ("127.0.0.1", "localhost", "::1"):
            _check_local_bind(host)
        for host in ("0.0.0.0", "::", "192.0.2.10", "example.com"):
            with self.assertRaises(SystemExit):
                _check_local_bind(host)

    def test_local_console_routes_have_no_management_login(self) -> None:
        import app
        paths = set(app.app.openapi()["paths"])
        self.assertFalse(any(path.startswith("/api/auth/") for path in paths))
        self.assertIn("/api/credentials/qr/start", paths)

    def test_avatar_proxy_still_rejects_untrusted_upstream_without_network(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            api_avatar(url="http://127.0.0.1/private")
        self.assertEqual(caught.exception.status_code, 400)


class ReliabilityTests(unittest.TestCase):
    def test_retry_names_merge_deferred_and_failed_in_order(self) -> None:
        result = {
            "failed": [{"name": "甲"}, {"name": "_system"}, {"name": "乙"}],
            "deferred": [{"name": "乙"}, {"name": "丙"}],
        }
        self.assertEqual(_collect_retry_names(result), ["甲", "乙", "丙"])

    def test_runtime_updates_do_not_lose_parallel_fields(self) -> None:
        threads = [
            threading.Thread(target=update_runtime, kwargs={f"field_{i}": i})
            for i in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state = load_runtime()
        for i in range(20):
            self.assertEqual(state[f"field_{i}"], i)

    def test_parallel_ledger_updates_keep_every_friend(self) -> None:
        names = [f"并发好友-{i}" for i in range(20)]
        threads = [
            threading.Thread(
                target=ledger.set_selected,
                args=([{"display_name": name, "selected": True}],),
            )
            for name in names
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        saved = {item["display_name"] for item in ledger.load_ledger()}
        self.assertTrue(set(names).issubset(saved))

    def test_missing_ledger_recovers_from_backup(self) -> None:
        ledger.set_selected([{"display_name": "备份好友", "selected": True}])
        ledger.LEDGER_PATH.replace(ledger.LEDGER_BAK_PATH)
        recovered = {item["display_name"] for item in ledger.load_ledger()}
        self.assertIn("备份好友", recovered)

    def test_partial_config_update_preserves_unmentioned_values(self) -> None:
        save_config({"friends": ["配置好友"], "max_friends_per_run": 7})
        save_config({"auto_run_enabled": False})
        saved = load_config()
        self.assertEqual(saved["friends"], ["配置好友"])
        self.assertEqual(saved["max_friends_per_run"], 7)
        self.assertFalse(saved["auto_run_enabled"])

    def test_reset_never_releases_a_live_worker_lock(self) -> None:
        self.assertTrue(app_common.run_lock.acquire(blocking=False))
        try:
            with self.assertRaises(HTTPException) as caught:
                api_reset_running()
            self.assertEqual(caught.exception.status_code, 409)
            self.assertTrue(app_common.run_lock.locked())
        finally:
            app_common.run_lock.release()


class CacheTests(unittest.TestCase):
    def test_html_is_not_cached_but_vendor_assets_are(self) -> None:
        async def respond(_request: Request) -> Response:
            return Response()

        html = asyncio.run(cache_policy(_request("/"), respond))
        vendor = asyncio.run(
            cache_policy(_request("/static/vendor/vue.js"), respond)
        )
        api = asyncio.run(cache_policy(_request("/api/status"), respond))
        self.assertIn("no-store", html.headers["cache-control"])
        self.assertIn("max-age=604800", vendor.headers["cache-control"])
        self.assertEqual(api.headers["cache-control"], "no-store")


class SessionWarnSwitchTests(unittest.TestCase):
    def _set_switch(self, value: str | None) -> None:
        if value is None:
            os.environ.pop("SESSION_WARN_EMAIL_ENABLED", None)
        else:
            os.environ["SESSION_WARN_EMAIL_ENABLED"] = value

    def tearDown(self) -> None:
        self._set_switch(None)

    def test_switch_off_skips_email_without_sending(self) -> None:
        self._set_switch("off")
        sent = []
        with mock.patch.object(session_service, "_send_email", side_effect=sent.append):
            result = session_service._send_warning("测试原因", force=True)
        self.assertFalse(result)
        self.assertEqual(sent, [])

    def test_default_on_reaches_send(self) -> None:
        self._set_switch(None)
        sent = []
        with mock.patch.object(session_service, "_send_email", side_effect=lambda *a: sent.append(a)):
            result = session_service._send_warning("测试原因", force=True)
        self.assertTrue(result)
        self.assertEqual(len(sent), 1)


class StatePathTests(unittest.TestCase):
    _FAKE_STATE = json.dumps({"cookies": [{"name": "sessionid", "value": "x"}]})

    def test_no_state_returns_none(self) -> None:
        self.assertIsNone(cfg_mod.get_valid_state_path())

    def test_root_state_is_no_longer_self_healed(self) -> None:
        with tempfile.TemporaryDirectory() as fake_root_name:
            fake_root = Path(fake_root_name)
            (fake_root / "state.json").write_text(self._FAKE_STATE, encoding="utf-8")
            with mock.patch.object(cfg_mod, "BASE_DIR", fake_root):
                self.assertIsNone(cfg_mod.get_valid_state_path())

    def test_data_state_is_found(self) -> None:
        cfg_mod.STATE_PATH.write_text(self._FAKE_STATE, encoding="utf-8")
        try:
            self.assertEqual(cfg_mod.get_valid_state_path(), cfg_mod.STATE_PATH)
        finally:
            cfg_mod.STATE_PATH.unlink(missing_ok=True)


class LocalBackgroundModeTests(unittest.TestCase):
    def test_background_switch_defaults_on_and_recognizes_disabled_values(self):
        from core import scheduler
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(scheduler.background_tasks_enabled())
        for value in ("0", "false", "off", "no"):
            with mock.patch.dict(os.environ, {"SPARKKEEPER_BACKGROUND_TASKS": value}):
                self.assertFalse(scheduler.background_tasks_enabled())

    def test_local_lifespan_skips_account_jobs_and_config_migration(self):
        import app
        async def start_stop():
            async with app.lifespan(app.app):
                pass
        with mock.patch.dict(os.environ, {"SPARKKEEPER_BACKGROUND_TASKS": "0"}), \
             mock.patch.object(app, "_check_local_bind"), \
             mock.patch.object(app, "_acquire_instance_lock"), \
             mock.patch.object(app, "migrate_legacy_email_config") as migration, \
             mock.patch.object(app.scheduler, "configure") as configure, \
             mock.patch.object(app.scheduler, "shutdown"), \
             mock.patch.object(app, "_start_watchdog") as watchdog, \
             mock.patch.object(app, "set_running"), \
             mock.patch.object(app, "update_runtime"):
            asyncio.run(start_stop())
        migration.assert_not_called()
        configure.assert_not_called()
        watchdog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
