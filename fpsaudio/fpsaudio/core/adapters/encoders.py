"""Encoder and decoder adapters: flac, fdkaac, opus-tools, wavpack.

Each one advertises what it can actually do, learned from its own ``--help``.
The important case is :class:`FdkAacAdapter`: Part 5 of the plan records that
the standalone ``fdkaac`` CLI's 7.1 support is *unverified*, so this adapter
never assumes it.  ``doctor`` reports what the real binary said, and the
planner refuses a 7.1 AAC target unless the capability was confirmed — it will
not silently downmix (§3.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..contracts import Command, DitherMode, Refusal, RefusalCode, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = [
    "FdkAacAdapter",
    "FlacAdapter",
    "OpusDecAdapter",
    "OpusEncAdapter",
    "RawFormat",
    "WavPackAdapter",
]


@dataclass(frozen=True, slots=True)
class RawFormat:
    """Describes a headerless PCM intermediate.

    Every external encoder here is fed raw PCM rather than a WAV file.  A WAV
    header cannot describe more than 4 GB, and a 2-hour 7.1 24-bit 48 kHz track
    is 8.29 GB (B-8); a headerless stream has no such ceiling, and the encoders
    all accept one as long as they are told the geometry.  The flags that carry
    that geometry are capability-checked, never assumed.
    """

    rate: int
    channels: int
    bits: int = 24

    @property
    def fdk_spec(self) -> str:
        return f"S{self.bits}L"

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.bits // 8


# --------------------------------------------------------------------------- #
# FLAC
# --------------------------------------------------------------------------- #

@dataclass
class FlacAdapter(BinaryAdapter):
    name: str = "flac"
    binary: str = "flac"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    required_for: tuple[str, ...] = ("FLAC encode/decode with verification",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        if "--test" in flags or "-t" in flags:
            features.add("verify_after_encode")
        if "--verify" in flags or "-V" in flags:
            features.add("verify_during_encode")
        if "--channel-map=none" in help_text or "--channel-map" in flags:
            features.add("channel_map")
        # libFLAC supports up to 8 channels; the CLI exposes no flag for it, so
        # this is a format fact rather than a detected one.
        features.add("channels<=8")
        return ToolCapabilities(flags=frozenset(flags), features=frozenset(features))

    def encode_command(
        self,
        source: Path,
        destination: Path,
        *,
        compression: int = 8,
        verify: bool = True,
        overwrite: bool = True,
        raw: RawFormat | None = None,
    ) -> Command:
        argv: list[str] = [f"-{max(0, min(8, compression))}"]
        if verify and self.detect().capabilities.has("verify_during_encode"):
            argv.append("--verify")
        if overwrite:
            argv.append("--force")
        if raw is not None:
            self.require_flag("--force-raw-format", purpose="headerless PCM input")
            argv += [
                "--force-raw-format",
                "--endian=little",
                "--sign=signed",
                f"--channels={raw.channels}",
                f"--bps={raw.bits}",
                f"--sample-rate={raw.rate}",
            ]
        argv += ["--output-name", str(destination), str(source)]
        return self.command(argv, purpose=f"FLAC encode -{compression} with verify")

    def test_command(self, path: Path) -> Command:
        self.require_flag("--test", purpose="FLAC integrity verification")
        return self.command(
            ["--test", "--silent", str(path)], purpose=f"verify {path.name} decodes cleanly"
        )

    def decode_command(
        self, source: Path, destination: Path, *, overwrite: bool = True
    ) -> Command:
        argv = ["--decode"]
        if overwrite:
            argv.append("--force")
        argv += ["--output-name", str(destination), str(source)]
        return self.command(argv, purpose=f"decode {source.name}")


# --------------------------------------------------------------------------- #
# fdkaac
# --------------------------------------------------------------------------- #

@dataclass
class FdkAacAdapter(BinaryAdapter):
    name: str = "fdkaac"
    binary: str = "fdkaac"
    version_argv: tuple[str, ...] = ("--help",)
    help_argv: tuple[str, ...] = ("--help",)
    #: fdkaac exits non-zero from --help on several builds.
    version_ok_codes: tuple[int, ...] = (0, 1, 2, 255)
    required_for: tuple[str, ...] = ("AAC encoding",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"fdkaac\s+(\d+\.\d+\.\d+)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        notes: list[str] = []
        blob = help_text or ""

        if "--bitrate-mode" in flags:
            features.add("vbr")
        if "--profile" in flags or "-p" in flags:
            features.add("profile_select")
        if "--raw-channels" in flags:
            features.add("raw_input")

        # 7.1 support is the open question Part 5 flags.  Only a help text that
        # actually names 7.1 / 8 channels counts as confirmation; anything else
        # leaves the capability unconfirmed and the planner refuses rather than
        # downmixing behind the user's back.
        if re.search(r"7\.1|\b8\s*channels?\b|channel[- ]config.*(7|8)", blob, re.IGNORECASE):
            features.add("aac_7.1")
        else:
            notes.append(
                "This fdkaac build does not advertise 7.1 / 8-channel AAC in its help "
                "output. 7.1 AAC targets will be refused rather than silently downmixed; "
                "see docs/TOOLS.md."
            )
        features.add("aac_5.1")
        return ToolCapabilities(
            flags=frozenset(flags), features=frozenset(features), notes=tuple(notes)
        )

    def supports_channels(self, channels: int) -> bool:
        caps = self.detect().capabilities
        if channels <= 6:
            return True
        if channels <= 8:
            return caps.has("aac_7.1")
        return False

    def encode_command(
        self,
        source: Path,
        destination: Path,
        *,
        bitrate: str | None = None,
        vbr_mode: int | None = None,
        profile: int = 2,  # 2 = AAC-LC
        channels: int | None = None,
        overwrite: bool = True,
        raw: RawFormat | None = None,
    ) -> Command:
        if channels is not None and not self.supports_channels(channels):
            raise Refusal(
                RefusalCode.CHANNEL_COUNT_UNSUPPORTED,
                f"This fdkaac build does not confirm support for {channels}-channel AAC.",
                remedies=[
                    "Encode to FLAC, Opus or WavPack instead — all handle 7.1 losslessly "
                    "or at high quality.",
                    "Install an fdkaac build with 7.1 support and re-run `fpsaudio doctor`.",
                    "Or accept an explicit downmix to 5.1 (this discards two channels).",
                ],
                override_token="downmix-to-5.1",
                detail={"channels": channels},
            )

        argv: list[str] = ["--profile", str(profile)]
        if vbr_mode is not None:
            self.require_flag("--bitrate-mode", purpose="fdkaac VBR encoding")
            argv += ["--bitrate-mode", str(vbr_mode)]
        elif bitrate:
            argv += ["--bitrate", _kbps(bitrate)]
        if raw is not None:
            for flag in ("--raw", "--raw-channels", "--raw-rate", "--raw-format"):
                self.require_flag(flag, purpose="headerless PCM input")
            argv += [
                "--raw",
                "--raw-channels", str(raw.channels),
                "--raw-rate", str(raw.rate),
                "--raw-format", raw.fdk_spec,
            ]
        argv += ["-o", str(destination), str(source)]
        return self.command(argv, purpose="AAC encode (fdkaac)")

    def quality_note(self) -> str:
        return (
            "AAC is produced by fdkaac (Fraunhofer FDK). It is the best non-Apple AAC "
            "encoder available, but at low bitrates Apple's AAC encoder still measures "
            "better. No Apple encoder DLLs are used in this build, by design."
        )


def _kbps(bitrate: str) -> str:
    """Normalise ``192k`` / ``192`` / ``192000`` to fdkaac's kbps integer."""
    text = str(bitrate).strip().lower().rstrip("bps").rstrip()
    if text.endswith("k"):
        return str(int(float(text[:-1])))
    value = float(text)
    return str(int(value / 1000)) if value >= 10000 else str(int(value))


# --------------------------------------------------------------------------- #
# Opus
# --------------------------------------------------------------------------- #

@dataclass
class OpusEncAdapter(BinaryAdapter):
    name: str = "opusenc"
    binary: str = "opusenc"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    required_for: tuple[str, ...] = ("Opus encoding",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features = {"opus"}
        if "--vbr" in flags:
            features.add("vbr")
        if "--bitrate" in flags:
            features.add("bitrate")
        return ToolCapabilities(flags=frozenset(flags), features=frozenset(features))

    def encode_command(
        self,
        source: Path,
        destination: Path,
        *,
        bitrate: str | None = None,
        vbr: bool = True,
        extra: Sequence[str] = (),
        raw: RawFormat | None = None,
    ) -> Command:
        argv: list[str] = ["--vbr"] if vbr else ["--hard-cbr"]
        if bitrate:
            argv += ["--bitrate", _kbps(bitrate)]
        if raw is not None:
            for flag in ("--raw", "--raw-bits", "--raw-rate", "--raw-chan"):
                self.require_flag(flag, purpose="headerless PCM input")
            argv += [
                "--raw",
                "--raw-bits", str(raw.bits),
                "--raw-rate", str(raw.rate),
                "--raw-chan", str(raw.channels),
                "--raw-endianness", "0",
            ]
        argv += list(extra)
        argv += [str(source), str(destination)]
        return self.command(argv, purpose="Opus encode")

    def sample_rate_note(self) -> str:
        return (
            "Opus always stores at 48 kHz internally. A source at another rate is "
            "resampled by the encoder; fpsaudio resamples to 48 kHz itself with libsoxr "
            "VHQ first, so the encoder never has to."
        )


@dataclass
class OpusDecAdapter(BinaryAdapter):
    name: str = "opusdec"
    binary: str = "opusdec"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    required_for: tuple[str, ...] = ("Opus decoding",)

    def decode_command(
        self, source: Path, destination: Path, *, rate: int | None = None
    ) -> Command:
        argv: list[str] = ["--float"]
        if rate:
            argv += ["--rate", str(rate)]
        argv += [str(source), str(destination)]
        return self.command(argv, purpose=f"decode {source.name}")


# --------------------------------------------------------------------------- #
# WavPack
# --------------------------------------------------------------------------- #

@dataclass
class WavPackAdapter(BinaryAdapter):
    name: str = "wavpack"
    binary: str = "wavpack"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    version_ok_codes: tuple[int, ...] = (0, 1, 2)
    required_for: tuple[str, ...] = ("WavPack encoding",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features = {"wavpack", "lossless"}
        if "-h" in flags or "--high" in flags:
            features.add("high_quality")
        if "-v" in flags or "--verify" in flags:
            features.add("verify")
        return ToolCapabilities(flags=frozenset(flags), features=frozenset(features))

    def encode_command(
        self,
        source: Path,
        destination: Path,
        *,
        high: bool = True,
        verify: bool = True,
        overwrite: bool = True,
        raw: RawFormat | None = None,
    ) -> Command:
        argv: list[str] = []
        if high:
            argv.append("-h")
        if verify:
            argv.append("-v")
        if overwrite:
            argv.append("-y")
        if raw is not None:
            self.require_flag("--raw-pcm", purpose="headerless PCM input")
            argv.append(f"--raw-pcm={raw.rate},{raw.bits},{raw.channels}")
        argv += ["-o", str(destination), str(source)]
        return self.command(argv, purpose="WavPack encode (lossless)")
