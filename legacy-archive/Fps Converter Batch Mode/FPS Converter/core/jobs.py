from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .probe import AudioStreamInfo, MediaFileInfo, discover_files, probe_media


@dataclass(slots=True)
class ConversionJob:
    job_id: str
    source_path: Path
    source_name: str
    is_container: bool
    audio_stream: AudioStreamInfo
    format_name: str
    status: str = "Pending"
    progress: float = 0.0
    message: str = ""
    output_path: Path | None = None
    error: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        return self.audio_stream.duration_seconds


@dataclass(slots=True)
class ScanResult:
    jobs: list[ConversionJob] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def scan_directory_for_jobs(directory: Path, recursive: bool = False) -> ScanResult:
    result = ScanResult()
    files = discover_files(directory, recursive=recursive)
    for file_path in files:
        try:
            media = probe_media(file_path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(str(exc))
            continue
        result.jobs.extend(_jobs_from_media(media))
    return result


def _jobs_from_media(media: MediaFileInfo) -> list[ConversionJob]:
    jobs: list[ConversionJob] = []
    for stream in media.audio_streams:
        label = media.path.stem
        if media.is_container:
            label = f"{media.path.stem}"
        jobs.append(
            ConversionJob(
                job_id=f"{media.path.name}:{stream.stream_index}",
                source_path=media.path,
                source_name=label,
                is_container=media.is_container,
                audio_stream=stream,
                format_name=media.format_name,
            )
        )
    if not media.audio_streams:
        jobs.append(
            ConversionJob(
                job_id=f"{media.path.name}:noaudio",
                source_path=media.path,
                source_name=media.path.stem,
                is_container=media.is_container,
                audio_stream=AudioStreamInfo(
                    stream_index=-1,
                    codec_name="n/a",
                    codec_long_name=None,
                    channels=None,
                    sample_rate=None,
                    bit_rate_bps=None,
                    language=None,
                    title=None,
                    duration_seconds=media.duration_seconds,
                ),
                format_name=media.format_name,
                status="Unsupported",
                error="No audio streams found",
            )
        )
    return jobs
