"""Keep the real loopback peer intact when launching the desktop ASGI server."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from desktop import launcher


class DesktopProxyBoundaryTests(unittest.TestCase):
    def test_desktop_server_does_not_rewrite_peer_from_forwarded_headers(self):
        fake_app = types.ModuleType("app")
        fake_app.app = object()
        fake_server = mock.Mock()
        with mock.patch.dict(sys.modules, {"app": fake_app}), \
             mock.patch("uvicorn.Config") as config, \
             mock.patch("uvicorn.Server", return_value=fake_server):
            worker = launcher.ServerThread(port=18765)
            worker.run()
        self.assertIsNone(worker.error)
        self.assertIs(config.call_args.args[0], fake_app.app)
        self.assertEqual(config.call_args.kwargs["host"], "127.0.0.1")
        self.assertEqual(config.call_args.kwargs["port"], 18765)
        self.assertIs(config.call_args.kwargs["proxy_headers"], False)
        fake_server.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
