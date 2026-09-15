"""Adapter registry.

One place that knows every external tool, what it is required for, and whether
it is present.  ``fpsaudio doctor`` renders this registry; the planner queries
it before building any command, so a missing tool becomes a refusal naming the
tool and the install command rather than a stack trace at run time.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from ..contracts import DetectResult
from .base import BinaryAdapter, RunOutcome, scale_progress
from .demuxers import Eac3toAdapter, TsMuxerAdapter
from .encoders import (
    FdkAacAdapter,
    FlacAdapter,
    OpusDecAdapter,
    OpusEncAdapter,
    WavPackAdapter,
)
from .ffmpeg import FFmpegAdapter, FFprobeAdapter
from .mediainfo import MediaInfoAdapter
from .mkvtoolnix import MkvExtractAdapter, MkvMergeAdapter
from .rubberband import RubberBandAdapter
from .soxr import (
    NumpyAdapter,
    PyLoudnormAdapter,
    SoundFileAdapter,
    SoxrAdapter,
)
from .truehdd import TrueHDDAdapter

__all__ = [
    "AdapterRegistry",
    "BinaryAdapter",
    "RunOutcome",
    "get_registry",
    "scale_progress",
    "set_tool_overrides",
]


#: Display order in ``doctor``, grouped by role.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Probe", ("mediainfo", "ffprobe")),
    ("Demux / mux", ("mkvextract", "mkvmerge", "tsmuxer", "eac3to", "ffmpeg")),
    ("Decode", ("truehdd", "flac", "opusdec")),
    ("DSP (in-process)", ("numpy", "soxr", "soundfile", "pyloudnorm")),
    ("Encode", ("fdkaac", "flac", "opusenc", "wavpack")),
    ("Stretch (opt-in)", ("rubberband",)),
)

#: Tools without which nothing at all works.  MediaInfo is here because it is
#: the only prober that can see Dolby Atmos / JOC: without it, every E-AC-3 and
#: TrueHD job hits the "Atmos presence unknown" refusal rather than risking a
#: silent flatten.
_ESSENTIAL: frozenset[str] = frozenset(
    {"ffmpeg", "ffprobe", "mediainfo", "numpy", "soxr", "soundfile"}
)


@dataclass
class AdapterRegistry:
    adapters: dict[str, BinaryAdapter] = field(default_factory=dict)

    def register(self, adapter: BinaryAdapter) -> BinaryAdapter:
        self.adapters[adapter.name] = adapter
        return adapter

    def get(self, name: str) -> BinaryAdapter | None:
        return self.adapters.get(name)

    def require(self, name: str) -> BinaryAdapter:
        adapter = self.adapters.get(name)
        if adapter is None:
            raise KeyError(f"no adapter registered under {name!r}")
        if not adapter.detect().found:
            adapter.refuse_missing()
        return adapter

    def __iter__(self) -> Iterator[BinaryAdapter]:
        return iter(self.adapters.values())

    # -- typed accessors used by the pipeline ------------------------------ #

    @property
    def ffmpeg(self) -> FFmpegAdapter:
        return self.adapters["ffmpeg"]  # type: ignore[return-value]

    @property
    def ffprobe(self) -> FFprobeAdapter:
        return self.adapters["ffprobe"]  # type: ignore[return-value]

    @property
    def mediainfo(self) -> MediaInfoAdapter:
        return self.adapters["mediainfo"]  # type: ignore[return-value]

    @property
    def fdkaac(self) -> FdkAacAdapter:
        return self.adapters["fdkaac"]  # type: ignore[return-value]

    @property
    def flac(self) -> FlacAdapter:
        return self.adapters["flac"]  # type: ignore[return-value]

    @property
    def opusenc(self) -> OpusEncAdapter:
        return self.adapters["opusenc"]  # type: ignore[return-value]

    @property
    def wavpack(self) -> WavPackAdapter:
        return self.adapters["wavpack"]  # type: ignore[return-value]

    @property
    def mkvmerge(self) -> MkvMergeAdapter:
        return self.adapters["mkvmerge"]  # type: ignore[return-value]

    @property
    def mkvextract(self) -> MkvExtractAdapter:
        return self.adapters["mkvextract"]  # type: ignore[return-value]

    @property
    def truehdd(self) -> TrueHDDAdapter:
        return self.adapters["truehdd"]  # type: ignore[return-value]

    @property
    def rubberband(self) -> RubberBandAdapter:
        return self.adapters["rubberband"]  # type: ignore[return-value]

    @property
    def soxr(self) -> SoxrAdapter:
        return self.adapters["soxr"]  # type: ignore[return-value]

    @property
    def soundfile(self) -> SoundFileAdapter:
        return self.adapters["soundfile"]  # type: ignore[return-value]

    @property
    def pyloudnorm(self) -> PyLoudnormAdapter:
        return self.adapters["pyloudnorm"]  # type: ignore[return-value]

    # -- reporting --------------------------------------------------------- #

    def detect_all(self, *, refresh: bool = False) -> dict[str, DetectResult]:
        return {name: a.detect(refresh=refresh) for name, a in self.adapters.items()}

    def groups(self) -> tuple[tuple[str, tuple[BinaryAdapter, ...]], ...]:
        out: list[tuple[str, tuple[BinaryAdapter, ...]]] = []
        for title, names in _GROUPS:
            members = tuple(self.adapters[n] for n in names if n in self.adapters)
            if members:
                out.append((title, members))
        return tuple(out)

    def missing_essential(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in sorted(_ESSENTIAL)
            if name in self.adapters and not self.adapters[name].detect().found
        )


def _build_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in (
        MediaInfoAdapter(),
        FFprobeAdapter(),
        FFmpegAdapter(),
        MkvExtractAdapter(),
        MkvMergeAdapter(),
        TsMuxerAdapter(),
        Eac3toAdapter(),
        TrueHDDAdapter(),
        FlacAdapter(),
        FdkAacAdapter(),
        OpusEncAdapter(),
        OpusDecAdapter(),
        WavPackAdapter(),
        RubberBandAdapter(),
        NumpyAdapter(),
        SoxrAdapter(),
        SoundFileAdapter(),
        PyLoudnormAdapter(),
    ):
        registry.register(adapter)
    return registry


_REGISTRY: AdapterRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> AdapterRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = _build_registry()
        return _REGISTRY


def set_tool_overrides(overrides: Mapping[str, str]) -> None:
    """Apply ``[tools]`` config entries, e.g. ``ffmpeg = "C:/ff/bin/ffmpeg.exe"``."""
    registry = get_registry()
    for name, path in overrides.items():
        adapter = registry.get(name)
        if adapter is None:
            continue
        adapter.override_path = str(path)
        adapter.detect(refresh=True)
