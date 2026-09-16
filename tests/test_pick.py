"""The OS file pickers used when the GUI runs in a browser (no pywebview window)."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from fpsconv import server


def _result(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)


def _folder_exists():
    """POSIX start folders on every OS: these pickers never run on Windows, but the tests do,
    and Windows can neither hold a quote in a folder name nor keep a path free of backslashes."""
    return mock.patch("os.path.isdir", return_value=True)


class OsascriptPickTest(unittest.TestCase):
    def test_files_returns_posix_paths_and_starts_in_the_given_folder(self):
        with _folder_exists(), mock.patch("subprocess.run", return_value=_result(0, "/Rips/a.mka\n/Rips/b.thd\n")) as run:
            self.assertEqual(server._osascript_pick("files", "/Rips"), ["/Rips/a.mka", "/Rips/b.thd"])
        script = " | ".join(run.call_args.args[0])
        self.assertIn('POSIX file "/Rips"', script)
        self.assertIn("choose file", script)
        self.assertIn("multiple selections allowed", script)
        self.assertIn("tell me to activate", script)

    def test_folder_strips_the_trailing_slash(self):
        with mock.patch("subprocess.run", return_value=_result(0, "/Users/x/Encodes/out/\n")):
            self.assertEqual(server._osascript_pick("folder", ""), ["/Users/x/Encodes/out"])

    def test_cancel_is_an_empty_pick_not_a_failure(self):
        with mock.patch("subprocess.run", return_value=_result(1, "", "execution error: User canceled. (-128)")):
            self.assertEqual(server._osascript_pick("files", ""), [])

    def test_other_errors_fall_back_to_the_in_page_browser(self):
        with mock.patch("subprocess.run", return_value=_result(1, "", "execution error: something else")):
            self.assertIsNone(server._osascript_pick("files", ""))
        with mock.patch("subprocess.run", side_effect=OSError("no osascript")):
            self.assertIsNone(server._osascript_pick("folder", ""))

    def test_quotes_in_the_start_folder_are_escaped(self):
        with _folder_exists(), mock.patch("subprocess.run", return_value=_result(0, "")) as run:
            server._osascript_pick("files", '/Rips/say "hi"')
        self.assertIn('POSIX file "/Rips/say \\"hi\\""', " ".join(run.call_args.args[0]))

    def test_a_missing_start_folder_is_simply_not_passed(self):
        with mock.patch("subprocess.run", return_value=_result(0, "")) as run:
            server._osascript_pick("files", "/nowhere/at/all")
        self.assertNotIn("POSIX file", " ".join(run.call_args.args[0]))


class ZenityPickTest(unittest.TestCase):
    def test_files_and_folder_commands(self):
        with _folder_exists(), mock.patch("subprocess.run", return_value=_result(0, "/a.mka\n/b.mka\n")) as run:
            self.assertEqual(server._zenity_pick("files", "/Rips"), ["/a.mka", "/b.mka"])
        self.assertIn("--multiple", run.call_args.args[0])
        self.assertIn("--filename=/Rips/", run.call_args.args[0])
        with mock.patch("subprocess.run", return_value=_result(0, "/out\n")) as run:
            self.assertEqual(server._zenity_pick("folder", ""), ["/out"])
        self.assertIn("--directory", run.call_args.args[0])

    def test_cancel_and_failure(self):
        with mock.patch("subprocess.run", return_value=_result(1)):
            self.assertEqual(server._zenity_pick("files", ""), [])
        with mock.patch("subprocess.run", return_value=_result(255)):
            self.assertIsNone(server._zenity_pick("files", ""))


if __name__ == "__main__":
    unittest.main()
