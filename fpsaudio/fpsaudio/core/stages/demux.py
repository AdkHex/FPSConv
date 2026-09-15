"""Demux stage — lift one elementary stream out of a container, bit-exactly.

Only used when a later stage needs the raw elementary stream (the Atmos path
feeds ``truehdd`` a ``.thd``), or when the source is Matroska and mkvextract is
available, because mkvextract preserves the ``CodecDelay`` that ffmpeg's
``-c copy`` can drop.

Every fallback is logged.  When mkvextract or tsMuxeR is absent the stage uses
ffmpeg ``-c copy`` and records in the run notes that it did — the two are not
equivalent for §3.6's delay handling, and pretending otherwise is how sync
errors become invisible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..contracts import Command, ProgressSink, StageResult
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["DemuxStage", "ELEMENTARY_SUFFIX"]

#: Elementary-stream suffix per codec, for tools that sniff by extension.
ELEMENTARY_SUFFIX: dict[str, str] = {
    "truehd": ".thd",
    "eac3": ".ec3",
    "ac3": ".ac3",
    "dts": ".dts",
    "flac": ".flac",
    "aac": ".aac",
    "opus": ".opus",
    "pcm": ".wav",
}


@dataclass
class DemuxStage(BaseStage):
    id: str = "demux"
    title: str = "Demux elementary stream"

    def _destination(self, ctx: RunContext):
        suffix = ELEMENTARY_SUFFIX.get(ctx.stream.codec, ".bin")
        return ctx.temp(f"stream_{ctx.stream.stream_index}{suffix}")

    def _use_mkvextract(self, ctx: RunContext) -> bool:
        return (
            "matroska" in (ctx.media.container or "").lower()
            and ctx.registry.mkvextract.detect().found
        )

    def describe(self, ctx: RunContext) -> str:
        tool = "mkvextract" if self._use_mkvextract(ctx) else "ffmpeg -c copy"
        return (
            f"Copy audio stream #{ctx.stream.stream_index} ({ctx.stream.codec}) out of "
            f"{ctx.media.path.name} with {tool}. No decode, no re-encode, no video."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        destination = self._destination(ctx)
        if self._use_mkvextract(ctx):
            return (
                ctx.registry.mkvextract.extract_command(
                    ctx.media.path, ctx.stream.stream_index, destination
                ),
            )
        return (
            ctx.registry.ffmpeg.demux_copy_command(
                ctx.media.path, ctx.stream.stream_index, destination
            ),
        )

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        destination = self._destination(ctx)
        use_mkv = self._use_mkvextract(ctx)
        adapter = ctx.registry.mkvextract if use_mkv else ctx.registry.ffmpeg
        command = self.commands(ctx)[0]

        if not use_mkv and "matroska" in (ctx.media.container or "").lower():
            ctx.warn(
                "MKVToolNix is not installed, so the stream was copied with ffmpeg "
                "instead of mkvextract. Any container-level CodecDelay may not have "
                "been carried across; check the sync report."
            )

        ok, message = run_command(
            adapter, command, ctx, self.id, progress, duration_s=ctx.stream.duration_s
        )
        if not ok:
            return self.failed(f"demux failed: {message}")

        ctx.current = destination
        ctx.artifacts["elementary"] = destination
        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            notes=(f"extracted with {'mkvextract' if use_mkv else 'ffmpeg -c copy'}",),
        )

    def advance(self, ctx: RunContext) -> None:
        ctx.current = self._destination(ctx)
