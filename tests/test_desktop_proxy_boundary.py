"""Keep the real loopback peer intact when launching the desktop ASGI server."""
from __future__ import annotations

import os
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
        with mock.patch.dict(os.environ, {"PORT": "8000"}), \
             mock.patch.dict(sys.modules, {"app": fake_app}), \
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


    def test_actual_desktop_port_is_fixed_before_app_import(self):
        import builtins
        observed=[]
        original_import=builtins.__import__
        fake_app=types.ModuleType("app");fake_app.app=object()
        def capture_import(name,*args,**kwargs):
            if name=="app":observed.append(os.environ.get("PORT"))
            return original_import(name,*args,**kwargs)
        with mock.patch.dict(os.environ,{"PORT":"8000"}), \
             mock.patch.dict(sys.modules,{"app":fake_app}), \
             mock.patch("builtins.__import__",side_effect=capture_import), \
             mock.patch("uvicorn.Config"), \
             mock.patch("uvicorn.Server",return_value=mock.Mock()):
            worker=launcher.ServerThread(port=18766)
            worker.run()
            self.assertIsNone(worker.error)
            self.assertEqual(os.environ["PORT"],"18766")
        self.assertEqual(observed,["18766"])

if __name__ == "__main__":
    unittest.main()
