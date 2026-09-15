"""MKVToolNix adapters: mkvextract (demux) and mkvmerge (mux).

Preferred over ffmpeg for Matroska because mkvextract preserves the track's
``CodecDelay`` and default-duration metadata that ffmpeg's ``-c copy`` path can
drop, and mkvmerge is the only tool here that can write the retimed delay back.
§3.6 — delays and sync offsets — is entirely absent from both legacy programs
(B-19).

When MKVToolNix is not installed the pipeline falls back to ffmpeg ``-c copy``
and **logs that it did so**, rather than pretending the two are equivalent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import Command, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["MkvExtractAdapter", "MkvMergeAdapter"]


@dataclass
class MkvMergeAdapter(BinaryAdapter):
    name: str = "mkvmerge"
    binary: str = "mkvmerge"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    required_for: tuple[str, ...] = ("Matroska muxing with exact delays",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"mkvmerge v(\S+)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        if "--identify" in flags:
            features.add("identify")
        if "--sync" in flags:
            features.add("delay")
        if "--identification-format" in flags:
            features.add("json_identify")
        return ToolCapabilities(flags=frozenset(flags), features=frozenset(features))

    def identify_command(self, path: Path) -> Command:
        self.require_flag("--identify", purpose="Matroska track identification")
        argv = ["--identification-format", "json", "--identify", str(path)]
        return self.command(argv, purpose=f"identify tracks in {path.name}", stdout_is_data=True)

    def identify(self, path: Path) -> dict[str, Any]:
        outcome = self.run(self.identify_command(path))
        if not outcome.ok:
            raise RuntimeError(outcome.message)
        return json.loads(outcome.stdout or "{}")

    def mux_command(
        self,
        destination: Path,
        audio: Path,
        *,
        delay_ms: float | None = None,
        language: str | None = None,
        title: str | None = None,
        default_track: bool = True,
    ) -> Command:
        """Mux one retimed audio track, carrying the rescaled delay (§3.6)."""
        argv: list[str] = ["--output", str(destination)]
        if language:
            argv += ["--language", f"0:{language}"]
        if title:
            argv += ["--track-name", f"0:{title}"]
        argv += ["--default-track-flag", f"0:{'yes' if default_track else 'no'}"]
        if delay_ms is not None:
            self.require_flag("--sync", purpose="applying the rescaled audio delay")
            argv += ["--sync", f"0:{delay_ms:.6f}".rstrip("0").rstrip(".")]
        argv.append(str(audio))
        return self.command(argv, purpose=f"mux {audio.name} into {destination.name}")


@dataclass
class MkvExtractAdapter(BinaryAdapter):
    name: str = "mkvextract"
    binary: str = "mkvextract"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    required_for: tuple[str, ...] = ("Matroska demux preserving CodecDelay",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"mkvextract v(\S+)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def extract_command(self, source: Path, track_id: int, destination: Path) -> Command:
        argv = ["tracks", str(source), f"{track_id}:{destination}"]
        return self.command(
            argv, purpose=f"extract track {track_id} from {source.name} bit-exactly"
        )

    def extract_chapters_command(self, source: Path, destination: Path) -> Command:
        argv = ["chapters", str(source), "--output", str(destination)]
        return self.command(argv, purpose=f"extract chapters from {source.name}")
