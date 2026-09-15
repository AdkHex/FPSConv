"""Common machinery for stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..adapters import BinaryAdapter
from ..contracts import Command, ProgressSink, StageResult
from .context import RunContext

__all__ = ["BaseStage", "run_command"]


@dataclass
class BaseStage:
    id: str = "stage"
    title: str = "Stage"

    def describe(self, ctx: RunContext) -> str:
        return self.title

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return ()

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        raise NotImplementedError

    def advance(self, ctx: RunContext) -> None:
        """Project this stage's effect onto a *simulated* context.

        Used only by ``--dry-run`` and the TUI's plan screen, so that a stage's
        described inputs are the intermediates the previous stage would really
        have produced.  Never called during a real run — ``run()`` mutates the
        context for that.
        """
        return None

    # -- helpers ----------------------------------------------------------- #

    def skipped(self, reason: str) -> StageResult:
        return StageResult(stage_id=self.id, ok=True, skipped=True, notes=(reason,))

    def failed(self, error: str) -> StageResult:
        return StageResult(stage_id=self.id, ok=False, error=error)


def run_command(
    adapter: BinaryAdapter,
    command: Command,
    ctx: RunContext,
    stage_id: str,
    progress: ProgressSink | None,
    *,
    duration_s: float | None = None,
    start: float = 0.0,
    end: float = 100.0,
) -> tuple[bool, str]:
    """Run one command, translating its output into progress events."""
    from ..adapters import scale_progress

    state: dict[str, object] = {}
    if duration_s:
        state["duration_s"] = duration_s

    def on_stdout(line: str) -> None:
        percent = adapter.parse_progress(line, state)
        if percent is not None:
            ctx.emit(progress, stage_id, scale_progress(percent, start, end), command.purpose)

    def on_stderr(line: str) -> None:
        percent = adapter.parse_progress(line, state)
        if percent is not None:
            ctx.emit(progress, stage_id, scale_progress(percent, start, end), command.purpose)

    outcome = adapter.run_streaming(
        command, on_stdout=on_stdout, on_stderr=on_stderr, cancel=ctx.cancel
    )
    ctx.emit(progress, stage_id, end, command.purpose)
    return outcome.ok, outcome.message
