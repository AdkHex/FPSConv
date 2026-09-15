from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


AUDIO_EXTENSIONS = {
    ".aac",
    ".ac3",
    ".eac3",
    ".m4a",
    ".mka",
    ".mp3",
    ".wav",
    ".flac",
    ".opus",
    ".ogg",
    ".wma",
}

CONTAINER_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".m4v",
    ".ts",
    ".m2ts",
}

SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | CONTAINER_EXTENSIONS


@dataclass(slots=True)
class AudioStreamInfo:
    stream_index: int
    codec_name: str
    codec_long_name: str | None
    channels: int | None
    sample_rate: int | None
    bit_rate_bps: int | None
    language: str | None
    title: str | None
    duration_seconds: float | None


@dataclass(slots=True)
class MediaFileInfo:
    path: Path
    format_name: str
    duration_seconds: float | None
    is_container: bool
    audio_streams: list[AudioStreamInfo]


def is_supported_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS


def discover_files(root: Path, recursive: bool = False) -> list[Path]:
    if not root.exists():
        return []
    iterator = root.rglob("*") if recursive else root.iterdir()
    files = [p for p in iterator if is_supported_file(p)]
    return sorted(files, key=lambda p: p.name.lower())


def _parse_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "N/A", "") else None
    except (TypeError, ValueError):
        return None


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "N/A", "") else None
    except (TypeError, ValueError):
        return None


def probe_media(path: Path) -> MediaFileInfo:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "ffprobe failed"
        raise RuntimeError(f"ffprobe failed for {path.name}: {message}")

    payload = json.loads(proc.stdout or "{}")
    format_info = payload.get("format", {})
    streams = payload.get("streams", [])

    audio_streams: list[AudioStreamInfo] = []
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        audio_streams.append(
            AudioStreamInfo(
                stream_index=int(stream.get("index", 0)),
                codec_name=(stream.get("codec_name") or "unknown").lower(),
                codec_long_name=stream.get("codec_long_name"),
                channels=_parse_int(stream.get("channels")),
                sample_rate=_parse_int(stream.get("sample_rate")),
                bit_rate_bps=_parse_int(stream.get("bit_rate")),
                language=tags.get("language"),
                title=tags.get("title"),
                duration_seconds=_parse_float(stream.get("duration")) or _parse_float(format_info.get("duration")),
            )
        )

    suffix = path.suffix.lower()
    is_container = suffix in CONTAINER_EXTENSIONS
    return MediaFileInfo(
        path=path,
        format_name=format_info.get("format_name") or suffix.lstrip("."),
        duration_seconds=_parse_float(format_info.get("duration")),
        is_container=is_container,
        audio_streams=audio_streams,
    )


def check_dependencies() -> list[str]:
    missing: list[str] = []
    for bin_name in ("ffmpeg", "ffprobe"):
        proc = subprocess.run([bin_name, "-version"], capture_output=True, text=True)
        if proc.returncode != 0:
            missing.append(bin_name)
    return missing
