"""Decode stage — source to a float32 RF64/W64 intermediate.

Two things happen here that happen nowhere in the legacy build:

* **``-drc_scale 0``.**  §3.3 names dynamic-range compression baked in at decode
  as "the single most common quality bug in DD+ conversion".  ``-drc_scale``
  appears nowhere in either legacy program (A-10, B-18), so every AC-3/E-AC-3
  job they ever ran carried DRC and dialnorm gain into the output.
* **Float32, not ``pcm_s24le``.**  The legacy HQ engines hardcoded 24-bit
  integer intermediates, requantising without dither before the stretch and
  again on encode (B-9).  Here the signal stays in float until the single
  dithered quantise at the end.

The intermediate container is W64 or RF64, never plain WAV: a 2-hour 7.1 24-bit
48 kHz track is 8.29 GB and would overflow the RIFF header (B-8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..contracts import Command, ProgressSink, StageResult
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["DecodeStage"]


@dataclass
class DecodeStage(BaseStage):
    id: str = "decode"
    title: str = "Decode to float32"

    def _destination(self, ctx: RunContext):
        suffix = ctx.registry.ffmpeg.intermediate_suffix()
        return ctx.temp(f"decoded{suffix}")

    def describe(self, ctx: RunContext) -> str:
        drc = ctx.spec.decode.drc_scale
        parts = [
            f"Decode {ctx.stream.codec} to 32-bit float "
            f"({ctx.stream.channels or '?'} ch, {ctx.stream.sample_rate or '?'} Hz)"
        ]
        if ctx.stream.codec in ("ac3", "eac3", "truehd"):
            parts.append(
                f"with -drc_scale {drc:g} so no dynamic-range compression is baked in"
            )
            if ctx.spec.decode.ignore_dialnorm and ctx.stream.codec in ("ac3", "eac3"):
                parts.append("and no dialnorm target-level gain")
        suffix = ".w64/.rf64"
        parts.append(f"into a {suffix} intermediate that can exceed 4 GB")
        return ", ".join(parts) + "."

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return (
            ctx.registry.ffmpeg.decode_command(
                ctx.media.path,
                ctx.stream.stream_index,
                self._destination(ctx),
                codec=ctx.stream.codec,
                sample_format="pcm_f32le",
                drc_scale=ctx.spec.decode.drc_scale,
                ignore_dialnorm=ctx.spec.decode.ignore_dialnorm,
            ),
        )

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        destination = self._destination(ctx)
        command = self.commands(ctx)[0]
        ok, message = run_command(
            ctx.registry.ffmpeg,
            command,
            ctx,
            self.id,
            progress,
            duration_s=ctx.stream.duration_s,
        )
        if not ok:
            return self.failed(f"decode failed: {message}")

        from ..audiofile import info

        meta = info(destination)

        # ffmpeg was asked for pcm_f32le. If the intermediate does not read back
        # as float, the writer and the reader disagree about the header and every
        # sample would be a reinterpreted bit pattern — silent, total corruption.
        # ffmpeg's W64 muxer does exactly this for >=3 channels, which is why
        # RF64 is preferred; this check makes any recurrence loud instead.
        if not meta.subtype.upper().startswith(("FLOAT", "DOUBLE")):
            return self.failed(
                f"the float32 intermediate {destination.name} reads back as "
                f"{meta.subtype}, so ffmpeg and libsndfile disagree about its header. "
                f"Refusing to continue: every sample would be misinterpreted. "
                f"Run `fpsaudio doctor --verbose` and report the ffmpeg build."
            )

        ctx.current = destination
        ctx.current_rate = meta.samplerate
        ctx.current_frames = meta.frames
        ctx.current_channels = meta.channels
        ctx.artifacts["decoded"] = destination

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            metrics={
                "frames": meta.frames,
                "sample_rate": meta.samplerate,
                "channels": meta.channels,
            },
            notes=(
                f"decoded {meta.frames} frames at {meta.samplerate} Hz, "
                f"{meta.channels} ch, float32",
            ),
        )

    def advance(self, ctx: RunContext) -> None:
        ctx.current = self._destination(ctx)
        ctx.current_rate = ctx.stream.sample_rate or ctx.current_rate
        ctx.current_frames = ctx.stream.sample_count
        ctx.current_channels = ctx.stream.channels
