"""In-process Python-module adapters: soxr, soundfile, numpy, pyloudnorm.

These are not binaries, so they are detected by import rather than by ``--help``.
They present the same :class:`~fpsaudio.core.contracts.DetectResult` surface so
``doctor`` can report Python wheels and external executables in one table.

libsoxr is the *preferred* resampler and the reason the resample runs
in-process: it is a linear-phase VHQ polyphase resampler that takes an exact
integer input/output rate pair, so the exact :class:`fractions.Fraction` reaches
the resampler intact.  Every legacy path destroyed exactness at the tool
boundary — ``atempo=1001/1000`` was evaluated to a C double, and the SoX and
Rubber Band paths truncated to ten decimal places on purpose (B-1).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DetectResult, Refusal, RefusalCode, ToolCapabilities
from .base import BinaryAdapter

__all__ = [
    "NumpyAdapter",
    "PyLoudnormAdapter",
    "PythonModuleAdapter",
    "SoundFileAdapter",
    "SoxrAdapter",
]


@dataclass
class PythonModuleAdapter(BinaryAdapter):
    """An adapter whose 'binary' is an importable Python module."""

    module: str = ""
    pip_name: str = ""
    features: tuple[str, ...] = ()

    def which(self) -> str | None:
        spec = importlib.util.find_spec(self.module) if self.module else None
        if spec is None:
            return None
        return spec.origin or self.module

    def _detect_uncached(self) -> DetectResult:
        try:
            mod = importlib.import_module(self.module)
        except ImportError as exc:
            return DetectResult(
                name=self.name,
                found=False,
                error=f"python module '{self.module}' is not installed ({exc})",
                required_for=self.required_for,
            )
        except Exception as exc:  # noqa: BLE001 - a broken wheel is not a crash
            return DetectResult(
                name=self.name,
                found=False,
                error=f"python module '{self.module}' failed to import: {exc}",
                required_for=self.required_for,
            )
        version = str(getattr(mod, "__version__", "") or "") or None
        return DetectResult(
            name=self.name,
            found=True,
            path=getattr(mod, "__file__", None),
            version=version,
            capabilities=self.module_capabilities(mod),
            required_for=self.required_for,
        )

    def module_capabilities(self, mod: Any) -> ToolCapabilities:
        return ToolCapabilities(features=frozenset(self.features))

    def load(self) -> Any:
        detected = self.detect()
        if not detected.found:
            raise Refusal(
                RefusalCode.TOOL_MISSING,
                f"The Python package '{self.pip_name or self.module}' is required "
                f"for this operation but is not installed.",
                remedies=[
                    f"Install it: .venv\\Scripts\\pip install {self.pip_name or self.module}",
                    "Or re-run install.ps1, which installs the whole set.",
                ],
                detail={"module": self.module, "error": detected.error},
            )
        return importlib.import_module(self.module)


@dataclass
class SoxrAdapter(PythonModuleAdapter):
    name: str = "soxr"
    binary: str = "soxr"
    module: str = "soxr"
    pip_name: str = "soxr"
    required_for: tuple[str, ...] = ("the default resample retime",)

    def module_capabilities(self, mod: Any) -> ToolCapabilities:
        features: set[str] = {"resample"}
        notes: list[str] = []
        quality = getattr(mod, "QQ", None)
        for name in ("VHQ", "HQ", "MQ", "LQ", "QQ"):
            if hasattr(mod, name):
                features.add(f"quality:{name}")
        if hasattr(mod, "ResampleStream"):
            features.add("streaming")
        else:
            notes.append(
                "This soxr build has no ResampleStream; resampling will be done in "
                "one block, which needs the whole track in memory."
            )
        if quality is None and "quality:VHQ" not in features:
            notes.append("soxr did not expose named quality constants; using 'VHQ' by name.")
        return ToolCapabilities(features=frozenset(features), notes=tuple(notes))


@dataclass
class SoundFileAdapter(PythonModuleAdapter):
    name: str = "soundfile"
    binary: str = "soundfile"
    module: str = "soundfile"
    pip_name: str = "soundfile"
    required_for: tuple[str, ...] = ("RF64 / W64 intermediates and verification reads",)

    def module_capabilities(self, mod: Any) -> ToolCapabilities:
        features: set[str] = set()
        notes: list[str] = []
        try:
            formats = {str(k).upper() for k in mod.available_formats()}
        except Exception:  # noqa: BLE001
            formats = set()
            notes.append("soundfile could not enumerate its formats.")
        for name in ("WAV", "W64", "RF64", "FLAC", "CAF", "AIFF"):
            if name in formats:
                features.add(f"format:{name}")
        if not features & {"format:W64", "format:RF64"}:
            notes.append(
                "Neither W64 nor RF64 is available in this libsndfile build; "
                "intermediates larger than 4 GB cannot be written."
            )
        return ToolCapabilities(features=frozenset(features), notes=tuple(notes))

    def large_file_format(self) -> str:
        """Pick an intermediate format that escapes the 4 GB RIFF ceiling (B-8)."""
        caps = self.detect().capabilities
        if caps.has("format:RF64"):
            return "RF64"
        if caps.has("format:W64"):
            return "W64"
        raise Refusal(
            RefusalCode.TOOL_MISSING,
            "libsndfile here supports neither RF64 nor W64, so a feature-length "
            "multichannel intermediate (up to ~8.3 GB for 2 h of 7.1 24-bit) cannot "
            "be written.",
            remedies=["Upgrade the soundfile wheel: pip install -U soundfile"],
        )


@dataclass
class NumpyAdapter(PythonModuleAdapter):
    name: str = "numpy"
    binary: str = "numpy"
    module: str = "numpy"
    pip_name: str = "numpy"
    required_for: tuple[str, ...] = ("all DSP: resample, dither, null test")


@dataclass
class PyLoudnormAdapter(PythonModuleAdapter):
    name: str = "pyloudnorm"
    binary: str = "pyloudnorm"
    module: str = "pyloudnorm"
    pip_name: str = "pyloudnorm"
    required_for: tuple[str, ...] = ("EBU R128 loudness delta verification",)
