"""Mutable run context threaded through a plan's stages.

Stages are pure with respect to *planning* — ``describe()`` and ``commands()``
never touch the filesystem — and mutate only this object while running.  That
split is what makes ``--dry-run`` total rather than approximate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from ..contracts import (
    AudioStream,
    JobSpec,
    MediaFile,
    ProgressEvent,
    ProgressSink,
)
from ..ratio import RetimeRatio

__all__ = ["RunContext"]


@dataclass
class RunContext:
    spec: JobSpec
    media: MediaFile
    stream: AudioStream
    ratio: RetimeRatio
    registry: Any

    #: Where intermediates live for this job.
    work_dir: Path = field(default_factory=Path)
    #: Final destination, already claimed atomically by the scheduler.
    output_path: Path = field(default_factory=Path)
    #: Bundle directory for the Dolby Media Encoder hand-off, when used.
    handoff_dir: Path | None = None

    # -- evolving state ---------------------------------------------------- #
    #: The file the next stage should read.  Starts as the source.
    current: Path = field(default_factory=Path)
    current_rate: int = 0
    current_frames: int | None = None
    current_channels: int | None = None
    #: Set once the retime has happened, so verify knows what to assert.
    retimed: bool = False

    target_codec: str = "flac"
    target_container: str = "flac"
    target_extension: str = "flac"
    target_rate: int | None = None
    target_bit_depth: int | None = None

    #: Bit-exact path bookkeeping: the PCM MD5 before the retime.
    source_pcm_md5: str | None = None
    expect_bit_exact: bool = False
    #: Bit depth the PCM MD5 is resolved to, so float intermediates and integer
    #: outputs are compared on the same grid.
    pcm_md5_bits: int | None = None

    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Path] = field(default_factory=dict)

    cancel: Callable[[], bool] | None = None
    dry_run: bool = False

    # -- helpers ----------------------------------------------------------- #

    def snapshot(self) -> "RunContext":
        """An independent copy, for simulating a plan without running it.

        ``--dry-run`` walks the stages on a snapshot, letting each project what
        it *would* produce, so the rendered commands name the real intermediate
        paths rather than the original source.
        """
        import copy

        clone = copy.copy(self)
        clone.notes = list(self.notes)
        clone.warnings = list(self.warnings)
        clone.metrics = dict(self.metrics)
        clone.artifacts = dict(self.artifacts)
        return clone

    @property
    def speed(self) -> Fraction:
        return self.ratio.speed

    def note(self, text: str) -> None:
        self.notes.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def temp(self, name: str) -> Path:
        return self.work_dir / name

    def is_cancelled(self) -> bool:
        return bool(self.cancel and self.cancel())

    def emit(
        self, progress: ProgressSink | None, stage_id: str, percent: float, message: str = ""
    ) -> None:
        if progress is None:
            return
        progress(
            ProgressEvent(
                job_id=self.spec.job_id,
                stage_id=stage_id,
                percent=max(0.0, min(100.0, percent)),
                message=message,
            )
        )
