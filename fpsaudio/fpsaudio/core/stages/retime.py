"""The retime stages: redeclare, resample, stretch.

This is where the central correction lives.  A frame-rate change is a speed
change, and a speed change is a **resample** — pitch moves with rate, exactly as
it does when film runs faster through a projector.

* :class:`RedeclareStage` implements §3.2's lossless retime, which exists in
  neither legacy program (A-12).  The PCM payload is copied verbatim and only
  the declared sample rate changes, so the PCM MD5 is unchanged.  Available
  whenever ``source_rate x speed`` lands on an integer.
* :class:`ResampleStage` is the default: libsoxr VHQ, driven by an exact integer
  rate pair derived from the :class:`fractions.Fraction`, so nothing is rounded
  on the way in.  The legacy code handed ``atempo=1001/1000`` to ffmpeg's option
  parser to evaluate as a C double, and truncated to ten decimal places for the
  SoX and Rubber Band engines (B-1).
* :class:`StretchStage` is Rubber Band, pitch-preserving, and **opt-in only**.
  It is the operation both legacy programs performed by default (A-2, B-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Sequence

from ..contracts import Command, ProgressEvent, ProgressSink, Refusal, RefusalCode, StageResult
from ..ratio import round_half_up
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["RedeclareStage", "ResampleStage", "StretchStage"]


# --------------------------------------------------------------------------- #
# Lossless retime — sample-rate redeclaration
# --------------------------------------------------------------------------- #

@dataclass
class RedeclareStage(BaseStage):
    id: str = "retime_lossless"
    title: str = "Retime by sample-rate redeclaration (bit-exact)"

    def _destination(self, ctx: RunContext):
        return ctx.temp(f"redeclared{ctx.current.suffix or '.w64'}")

    def new_rate(self, ctx: RunContext) -> int:
        rate = ctx.current_rate or ctx.stream.sample_rate or 0
        exact = ctx.ratio.redeclared_rate(rate)
        if exact.denominator != 1:
            raise Refusal(
                RefusalCode.REDECLARE_NOT_EXACT,
                f"A bit-exact retime needs {rate} Hz x {ctx.ratio.speed_str} to be a "
                f"whole number of hertz, but it is {exact.numerator}/{exact.denominator}.",
                remedies=[
                    "Use --method resample (the default) to do this properly with libsoxr.",
                    "Or pick a preset whose ratio divides evenly into this sample rate; "
                    "`fpsaudio explain --rate {rate}` lists them.".format(rate=rate),
                ],
                detail={"source_rate": rate, "exact_rate": str(exact)},
            )
        return int(exact)

    def describe(self, ctx: RunContext) -> str:
        rate = ctx.current_rate or ctx.stream.sample_rate or 0
        try:
            new_rate = self.new_rate(ctx)
        except Refusal as refusal:
            return f"NOT POSSIBLE: {refusal.message}"
        return (
            f"Redeclare {rate} Hz as {new_rate} Hz. The audio samples are copied "
            f"byte-for-byte; only the header changes, so the output's PCM MD5 will be "
            f"identical to the input's. This is a lossless retime."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        # Performed in-process via libsndfile, so there is no external command.
        return ()

    def advance(self, ctx: RunContext) -> None:
        ctx.current = self._destination(ctx)
        try:
            ctx.current_rate = self.new_rate(ctx)
        except Refusal:
            pass
        ctx.retimed = True
        ctx.expect_bit_exact = True

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        from ..audiofile import info, rewrite_sample_rate
        from ..dsp import pcm_md5_file

        new_rate = self.new_rate(ctx)
        destination = self._destination(ctx)
        # Hash on the source's integer grid so the later comparison against the
        # encoded output is representation-independent.
        depth = ctx.stream.bit_depth or 24
        ctx.pcm_md5_bits = depth
        before = pcm_md5_file(ctx.current, bit_depth=depth)
        ctx.emit(progress, self.id, 10.0, "redeclaring sample rate")

        frames = rewrite_sample_rate(ctx.current, destination, new_rate)
        after = pcm_md5_file(destination, bit_depth=depth)

        if before != after:
            return self.failed(
                "sample-rate redeclaration changed the PCM payload, which must never "
                f"happen (md5 {before} -> {after})"
            )

        meta = info(destination)
        ctx.current = destination
        ctx.current_rate = meta.samplerate
        ctx.current_frames = meta.frames
        ctx.retimed = True
        ctx.source_pcm_md5 = before
        ctx.expect_bit_exact = True
        ctx.artifacts["retimed"] = destination
        ctx.emit(progress, self.id, 100.0, "redeclared")

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            metrics={
                "new_sample_rate": new_rate,
                "frames": frames,
                "pcm_md5": before,
                "bit_exact": True,
            },
            notes=(
                f"{ctx.current_rate} Hz declared; {frames} frames unchanged; "
                f"PCM MD5 {before} preserved",
            ),
        )


# --------------------------------------------------------------------------- #
# Resample — the default
# --------------------------------------------------------------------------- #

@dataclass
class ResampleStage(BaseStage):
    id: str = "resample"
    title: str = "Retime by resampling (libsoxr VHQ)"

    def _destination(self, ctx: RunContext):
        return ctx.temp(f"retimed{ctx.current.suffix or '.w64'}")

    def target_rate(self, ctx: RunContext) -> int:
        return int(ctx.target_rate or ctx.current_rate or ctx.stream.sample_rate or 48000)

    def describe(self, ctx: RunContext) -> str:
        from ..dsp import integer_rate_pair

        in_rate = ctx.current_rate or ctx.stream.sample_rate or 48000
        out_rate = self.target_rate(ctx)
        soxr_in, soxr_out = integer_rate_pair(
            in_rate=in_rate, out_rate=out_rate, speed=ctx.speed
        )
        frames = ctx.current_frames or ctx.stream.sample_count
        expected = (
            ctx.ratio.output_samples(frames, src_rate=in_rate, dst_rate=out_rate)
            if frames
            else None
        )
        text = (
            f"Resample with libsoxr VHQ at the exact ratio {soxr_out}/{soxr_in} "
            f"(speed {ctx.ratio.speed_str}, {ctx.ratio.percent_approx():+.4f}%). "
            f"Pitch moves with speed, as a real film speed change does. "
            f"Output stays at {out_rate} Hz."
        )
        if expected is not None:
            text += f" Expect exactly {expected} frames out of {frames} in."
        return text

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return ()  # in-process

    def advance(self, ctx: RunContext) -> None:
        out_rate = self.target_rate(ctx)
        if ctx.current_frames and ctx.current_rate:
            ctx.current_frames = ctx.ratio.output_samples(
                ctx.current_frames, src_rate=ctx.current_rate, dst_rate=out_rate
            )
        ctx.current = self._destination(ctx)
        ctx.current_rate = out_rate
        ctx.retimed = True

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        from ..dsp import resample_file

        destination = self._destination(ctx)
        out_rate = self.target_rate(ctx)

        def on_progress(percent: float) -> None:
            ctx.emit(progress, self.id, percent, "resampling")

        report = resample_file(
            ctx.current,
            destination,
            speed=ctx.speed,
            out_rate=out_rate,
            progress=on_progress,
        )

        if not report.exact:
            return self.failed(
                f"resample produced {report.output_frames} frames where the exact "
                f"rational demands {report.expected_frames}"
            )

        ctx.current = destination
        ctx.current_rate = report.out_rate
        ctx.current_frames = report.output_frames
        ctx.retimed = True
        ctx.artifacts["retimed"] = destination
        if report.trimmed or report.padded:
            ctx.note(
                f"resampler output adjusted to the exact frame count "
                f"(trimmed {report.trimmed}, padded {report.padded})"
            )

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            metrics=report.to_dict(),
            notes=(
                f"{report.input_frames} -> {report.output_frames} frames at "
                f"{report.out_rate} Hz, exact ratio "
                f"{report.soxr_out_rate}/{report.soxr_in_rate}",
            ),
        )


# --------------------------------------------------------------------------- #
# Stretch — opt-in only
# --------------------------------------------------------------------------- #

@dataclass
class StretchStage(BaseStage):
    id: str = "stretch"
    title: str = "Retime by pitch-preserving stretch (Rubber Band) [opt-in]"

    def _destination(self, ctx: RunContext):
        return ctx.temp("stretched.wav")

    def describe(self, ctx: RunContext) -> str:
        scale = Fraction(1) / ctx.speed
        return (
            f"Time-stretch by {scale.numerator}/{scale.denominator} with Rubber Band, "
            f"keeping the original pitch. This is NOT what happens when film runs at a "
            f"different speed — you have opted into it explicitly. Expect phase-vocoder "
            f"artefacts on transients."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return (
            ctx.registry.rubberband.stretch_command(
                ctx.current, self._destination(ctx), speed=ctx.speed
            ),
        )

    def advance(self, ctx: RunContext) -> None:
        if ctx.current_frames and ctx.current_rate:
            ctx.current_frames = ctx.ratio.output_samples(
                ctx.current_frames, src_rate=ctx.current_rate
            )
        ctx.current = self._destination(ctx)
        ctx.retimed = True

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        from ..audiofile import info

        destination = self._destination(ctx)
        command = self.commands(ctx)[0]
        ok, message = run_command(
            ctx.registry.rubberband,
            command,
            ctx,
            self.id,
            progress,
            duration_s=ctx.stream.duration_s,
        )
        if not ok:
            return self.failed(f"Rubber Band stretch failed: {message}")

        meta = info(destination)
        expected = ctx.ratio.output_samples(
            ctx.current_frames or meta.frames,
            src_rate=ctx.current_rate or meta.samplerate,
            dst_rate=meta.samplerate,
        )
        drift = meta.frames - expected

        ctx.current = destination
        ctx.current_rate = meta.samplerate
        ctx.current_frames = meta.frames
        ctx.retimed = True
        ctx.artifacts["retimed"] = destination
        if drift:
            ctx.warn(
                f"Rubber Band produced {meta.frames} frames where the exact ratio "
                f"demands {expected} ({drift:+d}). A stretcher cannot hit an exact "
                f"rational length; use --method resample if sample-exactness matters."
            )

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            metrics={
                "frames": meta.frames,
                "expected_frames": expected,
                "frame_drift": drift,
                "pitch_preserved": True,
            },
            notes=(f"stretched to {meta.frames} frames (exact target {expected})",),
        )
