"""Auto-update from GitHub Releases (Windows installed build only).

Every push to ``main`` makes the release workflow publish:

* ``FPSConv-Setup-<version>.exe`` — the Inno Setup installer
* ``latest.json`` — ``{"version", "url", "sha256", "size", "notes", "published_at"}``

The app fetches ``latest.json`` through the stable
``releases/latest/download/`` URL (no API rate limit), compares the version
with its own, downloads the installer, verifies the SHA-256, and runs it
silently.  The installer closes the app, replaces the files and relaunches it —
the same experience as the Tauri updater in GDExplorer / RsKV.

Nothing here runs unless the app is the frozen Windows build; from source the
UI just reports that updates apply to the installed version.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import GITHUB_OWNER, GITHUB_REPO, __version__, config

LATEST_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest/download/latest.json"
CHECK_INTERVAL_S = 6 * 3600
USER_AGENT = f"FPSConv/{__version__}"


def is_installed_build() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest(url: str = LATEST_URL, timeout: float = 15, attempts: int = 3) -> dict:
    """GitHub's download redirector occasionally answers 5xx; try a few times."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def download(url: str, dest: Path, sha256: str, progress: Optional[Callable[[float], None]] = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total * 100)
    if sha256 and digest.hexdigest().lower() != sha256.lower():
        tmp.unlink(missing_ok=True)
        raise ValueError("downloaded installer failed its SHA-256 check")
    os.replace(tmp, dest)
    return dest


def launch_installer(path: Path) -> None:
    """Run the Inno Setup installer silently; it closes and relaunches the app."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    subprocess.Popen(
        [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
         "/RESTARTAPPLICATIONS", "/LOG=" + str(config.config_dir() / "update-install.log")],
        creationflags=flags, close_fds=True,
    )


class Updater:
    """Background checker with a small state machine the GUI can poll.

    state: disabled | idle | checking | available | downloading | ready | installing | error | up-to-date
    """

    def __init__(self, is_busy: Callable[[], bool], on_install: Callable[[], None]) -> None:
        self._busy = is_busy
        self._on_install = on_install
        self._lock = threading.Lock()
        self.state = "idle" if is_installed_build() else "disabled"
        self.current = __version__
        self.latest: dict = {}
        self.error = ""
        self.progress = 0.0
        self.checked_at = 0.0
        self.installer: Optional[Path] = None
        self._thread: Optional[threading.Thread] = None

    # -- state ------------------------------------------------------------ #

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state, "current": self.current,
                "latest": self.latest.get("version", ""), "notes": self.latest.get("notes", ""),
                "error": self.error, "progress": round(self.progress, 1),
                "checked_at": self.checked_at, "installed_build": is_installed_build(),
                "auto": bool(config.load_settings().get("auto_update", True)),
            }

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    # -- actions ---------------------------------------------------------- #

    def start(self) -> None:
        if self.state == "disabled":
            return
        self._thread = threading.Thread(target=self._loop, name="fpsconv-updater", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        time.sleep(5)
        while True:
            try:
                self.check()
                if self.state == "available":
                    self.download_update()
                if self.state == "ready" and config.load_settings().get("auto_update", True):
                    # Wait for the queue to drain, then install without asking.
                    while self._busy():
                        time.sleep(10)
                    self.install()
                    return
            except Exception as exc:  # noqa: BLE001 - never take the app down
                self._set(state="error", error=str(exc))
            time.sleep(CHECK_INTERVAL_S)

    def check(self) -> dict:
        if self.state == "disabled":
            return self.snapshot()
        self._set(state="checking", error="")
        try:
            latest = fetch_latest()
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", error=f"could not reach GitHub: {exc}", checked_at=time.time())
            return self.snapshot()
        self._set(latest=latest, checked_at=time.time())
        if is_newer(latest.get("version", ""), self.current):
            if self.installer and self.installer.exists() and self.installer.name.endswith(f"{latest['version']}.exe"):
                self._set(state="ready")
            else:
                self._set(state="available")
        else:
            self._set(state="up-to-date")
        return self.snapshot()

    def download_update(self) -> dict:
        latest = self.latest
        if not latest.get("url"):
            self._set(state="error", error="release has no installer URL")
            return self.snapshot()
        self._set(state="downloading", progress=0.0)
        dest = config.config_dir() / "updates" / f"FPSConv-Setup-{latest['version']}.exe"
        try:
            download(latest["url"], dest, latest.get("sha256", ""),
                     progress=lambda p: self._set(progress=p))
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", error=f"download failed: {exc}")
            return self.snapshot()
        # Remove older downloaded installers.
        for old in dest.parent.glob("FPSConv-Setup-*.exe"):
            if old != dest:
                old.unlink(missing_ok=True)
        self._set(state="ready", installer=dest, progress=100.0)
        return self.snapshot()

    def install(self) -> dict:
        if self.state != "ready" or not self.installer:
            return self.snapshot()
        self._set(state="installing")
        try:
            launch_installer(self.installer)
        except Exception as exc:  # noqa: BLE001
            self._set(state="error", error=f"could not start installer: {exc}")
            return self.snapshot()
        # Give the installer a moment to start, then stop the server so the
        # files can be replaced. The installer relaunches the app afterwards.
        threading.Timer(1.5, self._on_install).start()
        return self.snapshot()
