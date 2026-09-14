"""Application log: a rotating file plus an in-memory ring the GUI can read.

* file  : <config dir>/logs/fpsconv.log (1 MB × 5)
* memory: last 3000 records, each with a sequence number so the GUI can poll
  ``/api/logs?since=N`` cheaply.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from pathlib import Path

from . import config

_ring: deque = deque(maxlen=3000)
_seq = 0
_lock = threading.Lock()
_configured = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001
            msg = record.getMessage()
        with _lock:
            _seq += 1
            _ring.append({"seq": _seq, "t": record.created, "level": record.levelname,
                          "logger": record.name.replace("fpsconv.", ""), "msg": msg})


def setup(level: int = logging.INFO) -> Path:
    """Install handlers once; returns the log file path."""
    global _configured
    root = logging.getLogger("fpsconv")
    log_dir = config.config_dir() / "logs"
    path = log_dir / "fpsconv.log"
    if _configured:
        return path
    root.setLevel(level)
    root.propagate = False
    ring = _RingHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ring)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(fh)
    except OSError:
        pass
    _configured = True
    return path


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"fpsconv.{name}")


def records(since: int = 0, limit: int = 500) -> dict:
    with _lock:
        items = [r for r in _ring if r["seq"] > since]
    return {"seq": _seq, "records": items[-limit:], "dropped": max(0, len(items) - limit)}


def stamp(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))
