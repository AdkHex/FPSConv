from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


LOSSY_CODECS = {"aac", "ac3", "eac3", "mp3", "opus"}
CONTAINER_EXTENSIONS = {
    "m4a": "m4a",
    "aac": "aac",
    "ac3": "ac3",
    "eac3": "eac3",
    "mp3": "mp3",
    "flac": "flac",
    "wav": "wav",
    "opus": "opus",
}


@dataclass(slots=True)
class BatchSettings:
    input_dir: Path
    output_dir: Path
    recursive: bool = False
    profile_key: str = "23.976_to_25"
    mode: str = "retime"  # retime | convert
    engine: str = "ffmpeg"  # ffmpeg | rubberband_hq
    target_codec: str = "auto"
    target_container: str = "m4a"
    bitrate: str = "192k"
    overwrite_policy: str = "skip"  # skip | overwrite | rename
    parallel_jobs: int = 2


def resolve_codec(target_codec: str, source_codec: str | None) -> str:
    if target_codec != "auto":
        return target_codec
    source = (source_codec or "").lower()
    if source in CONTAINER_EXTENSIONS:
        return source
    return "aac"


def resolve_extension(target_container: str, codec: str) -> str:
    if target_container == "auto":
        return CONTAINER_EXTENSIONS.get(codec, "m4a")
    return CONTAINER_EXTENSIONS.get(target_container, target_container)


def codec_needs_bitrate(codec: str) -> bool:
    return codec in LOSSY_CODECS
