"""Transport-stream demuxers: tsMuxeR and eac3to.

Part 5 of the plan prefers these over ffmpeg for M2TS / TS / MPLS sources,
because ffmpeg's ``-c copy`` on a Blu-ray transport stream can drop the initial
padding and the exact PTS offset that §3.6 needs.  Both are optional: when
neither is present the pipeline falls back to ffmpeg and says so in the log and
in the verification report, rather than silently producing a different result.

eac3to is Windows-only and its argument order is positional and unusual, so the
adapter builds it explicitly and never guesses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Command, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["Eac3toAdapter", "TsMuxerAdapter"]


@dataclass
class TsMuxerAdapter(BinaryAdapter):
    name: str = "tsmuxer"
    binary: str = "tsMuxeR"
    aliases: tuple[str, ...] = ("tsmuxer", "tsMuxeR.exe")
    version_argv: tuple[str, ...] = ()
    help_argv: tuple[str, ...] = ()
    version_ok_codes: tuple[int, ...] = (0, 1, 2, 255)
    required_for: tuple[str, ...] = ("preferred M2TS / MPLS demux",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        return ToolCapabilities(features=frozenset({"m2ts", "mpls"}))

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"tsMuxeR\s+v?(\S+)", text or "", re.IGNORECASE)
        return match.group(1) if match else None

    def demux_command(self, meta_file: Path, output_dir: Path) -> Command:
        """tsMuxeR is driven by a meta file; the caller writes it first."""
        return self.command(
            [str(meta_file), str(output_dir)],
            purpose=f"demux via meta file {meta_file.name}",
        )

    @staticmethod
    def build_meta(source: Path, track_id: int, codec_hint: str) -> str:
        return (
            "MUXOPT --no-pcr-on-video-pid --new-audio-pes --demux --vbr\n"
            f"{codec_hint}, \"{source}\", track={track_id}\n"
        )


@dataclass
class Eac3toAdapter(BinaryAdapter):
    name: str = "eac3to"
    binary: str = "eac3to"
    version_argv: tuple[str, ...] = ()
    help_argv: tuple[str, ...] = ()
    version_ok_codes: tuple[int, ...] = (0, 1, 2, 255)
    required_for: tuple[str, ...] = ("preferred Blu-ray audio extraction",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"eac3to\s+v?(\d+\.\d+)", text or "", re.IGNORECASE)
        return match.group(1) if match else None

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features = {"bluray"}
        if "-log" in flags or "-log=" in (help_text or ""):
            features.add("logfile")
        return ToolCapabilities(flags=frozenset(flags), features=frozenset(features))

    def extract_command(self, source: Path, track_number: int, destination: Path) -> Command:
        """``eac3to <source> <track>: <destination>`` — bit-exact extraction."""
        return self.command(
            [str(source), f"{track_number}:", str(destination), "-log=NUL"],
            purpose=f"extract track {track_number} from {source.name}",
        )
