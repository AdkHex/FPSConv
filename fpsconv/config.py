"""Persistent settings and job history.

Stored per user, outside the app folder, so an upgrade never wipes them:

* Windows : %APPDATA%\\FPSConv\\settings.json / history.json
* macOS   : ~/Library/Application Support/FPSConv/
* Linux   : ~/.config/FPSConv/
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "out_dir": "",
    "conv_type": "23.976-25",
    "mode": "single",          # single | batch
    "workers": 2,
    "bitrate": 0,              # 0 = use the source bitrate (fps.py behaviour)
    "overwrite": "overwrite",  # overwrite | skip | rename
    "auto_update": True,       # install new releases automatically when the queue is idle
    "native_window": True,     # open in a desktop window (pywebview) instead of the browser
    "task": "fps",             # fps | encode
    "encode": {                # the audio-only encode task
        "target": "ddp",       # ddp | dd
        "channels": 0,         # 0 = same as source, else 1 / 2 / 6 / 8
        "bitrate": 0,          # 0 = DEE default for the layout
        "atmos": True,         # keep Atmos when the source has it (TrueHD Atmos -> DDP Atmos)
        "drc": "film_light",
    },
    "tools": {"ffmpeg": "", "ffprobe": "", "deew_python": "",
              "mediainfo": "", "deezy": "", "deezy_python": "", "truehdd": ""},
}

_NESTED = ("tools", "encode")

_lock = threading.Lock()


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "FPSConv"


def _read(name: str, default: Any) -> Any:
    path = config_dir() / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(name: str, payload: Any) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{name}.tmp"
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, directory / name)


def load_settings() -> dict[str, Any]:
    with _lock:
        data = _read("settings.json", {})
    merged = json.loads(json.dumps(DEFAULTS))
    if isinstance(data, dict):
        for key, value in data.items():
            if key in _NESTED and isinstance(value, dict):
                merged[key].update(_clean(key, value))
            elif key in merged and key not in _NESTED:
                merged[key] = value
    return merged


def _clean(section: str, value: dict) -> dict:
    """Only known keys of a nested section, coerced to the type of the default."""
    out = {}
    for k, v in value.items():
        if k not in DEFAULTS[section]:
            continue
        default = DEFAULTS[section][k]
        if isinstance(default, bool):
            out[k] = bool(v)
        elif isinstance(default, int):
            try:
                out[k] = int(v or 0)
            except (TypeError, ValueError):
                out[k] = default
        else:
            out[k] = str(v or "").strip()
    return out


def save_settings(update: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    for key, value in update.items():
        if key in _NESTED and isinstance(value, dict):
            current[key].update(_clean(key, value))
        elif key in DEFAULTS and key not in _NESTED:
            current[key] = value
    with _lock:
        _write("settings.json", current)
    return current


def load_history() -> list[dict[str, Any]]:
    with _lock:
        data = _read("history.json", [])
    return data if isinstance(data, list) else []


def save_history(entries: list[dict[str, Any]], keep: int = 200) -> None:
    with _lock:
        _write("history.json", entries[-keep:])
