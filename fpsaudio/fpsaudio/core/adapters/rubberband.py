"""Rubber Band adapter — pitch-preserving time-stretch, **opt-in only**.

This is the operation both legacy programs performed by default, via
``atempo`` (A-2), ``sox tempo`` and ``rubberband --tempo`` (B-2).  It is the
wrong default: a film speed change moves pitch with speed.  Rubber Band
survives here for the one case §3.7 describes — a user who explicitly wants the
pitch left alone — and it is reachable only through
``--method stretch``.

The legacy code tried ``--tempo`` and, on failure, ``-T``; and ``tempo -s`` then
``tempo`` for SoX (B-10).  That converted a real error into a silent retry that
reported only the second failure's message.  Here the flag is chosen from
detected capabilities, once, and a missing capability is a refusal that names
the tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ..contracts import Command, Refusal, RefusalCode, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["RubberBandAdapter"]

#: Rubber Band's CLI takes a decimal ratio.  We hand it as many digits as a
#: double can carry, and the verification stage checks the realised sample count
#: against the exact rational — so the residual is measured, not assumed.
_RATIO_DIGITS = 17


@dataclass
class RubberBandAdapter(BinaryAdapter):
    name: str = "rubberband"
    binary: str = "rubberband"
    aliases: tuple[str, ...] = ("rubberband-r3",)
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    version_ok_codes: tuple[int, ...] = (0, 1, 2)
    required_for: tuple[str, ...] = ("pitch-preserving stretch (--method stretch)",)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        notes: list[str] = []
        blob = help_text or ""

        if "--time" in flags or "-t" in flags:
            features.add("time_ratio")
        if "--tempo" in flags or "-T" in flags:
            features.add("tempo_ratio")
        if "--fine" in flags or re.search(r"^\s*-3\b", blob, re.MULTILINE):
            features.add("engine_r3")
        if "--pitch" in flags:
            features.add("pitch")
        if "--formant" in flags or "-F" in flags:
            features.add("formant")

        if "engine_r3" not in features:
            notes.append(
                "This Rubber Band build does not advertise the R3 engine (-3 / --fine); "
                "stretch quality will be the older R2 engine."
            )
        return ToolCapabilities(
            flags=frozenset(flags), features=frozenset(features), notes=tuple(notes)
        )

    def stretch_command(
        self,
        source: Path,
        destination: Path,
        *,
        speed: Fraction,
        r3: bool = True,
        formant_preserve: bool = False,
    ) -> Command:
        """Stretch so that duration scales by ``1/speed`` with pitch unchanged.

        Rubber Band's ``--time`` takes a *duration* multiplier and ``--tempo``
        takes its reciprocal.  Whichever the installed build advertises is used;
        neither is guessed.
        """
        caps = self.detect().capabilities
        argv: list[str] = []

        if r3:
            if caps.has("engine_r3"):
                argv.append("--fine" if caps.has_flag("--fine") else "-3")
        if formant_preserve:
            self.require_flag("--formant", purpose="formant-preserving stretch")
            argv.append("--formant")

        duration_scale = Fraction(1) / speed
        if caps.has("time_ratio"):
            argv += ["--time", _decimal(duration_scale)]
        elif caps.has("tempo_ratio"):
            argv += ["--tempo", _decimal(speed)]
        else:
            raise Refusal(
                RefusalCode.TOOL_MISSING,
                "This Rubber Band build advertises neither --time nor --tempo, so the "
                "stretch ratio cannot be passed to it.",
                remedies=[
                    "Install Rubber Band 3.x (https://breakfastquay.com/rubberband/).",
                    "Or use the default --method resample, which does not need "
                    "Rubber Band at all.",
                ],
            )

        argv += [str(source), str(destination)]
        return self.command(
            argv,
            purpose=(
                f"pitch-preserving stretch by {duration_scale.numerator}/"
                f"{duration_scale.denominator} (opt-in; not a film speed change)"
            ),
        )

    def parse_progress(self, line: str, state: dict[str, Any]) -> float | None:
        match = re.search(r"(\d{1,3})\s*%", line or "")
        if not match:
            return None
        return max(0.0, min(100.0, float(match.group(1))))

    def quality_note(self) -> str:
        return (
            "Rubber Band preserves pitch. That is NOT what happens when film is "
            "projected at a different speed: real speed change moves pitch with rate. "
            "Use this only when you deliberately want the original pitch retained, and "
            "expect phase-vocoder artefacts on transients."
        )


def _decimal(value: Fraction) -> str:
    """Render a Fraction as the longest decimal a double can carry."""
    text = f"{float(value):.{_RATIO_DIGITS}f}".rstrip("0").rstrip(".")
    return text or "1"
