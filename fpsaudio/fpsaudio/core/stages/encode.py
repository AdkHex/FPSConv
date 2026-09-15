"""Quantise and encode stages.

§3.4: 32-bit float end to end, quantise **once**, dither only at that single
quantise.  :class:`QuantizeStage` is that one point; every stage before it works
in float.  The legacy build quantised to undithered 24-bit before the stretch
and again on encode, twice per job (B-9).

External encoders are fed **headerless PCM**.  A WAV header cannot describe more
than 4 GB and a 2-hour 7.1 24-bit 48 kHz track is 8.29 GB (B-8); a raw stream
has no ceiling.  The flags that describe the raw geometry are capability-checked
against each binary's own ``--help`` rather than assumed (B-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..adapters.encoders import RawFormat
from ..config import codec_info
from ..contracts import (
    Command,
    DitherMode,
    ProgressSink,
    Refusal,
    RefusalCode,
    StageResult,
)
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["EncodeStage", "QuantizeStage"]


@dataclass
class QuantizeStage(BaseStage):
    id: str = "quantize"
    title: str = "Quantise once, with dither"

    def _destination(self, ctx: RunContext):
        return ctx.temp("quantized.pcm")

    def bit_depth(self, ctx: RunContext) -> int:
        if ctx.expect_bit_exact:
            # On the bit-exact path the samples already sit exactly on the
            # source's integer grid; anything else would move them.
            return ctx.stream.bit_depth or 24
        if ctx.target_bit_depth:
            return ctx.target_bit_depth
        source_depth = ctx.stream.bit_depth or 24
        # Never coarser than the source, never finer than is meaningful: a
        # 16-bit source stays 16-bit, a 24-bit or float source becomes 24-bit.
        return 16 if source_depth <= 16 else 24

    def dither_mode(self, ctx: RunContext) -> DitherMode:
        """Dither is *wrong* on the bit-exact path.

        A redeclaration must not alter a single sample. The decoded float
        already lands exactly on the source's integer grid, so rounding without
        dither reproduces it byte-for-byte; adding dither would inject noise and
        break the PCM MD5 guarantee.
        """
        if ctx.expect_bit_exact:
            return DitherMode.NONE
        return ctx.spec.output.dither

    def describe(self, ctx: RunContext) -> str:
        depth = self.bit_depth(ctx)
        mode = self.dither_mode(ctx)
        if ctx.expect_bit_exact:
            return (
                f"Round float32 back to the source's {depth}-bit grid with NO dither, "
                f"so the samples are reproduced exactly and the PCM MD5 still matches."
            )
        if mode is DitherMode.NONE:
            return (
                f"Quantise float32 to {depth}-bit with NO dither (you turned it off). "
                f"Expect correlated quantisation distortion at low levels."
            )
        return (
            f"Quantise float32 to {depth}-bit exactly once, with {mode.value.upper()} "
            f"dither. This is the only quantisation in the whole chain."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return ()  # in-process

    def advance(self, ctx: RunContext) -> None:
        ctx.current = self._destination(ctx)
        ctx.target_bit_depth = self.bit_depth(ctx)

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        from ..dsp import quantize_to_raw

        depth = self.bit_depth(ctx)
        destination = self._destination(ctx)

        def on_progress(percent: float) -> None:
            ctx.emit(progress, self.id, percent, "quantising")

        report = quantize_to_raw(
            ctx.current,
            destination,
            bit_depth=depth,
            mode=self.dither_mode(ctx),
            progress=on_progress,
        )

        # The raw stream is headerless, so its geometry has to travel in the
        # context rather than in the file.
        ctx.current_rate = report["sample_rate"]
        ctx.current_channels = report["channels"]
        ctx.current_frames = report["frames"]
        ctx.current = destination
        ctx.target_bit_depth = depth
        ctx.artifacts["quantized"] = destination
        if report["clipped_samples"]:
            ctx.warn(
                f"{report['clipped_samples']} samples clipped at the {depth}-bit "
                f"ceiling. The source likely has intersample peaks above 0 dBFS."
            )
        return StageResult(
            stage_id=self.id, ok=True, outputs=(destination,), metrics=report,
            notes=(f"{report['frames']} frames to {depth}-bit, dither={report['dither']}",),
        )


# --------------------------------------------------------------------------- #
# Encode
# --------------------------------------------------------------------------- #

@dataclass
class EncodeStage(BaseStage):
    id: str = "encode"
    title: str = "Encode to the target format"

    def _raw(self, ctx: RunContext) -> RawFormat:
        # Before the retime has run, ``current_rate`` is still the source rate,
        # so describe against the planned output rate; afterwards the realised
        # rate is authoritative.
        rate = (
            ctx.current_rate
            if ctx.retimed
            else (ctx.target_rate or ctx.current_rate or ctx.stream.sample_rate)
        )
        return RawFormat(
            rate=int(rate or 48000),
            channels=int(ctx.current_channels or ctx.stream.channels or 2),
            bits=int(ctx.target_bit_depth or QuantizeStage().bit_depth(ctx)),
        )

    def describe(self, ctx: RunContext) -> str:
        info = codec_info(ctx.target_codec)
        raw = self._raw(ctx)
        head = (
            f"Encode to {info.label} ({raw.channels} ch, {raw.rate} Hz, "
            f"{raw.bits}-bit input) -> {ctx.output_path.name}"
        )
        bits = [head]
        if info.needs_bitrate:
            bits.append(f"at {ctx.spec.output.bitrate or 'the encoder default'}")
        if info.lossless:
            bits.append("losslessly")
        if info.notes:
            bits.append(f"({info.notes})")
        return " ".join(bits)

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        codec = ctx.target_codec
        raw = self._raw(ctx)
        registry = ctx.registry
        destination = ctx.output_path

        if codec == "flac":
            return (
                registry.flac.encode_command(
                    ctx.current, destination, compression=8, verify=True, raw=raw
                ),
            )
        if codec == "aac":
            return (
                registry.fdkaac.encode_command(
                    ctx.current,
                    destination,
                    bitrate=ctx.spec.output.bitrate,
                    channels=raw.channels,
                    raw=raw,
                ),
            )
        if codec == "opus":
            return (
                registry.opusenc.encode_command(
                    ctx.current, destination, bitrate=ctx.spec.output.bitrate, raw=raw
                ),
            )
        if codec == "wavpack":
            return (
                registry.wavpack.encode_command(
                    ctx.current, destination, high=True, verify=True, raw=raw
                ),
            )
        if codec == "pcm":
            return ()  # written in-process
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"No encoder is wired up for {codec}.",
        )

    def advance(self, ctx: RunContext) -> None:
        ctx.current = ctx.output_path
        ctx.artifacts["encoded"] = ctx.output_path

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        codec = ctx.target_codec
        if codec == "pcm":
            return self._write_pcm(ctx, progress)

        adapter = {
            "flac": ctx.registry.flac,
            "aac": ctx.registry.fdkaac,
            "opus": ctx.registry.opusenc,
            "wavpack": ctx.registry.wavpack,
        }[codec]

        command = self.commands(ctx)[0]
        ok, message = run_command(
            adapter,
            command,
            ctx,
            self.id,
            progress,
            duration_s=_retimed_duration(ctx),
        )
        if not ok:
            return self.failed(f"{codec} encode failed: {message}")

        notes: list[str] = []
        if codec == "flac" and ctx.registry.flac.detect().capabilities.has(
            "verify_after_encode"
        ):
            test_ok, test_msg = run_command(
                ctx.registry.flac,
                ctx.registry.flac.test_command(ctx.output_path),
                ctx,
                self.id,
                progress,
            )
            if not test_ok:
                return self.failed(f"flac --test failed on the output: {test_msg}")
            notes.append("flac --test passed on the encoded output")

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(ctx.output_path,),
            notes=tuple(notes),
            metrics={"codec": codec, "bitrate": ctx.spec.output.bitrate},
        )

    def _write_pcm(self, ctx: RunContext, progress: ProgressSink | None) -> StageResult:
        """PCM output is written in-process as RF64/W64 (no 4 GB ceiling).

        The PCM target skips :class:`QuantizeStage` entirely and quantises
        straight into the output file, so there is still exactly one
        quantisation in the chain.
        """
        from ..dsp import dither_and_quantize

        depth = ctx.target_bit_depth or ctx.stream.bit_depth or 24

        def on_progress(percent: float) -> None:
            ctx.emit(progress, self.id, percent, "writing PCM")

        report = dither_and_quantize(
            ctx.current,
            ctx.output_path,
            bit_depth=depth,
            mode=ctx.spec.output.dither,
            progress=on_progress,
        )
        if report["clipped_samples"]:
            ctx.warn(
                f"{report['clipped_samples']} samples clipped at the {depth}-bit ceiling."
            )
        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(ctx.output_path,),
            metrics={"codec": "pcm", **report},
        )


def _retimed_duration(ctx: RunContext) -> float | None:
    """Duration of the thing actually being written, not the source (B-20).

    The legacy progress denominator was always the input duration, so a retimed
    encode's progress bar was off by up to 4.3%.
    """
    if ctx.current_frames and ctx.current_rate:
        return ctx.current_frames / ctx.current_rate
    if ctx.stream.duration_s:
        return float(ctx.ratio.output_seconds(int(ctx.stream.duration_s)))
    return None
