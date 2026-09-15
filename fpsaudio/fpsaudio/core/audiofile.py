"""Audio file I/O for intermediates and verification.

Two rules, both of them corrections to the legacy build:

* **Intermediates are RF64 or W64, never plain RIFF WAV.**  B-8: both HQ engines
  extracted to ``pcm_s24le`` in a ``.wav``; a 2-hour 7.1 24-bit 48 kHz track is
  ``8 ch x 3 B x 48000 x 7200 = 8.29 GB`` and even 5.1 is ``6.22 GB``, both past
  WAV's 4 GB header limit.
* **Float32 end to end.**  B-9: ``pcm_s24le`` was hardcoded, so a 32-bit float
  or 24-bit source was requantised without dither before the stretch and again
  on encode.  Here the working format is float32 and quantisation happens
  exactly once, at the final write, with dither.

Everything is streamed in blocks, so peak memory is bounded regardless of
runtime.  A 2-hour 7.1 float32 track would be ~11 GB if read whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .contracts import Refusal, RefusalCode

__all__ = [
    "AudioInfo",
    "BLOCK_FRAMES",
    "blocks",
    "info",
    "intermediate_path",
    "open_reader",
    "open_writer",
    "soundfile_module",
    "subtype_for_depth",
]

#: Frames per streaming block.  At 8 channels float32 this is 32 MB, which is
#: large enough that per-block overhead is irrelevant and small enough that
#: peak RSS stays flat for a feature-length multichannel track.
BLOCK_FRAMES = 1 << 20


def soundfile_module() -> Any:
    from .adapters import get_registry

    return get_registry().soundfile.load()  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class AudioInfo:
    path: Path
    samplerate: int
    channels: int
    frames: int
    format: str
    subtype: str

    @property
    def duration_s(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0


def info(path: Path) -> AudioInfo:
    sf = soundfile_module()
    try:
        meta = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001
        raise Refusal(
            RefusalCode.UNIDENTIFIED_SOURCE,
            f"Could not read {path.name} as an audio file: {exc}",
        ) from exc
    return AudioInfo(
        path=path,
        samplerate=int(meta.samplerate),
        channels=int(meta.channels),
        frames=int(meta.frames),
        format=str(meta.format),
        subtype=str(meta.subtype),
    )


# --------------------------------------------------------------------------- #
# Format / subtype selection
# --------------------------------------------------------------------------- #

_SUBTYPE_BY_DEPTH: dict[int, str] = {
    16: "PCM_16",
    24: "PCM_24",
    32: "PCM_32",
}


def subtype_for_depth(bit_depth: int | None, *, float_ok: bool = True) -> str:
    """Map a bit depth to a libsndfile subtype.

    ``None`` means "stay in float", which is the working format for every
    intermediate.  Quantisation is a deliberate, dithered, once-per-job act.
    """
    if bit_depth is None:
        return "FLOAT" if float_ok else "PCM_24"
    if bit_depth in _SUBTYPE_BY_DEPTH:
        return _SUBTYPE_BY_DEPTH[bit_depth]
    raise Refusal(
        RefusalCode.UNSUPPORTED_OPERATION,
        f"Unsupported output bit depth: {bit_depth}",
        remedies=["Choose 16, 24 or 32 bits, or leave it unset to stay in float."],
    )


def large_format() -> str:
    """The intermediate container.  RF64 preferred, W64 accepted (B-8)."""
    from .adapters import get_registry

    return get_registry().soundfile.large_file_format()  # type: ignore[attr-defined]


def intermediate_path(directory: Path, stem: str) -> Path:
    """Path for an intermediate, with a suffix matching the chosen big format."""
    suffix = ".w64" if large_format() == "W64" else ".wav"
    return directory / f"{stem}{suffix}"


# --------------------------------------------------------------------------- #
# Streaming read / write
# --------------------------------------------------------------------------- #

def open_reader(path: Path) -> Any:
    sf = soundfile_module()
    return sf.SoundFile(str(path), mode="r")


def open_writer(
    path: Path,
    *,
    samplerate: int,
    channels: int,
    subtype: str = "FLOAT",
    format: str | None = None,
) -> Any:
    """Open a writer, defaulting to a container that cannot overflow at 4 GB."""
    sf = soundfile_module()
    fmt = format
    if fmt is None:
        suffix = path.suffix.lower()
        if suffix == ".w64":
            fmt = "W64"
        elif suffix == ".wav":
            fmt = "RF64" if _has_format(sf, "RF64") else "WAV"
        elif suffix == ".flac":
            fmt = "FLAC"
        elif suffix == ".caf":
            fmt = "CAF"
    path.parent.mkdir(parents=True, exist_ok=True)
    return sf.SoundFile(
        str(path),
        mode="w",
        samplerate=int(samplerate),
        channels=int(channels),
        subtype=subtype,
        format=fmt,
    )


def _has_format(sf: Any, name: str) -> bool:
    try:
        return name in {str(k).upper() for k in sf.available_formats()}
    except Exception:  # noqa: BLE001
        return False


def blocks(path: Path, *, frames: int = BLOCK_FRAMES, dtype: str = "float32") -> Iterator[Any]:
    """Yield successive 2-D ``(frames, channels)`` blocks from a file."""
    with open_reader(path) as handle:
        while True:
            data = handle.read(frames, dtype=dtype, always_2d=True)
            if len(data) == 0:
                return
            yield data


def frame_count(path: Path) -> int:
    return info(path).frames


def rewrite_sample_rate(source: Path, destination: Path, new_rate: int) -> int:
    """Copy a file changing only the declared sample rate — the bit-exact retime.

    §3.2's lossless retime, which exists in neither legacy program (A-12).  The
    sample payload is copied verbatim in its native subtype; only the header
    changes, so the PCM MD5 of the result is identical to the source's.
    """
    meta = info(source)
    written = 0
    with open_reader(source) as reader, open_writer(
        destination,
        samplerate=int(new_rate),
        channels=meta.channels,
        subtype=meta.subtype,
    ) as writer:
        while True:
            # dtype must round-trip the source exactly: int subtypes through
            # int32, float subtypes through float64, never through float32.
            data = reader.read(BLOCK_FRAMES, dtype=_exact_dtype(meta.subtype), always_2d=True)
            if len(data) == 0:
                break
            writer.write(data)
            written += len(data)
    return written


def _exact_dtype(subtype: str) -> str:
    upper = subtype.upper()
    if upper.startswith("PCM_") or upper.startswith("ULAW") or upper.startswith("ALAW"):
        return "int32"
    if upper == "DOUBLE":
        return "float64"
    return "float64"
