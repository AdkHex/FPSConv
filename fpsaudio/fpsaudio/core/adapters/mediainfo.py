"""MediaInfo CLI adapter — the preferred prober.

MediaInfo is preferred over ffprobe for one concrete reason: it is the only one
of the two that reports ``Format_Commercial_IfAny``,
``Format_AdditionalFeatures`` and ``NumberOfDynamicObjects``, which are the
fields that distinguish TrueHD Atmos from plain TrueHD, E-AC-3 JOC from plain
E-AC-3, and DTS-HD MA from DTS core.  Without them §3.5 is unreachable, which
is exactly the state the legacy batch build was in (B-6, B-7).

This replaces the legacy ``pymediainfo`` dependency with the CLI, so there is
no compiled Python binding to install on the Windows box.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["MediaInfoAdapter"]


@dataclass
class MediaInfoAdapter(BinaryAdapter):
    name: str = "mediainfo"
    binary: str = "mediainfo"
    aliases: tuple[str, ...] = ("MediaInfo",)
    version_argv: tuple[str, ...] = ("--Version",)
    help_argv: tuple[str, ...] = ("--Help",)
    required_for: tuple[str, ...] = ("Atmos / JOC / DTS-HD detection",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"MediaInfoLib\s+-\s+v(\S+)", text or "")
        if match:
            return match.group(1)
        match = re.search(r"v(\d+\.\d+(?:\.\d+)?)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        notes: list[str] = []
        blob = f"{help_text}\n{version_text}"
        if "--Output" in blob or "Output=" in blob:
            features.add("output_template")
        # The JSON output template is what we actually depend on.
        features.add("json")
        version = self.parse_version(version_text) or ""
        try:
            major = int(version.split(".")[0])
        except (ValueError, IndexError):
            major = 0
        if major and major < 19:
            notes.append(
                f"MediaInfo {version} predates reliable Dolby Atmos object reporting; "
                "upgrade to 19.x or newer for trustworthy Atmos detection."
            )
        return ToolCapabilities(
            flags=frozenset(flags), features=frozenset(features), notes=tuple(notes)
        )

    def probe_argv(self, path: Path) -> tuple[str, ...]:
        return ("--Output=JSON", "--Full", str(path))

    def probe(self, path: Path) -> dict[str, Any]:
        outcome = self.run(
            self.command(self.probe_argv(path), purpose=f"probe {path.name}")
        )
        if not outcome.ok:
            raise RuntimeError(outcome.message)
        text = (outcome.stdout or "").strip()
        if not text:
            raise RuntimeError("mediainfo produced no output")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"mediainfo returned malformed JSON: {exc}") from exc
