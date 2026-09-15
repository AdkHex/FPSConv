"""Codec/container registry and TOML configuration.

Two structural corrections to the legacy build live here.

**The codec registry and the extension map are separate things.**  In
``core/config.py`` the legacy build used one dict, ``CONTAINER_EXTENSIONS``, as
both.  Because ``truehd``, ``dts``, ``mlp``, ``pcm_s24le`` and ``alac`` were
absent from it, ``resolve_codec`` fell through to ``return "aac"`` — so a
TrueHD 7.1 24-bit source was silently converted to lossy AAC with no disclosure
(B-3), and at a broken bitrate (B-4).  Here an unrecognised source codec is a
:class:`Refusal`, never a guess.

**Container/codec pairs are validated.**  The legacy ``resolve_extension``
ignored the codec entirely when a container was named, so codec ``flac`` with
container ``m4a`` produced a ``.m4a`` and ``-c:a flac``, and ffmpeg failed with
"Could not find tag for codec flac" (B-11).  Here the pair is checked before
any command is built.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    AtmosPolicy,
    DitherMode,
    Refusal,
    RefusalCode,
    RetimeMethod,
)

__all__ = [
    "CODECS",
    "CONTAINERS",
    "AppConfig",
    "CodecInfo",
    "ContainerInfo",
    "config_search_paths",
    "encodable_codecs",
    "load_config",
    "resolve_codec",
    "resolve_container",
    "validate_pair",
]


# --------------------------------------------------------------------------- #
# Codec registry
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class CodecInfo:
    id: str
    label: str
    lossless: bool
    #: ``None`` means this codec cannot be produced by this toolchain at all.
    encoder: str | None
    decoder: str | None
    needs_bitrate: bool = False
    max_channels: int | None = None
    default_extension: str = ""
    #: Why an encode is impossible, shown verbatim in the refusal.
    encode_blocked_reason: str = ""
    notes: str = ""

    @property
    def encodable(self) -> bool:
        return self.encoder is not None


#: Part 5 of the plan, made executable.  With no DEE and no Dolby Media Encoder
#: CLI, the output format list is FLAC, AAC, Opus, WavPack and PCM.  Everything
#: Dolby is decode-and-retime-only plus the DME hand-off; DTS is out of scope.
CODECS: dict[str, CodecInfo] = {
    "flac": CodecInfo(
        "flac", "FLAC", lossless=True, encoder="flac", decoder="flac",
        max_channels=8, default_extension="flac",
        notes="Verified on encode with `flac --test`.",
    ),
    "aac": CodecInfo(
        "aac", "AAC-LC", lossless=False, encoder="fdkaac", decoder="ffmpeg",
        needs_bitrate=True, max_channels=8, default_extension="m4a",
        notes=(
            "Encoded by fdkaac (Fraunhofer FDK). Best non-Apple AAC available; at low "
            "bitrates Apple's encoder still measures better. 7.1 support depends on the "
            "installed fdkaac build and is checked, never assumed."
        ),
    ),
    "opus": CodecInfo(
        "opus", "Opus", lossless=False, encoder="opusenc", decoder="opusdec",
        needs_bitrate=True, max_channels=8, default_extension="opus",
        notes="Always stored at 48 kHz; fpsaudio resamples to 48 kHz first.",
    ),
    "wavpack": CodecInfo(
        "wavpack", "WavPack", lossless=True, encoder="wavpack", decoder="ffmpeg",
        max_channels=8, default_extension="wv",
    ),
    "pcm": CodecInfo(
        "pcm", "PCM (RF64/W64)", lossless=True, encoder="soundfile", decoder="ffmpeg",
        default_extension="wav",
        notes="Written as RF64 or W64 so a feature-length multichannel track fits.",
    ),
    # --- decode-only ------------------------------------------------------ #
    "ac3": CodecInfo(
        "ac3", "Dolby Digital", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=6, default_extension="ac3",
        encode_blocked_reason=(
            "Dolby Digital encoding requires the Dolby Encoding Engine (DEE), which is "
            "not present on this machine and is not freely licensable."
        ),
    ),
    "eac3": CodecInfo(
        "eac3", "Dolby Digital Plus", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=8, default_extension="ec3",
        encode_blocked_reason=(
            "Dolby Digital Plus encoding requires the Dolby Encoding Engine (DEE), "
            "which is not present. DD+ Atmos additionally requires a JOC encoder."
        ),
    ),
    "truehd": CodecInfo(
        "truehd", "Dolby TrueHD", lossless=True, encoder=None, decoder="truehdd",
        max_channels=8, default_extension="thd",
        encode_blocked_reason=(
            "TrueHD encoding is only available through Dolby Media Encoder, which is "
            "GUI-only and cannot be automated. fpsaudio produces a hand-off bundle "
            "instead: see `fpsaudio convert --atmos-policy handoff`."
        ),
    ),
    "dts": CodecInfo(
        "dts", "DTS / DTS-HD", lossless=False, encoder=None, decoder=None,
        max_channels=8, default_extension="dts",
        encode_blocked_reason="DTS is out of scope for this toolchain by design.",
    ),
    "alac": CodecInfo(
        "alac", "Apple Lossless", lossless=True, encoder=None, decoder="ffmpeg",
        max_channels=8, default_extension="m4a",
        encode_blocked_reason="ALAC encoding is not enabled in this build; use FLAC.",
    ),
    "mp3": CodecInfo(
        "mp3", "MP3", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=2, default_extension="mp3",
        encode_blocked_reason="MP3 output is not offered; use AAC or Opus.",
    ),
    "mp2": CodecInfo(
        "mp2", "MPEG-1 Layer II", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=2, default_extension="mp2",
        encode_blocked_reason="MP2 output is not offered; use AAC or Opus.",
    ),
    "vorbis": CodecInfo(
        "vorbis", "Vorbis", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=8, default_extension="ogg",
        encode_blocked_reason="Vorbis output is not offered; use Opus.",
    ),
    "wma": CodecInfo(
        "wma", "Windows Media Audio", lossless=False, encoder=None, decoder="ffmpeg",
        needs_bitrate=True, max_channels=6, default_extension="wma",
        encode_blocked_reason="WMA output is not offered.",
    ),
    "tta": CodecInfo(
        "tta", "True Audio", lossless=True, encoder=None, decoder="ffmpeg",
        default_extension="tta", encode_blocked_reason="TTA output is not offered; use FLAC.",
    ),
    "tak": CodecInfo(
        "tak", "TAK", lossless=True, encoder=None, decoder="ffmpeg",
        default_extension="tak", encode_blocked_reason="TAK output is not offered; use FLAC.",
    ),
    "ape": CodecInfo(
        "ape", "Monkey's Audio", lossless=True, encoder=None, decoder="ffmpeg",
        default_extension="ape", encode_blocked_reason="APE output is not offered; use FLAC.",
    ),
}


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    id: str
    extension: str
    codecs: frozenset[str]
    label: str = ""

    def accepts(self, codec: str) -> bool:
        return codec in self.codecs


CONTAINERS: dict[str, ContainerInfo] = {
    "flac": ContainerInfo("flac", "flac", frozenset({"flac"}), "native FLAC"),
    "m4a": ContainerInfo("m4a", "m4a", frozenset({"aac", "alac"}), "MP4 audio"),
    "aac": ContainerInfo("aac", "aac", frozenset({"aac"}), "raw ADTS"),
    "opus": ContainerInfo("opus", "opus", frozenset({"opus"}), "Ogg Opus"),
    "ogg": ContainerInfo("ogg", "ogg", frozenset({"opus", "vorbis"}), "Ogg"),
    "wv": ContainerInfo("wv", "wv", frozenset({"wavpack"}), "WavPack"),
    "wav": ContainerInfo("wav", "wav", frozenset({"pcm"}), "RF64 / WAV"),
    "w64": ContainerInfo("w64", "w64", frozenset({"pcm"}), "Sony Wave64"),
    "mka": ContainerInfo(
        "mka", "mka",
        frozenset({"flac", "aac", "opus", "wavpack", "pcm", "ac3", "eac3", "truehd", "dts"}),
        "Matroska audio",
    ),
}


def encodable_codecs() -> tuple[str, ...]:
    return tuple(sorted(c.id for c in CODECS.values() if c.encodable))


def codec_info(codec: str) -> CodecInfo:
    info = CODECS.get(codec.lower())
    if info is None:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Unknown codec {codec!r}.",
            remedies=[
                "Encodable targets: " + ", ".join(encodable_codecs()),
                "Run `fpsaudio formats` for the full matrix.",
            ],
        )
    return info


def resolve_codec(target: str, source_codec: str | None, *, source_lossless: bool) -> str:
    """Decide the output codec.

    ``auto`` means "stay in the same family": a lossless source stays lossless,
    a lossy source keeps its codec where we can encode it.  It never means
    "fall back to AAC" — that fallback is B-3, the silent TrueHD→AAC conversion.
    """
    wanted = (target or "auto").strip().lower()
    if wanted != "auto":
        info = codec_info(wanted)
        if not info.encodable:
            raise Refusal(
                RefusalCode.DOLBY_ENCODER_MISSING
                if wanted in ("ac3", "eac3", "truehd")
                else (
                    RefusalCode.DTS_OUT_OF_SCOPE
                    if wanted == "dts"
                    else RefusalCode.UNSUPPORTED_OPERATION
                ),
                f"Cannot encode to {info.label}. {info.encode_blocked_reason}",
                remedies=_encode_alternatives(info),
            )
        return info.id

    source = (source_codec or "").lower()
    if source in CODECS and CODECS[source].encodable:
        return source

    if source_lossless:
        return "flac"

    if source in CODECS:
        info = CODECS[source]
        raise Refusal(
            RefusalCode.LOSSLESS_TO_LOSSY if info.lossless else RefusalCode.UNSUPPORTED_OPERATION,
            f"Source codec {info.label} cannot be re-encoded by this toolchain, and "
            f"'auto' will not silently pick a different format for you.",
            remedies=[
                "Name the target explicitly, e.g. --codec flac (lossless) or "
                "--codec opus / --codec aac (lossy).",
                f"{info.encode_blocked_reason}" if info.encode_blocked_reason else "",
            ],
            detail={"source_codec": source},
        )

    raise Refusal(
        RefusalCode.UNIDENTIFIED_SOURCE,
        f"Source codec {source_codec!r} was not recognised, so 'auto' cannot choose "
        f"an output format. It will not guess.",
        remedies=[
            "Run `fpsaudio inspect <file>` to see what the probers reported.",
            "Name the target explicitly with --codec.",
        ],
    )


def _encode_alternatives(info: CodecInfo) -> list[str]:
    out = []
    if info.lossless:
        out.append("--codec flac or --codec wavpack keeps the audio lossless.")
    out.append("--codec opus or --codec aac produces a lossy output.")
    if info.id in ("truehd", "eac3"):
        out.append(
            "--atmos-policy handoff produces everything Dolby Media Encoder needs, "
            "so you can finish the encode by hand in the GUI."
        )
    return out


def validate_pair(container: str, codec: str) -> None:
    """Reject a container/codec combination ffmpeg would fail on later (B-11)."""
    info = CONTAINERS.get(container.lower())
    if info is None:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Unknown container {container!r}.",
            remedies=["Containers: " + ", ".join(sorted(CONTAINERS))],
        )
    if not info.accepts(codec):
        accepted = ", ".join(sorted(info.codecs))
        homes = sorted(c for c, ci in CONTAINERS.items() if ci.accepts(codec))
        raise Refusal(
            RefusalCode.CONTAINER_CODEC_MISMATCH,
            f"Container '{container}' cannot hold {codec}.",
            remedies=[
                f"'{container}' accepts: {accepted}.",
                f"{codec} fits in: {', '.join(homes) or 'no container in this build'}.",
                "Or use --container auto to let fpsaudio pick.",
            ],
        )


def resolve_container(target: str, codec: str) -> tuple[str, str]:
    """Return ``(container_id, extension)``, validating the pair."""
    wanted = (target or "auto").strip().lower()
    if wanted == "auto":
        default = codec_info(codec).default_extension
        container = default if default in CONTAINERS else codec
        if container not in CONTAINERS:
            raise Refusal(
                RefusalCode.UNSUPPORTED_OPERATION,
                f"No default container is defined for {codec}.",
            )
        validate_pair(container, codec)
        return container, CONTAINERS[container].extension
    validate_pair(wanted, codec)
    return wanted, CONTAINERS[wanted].extension


# --------------------------------------------------------------------------- #
# Application configuration
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class AppConfig:
    # defaults
    preset: str = "23.976_to_25"
    method: RetimeMethod = RetimeMethod.RESAMPLE
    codec: str = "auto"
    container: str = "auto"
    bitrate: str | None = None
    template: str = "{stem}__a{index}__{profile}"
    overwrite: str = "skip"
    dither: DitherMode = DitherMode.TPDF
    bit_depth: int | None = None
    target_sample_rate: int | None = None
    atmos_policy: AtmosPolicy = AtmosPolicy.REFUSE
    drc_scale: float = 0.0
    ignore_dialnorm: bool = True
    # concurrency (§5: file-level and encoder-level are separate limits)
    file_workers: int = 0        # 0 => auto
    encoder_workers: int = 0     # 0 => auto
    # verification
    verify_pcm_md5: bool = True
    verify_null_test: bool = False
    verify_loudness: bool = False
    # paths
    output_dir: Path | None = None
    state_dir: Path | None = None
    temp_dir: Path | None = None
    tools: Mapping[str, str] = field(default_factory=dict)
    keep_intermediates: bool = False

    def merged(self, **overrides: Any) -> "AppConfig":
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)


#: Defaults, kept as data because :class:`AppConfig` uses ``slots=True`` and so
#: has no readable class-level defaults.
DEFAULTS: dict[str, Any] = {
    "preset": "23.976_to_25",
    "method": RetimeMethod.RESAMPLE.value,
    "codec": "auto",
    "container": "auto",
    "template": "{stem}__a{index}__{profile}",
    "overwrite": "skip",
    "dither": DitherMode.TPDF.value,
    "atmos_policy": AtmosPolicy.REFUSE.value,
}


def config_search_paths() -> list[Path]:
    """Where a config file is looked for, in increasing precedence."""
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "fpsaudio" / "config.toml")
    paths.append(Path.home() / ".config" / "fpsaudio" / "config.toml")
    paths.append(Path.cwd() / "fpsaudio.toml")
    return paths


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # pragma: no cover - Python < 3.11
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            "Reading TOML config requires Python 3.11 or newer.",
            remedies=["Install Python 3.11+ and re-run install.ps1."],
        ) from exc
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION, f"Cannot read {path}: {exc}"
        ) from exc
    except Exception as exc:  # tomllib.TOMLDecodeError
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"{path} is not valid TOML: {exc}",
        ) from exc


def load_config(explicit: Path | None = None) -> tuple[AppConfig, list[Path]]:
    """Load and merge config files.  Returns the config and the files used."""
    candidates = [explicit] if explicit else config_search_paths()
    data: dict[str, Any] = {}
    used: list[Path] = []
    for path in candidates:
        if path and path.exists():
            _deep_update(data, _load_toml(path))
            used.append(path)

    defaults = data.get("defaults", {})
    output = data.get("output", {})
    verify = data.get("verify", {})
    limits = data.get("concurrency", {})
    tools = data.get("tools", {})
    decode = data.get("decode", {})

    # AppConfig uses slots, so its class attributes are descriptors rather than
    # values; the defaults live in DEFAULTS instead.
    config = AppConfig(
        preset=defaults.get("preset", DEFAULTS["preset"]),
        method=RetimeMethod(defaults.get("method", DEFAULTS["method"])),
        codec=output.get("codec", DEFAULTS["codec"]),
        container=output.get("container", DEFAULTS["container"]),
        bitrate=output.get("bitrate"),
        template=output.get("template", DEFAULTS["template"]),
        overwrite=output.get("overwrite", DEFAULTS["overwrite"]),
        dither=DitherMode(output.get("dither", DEFAULTS["dither"])),
        bit_depth=output.get("bit_depth"),
        target_sample_rate=output.get("sample_rate"),
        atmos_policy=AtmosPolicy(defaults.get("atmos_policy", DEFAULTS["atmos_policy"])),
        drc_scale=float(decode.get("drc_scale", 0.0)),
        ignore_dialnorm=bool(decode.get("ignore_dialnorm", True)),
        file_workers=int(limits.get("files", 0)),
        encoder_workers=int(limits.get("encoders", 0)),
        verify_pcm_md5=bool(verify.get("pcm_md5", True)),
        verify_null_test=bool(verify.get("null_test", False)),
        verify_loudness=bool(verify.get("loudness", False)),
        output_dir=Path(output["directory"]) if output.get("directory") else None,
        state_dir=Path(data["state"]["directory"]) if data.get("state", {}).get("directory") else None,
        temp_dir=Path(data["temp"]["directory"]) if data.get("temp", {}).get("directory") else None,
        tools=dict(tools),
        keep_intermediates=bool(output.get("keep_intermediates", False)),
    )
    return config, used


def _deep_update(base: dict[str, Any], extra: Mapping[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
