"""The drag-and-drop bridge: dropped paths reach the page through onNativeDrop()."""

from __future__ import annotations

import json
import sys
import types
import unittest
from unittest import mock

from fpsconv import window


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self


class _FakeWindow:
    """Just enough of a pywebview window: events.loaded, dom.document.events.drop, evaluate_js."""

    def __init__(self):
        self.events = types.SimpleNamespace(loaded=_Event())
        self.dom = types.SimpleNamespace(document=types.SimpleNamespace(events=types.SimpleNamespace(drop=_Event())))
        self.js: list[str] = []

    def evaluate_js(self, script):
        self.js.append(script)


class _FakeDOMEventHandler:
    def __init__(self, callback, prevent_default=False, stop_propagation=False, **_):
        self.callback = callback
        self.prevent_default = prevent_default
        self.stop_propagation = stop_propagation


def _with_fake_webview(test):
    fake_dom = types.ModuleType("webview.dom")
    fake_dom.DOMEventHandler = _FakeDOMEventHandler
    fake_webview = types.ModuleType("webview")
    fake_webview.dom = fake_dom
    return mock.patch.dict(sys.modules, {"webview": fake_webview, "webview.dom": fake_dom})(test)


class ForwardDropsTest(unittest.TestCase):
    @_with_fake_webview
    def test_binds_on_every_page_load_and_forwards_full_paths(self):
        win = _FakeWindow()
        window._forward_drops(win)
        self.assertEqual(len(win.events.loaded.handlers), 1)
        self.assertEqual(win.dom.document.events.drop.handlers, [], "nothing registered before the page is loaded")

        win.events.loaded.handlers[0]()          # page loaded → the drop listener is installed
        (handler,) = win.dom.document.events.drop.handlers
        self.assertTrue(handler.prevent_default, "WebView2 must not navigate to the dropped file")

        handler.callback({"type": "drop", "dataTransfer": {"files": [
            {"name": "a.mka", "pywebviewFullPath": r"D:\Rips\a.mka"},
            {"name": "b.thd", "pywebviewFullPath": r"D:\Rips\b.thd"},
            {"name": "web-only.txt"},            # a file whose path pywebview could not resolve
        ]}})
        self.assertEqual(len(win.js), 1)
        self.assertIn("onNativeDrop(", win.js[0])
        self.assertIn(json.dumps([r"D:\Rips\a.mka", r"D:\Rips\b.thd"]), win.js[0])

    @_with_fake_webview
    def test_drop_without_paths_calls_nothing(self):
        win = _FakeWindow()
        window._forward_drops(win)
        win.events.loaded.handlers[0]()
        win.dom.document.events.drop.handlers[0].callback({"type": "drop", "dataTransfer": {"files": []}})
        self.assertEqual(win.js, [])

    def test_no_dom_support_is_not_fatal(self):
        win = _FakeWindow()
        with mock.patch.dict(sys.modules, {"webview": None, "webview.dom": None}):
            window._forward_drops(win)           # ImportError inside → silently no drag-and-drop
        self.assertEqual(win.events.loaded.handlers, [])


if __name__ == "__main__":
    unittest.main()
