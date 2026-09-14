"""Job queue: runs jobs on a small worker pool, keeps state for the GUI,
and remembers finished jobs across restarts (history.json)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import config, engine
from . import log as applog
from .engine import Job

LOG = applog.get("queue")

TERMINAL = ("done", "failed", "cancelled", "skipped")


class JobQueue:
    def __init__(self, workers: int = 1, work_root: str = ".temp_jobs", history: bool = True) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._workers = max(1, workers)
        self._work_root = work_root
        self._version = 0
        self._history = history
        if history:
            for entry in config.load_history():
                try:
                    job = Job.from_dict(entry)
                except Exception:  # noqa: BLE001 - a bad history line is not fatal
                    continue
                if job.state in TERMINAL:
                    self._jobs[job.id] = job
                    self._order.append(job.id)

    # -- state ------------------------------------------------------------ #

    def _touch(self, _payload: dict | None = None) -> None:
        with self._lock:
            self._version += 1

    def _persist(self) -> None:
        if not self._history:
            return
        with self._lock:
            entries = [self._jobs[i].to_dict() for i in self._order if self._jobs[i].state in TERMINAL]
        for e in entries:
            e["log"] = e["log"][-20:]
        config.save_history(entries)

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [self._jobs[i].to_dict() for i in self._order]
            counts: dict[str, int] = {}
            for j in jobs:
                counts[j["state"]] = counts.get(j["state"], 0) + 1
            return {"version": self._version, "jobs": jobs, "counts": counts,
                    "workers": self._workers}

    def set_workers(self, n: int) -> None:
        self._workers = max(1, min(8, int(n)))
        if self._pool is not None:
            self._pool._max_workers = self._workers  # takes effect for new submits

    # -- control ---------------------------------------------------------- #

    def add(self, items: list[dict], conv_type: str, out_dir: str, *,
            bitrate: int = 0, overwrite: str = "overwrite") -> list[Job]:
        """``items`` are ``{"path": ..., "stream_index": ..., "conv_type": ...}``."""
        added: list[Job] = []
        with self._lock:
            for item in items:
                src = os.path.abspath(item["path"])
                stream = int(item.get("stream_index", 0))
                mode = item.get("conv_type") or conv_type
                dup = next(
                    (self._jobs[i] for i in self._order
                     if self._jobs[i].source == src and self._jobs[i].conv_type == mode
                     and self._jobs[i].stream_index == stream
                     and self._jobs[i].state in ("queued", "running")),
                    None,
                )
                if dup:
                    continue
                job = Job(source=src, conv_type=mode, out_dir=os.path.abspath(out_dir),
                          stream_index=stream, bitrate_override=int(bitrate or 0),
                          overwrite=overwrite)
                self._jobs[job.id] = job
                self._order.append(job.id)
                added.append(job)
            self._version += 1
        if added:
            LOG.info("queued %d job(s) → %s", len(added), os.path.abspath(out_dir))
        self._submit(added)
        return added

    def _submit(self, jobs: list[Job]) -> None:
        if not jobs:
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="fpsconv")
        for job in jobs:
            self._pool.submit(self._run, job)

    def _run(self, job: Job) -> None:
        if job._cancel.is_set():
            job.state = "cancelled"
            job.error = "cancelled before start"
            self._touch()
            self._persist()
            return
        engine.convert(job, notify=self._touch, work_root=self._work_root)
        self._touch()
        self._persist()

    def retry(self, job_id: str) -> Optional[Job]:
        with self._lock:
            old = self._jobs.get(job_id)
            if old is None or old.state not in TERMINAL:
                return None
            job = Job(source=old.source, conv_type=old.conv_type, out_dir=old.out_dir,
                      stream_index=old.stream_index, bitrate_override=old.bitrate_override,
                      overwrite=old.overwrite)
            pos = self._order.index(job_id)
            self._order[pos] = job.id
            del self._jobs[job_id]
            self._jobs[job.id] = job
            self._version += 1
        self._submit([job])
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.state == "queued":
            job.cancel()
            job.state = "cancelled"
            job.error = "cancelled before start"
            LOG.info("cancelled before start: %s", os.path.basename(job.source))
        elif job.state == "running":
            LOG.info("cancelling: %s", os.path.basename(job.source))
            job.cancel()
        else:
            return False
        self._touch()
        return True

    def cancel_all(self) -> int:
        with self._lock:
            ids = list(self._order)
        return sum(1 for i in ids if self.cancel(i))

    def clear_finished(self) -> int:
        with self._lock:
            keep = [i for i in self._order if self._jobs[i].state in ("queued", "running")]
            removed = len(self._order) - len(keep)
            self._order = keep
            self._jobs = {i: self._jobs[i] for i in keep}
            self._version += 1
        self._persist()
        return removed

    def remove(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state in ("queued", "running"):
                return False
            self._order.remove(job_id)
            del self._jobs[job_id]
            self._version += 1
        self._persist()
        return True

    def busy(self) -> bool:
        with self._lock:
            return any(self._jobs[i].state in ("queued", "running") for i in self._order)
