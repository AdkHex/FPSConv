import contextlib
import io
import unittest
from unittest import mock

from fpsconv import __main__ as entry
from fpsconv import tombstone


class TombstoneTests(unittest.TestCase):
    def test_every_command_exits_1_silently(self):
        for argv in (["--version"], ["doctor"], ["convert", "a.ac3", "-m", "23.976-25"],
                     ["encode", "a.thd"], ["deew", "--version"], ["x.py"], ["probe", "a"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(entry.main(argv), 1, argv)
            self.assertEqual(out.getvalue() + err.getvalue(), "", argv)

    def test_gui_launch_shows_the_black_screen(self):
        for argv in ([], ["gui"]):
            with mock.patch.object(tombstone, "run", return_value=0) as run:
                self.assertEqual(entry.main(argv), 0)
                run.assert_called_once()

    def test_window_is_pure_black_with_nothing_in_it(self):
        html = tombstone.BLACK_HTML
        self.assertIn("background:#000", html)
        body = html.split("<body", 1)[1].split(">", 1)[1].split("</body>", 1)[0]
        self.assertEqual(body, "")

    def test_run_forces_auto_update_and_starts_updater(self):
        fake_webview = mock.MagicMock()
        with mock.patch.object(tombstone.config, "save_settings") as save, \
             mock.patch.object(tombstone.updater, "Updater") as upd, \
             mock.patch.dict("sys.modules", {"webview": fake_webview}):
            self.assertEqual(tombstone.run(), 0)
        save.assert_called_once_with({"auto_update": True})
        upd.return_value.start.assert_called_once()
        kwargs = fake_webview.create_window.call_args.kwargs
        self.assertEqual(kwargs["html"], tombstone.BLACK_HTML)
        self.assertEqual(kwargs["background_color"], "#000000")
        fake_webview.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
