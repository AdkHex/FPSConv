"""Queue, persistence, resume and concurrency.

The scheduler is ported from ``Fps Converter Batch Mode/FPS Converter/app.py:460-547``
— its submit-as-slots-free ``ThreadPoolExecutor`` over files, with
``wait(..., return_when=FIRST_COMPLETED, timeout=0.2)``, was genuinely good
design (B-C) — and then given the things it lacked:

* **Separate encoder concurrency (§5).**  The legacy build had one limit for
  everything.  Decode and resample are memory- and CPU-bound in different ways
  from an encoder, so they get their own semaphore.
* **Persistence and real resume (B-15).**  The legacy queue lived in
  ``self.jobs`` in memory and vanished on exit; its only resume-like behaviour
  was ``overwrite_policy="skip"``, which skipped on *filename existence* — so a
  half-written output from a killed run counted as complete.  Here a job is
  complete only when a **content-hashed completion marker** says so, and that
  marker is written after verification passes.
* **A defined ownership model (B-21).**  Worker threads never mutate shared
  job records.  They return an immutable outcome; the scheduler thread is the
  only writer.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .contracts import (
    JobSpec,
    ProgressEvent,
    ProgressSink,
    Refusal,
    StageResult,
)
from .plan import Plan, build_plan

__all__ = [
    "JobOutcome",
    "JobRecord",
    "JobState",
    "QueueStore",
    "Scheduler",
    "auto_workers",
    "run_plan",
]


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    REFUSED = "refused"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Immutable result handed back from a worker thread.

    Workers never write to shared state; the scheduler thread applies this.
    """

    job_id: str
    state: JobState
    output: Path | None = None
    error: str | None = None
    refusal: dict[str, Any] | None = None
    stages: tuple[dict[str, Any], ...] = ()
    verification: dict[str, Any] | None = None
    duration_s: float = 0.0
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class JobRecord:
    spec: JobSpec
    state: JobState = JobState.PENDING
    progress: float = 0.0
    message: str = ""
    output: Path | None = None
    error: str | None = None
    refusal: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    duration_s: float = 0.0

    @property
    def job_id(self) -> str:
        return self.spec.job_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "progress": self.progress,
            "message": self.message,
            "output": str(self.output) if self.output else None,
            "error": self.error,
            "refusal": self.refusal,
            "verification": self.verification,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "duration_s": self.duration_s,
            "content_key": self.spec.content_key(),
            "spec": self.spec.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(
            spec=JobSpec.from_dict(data["spec"]),
            state=JobState(data.get("state", "pending")),
            progress=float(data.get("progress", 0.0)),
            message=data.get("message", ""),
            output=Path(data["output"]) if data.get("output") else None,
            error=data.get("error"),
            refusal=data.get("refusal"),
            verification=data.get("verification"),
            notes=tuple(data.get("notes", ())),
            warnings=tuple(data.get("warnings", ())),
            duration_s=float(data.get("duration_s", 0.0)),
        )


def auto_workers(kind: str = "files") -> int:
    """Default concurrency.

    Files get up to 4 workers, matching the legacy default; encoders get half
    that, because an encoder saturates a core in a way a demux does not.
    """
    cpu = os.cpu_count() or 2
    if kind == "encoders":
        return max(1, min(4, cpu // 2))
    return max(1, min(4, cpu))


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

class QueueStore:
    """Durable queue state plus content-hashed completion markers.

    A job is resumable-complete only if a marker exists whose name is the
    **content hash of the entire JobSpec**.  Change the preset, the codec, the
    bitrate or the output template and the hash changes, so the job correctly
    runs again — unlike the legacy filename-existence check, which treated any
    file with the right name as finished (B-15).
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.markers = directory / "completed"
        self.state_file = directory / "queue.json"
        self._lock = threading.Lock()

    def ensure(self) -> None:
        self.markers.mkdir(parents=True, exist_ok=True)

    # -- completion markers ------------------------------------------------ #

    def marker_path(self, spec: JobSpec) -> Path:
        return self.markers / f"{spec.content_key()}.json"

    def is_complete(self, spec: JobSpec) -> tuple[bool, str]:
        marker = self.marker_path(spec)
        if not marker.exists():
            return False, "no completion marker"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, "completion marker is unreadable"
        output = payload.get("output")
        if not output:
            return False, "completion marker names no output"
        path = Path(output)
        if not path.exists():
            return False, f"recorded output {path.name} no longer exists"
        recorded_size = payload.get("size_bytes")
        if recorded_size is not None and path.stat().st_size != recorded_size:
            return False, (
                f"recorded output {path.name} changed size since it was written "
                f"({recorded_size} -> {path.stat().st_size}); treating as incomplete"
            )
        return True, f"completed earlier, verified, output {path.name}"

    def mark_complete(self, spec: JobSpec, outcome: JobOutcome) -> None:
        self.ensure()
        marker = self.marker_path(spec)
        payload = {
            "job_id": spec.job_id,
            "content_key": spec.content_key(),
            "source": str(spec.source),
            "output": str(outcome.output) if outcome.output else None,
            "size_bytes": (
                outcome.output.stat().st_size
                if outcome.output and outcome.output.exists()
                else None
            ),
            "verification": outcome.verification,
            "completed_at": time.time(),
        }
        _atomic_write(marker, json.dumps(payload, indent=2))

    def clear(self, spec: JobSpec) -> None:
        marker = self.marker_path(spec)
        if marker.exists():
            marker.unlink()

    # -- queue snapshot ---------------------------------------------------- #

    def save(self, records: Sequence[JobRecord]) -> None:
        with self._lock:
            self.ensure()
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "jobs": [record.to_dict() for record in records],
            }
            _atomic_write(self.state_file, json.dumps(payload, indent=2))

    def load(self) -> list[JobRecord]:
        if not self.state_file.exists():
            return []
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        records: list[JobRecord] = []
        for entry in payload.get("jobs", ()):
            try:
                records.append(JobRecord.from_dict(entry))
            except (KeyError, ValueError, Refusal):
                continue
        return records


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a kill cannot truncate the state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)


# --------------------------------------------------------------------------- #
# Running one plan
# --------------------------------------------------------------------------- #

def run_plan(
    plan: Plan,
    *,
    progress: ProgressSink | None = None,
    cancel: Callable[[], bool] | None = None,
    keep_work_dir: bool = False,
) -> JobOutcome:
    """Execute every stage of a plan in order, reporting scaled progress."""
    started = time.monotonic()
    if plan.skip_reason:
        return JobOutcome(
            plan.job_id,
            JobState.SKIPPED,
            output=plan.output_path,
            notes=(plan.skip_reason,),
        )
    ctx = plan.ctx
    ctx.cancel = cancel
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    ctx.output_path.parent.mkdir(parents=True, exist_ok=True)

    stage_results: list[StageResult] = []
    total = len(plan.stages) or 1

    try:
        for index, stage in enumerate(plan.stages):
            if cancel and cancel():
                return JobOutcome(
                    plan.job_id, JobState.CANCELLED, error="cancelled before " + stage.id
                )

            start = index / total * 100.0
            end = (index + 1) / total * 100.0

            def scaled(event: ProgressEvent, _s: float = start, _e: float = end) -> None:
                if progress is None:
                    return
                progress(
                    ProgressEvent(
                        job_id=event.job_id,
                        stage_id=event.stage_id,
                        percent=_s + (_e - _s) * (event.percent / 100.0),
                        message=event.message or stage.title,
                        detail=event.detail,
                    )
                )

            result = stage.run(ctx, scaled)
            stage_results.append(result)
            if not result.ok:
                return JobOutcome(
                    plan.job_id,
                    JobState.FAILED,
                    output=ctx.output_path if ctx.output_path.exists() else None,
                    error=result.error or f"{stage.id} failed",
                    stages=tuple(r.to_dict() for r in stage_results),
                    duration_s=time.monotonic() - started,
                    notes=tuple(ctx.notes),
                    warnings=tuple(ctx.warnings),
                )

        verification = ctx.metrics.get("verification")
        return JobOutcome(
            plan.job_id,
            JobState.DONE,
            output=ctx.output_path,
            stages=tuple(r.to_dict() for r in stage_results),
            verification=verification,
            duration_s=time.monotonic() - started,
            notes=tuple(ctx.notes),
            warnings=tuple(ctx.warnings),
        )

    except Refusal as refusal:
        return JobOutcome(
            plan.job_id,
            JobState.REFUSED,
            error=refusal.message,
            refusal=refusal.to_dict(),
            duration_s=time.monotonic() - started,
        )
    except Exception as exc:  # noqa: BLE001 - a worker must never take the run down
        return JobOutcome(
            plan.job_id,
            JobState.FAILED,
            error=f"{type(exc).__name__}: {exc}",
            stages=tuple(r.to_dict() for r in stage_results),
            duration_s=time.monotonic() - started,
        )
    finally:
        if not keep_work_dir:
            shutil.rmtree(ctx.work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The scheduler
# --------------------------------------------------------------------------- #

@dataclass
class Scheduler:
    """Runs a batch of specs, with resume, persistence and two concurrency limits."""

    store: QueueStore
    file_workers: int = 0
    encoder_workers: int = 0
    keep_work_dir: bool = False
    work_root: Path | None = None
    on_event: Callable[[str, dict[str, Any]], None] | None = None

    _records: dict[str, JobRecord] = field(default_factory=dict, init=False)
    _order: list[str] = field(default_factory=list, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _encoder_slots: threading.Semaphore | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    #: Output path -> job_id, so an in-batch collision is caught rather than
    #: letting two jobs write the same file (B-12's second half).
    _reserved: dict[Path, str] = field(default_factory=dict, init=False)
    _reserve_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.file_workers = self.file_workers or auto_workers("files")
        self.encoder_workers = self.encoder_workers or auto_workers("encoders")
        self._encoder_slots = threading.Semaphore(self.encoder_workers)

    # -- queue management -------------------------------------------------- #

    def add(self, specs: Iterable[JobSpec]) -> list[JobRecord]:
        added: list[JobRecord] = []
        for spec in specs:
            record = JobRecord(spec=spec)
            self._records[record.job_id] = record
            self._order.append(record.job_id)
            added.append(record)
        return added

    @property
    def records(self) -> list[JobRecord]:
        return [self._records[job_id] for job_id in self._order]

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(kind, payload)

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        *,
        probe: Callable[[Path], Any],
        resume: bool = True,
        dry_run: bool = False,
    ) -> list[JobRecord]:
        records = self.records
        pending: list[JobRecord] = []

        for record in records:
            if resume:
                complete, reason = self.store.is_complete(record.spec)
                if complete:
                    record.state = JobState.SKIPPED
                    record.progress = 100.0
                    record.message = reason
                    self._emit("job", {"job_id": record.job_id, "state": record.state.value})
                    continue
            pending.append(record)

        if not pending:
            self.store.save(self.records)
            return self.records

        workers = max(1, min(self.file_workers, len(pending)))
        self._emit(
            "batch_start",
            {"total": len(pending), "workers": workers, "encoder_slots": self.encoder_workers},
        )

        submitted = completed = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fpsaudio") as pool:
            active: dict[Future, JobRecord] = {}

            # Submit-as-slots-free, ported from app.py:474-500.
            while active or submitted < len(pending):
                while (
                    not self._stop.is_set()
                    and submitted < len(pending)
                    and len(active) < workers
                ):
                    record = pending[submitted]
                    submitted += 1
                    record.state = JobState.RUNNING
                    record.progress = 0.0
                    self._emit("job", {"job_id": record.job_id, "state": "running"})
                    active[pool.submit(self._run_one, record.spec, probe, dry_run)] = record

                self._emit(
                    "batch_progress",
                    {
                        "completed": completed,
                        "total": len(pending),
                        "active": len(active),
                        "queued": max(0, len(pending) - submitted),
                    },
                )

                if not active:
                    break

                finished, _ = wait(list(active), timeout=0.2, return_when=FIRST_COMPLETED)
                if not finished:
                    continue

                for future in finished:
                    record = active.pop(future)
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001
                        outcome = JobOutcome(
                            record.job_id, JobState.FAILED, error=f"{type(exc).__name__}: {exc}"
                        )
                    completed += 1
                    self._apply(record, outcome)
                    self.store.save(self.records)

        self.store.save(self.records)
        self._emit("batch_done", self.summary())
        return self.records

    def _apply(self, record: JobRecord, outcome: JobOutcome) -> None:
        """Only the scheduler thread mutates records (fixes B-21's undefined model)."""
        with self._lock:
            record.state = outcome.state
            record.output = outcome.output
            record.error = outcome.error
            record.refusal = outcome.refusal
            record.verification = outcome.verification
            record.notes = outcome.notes
            record.warnings = outcome.warnings
            record.duration_s = outcome.duration_s
            record.progress = 100.0 if outcome.state is JobState.DONE else record.progress
        if outcome.state is JobState.DONE:
            self.store.mark_complete(record.spec, outcome)
        self._emit(
            "job",
            {
                "job_id": record.job_id,
                "state": record.state.value,
                "error": record.error,
                "output": str(record.output) if record.output else None,
            },
        )

    def _run_one(
        self, spec: JobSpec, probe: Callable[[Path], Any], dry_run: bool
    ) -> JobOutcome:
        try:
            media = probe(spec.source)
            work_root = self.work_root or (spec.output.directory / ".fpsaudio-work")
            # Plan without claiming, so the in-batch collision check below can
            # produce a useful diagnostic before any file is created.
            plan = build_plan(
                spec, media, work_dir=work_root / spec.short_id, claim_output=False
            )
        except Refusal as refusal:
            return JobOutcome(
                spec.job_id, JobState.REFUSED, error=refusal.message,
                refusal=refusal.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001
            return JobOutcome(spec.job_id, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")

        if dry_run:
            return JobOutcome(
                spec.job_id, JobState.SKIPPED, output=plan.output_path,
                notes=("dry run: nothing was written",) + plan.notes,
                warnings=plan.warnings,
            )

        # Two different sources can render to the same output name — most easily
        # two files called audio.mkv in different folders, which is precisely the
        # case the legacy build silently collapsed into one file (B-12). Catch it
        # and say so, rather than producing fewer outputs than jobs.
        with self._reserve_lock:
            owner = self._reserved.get(plan.output_path)
            if owner is not None and owner != spec.job_id:
                return JobOutcome(
                    spec.job_id,
                    JobState.REFUSED,
                    output=plan.output_path,
                    error=(
                        f"another job in this batch already writes to "
                        f"{plan.output_path.name}; both would end up in one file"
                    ),
                    refusal={
                        "code": "output_path_collision",
                        "message": (
                            f"Two sources map to the same output name: "
                            f"{plan.output_path.name}"
                        ),
                        "remedies": [
                            "Add {parent} to the output template so sibling folders "
                            "stay distinct, e.g. --template '{parent}__{stem}__a{index}__{profile}'.",
                            "Or use --rename to number the collisions.",
                        ],
                        "override_token": None,
                        "detail": {"output": str(plan.output_path)},
                    },
                )
            self._reserved[plan.output_path] = spec.job_id

        # Only now can "the output already exists" mean what it says: no other
        # job in this batch claimed this path, so the file is from an earlier run.
        if plan.skip_reason:
            return JobOutcome(
                spec.job_id, JobState.SKIPPED, output=plan.output_path,
                notes=(plan.skip_reason,),
            )

        # Now take the path for real. O_EXCL makes this atomic against anything
        # outside this batch too.
        try:
            from .naming import claim as claim_path

            plan.ctx.output_path = claim_path(
                plan.output_path, overwrite=spec.output.overwrite
            )
        except Refusal as refusal:
            return JobOutcome(
                spec.job_id, JobState.REFUSED, error=refusal.message,
                refusal=refusal.to_dict(),
            )

        def on_progress(event: ProgressEvent) -> None:
            record = self._records.get(event.job_id)
            if record is not None:
                record.progress = event.percent
                record.message = event.message
            self._emit(
                "progress",
                {
                    "job_id": event.job_id,
                    "stage": event.stage_id,
                    "percent": event.percent,
                    "message": event.message,
                },
            )

        # The encoder semaphore is held only for the stages that need it, so a
        # decode never blocks behind another job's encode.
        return _run_with_encoder_slot(
            plan,
            self._encoder_slots,
            progress=on_progress,
            cancel=self._stop.is_set,
            keep_work_dir=self.keep_work_dir,
        )

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        return {"total": len(self.records), **counts}


def _run_with_encoder_slot(
    plan: Plan,
    slots: threading.Semaphore | None,
    *,
    progress: ProgressSink | None,
    cancel: Callable[[], bool] | None,
    keep_work_dir: bool,
) -> JobOutcome:
    """Hold an encoder slot only while an encode stage is actually running.

    Acquiring around the whole plan would serialise decodes behind other jobs'
    encodes, which is the opposite of what §5's split limits are for.  So only
    the CPU-saturating stages are wrapped.
    """
    if slots is None:
        return run_plan(plan, progress=progress, cancel=cancel, keep_work_dir=keep_work_dir)

    gated = tuple(
        _GatedStage(stage, slots) if stage.id in ("encode", "quantize") else stage
        for stage in plan.stages
    )
    return run_plan(
        replace(plan, stages=gated),
        progress=progress,
        cancel=cancel,
        keep_work_dir=keep_work_dir,
    )


@dataclass
class _GatedStage:
    """Wraps a stage so it holds an encoder slot while it runs."""

    inner: Any
    slots: threading.Semaphore

    @property
    def id(self) -> str:
        return self.inner.id

    @property
    def title(self) -> str:
        return self.inner.title

    def describe(self, ctx: Any) -> str:
        return self.inner.describe(ctx)

    def commands(self, ctx: Any) -> Sequence[Any]:
        return self.inner.commands(ctx)

    def run(self, ctx: Any, progress: ProgressSink | None = None) -> StageResult:
        self.slots.acquire()
        try:
            return self.inner.run(ctx, progress)
        finally:
            self.slots.release()
