"""truehdd adapter — the only route to Dolby Atmos objects in this toolchain.

ffmpeg decodes TrueHD to its channel bed and discards the object metadata, and
it decodes E-AC-3 JOC to the 5.1 core and discards the objects entirely.  So
any Atmos work depends on ``truehdd``, an open-source Rust TrueHD decoder that
can emit DAMF (Dolby Atmos Master Format) or ADM BWF.

**Honesty note, carried from Part 8 risk 3 of the plan.** ``truehdd`` is a young
project and no real TrueHD Atmos file was available to test against.  This
adapter therefore does not hardcode a command line it cannot prove: it reads
``--help``, requires the subcommand and flags it is about to use to actually
appear there, and refuses with the tool's own help text quoted if they do not.
``fpsaudio doctor --verbose`` prints the detected surface so it can be checked
against the real binary on the Windows box.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..contracts import Command, Refusal, RefusalCode, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["TrueHDDAdapter"]

_SUBCOMMAND_RE = re.compile(r"^\s{2,}(decode|info|demux)\b", re.MULTILINE)


@dataclass
class TrueHDDAdapter(BinaryAdapter):
    name: str = "truehdd"
    binary: str = "truehdd"
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    version_ok_codes: tuple[int, ...] = (0, 1, 2)
    required_for: tuple[str, ...] = ("Dolby Atmos object decoding (TrueHD)",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        notes: list[str] = []
        blob = (help_text or "").lower()

        for sub in _SUBCOMMAND_RE.findall(help_text or ""):
            features.add(f"subcommand:{sub}")
        if "damf" in blob:
            features.add("output:damf")
        if "adm" in blob or "bwf" in blob:
            features.add("output:adm_bwf")
        if "--output-path" in flags or "--output" in flags or "-o" in flags:
            features.add("output_flag")

        if not features & {"output:damf", "output:adm_bwf"}:
            notes.append(
                "truehdd's help output names neither DAMF nor ADM/BWF output. "
                "Atmos hand-off will be refused until this is confirmed on the real "
                "binary; paste `fpsaudio doctor --verbose` output to update the adapter."
            )
        return ToolCapabilities(
            flags=frozenset(flags), features=frozenset(features), notes=tuple(notes)
        )

    # -- capability gates -------------------------------------------------- #

    def output_format(self, preferred: str = "damf") -> str:
        caps = self.detect().capabilities
        order = ("damf", "adm_bwf") if preferred == "damf" else ("adm_bwf", "damf")
        for candidate in order:
            if caps.has(f"output:{candidate}"):
                return candidate
        raise Refusal(
            RefusalCode.TOOL_MISSING,
            "truehdd is installed but does not advertise DAMF or ADM/BWF output, so "
            "the Atmos object bed cannot be extracted.",
            remedies=[
                "Upgrade truehdd to a build that supports `--output-format damf`.",
                "Run `fpsaudio doctor --verbose` and paste the truehdd section so the "
                "adapter can be matched to your build.",
                "Or convert the channel bed only, accepting the loss of all objects.",
            ],
            override_token="flatten-atmos",
        )

    def _output_flag(self) -> str:
        caps = self.detect().capabilities
        for flag in ("--output-path", "--output", "-o"):
            if caps.has_flag(flag):
                return flag
        self.require_flag("--output-path", purpose="writing the decoded Atmos master")
        return "--output-path"  # unreachable; require_flag raises

    def decode_command(
        self,
        source: Path,
        destination: Path,
        *,
        output_format: str = "damf",
    ) -> Command:
        """Decode a TrueHD (Atmos) stream to an object-preserving master.

        Every flag used here is confirmed present in the installed binary's
        help output before the command is built.
        """
        caps = self.detect().capabilities
        if not caps.has("subcommand:decode"):
            raise Refusal(
                RefusalCode.TOOL_MISSING,
                "truehdd does not advertise a `decode` subcommand.",
                remedies=[
                    "Install truehdd from https://github.com/truehdd/truehdd and re-run "
                    "`fpsaudio doctor`.",
                ],
            )
        fmt = self.output_format(output_format)
        argv: list[str] = ["decode", str(source)]
        if caps.has_flag("--output-format"):
            argv += ["--output-format", "damf" if fmt == "damf" else "adm"]
        argv += [self._output_flag(), str(destination)]
        return self.command(
            argv,
            purpose=f"decode {source.name} to {fmt.upper()} preserving Atmos objects",
        )

    def info_command(self, source: Path) -> Command:
        if not self.detect().capabilities.has("subcommand:info"):
            self.require_flag("--info", purpose="inspecting the TrueHD object bed")
        return self.command(
            ["info", str(source)], purpose=f"report Atmos object layout of {source.name}"
        )
