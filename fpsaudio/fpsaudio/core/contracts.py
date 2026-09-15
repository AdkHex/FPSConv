"""Frozen shared interfaces for the fpsaudio toolchain.

Every other module in ``core``, ``cli`` and ``tui`` codes against the models in
this file and nothing else.  It deliberately has **zero third-party imports** so
that the whole planning / dry-run / audit surface works on a bare Python 3.11+
interpreter, before any binary or wheel has been installed.

Design rules encoded here (these are the corrections to the legacy programs):

* A frame-rate change is a **speed change**.  It is described by an exact
  :class:`fractions.Fraction`, never a float.  See :mod:`fpsaudio.core.ratio`.
* Nothing is ever silently transcoded, downmixed, flattened or dropped.  Every
  such situation raises :class:`Refusal`, which carries remedies and an explicit
  override token the user must type.
* Every stage can render its exact ``argv`` without running anything, so
  ``--dry-run`` is total rather than best-effort.
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

SPEC_VERSION = 1

__all__ = [
    "SPEC_VERSION",
    "AtmosInfo",
    "AtmosPolicy",
    "AudioStream",
    "Chapter",
    "Command",
    "DetectResult",
    "DecodeSpec",
    "DitherMode",
    "JobSpec",
    "MediaFile",
    "OutputSpec",
    "ProgressEvent",
    "Refusal",
    "RefusalCode",
    "RetimeMethod",
    "RetimeSpec",
    "Severity",
    "Stage",
    "StageResult",
    "ToolCapabilities",
    "VerifyCheck",
    "VerifyResult",
    "VerifySpec",
    "fraction_from_str",
    "fraction_to_str",
]


# --------------------------------------------------------------------------- #
# Fraction serialisation — exactness survives every round trip through disk.
# --------------------------------------------------------------------------- #

def fraction_to_str(value: Fraction) -> str:
    """Render a Fraction as ``"num/den"``.  Never lossy, never a float."""
    return f"{value.numerator}/{value.denominator}"


def fraction_from_str(text: str | Fraction | int) -> Fraction:
    """Parse ``"num/den"``, ``"24000/1001"``, ``"25"`` or a decimal literal.

    Decimal literals go through :class:`Fraction`'s exact decimal constructor,
    so ``"23.976"`` becomes ``2997/125`` — the literal value typed, not the
    broadcast rate.  Callers that mean 24000/1001 should say so; see
    :func:`fpsaudio.core.ratio.parse_fps`, which applies the broadcast aliases.
    """
    if isinstance(text, Fraction):
        return text
    if isinstance(text, int):
        return Fraction(text)
    return Fraction(str(text).strip())


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #

class RetimeMethod(str, enum.Enum):
    """How the speed change is physically realised."""

    NONE = "none"
    #: Rewrite the declared sample rate.  Bit-exact: the PCM payload is
    #: byte-identical, only the header changes.  Requires the new rate to be an
    #: exact integer.  This is the path §3.2 of the master prompt asks for and
    #: that neither legacy program implemented.
    REDECLARE = "redeclare"
    #: Redeclare, then resample back to a normal rate with libsoxr VHQ.
    #: Pitch moves with speed, which is what a film speed change does.
    RESAMPLE = "resample"
    #: Pitch-preserving time-stretch (Rubber Band R3).  Explicit opt-in only;
    #: this is the operation both legacy programs performed by default.
    STRETCH = "stretch"


class DitherMode(str, enum.Enum):
    NONE = "none"
    TPDF = "tpdf"
    SHAPED = "shaped"


class AtmosPolicy(str, enum.Enum):
    #: Stop with a Refusal rather than lose the object bed.
    REFUSE = "refuse"
    #: Retime the object metadata and stop with a DME hand-off bundle.
    HANDOFF = "handoff"
    #: Flatten to the channel bed.  Requires a typed confirmation token.
    FLATTEN = "flatten"


class Severity(str, enum.Enum):
    INFO = "info"
    WARN = "warn"
    FAIL = "fail"


class RefusalCode(str, enum.Enum):
    DTS_OUT_OF_SCOPE = "dts_out_of_scope"
    DOLBY_ENCODER_MISSING = "dolby_encoder_missing"
    TRUEHD_ENCODE_MANUAL = "truehd_encode_manual"
    JOC_DECODER_MISSING = "joc_decoder_missing"
    ATMOS_WOULD_FLATTEN = "atmos_would_flatten"
    LOSSLESS_TO_LOSSY = "lossless_to_lossy"
    CHANNEL_COUNT_UNSUPPORTED = "channel_count_unsupported"
    WOULD_DOWNMIX = "would_downmix"
    CONTAINER_CODEC_MISMATCH = "container_codec_mismatch"
    TOOL_MISSING = "tool_missing"
    UNIDENTIFIED_SOURCE = "unidentified_source"
    NO_AUDIO_STREAM = "no_audio_stream"
    REDECLARE_NOT_EXACT = "redeclare_not_exact"
    UNSUPPORTED_OPERATION = "unsupported_operation"


class Refusal(Exception):
    """A refusal to perform an operation that would silently lose information.

    Every refusal names what would have been lost, what the alternatives are,
    and — where an override exists at all — the exact token the user must type.
    A refusal is never raised for something the tool could have done correctly.
    """

    def __init__(
        self,
        code: RefusalCode,
        message: str,
        *,
        remedies: Sequence[str] = (),
        override_token: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remedies = tuple(remedies)
        self.override_token = override_token
        self.detail = dict(detail or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "remedies": list(self.remedies),
            "override_token": self.override_token,
            "detail": self.detail,
        }

    def render(self) -> str:
        lines = [f"REFUSED [{self.code.value}]: {self.message}"]
        for remedy in self.remedies:
            lines.append(f"  - {remedy}")
        if self.override_token:
            lines.append(
                f"  To override anyway, re-run with --accept {self.override_token} "
                f"(this acknowledges the loss described above)."
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Probe model — deliberately keeps everything the legacy model discarded (B-6)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class AtmosInfo:
    """Object-audio findings for one stream.

    ``certainty`` distinguishes "the prober told us" from "we are guessing from
    a channel count".  The tool never acts destructively on a guess.
    """

    present: bool = False
    kind: str | None = None  # truehd_atmos | eac3_joc | dtsx
    objects: int | None = None
    bed_channels: int | None = None
    certainty: str = "absent"  # confirmed | likely | unknown | absent
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AudioStream:
    stream_index: int
    codec: str = "unknown"
    codec_long: str | None = None
    profile: str | None = None
    commercial_name: str | None = None
    channels: int | None = None
    channel_layout: str | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    bit_rate_bps: int | None = None
    bit_rate_mode: str | None = None
    duration_s: float | None = None
    start_time_s: float | None = None
    codec_delay_ns: int | None = None
    sample_count: int | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False
    lossless: bool = False
    atmos: AtmosInfo = field(default_factory=AtmosInfo)
    source_tool: str = "unknown"
    identified: bool = True
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        bits = [f"#{self.stream_index}", self.codec]
        if self.profile:
            bits.append(self.profile)
        if self.channel_layout:
            bits.append(self.channel_layout)
        elif self.channels:
            bits.append(f"{self.channels}ch")
        if self.sample_rate:
            bits.append(f"{self.sample_rate} Hz")
        if self.bit_depth:
            bits.append(f"{self.bit_depth}-bit")
        if self.atmos.present:
            bits.append("ATMOS")
        if self.language:
            bits.append(self.language)
        return " | ".join(str(b) for b in bits)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["atmos"] = self.atmos.to_dict()
        data.pop("raw", None)
        return data


@dataclass(frozen=True, slots=True)
class Chapter:
    index: int
    start_s: float
    end_s: float
    title: str | None = None


@dataclass(frozen=True, slots=True)
class MediaFile:
    path: Path
    container: str = "unknown"
    format_name: str | None = None
    duration_s: float | None = None
    size_bytes: int | None = None
    has_video: bool = False
    audio: tuple[AudioStream, ...] = ()
    chapters: tuple[Chapter, ...] = ()
    source_tool: str = "unknown"
    identified: bool = True
    problems: tuple[str, ...] = ()

    def stream(self, index: int) -> AudioStream:
        for stream in self.audio:
            if stream.stream_index == index:
                return stream
        raise Refusal(
            RefusalCode.NO_AUDIO_STREAM,
            f"{self.path.name} has no audio stream with index {index}.",
            remedies=[
                "Run `fpsaudio inspect` on the file to list its real stream indices.",
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "container": self.container,
            "format_name": self.format_name,
            "duration_s": self.duration_s,
            "size_bytes": self.size_bytes,
            "has_video": self.has_video,
            "audio": [s.to_dict() for s in self.audio],
            "chapters": [asdict(c) for c in self.chapters],
            "source_tool": self.source_tool,
            "identified": self.identified,
            "problems": list(self.problems),
        }


# --------------------------------------------------------------------------- #
# Adapter contract  (detect / version / capabilities / build_argv / parse_progress)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """Capabilities learned from the binary itself, never remembered.

    ``flags`` holds every long/short option observed in ``--help`` output;
    ``features`` holds semantic findings (``"aac_7.1"``, ``"encoder:libopus"``).
    Adapters consult this instead of guessing a flag and retrying on failure,
    which is the B-10 defect.
    """

    flags: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags

    def has(self, feature: str) -> bool:
        return feature in self.features


@dataclass(frozen=True, slots=True)
class DetectResult:
    name: str
    found: bool
    path: str | None = None
    version: str | None = None
    capabilities: ToolCapabilities = field(default_factory=ToolCapabilities)
    error: str | None = None
    required_for: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "found": self.found,
            "path": self.path,
            "version": self.version,
            "flags": sorted(self.capabilities.flags),
            "features": sorted(self.capabilities.features),
            "notes": list(self.capabilities.notes),
            "error": self.error,
            "required_for": list(self.required_for),
        }


@dataclass(frozen=True, slots=True)
class Command:
    """One fully-resolved external invocation.

    Built during planning so that ``--dry-run`` prints exactly what would run.
    ``argv`` is a tuple: it is never re-parsed, re-split or shell-interpolated.
    """

    adapter: str
    argv: tuple[str, ...]
    purpose: str = ""
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    #: Fraction of the owning stage this command represents, for progress.
    weight: float = 1.0
    #: Set when the command's stdout is a data stream rather than diagnostics.
    stdout_is_data: bool = False

    def rendered(self) -> str:
        return " ".join(_quote(a) for a in self.argv)


def _quote(arg: str) -> str:
    if arg and all(c not in arg for c in ' \t"\'\\|&;<>()$`'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    job_id: str
    stage_id: str
    percent: float
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)


ProgressSink = Callable[[ProgressEvent], None]


# --------------------------------------------------------------------------- #
# Job specification — the single artefact the CLI and the TUI both produce
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class RetimeSpec:
    src_fps: Fraction
    dst_fps: Fraction
    method: RetimeMethod = RetimeMethod.RESAMPLE
    #: Overrides the derived src/dst quotient when the user gives a raw ratio.
    ratio_override: Fraction | None = None
    #: Target output sample rate.  ``None`` keeps the source rate (which for
    #: REDECLARE means "let the rate carry the speed change").
    target_sample_rate: int | None = None
    #: Rubber Band only.  Ignored by every other method.
    stretch_engine: str = "rubberband"

    def to_dict(self) -> dict[str, Any]:
        return {
            "src_fps": fraction_to_str(self.src_fps),
            "dst_fps": fraction_to_str(self.dst_fps),
            "method": self.method.value,
            "ratio_override": (
                fraction_to_str(self.ratio_override) if self.ratio_override else None
            ),
            "target_sample_rate": self.target_sample_rate,
            "stretch_engine": self.stretch_engine,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RetimeSpec":
        override = data.get("ratio_override")
        return cls(
            src_fps=fraction_from_str(data["src_fps"]),
            dst_fps=fraction_from_str(data["dst_fps"]),
            method=RetimeMethod(data.get("method", "resample")),
            ratio_override=fraction_from_str(override) if override else None,
            target_sample_rate=data.get("target_sample_rate"),
            stretch_engine=data.get("stretch_engine", "rubberband"),
        )


@dataclass(frozen=True, slots=True)
class DecodeSpec:
    #: ``-drc_scale 0``.  §3.3's "single most common quality bug in DD+
    #: conversion"; absent from both legacy programs (A-10, B-18).
    drc_scale: float = 0.0
    #: Undo the dialnorm attenuation the decoder would otherwise bake in.
    ignore_dialnorm: bool = True
    #: 32-bit float end to end.  Quantise once, at the final encode.
    working_bit_depth: str = "float32"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecodeSpec":
        return cls(
            drc_scale=float(data.get("drc_scale", 0.0)),
            ignore_dialnorm=bool(data.get("ignore_dialnorm", True)),
            working_bit_depth=str(data.get("working_bit_depth", "float32")),
        )


@dataclass(frozen=True, slots=True)
class OutputSpec:
    directory: Path
    #: §8 token template.  Tokens: {stem} {index} {profile} {codec} {lang}
    #: {channels} {rate} {srcfps} {dstfps} {parent} {ext}
    template: str = "{stem}__a{index}__{profile}"
    container: str = "auto"
    codec: str = "auto"
    bitrate: str | None = None
    quality: str | None = None
    bit_depth: int | None = None
    dither: DitherMode = DitherMode.TPDF
    overwrite: str = "skip"  # skip | overwrite | rename | fail
    keep_intermediates: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["directory"] = str(self.directory)
        data["dither"] = self.dither.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutputSpec":
        return cls(
            directory=Path(data["directory"]),
            template=data.get("template", "{stem}__a{index}__{profile}"),
            container=data.get("container", "auto"),
            codec=data.get("codec", "auto"),
            bitrate=data.get("bitrate"),
            quality=data.get("quality"),
            bit_depth=data.get("bit_depth"),
            dither=DitherMode(data.get("dither", "tpdf")),
            overwrite=data.get("overwrite", "skip"),
            keep_intermediates=bool(data.get("keep_intermediates", False)),
        )


@dataclass(frozen=True, slots=True)
class VerifySpec:
    sample_count: bool = True
    duration_drift: bool = True
    pcm_md5: bool = True
    null_test: bool = False
    loudness: bool = False
    channel_layout: bool = True
    #: Acceptance gates from Part 6 of the plan.
    duration_tolerance_ms: float = 1.0
    null_test_max_dbfs: float = -140.0
    loudness_tolerance_lu: float = 0.1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerifySpec":
        known = {f: data[f] for f in _VERIFY_FIELDS if f in data}
        return cls(**known)


_VERIFY_FIELDS = (
    "sample_count",
    "duration_drift",
    "pcm_md5",
    "null_test",
    "loudness",
    "channel_layout",
    "duration_tolerance_ms",
    "null_test_max_dbfs",
    "loudness_tolerance_lu",
)


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A single unit of work: one audio stream, one retime, one output.

    Serialisable to TOML/JSON and built identically by the CLI and the TUI, so
    ``--dry-run``, ``--manifest`` and ``--dump-manifest`` all operate on the
    same artefact.
    """

    source: Path
    stream_index: int
    retime: RetimeSpec
    output: OutputSpec
    decode: DecodeSpec = field(default_factory=DecodeSpec)
    verify: VerifySpec = field(default_factory=VerifySpec)
    atmos_policy: AtmosPolicy = AtmosPolicy.REFUSE
    #: Override tokens the user has typed to accept a documented loss.
    accepted: tuple[str, ...] = ()
    profile_key: str = "custom"
    spec_version: int = SPEC_VERSION
    tags: Mapping[str, str] = field(default_factory=dict)

    # -- identity ---------------------------------------------------------- #

    @property
    def job_id(self) -> str:
        """Stable, collision-free id.

        Keyed on the *resolved absolute path*, not the basename — B-12 is the
        bug where two ``audio.mkv`` files in different folders shared an id and
        overwrote each other's output.
        """
        raw = f"{self.source.resolve()}::{self.stream_index}::{self.profile_key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def short_id(self) -> str:
        return self.job_id[:8]

    def content_key(self) -> str:
        """Content hash of the full spec, for resume completion markers (B-15)."""
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def accepts(self, token: str | None) -> bool:
        return bool(token) and token in self.accepted

    def with_accepted(self, token: str) -> "JobSpec":
        if token in self.accepted:
            return self
        return replace(self, accepted=self.accepted + (token,))

    # -- serialisation ----------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "source": str(self.source),
            "stream_index": self.stream_index,
            "profile_key": self.profile_key,
            "retime": self.retime.to_dict(),
            "output": self.output.to_dict(),
            "decode": self.decode.to_dict(),
            "verify": self.verify.to_dict(),
            "atmos_policy": self.atmos_policy.value,
            "accepted": list(self.accepted),
            "tags": dict(self.tags),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobSpec":
        version = int(data.get("spec_version", SPEC_VERSION))
        if version > SPEC_VERSION:
            raise Refusal(
                RefusalCode.UNSUPPORTED_OPERATION,
                f"Manifest declares spec_version {version}; this build understands "
                f"up to {SPEC_VERSION}.",
                remedies=["Upgrade fpsaudio, or re-create the manifest."],
            )
        return cls(
            source=Path(data["source"]),
            stream_index=int(data["stream_index"]),
            retime=RetimeSpec.from_dict(data["retime"]),
            output=OutputSpec.from_dict(data["output"]),
            decode=DecodeSpec.from_dict(data.get("decode", {})),
            verify=VerifySpec.from_dict(data.get("verify", {})),
            atmos_policy=AtmosPolicy(data.get("atmos_policy", "refuse")),
            accepted=tuple(data.get("accepted", ())),
            profile_key=data.get("profile_key", "custom"),
            spec_version=version,
            tags=dict(data.get("tags", {})),
        )

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# --------------------------------------------------------------------------- #
# Stage contract
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class StageResult:
    stage_id: str
    ok: bool = True
    outputs: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "ok": self.ok,
            "outputs": [str(p) for p in self.outputs],
            "notes": list(self.notes),
            "metrics": self.metrics,
            "error": self.error,
            "skipped": self.skipped,
        }


class Stage(Protocol):
    """One step of a plan.

    Every stage must be able to answer ``describe()`` and ``commands()``
    *without side effects*, so a plan can be fully explained before it runs.
    """

    id: str
    title: str

    def describe(self, ctx: Any) -> str: ...

    def commands(self, ctx: Any) -> Sequence[Command]: ...

    def run(self, ctx: Any, progress: ProgressSink | None = None) -> StageResult: ...


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class VerifyCheck:
    name: str
    ok: bool
    severity: Severity = Severity.FAIL
    detail: str = ""
    measured: Any = None
    expected: Any = None
    tolerance: Any = None
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["severity"] = self.severity.value
        return data

    def render(self) -> str:
        if self.skipped:
            return f"  ~ {self.name}: skipped ({self.skip_reason})"
        mark = "PASS" if self.ok else ("WARN" if self.severity is Severity.WARN else "FAIL")
        line = f"  {mark:4} {self.name}"
        if self.detail:
            line += f": {self.detail}"
        return line


@dataclass(frozen=True, slots=True)
class VerifyResult:
    checks: tuple[VerifyCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return all(
            c.ok or c.skipped or c.severity is not Severity.FAIL for c in self.checks
        )

    @property
    def failures(self) -> tuple[VerifyCheck, ...]:
        return tuple(
            c for c in self.checks
            if not c.ok and not c.skipped and c.severity is Severity.FAIL
        )

    @property
    def warnings(self) -> tuple[VerifyCheck, ...]:
        return tuple(
            c for c in self.checks
            if not c.ok and not c.skipped and c.severity is Severity.WARN
        )

    def extend(self, more: Iterable[VerifyCheck]) -> "VerifyResult":
        return VerifyResult(checks=self.checks + tuple(more))

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks]}

    def render(self) -> str:
        head = "VERIFY: PASS" if self.ok else "VERIFY: FAIL"
        return "\n".join([head, *(c.render() for c in self.checks)])


Optional  # re-exported for adapters that import from contracts only
