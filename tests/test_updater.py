"""Updater logic that needs no network: version maths and the download/verify step."""

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fpsconv import updater


class Versions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.0.42"), (1, 0, 42))
        self.assertEqual(updater.parse_version("0.0.0-dev"), (0, 0, 0))
        self.assertEqual(updater.parse_version(""), (0,))

    def test_is_newer(self):
        self.assertTrue(updater.is_newer("1.0.43", "1.0.42"))
        self.assertTrue(updater.is_newer("1.1.0", "1.0.999"))
        self.assertFalse(updater.is_newer("1.0.42", "1.0.42"))
        self.assertFalse(updater.is_newer("1.0.41", "1.0.42"))
        # a source checkout (0.0.0-dev) sees every release as newer
        self.assertTrue(updater.is_newer("1.0.1", "0.0.0-dev"))


class Download(unittest.TestCase):
    def test_download_verifies_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "FPSConv-Setup-9.9.9.exe"
            src.write_bytes(b"not really an installer" * 1000)
            sha = hashlib.sha256(src.read_bytes()).hexdigest()
            dest = Path(tmp) / "dl" / src.name
            seen = []
            out = updater.download(src.as_uri(), dest, sha, progress=seen.append)
            self.assertEqual(out.read_bytes(), src.read_bytes())
            self.assertTrue(seen and seen[-1] == 100.0)
            with self.assertRaises(ValueError):
                updater.download(src.as_uri(), Path(tmp) / "dl" / "bad.exe", "0" * 64)
            self.assertFalse((Path(tmp) / "dl" / "bad.exe").exists())


class StateMachine(unittest.TestCase):
    def test_check_marks_available_then_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "FPSConv-Setup-9.9.9.exe"
            src.write_bytes(b"x" * 100)
            latest = {"version": "9.9.9", "url": src.as_uri(),
                      "sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "notes": "n"}
            with mock.patch.object(updater, "is_installed_build", return_value=True), \
                 mock.patch.object(updater, "fetch_latest", return_value=latest), \
                 mock.patch.object(updater.config, "config_dir", return_value=Path(tmp) / "cfg"):
                installed = []
                with mock.patch.object(updater, "launch_installer", side_effect=installed.append):
                    u = updater.Updater(is_busy=lambda: False, on_install=lambda: None)
                    self.assertEqual(u.state, "idle")
                    # check() kicks off the download at once (manual "Check now" included)
                    self.assertIn(u.check()["state"], ("available", "downloading", "ready", "installing"))
                    for _ in range(100):
                        if u.state in ("ready", "installing"):
                            break
                        time.sleep(0.05)
                    self.assertTrue(u.installer and u.installer.exists())
                    for _ in range(100):          # auto_update defaults to on → installs when idle
                        if installed:
                            break
                        time.sleep(0.05)
                    self.assertEqual(installed, [u.installer])

    def test_manual_install_downloads_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "FPSConv-Setup-9.9.9.exe"
            src.write_bytes(b"y" * 100)
            latest = {"version": "9.9.9", "url": src.as_uri(),
                      "sha256": hashlib.sha256(src.read_bytes()).hexdigest()}
            launched = []
            with mock.patch.object(updater, "is_installed_build", return_value=True), \
                 mock.patch.object(updater.config, "config_dir", return_value=Path(tmp) / "cfg"), \
                 mock.patch.object(updater, "launch_installer", side_effect=launched.append), \
                 mock.patch.object(updater.threading, "Timer") as timer:
                u = updater.Updater(is_busy=lambda: True, on_install=lambda: None)
                u._set(state="available", latest=latest)
                self.assertEqual(u.install()["state"], "installing")
                self.assertEqual(len(launched), 1)
                timer.assert_called()

    def test_up_to_date_and_errors(self):
        with mock.patch.object(updater, "is_installed_build", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value={"version": "0.0.0"}):
            u = updater.Updater(is_busy=lambda: False, on_install=lambda: None)
            self.assertEqual(u.check()["state"], "up-to-date")
        with mock.patch.object(updater, "is_installed_build", return_value=True), \
             mock.patch.object(updater, "fetch_latest", side_effect=OSError("offline")):
            u = updater.Updater(is_busy=lambda: False, on_install=lambda: None)
            self.assertEqual(u.check()["state"], "error")

    def test_disabled_from_source(self):
        with mock.patch.object(updater, "is_installed_build", return_value=False):
            u = updater.Updater(is_busy=lambda: False, on_install=lambda: None)
            self.assertEqual(u.snapshot()["state"], "disabled")
            self.assertEqual(u.check()["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
