"""The planner: ``JobSpec`` + ``MediaFile`` -> an ordered list of stages.

Everything that can be refused is refused *here*, before a byte moves, so
``--dry-run`` shows the same refusals a real run would hit.  That is the whole
point of the split: the legacy build discovered its problems mid-ffmpeg, as an
error message about a filtergraph (A-3), or did not discover them at all and
silently produced the wrong thing (B-3, B-7).

The refusals implemented here, each with the finding it corresponds to:

* DTS / DTS-HD MA / DTS:X — out of scope by decision; detected and refused,
  never silently transcoded.
* DD / DD+ / TrueHD *encode* — no DEE, no DME CLI.  TrueHD gets a hand-off
  instead of a bare refusal.
* E-AC-3 JOC (DD+ Atmos) source — no JOC decoder exists outside Dolby's tools,
  so this refuses and offers the TrueHD track from the same file, or a typed
  confirmation to flatten (§3.5).
* Lossless -> lossy — requires a typed confirmation (§3.3).  This is the exact
  path by which the legacy build turned TrueHD 7.1 into 128 kbps AAC (B-3, B-4).
* Channel counts the installed encoder does not confirm it supports — refused,
  never silently downmixed.
* Container/codec pairs ffmpeg would reject later (B-11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .config import CODECS, codec_info, resolve_codec, resolve_container
from .contracts import (
    AtmosPolicy,
    AudioStream,
    Command,
    JobSpec,
    MediaFile,
    Refusal,
    RefusalCode,
    RetimeMethod,
    Stage,
)
from .naming import build_name, resolve_output
from .ratio import RetimeRatio, resolve_ratio
from .stages import (
    AtmosDecodeStage,
    DecodeStage,
    DemuxStage,
    EncodeStage,
    HandoffStage,
    MuxStage,
    QuantizeStage,
    RedeclareStage,
    ResampleStage,
    RunContext,
    StretchStage,
    VerifyStage,
)

__all__ = ["Plan", "build_plan", "ratio_for"]


@dataclass
class Plan:
    """A fully-resolved, explainable unit of work."""

    spec: JobSpec
    ctx: RunContext
    stages: tuple[Stage, ...]
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Set when the overwrite policy says this job should not run at all.
    #: A plan with a skip_reason must never execute its stages — the legacy
    #: build resolved "skip" and then wrote the file anyway.
    skip_reason: str | None = None

    @property
    def job_id(self) -> str:
        return self.spec.job_id

    @property
    def output_path(self) -> Path:
        return self.ctx.output_path

    def _simulate(self):
        """Walk the stages on a *copy* of the context.

        Each stage projects what it would produce before the next one is asked
        to describe itself, so a dry run names the real intermediate paths and
        the real post-retime sample rate rather than the original source's.
        """
        ctx = self.ctx.snapshot()
        for stage in self.stages:
            yield stage, ctx
            try:
                stage.advance(ctx)
            except Exception:  # noqa: BLE001 - projection is best-effort
                pass

    def commands(self) -> list[Command]:
        """Every external command this plan would run.

        Never raises.  A dry run on a machine that is missing a tool must still
        explain the whole plan — that is the point of a dry run — so an
        unresolvable stage is reported through :meth:`unresolved` instead of
        aborting the description.
        """
        out: list[Command] = []
        for stage, ctx in self._simulate():
            try:
                out.extend(stage.commands(ctx))
            except Exception:  # noqa: BLE001
                continue
        return out

    def unresolved(self) -> list[tuple[str, str]]:
        """Stages whose commands cannot be built yet, and why."""
        problems: list[tuple[str, str]] = []
        for stage, ctx in self._simulate():
            try:
                stage.commands(ctx)
            except Refusal as refusal:
                problems.append((stage.id, refusal.message))
            except Exception as exc:  # noqa: BLE001
                problems.append((stage.id, f"{type(exc).__name__}: {exc}"))
        return problems

    def describe(self) -> str:
        """The "what will happen" panel — the TUI shows this before anything runs."""
        stream = self.ctx.stream
        lines = [
            f"Source   : {self.spec.source}",
            f"Stream   : {stream.label}",
            f"Retime   : {self.ctx.ratio.label}   speed {self.ctx.ratio.speed_str} "
            f"({self.ctx.ratio.percent_approx():+.4f}%)",
            f"Method   : {self.spec.retime.method.value}",
            f"Output   : {self.ctx.output_path}",
            f"Format   : {codec_info(self.ctx.target_codec).label} "
            f"in .{self.ctx.target_extension}",
            "",
            "Steps:",
        ]
        for index, (stage, ctx) in enumerate(self._simulate(), 1):
            lines.append(f"  {index}. {stage.title}")
            lines.append(f"     {stage.describe(ctx)}")
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"  - {note}" for note in self.notes)
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  ! {warn}" for warn in self.warnings)
        return "\n".join(lines)

    def render_commands(self) -> str:
        lines = [
            f"  [{c.adapter}] {c.purpose}\n    {c.rendered()}" for c in self.commands()
        ]
        for stage_id, reason in self.unresolved():
            lines.append(f"  [{stage_id}] command not resolvable yet: {reason}")
        if not lines:
            return "  (every step runs in-process; no external commands)"
        return "\n".join(lines)


def ratio_for(spec: JobSpec) -> RetimeRatio:
    return resolve_ratio(
        src_fps=spec.retime.src_fps,
        dst_fps=spec.retime.dst_fps,
        ratio=spec.retime.ratio_override,
    )


# --------------------------------------------------------------------------- #
# The planner
# --------------------------------------------------------------------------- #

def build_plan(
    spec: JobSpec,
    media: MediaFile,
    *,
    registry: Any = None,
    work_dir: Path | None = None,
    claim_output: bool = False,
) -> Plan:
    from .adapters import get_registry

    reg = registry if registry is not None else get_registry()

    if not media.identified:
        raise Refusal(
            RefusalCode.UNIDENTIFIED_SOURCE,
            f"No prober could identify {media.path.name}.",
            remedies=[
                *(f"{p}" for p in media.problems),
                "Install MediaInfo and ffmpeg, then run `fpsaudio doctor`.",
            ],
        )

    stream = media.stream(spec.stream_index)
    ratio = ratio_for(spec)
    notes: list[str] = []
    warnings: list[str] = []

    _refuse_dts(stream)
    _check_atmos(spec, media, stream, notes, warnings)

    #: In hand-off mode the deliverable is not an encoded file at all: it is a
    #: retimed RF64/W64 essence plus the retimed object metadata and a recipe,
    #: because Dolby Media Encoder is GUI-only and cannot be driven from here.
    atmos_handoff = (
        spec.atmos_policy is AtmosPolicy.HANDOFF
        and stream.atmos.present
        and stream.atmos.kind == "truehd_atmos"
    )

    if atmos_handoff:
        target_codec = "pcm"
        container, extension = "w64", "w64"
        notes.append(
            "Hand-off mode: the output is a 32-bit float RF64/W64 essence, not an "
            "encoded TrueHD file. TrueHD encoding cannot be automated."
        )
    else:
        target_codec = resolve_codec(
            spec.output.codec, stream.codec, source_lossless=stream.lossless
        )
        _refuse_lossless_to_lossy(spec, stream, target_codec)
        container, extension = resolve_container(spec.output.container, target_codec)
        _check_channels(spec, stream, target_codec, reg)

    method = _choose_method(spec, stream, ratio, target_codec, notes, warnings)

    ctx = RunContext(
        spec=spec,
        media=media,
        stream=stream,
        ratio=ratio,
        registry=reg,
        work_dir=work_dir or Path.cwd() / ".fpsaudio-work" / spec.short_id,
        current=media.path,
        current_rate=stream.sample_rate or 0,
        current_frames=stream.sample_count,
        current_channels=stream.channels,
        target_codec=target_codec,
        target_container=container,
        target_extension=extension,
        target_rate=spec.retime.target_sample_rate,
        target_bit_depth=spec.output.bit_depth,
        expect_bit_exact=method is RetimeMethod.REDECLARE,
    )
    ctx.metrics["source_frames"] = stream.sample_count
    ctx.metrics["source_rate"] = stream.sample_rate

    # Pin the rate the output will actually carry, so every stage can describe
    # itself accurately before anything has run.
    if method is RetimeMethod.REDECLARE and stream.sample_rate:
        ctx.target_rate = int(ratio.redeclared_rate(stream.sample_rate))
    elif ctx.target_rate is None:
        ctx.target_rate = stream.sample_rate

    ctx.output_path, skip_reason = _resolve_output_path(
        spec, stream, ctx, method, claim=claim_output
    )
    if atmos_handoff:
        bundle = ctx.output_path.parent / f"{ctx.output_path.stem}_DME"
        ctx.handoff_dir = bundle
        ctx.output_path = bundle / f"{ctx.output_path.stem}_essence.w64"

    stages = _assemble_stages(spec, ctx, method, notes, warnings, handoff=atmos_handoff)

    ctx.notes.extend(notes)
    ctx.warnings.extend(warnings)
    return Plan(
        spec=spec,
        ctx=ctx,
        stages=tuple(stages),
        notes=tuple(notes),
        warnings=tuple(warnings),
        skip_reason=skip_reason,
    )


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #

def _refuse_dts(stream: AudioStream) -> None:
    if stream.codec != "dts":
        return
    label = stream.commercial_name or stream.profile or "DTS"
    raise Refusal(
        RefusalCode.DTS_OUT_OF_SCOPE,
        f"{label} is out of scope for this toolchain, by design. It will not be "
        f"silently transcoded to something else.",
        remedies=[
            "Extract the DTS track with eac3to or MakeMKV and retime it in a tool "
            "that licenses a DTS decoder.",
            "If this file also carries a TrueHD, DD+ or PCM track, retime that one "
            "instead — run `fpsaudio inspect` to list them.",
        ],
        detail={"codec": stream.codec, "profile": stream.profile},
    )


def _check_atmos(
    spec: JobSpec,
    media: MediaFile,
    stream: AudioStream,
    notes: list[str],
    warnings: list[str],
) -> None:
    atmos = stream.atmos

    # E-AC-3 JOC: no JOC decoder exists outside Dolby's own tools.
    if atmos.present and atmos.kind == "eac3_joc":
        if spec.accepts("flatten-atmos"):
            warnings.append(
                "DD+ Atmos (JOC) source: you accepted flattening. The object bed will "
                "be discarded and only the 5.1 core is retimed."
            )
            return
        alternatives = [
            s for s in media.audio
            if s.stream_index != stream.stream_index and s.codec == "truehd"
        ]
        remedies = []
        if alternatives:
            remedies.append(
                "This file also has a TrueHD track that can carry Atmos: "
                + ", ".join(f"stream #{s.stream_index}" for s in alternatives)
                + ". Retime that one instead."
            )
        remedies.append(
            "Or accept the loss explicitly: the 5.1 core will be retimed and every "
            "Atmos object discarded."
        )
        raise Refusal(
            RefusalCode.JOC_DECODER_MISSING,
            "This is a Dolby Digital Plus Atmos (E-AC-3 JOC) track. No JOC decoder "
            "exists outside Dolby's own tools, so decoding it here would keep the 5.1 "
            "core and silently throw the object bed away.",
            remedies=remedies,
            override_token="flatten-atmos",
            detail={"atmos": atmos.to_dict()},
        )

    # TrueHD Atmos: honour the policy.
    if atmos.present and atmos.kind == "truehd_atmos":
        if spec.atmos_policy is AtmosPolicy.HANDOFF:
            notes.append(
                "TrueHD Atmos: objects will be decoded with truehdd, retimed, and "
                "written as a Dolby Media Encoder hand-off bundle."
            )
            return
        if spec.atmos_policy is AtmosPolicy.FLATTEN:
            if not spec.accepts("flatten-atmos"):
                raise Refusal(
                    RefusalCode.ATMOS_WOULD_FLATTEN,
                    "Flattening a TrueHD Atmos track discards every dynamic object and "
                    "keeps only the channel bed. That is not reversible.",
                    remedies=[
                        "Use --atmos-policy handoff to keep the objects and finish in "
                        "Dolby Media Encoder.",
                    ],
                    override_token="flatten-atmos",
                    detail={"atmos": atmos.to_dict()},
                )
            warnings.append(
                f"TrueHD Atmos: you accepted flattening. "
                f"{atmos.objects or 'All'} dynamic objects will be discarded."
            )
            return
        raise Refusal(
            RefusalCode.ATMOS_WOULD_FLATTEN,
            "This is a Dolby TrueHD Atmos track. Converting it to a channel format "
            "would discard the object bed.",
            remedies=[
                "--atmos-policy handoff  keeps the objects: fpsaudio retimes the "
                "essence and the object metadata and hands off to Dolby Media Encoder.",
                "--atmos-policy flatten   discards the objects (requires confirmation).",
            ],
            override_token="flatten-atmos",
            detail={"atmos": atmos.to_dict()},
        )

    # The prober could not tell us. Never guess on an object-capable codec.
    if atmos.certainty == "unknown" and stream.codec in ("eac3", "truehd"):
        if spec.accepts("unverified-atmos"):
            warnings.append(
                f"Atmos presence in this {stream.codec} track was never verified — "
                f"MediaInfo was not available. You accepted the risk; if the source "
                f"does carry objects, they are being discarded right now."
            )
            return
        raise Refusal(
            RefusalCode.ATMOS_WOULD_FLATTEN,
            f"Whether this {stream.codec} track carries Dolby Atmos could not be "
            f"determined: {'; '.join(atmos.evidence) or 'no evidence available'}. "
            f"Proceeding might silently discard an object bed.",
            remedies=[
                "Install MediaInfo — it is the only prober here that reports JOC and "
                "Atmos object counts. Then re-run.",
                "Or accept the risk explicitly.",
            ],
            override_token="unverified-atmos",
            detail={"codec": stream.codec, "certainty": atmos.certainty},
        )


def _refuse_lossless_to_lossy(
    spec: JobSpec, stream: AudioStream, target_codec: str
) -> None:
    target = codec_info(target_codec)
    if not stream.lossless or target.lossless:
        return
    if spec.accepts("lossless-to-lossy"):
        return
    label = stream.commercial_name or stream.profile or CODECS.get(
        stream.codec, codec_info("flac")
    ).label
    raise Refusal(
        RefusalCode.LOSSLESS_TO_LOSSY,
        f"The source is lossless ({label}) and the target is lossy "
        f"({target.label}). That is a one-way loss of quality.",
        remedies=[
            "--codec flac or --codec wavpack keeps it lossless.",
            f"Or confirm you want the lossy encode.",
        ],
        override_token="lossless-to-lossy",
        detail={"source_codec": stream.codec, "target_codec": target_codec},
    )


def _check_channels(
    spec: JobSpec, stream: AudioStream, target_codec: str, registry: Any
) -> None:
    channels = stream.channels
    if not channels:
        return
    info = codec_info(target_codec)
    if info.max_channels and channels > info.max_channels:
        raise Refusal(
            RefusalCode.CHANNEL_COUNT_UNSUPPORTED,
            f"{info.label} cannot carry {channels} channels "
            f"(its ceiling is {info.max_channels}).",
            remedies=[
                "Choose a format that can: flac, wavpack and pcm all handle 7.1.",
                "Or downmix explicitly — fpsaudio will not do it behind your back.",
            ],
        )
    # fdkaac's 7.1 support is the open question from Part 5 of the plan.
    if target_codec == "aac" and channels > 6:
        adapter = registry.get("fdkaac")
        if adapter is not None and adapter.detect().found:
            if not adapter.supports_channels(channels) and not spec.accepts(
                "downmix-to-5.1"
            ):
                raise Refusal(
                    RefusalCode.CHANNEL_COUNT_UNSUPPORTED,
                    f"The installed fdkaac does not advertise {channels}-channel AAC, "
                    f"so encoding would either fail or silently downmix.",
                    remedies=[
                        "Use --codec flac / wavpack / opus, all of which handle 7.1.",
                        "Install an fdkaac build with 7.1 support, then re-run "
                        "`fpsaudio doctor` to confirm.",
                        "Or accept an explicit downmix to 5.1 (this discards two "
                        "channels permanently).",
                    ],
                    override_token="downmix-to-5.1",
                    detail={"channels": channels},
                )


# --------------------------------------------------------------------------- #
# Method selection
# --------------------------------------------------------------------------- #

def _choose_method(
    spec: JobSpec,
    stream: AudioStream,
    ratio: RetimeRatio,
    target_codec: str,
    notes: list[str],
    warnings: list[str],
) -> RetimeMethod:
    requested = spec.retime.method

    if ratio.is_identity:
        notes.append("No retime requested; this is a format conversion only.")
        return RetimeMethod.NONE

    if requested is RetimeMethod.STRETCH:
        warnings.append(
            "You chose the pitch-preserving stretch. This is NOT what happens when "
            "film runs at a different speed — a real speed change moves pitch with "
            "rate. Both legacy converters did this by default, which is why they "
            "sounded wrong."
        )
        return RetimeMethod.STRETCH

    if requested is RetimeMethod.REDECLARE:
        rate = stream.sample_rate or 0
        if not rate:
            raise Refusal(
                RefusalCode.REDECLARE_NOT_EXACT,
                "The source sample rate is unknown, so a bit-exact redeclaration "
                "cannot be checked for exactness.",
                remedies=["Use --method resample."],
            )
        if not ratio.redeclare_is_exact(rate):
            exact = ratio.redeclared_rate(rate)
            raise Refusal(
                RefusalCode.REDECLARE_NOT_EXACT,
                f"A bit-exact retime would need {rate} Hz to become "
                f"{exact.numerator}/{exact.denominator} Hz, which is not a whole "
                f"number of hertz.",
                remedies=[
                    "Use --method resample (the default) — libsoxr VHQ, exact frame "
                    "count, transparent below -140 dBFS.",
                    f"Or run `fpsaudio explain --rate {rate}` to see which presets do "
                    f"admit a bit-exact retime at this rate.",
                ],
                detail={"source_rate": rate, "would_be": str(exact)},
            )
        new_rate = int(ratio.redeclared_rate(rate))
        info = codec_info(target_codec)
        if target_codec == "opus" and new_rate != 48000:
            raise Refusal(
                RefusalCode.UNSUPPORTED_OPERATION,
                f"A bit-exact retime here produces {new_rate} Hz, but Opus only stores "
                f"48 kHz, so the encoder would resample and the bit-exactness would be "
                f"lost anyway.",
                remedies=[
                    "Use --codec flac / wavpack / pcm to keep the redeclared rate.",
                    "Or use --method resample with --codec opus.",
                ],
            )
        notes.append(
            f"Bit-exact retime: {rate} Hz will be redeclared as {new_rate} Hz. "
            f"Not one sample changes; the PCM MD5 is asserted to be identical."
        )
        if not info.lossless:
            warnings.append(
                f"The retime is bit-exact, but the output codec ({info.label}) is "
                f"lossy, so the encode will still discard information."
            )
        return RetimeMethod.REDECLARE

    return RetimeMethod.RESAMPLE


# --------------------------------------------------------------------------- #
# Output naming
# --------------------------------------------------------------------------- #

def _resolve_output_path(
    spec: JobSpec,
    stream: AudioStream,
    ctx: RunContext,
    method: RetimeMethod,
    *,
    claim: bool,
) -> tuple[Path, str | None]:
    from .naming import claim as claim_path

    rate = ctx.target_rate or stream.sample_rate
    if method is RetimeMethod.REDECLARE and stream.sample_rate:
        rate = int(ctx.ratio.redeclared_rate(stream.sample_rate))

    stem = build_name(
        template=spec.output.template,
        source=spec.source,
        stream=stream,
        profile_key=spec.profile_key if spec.profile_key != "custom" else ctx.ratio.key,
        codec=ctx.target_codec,
        method=method.value,
        sample_rate=rate,
        bit_depth=spec.output.bit_depth,
        extra={
            "srcfps": _fps_text(ctx.ratio.src_fps),
            "dstfps": _fps_text(ctx.ratio.dst_fps),
        },
    )
    result = resolve_output(
        directory=spec.output.directory,
        stem=stem,
        extension=ctx.target_extension,
        overwrite=spec.output.overwrite,
    )
    if result.action == "fail":
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"{result.path} already exists and the overwrite policy is 'fail'.",
            remedies=["Use --overwrite, --rename or --skip-existing."],
        )
    if result.action == "skip":
        # Honour the policy. The legacy build computed "skip" and then wrote the
        # file regardless, so --skip-existing silently overwrote (B-15).
        return result.path, result.reason or "output already exists"
    if claim:
        return claim_path(result.path, overwrite=spec.output.overwrite), None
    return result.path, None


def _fps_text(value: Any) -> str:
    from .ratio import format_fps

    return format_fps(value)


# --------------------------------------------------------------------------- #
# Stage assembly
# --------------------------------------------------------------------------- #

def _assemble_stages(
    spec: JobSpec,
    ctx: RunContext,
    method: RetimeMethod,
    notes: list[str],
    warnings: list[str],
    *,
    handoff: bool = False,
) -> list[Stage]:
    stages: list[Stage] = []

    # Opus stores only 48 kHz. Target it directly rather than resampling once
    # here and letting the encoder resample again.
    if (
        method is RetimeMethod.RESAMPLE
        and ctx.target_codec == "opus"
        and (ctx.target_rate or ctx.stream.sample_rate) != 48000
    ):
        ctx.target_rate = 48000
        notes.append(
            "Opus stores only 48 kHz, so the resample targets 48 kHz directly rather "
            "than letting the encoder resample a second time."
        )

    if handoff:
        # truehdd needs the elementary stream, so demux before decoding.
        stages.append(DemuxStage())
        stages.append(AtmosDecodeStage())

    stages.append(DecodeStage())

    if method is RetimeMethod.REDECLARE:
        stages.append(RedeclareStage())
    elif method is RetimeMethod.RESAMPLE:
        stages.append(ResampleStage())
    elif method is RetimeMethod.STRETCH:
        stages.append(StretchStage())

    # PCM quantises straight into its own output, so it needs no separate step.
    if ctx.target_codec != "pcm":
        stages.append(QuantizeStage())

    stages.append(EncodeStage())

    if handoff:
        stages.append(HandoffStage())
        notes.append(
            "No TrueHD encode will be attempted: Dolby Media Encoder is GUI-only. "
            "You finish the last step by hand, following DME_RECIPE.md."
        )

    if ctx.target_container == "mka":
        stages.append(MuxStage())

    stages.append(VerifyStage())
    return stages
