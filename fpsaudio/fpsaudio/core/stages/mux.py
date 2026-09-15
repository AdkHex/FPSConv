"""Mux stage — wrap the encoded track, carrying the rescaled delay.

§3.6: delays and sync offsets have to move with the audio.  A source with a
+42 ms audio delay retimed 23.976 -> 25 needs that delay to become
``42 x 960/1001 = 40.28 ms``; leaving it at 42 ms puts the track 1.7 ms out
before a single sample has been heard.  Neither legacy program read
``start_time``, MKV ``CodecDelay`` or chapters at all (B-19).

Chapters are rescaled by the same exact rational.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

from ..contracts import Chapter, Command, ProgressSink, StageResult
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["MuxStage", "rescale_chapters", "rescaled_delay_ms"]


def rescaled_delay_ms(ctx: RunContext) -> float | None:
    """The source delay, moved by the same exact ratio as the audio."""
    delay_s = ctx.stream.start_time_s
    if delay_s is None and ctx.stream.codec_delay_ns is not None and ctx.stream.sample_rate:
        delay_s = ctx.stream.codec_delay_ns / 1e9
    if not delay_s:
        return None
    exact = ctx.ratio.scale_timestamp(Fraction(delay_s).limit_denominator(10**9))
    return float(exact) * 1000.0


def rescale_chapters(chapters: Sequence[Chapter], ctx: RunContext) -> tuple[Chapter, ...]:
    return tuple(
        Chapter(
            index=chapter.index,
            start_s=float(ctx.ratio.scale_timestamp(
                Fraction(chapter.start_s).limit_denominator(10**9)
            )),
            end_s=float(ctx.ratio.scale_timestamp(
                Fraction(chapter.end_s).limit_denominator(10**9)
            )),
            title=chapter.title,
        )
        for chapter in chapters
    )


@dataclass
class MuxStage(BaseStage):
    id: str = "mux"
    title: str = "Mux into a container"

    def describe(self, ctx: RunContext) -> str:
        delay = rescaled_delay_ms(ctx)
        text = f"Mux the encoded track into {ctx.output_path.name} with mkvmerge"
        if delay is not None:
            original = (ctx.stream.start_time_s or 0.0) * 1000.0
            text += (
                f", applying the rescaled delay {delay:.3f} ms "
                f"(source {original:.3f} ms x {ctx.ratio.duration_scale.numerator}/"
                f"{ctx.ratio.duration_scale.denominator})"
            )
        else:
            text += " (source declares no delay)"
        if ctx.media.chapters:
            text += f"; {len(ctx.media.chapters)} chapters rescaled by the same ratio"
        return text + "."

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        source = ctx.artifacts.get("encoded", ctx.current)
        return (
            ctx.registry.mkvmerge.mux_command(
                ctx.output_path,
                source,
                delay_ms=rescaled_delay_ms(ctx),
                language=ctx.stream.language,
                title=ctx.stream.title,
                default_track=ctx.stream.default,
            ),
        )

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        command = self.commands(ctx)[0]
        ok, message = run_command(
            ctx.registry.mkvmerge, command, ctx, self.id, progress
        )
        if not ok:
            return self.failed(f"mux failed: {message}")

        chapters = rescale_chapters(ctx.media.chapters, ctx)
        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(ctx.output_path,),
            metrics={
                "delay_ms": rescaled_delay_ms(ctx),
                "chapters_rescaled": len(chapters),
            },
        )
