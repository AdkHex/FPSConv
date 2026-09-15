from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import BatchSettings, codec_needs_bitrate, resolve_codec, resolve_extension
from .jobs import ConversionJob
from .naming import build_output_path
from .profiles import get_profile


ProgressCallback = Callable[[str, dict], None]


@dataclass(slots=True)
class RunResult:
    ok: bool
    status: str
    output_path: Path | None = None
    error: str | None = None


@dataclass(slots=True)
class JobOutputPlan:
    profile_key: str
    target_codec: str
    target_ext: str
    output_path: Path | None
    action: str


def _build_output_plan(job: ConversionJob, settings: BatchSettings) -> JobOutputPlan:
    profile = get_profile(settings.profile_key if settings.mode == "retime" else "none")
    target_codec = resolve_codec(settings.target_codec, job.audio_stream.codec_name)
    target_ext = resolve_extension(settings.target_container, target_codec)

    output_path, action = build_output_path(
        output_dir=settings.output_dir,
        source_name=job.source_name,
        stream_index=job.audio_stream.stream_index if job.audio_stream.stream_index >= 0 else 0,
        profile_slug=profile.key,
        extension=target_ext,
        overwrite_policy=settings.overwrite_policy,
    )
    return JobOutputPlan(
        profile_key=profile.key,
        target_codec=target_codec,
        target_ext=target_ext,
        output_path=output_path,
        action=action,
    )


def build_ffmpeg_command(job: ConversionJob, settings: BatchSettings) -> tuple[list[str], Path | None, str]:
    plan = _build_output_plan(job, settings)
    if plan.action == "skip":
        return [], plan.output_path, "skip"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y" if settings.overwrite_policy == "overwrite" else "-n",
        "-progress",
        "pipe:1",
        "-i",
        str(job.source_path),
    ]

    if job.audio_stream.stream_index < 0:
        raise RuntimeError("Job has no valid audio stream")

    cmd += ["-map", f"0:{job.audio_stream.stream_index}", "-vn", "-sn", "-dn", "-c:a", plan.target_codec]
    bitrate_arg = _select_bitrate_arg(settings, job, plan.target_codec)
    if codec_needs_bitrate(plan.target_codec) and bitrate_arg:
        cmd += ["-b:a", bitrate_arg]
    profile = get_profile(plan.profile_key)
    if settings.mode == "retime" and profile.key != "none":
        cmd += ["-af", f"atempo={profile.atempo_value}"]
    cmd.append(str(plan.output_path))
    return cmd, plan.output_path, "write"


def run_job(job: ConversionJob, settings: BatchSettings, callback: ProgressCallback | None = None) -> RunResult:
    if settings.engine == "rubberband_hq" and settings.mode == "retime":
        return _run_job_rubberband(job, settings, callback=callback)
    if settings.engine == "sox_hq" and settings.mode == "retime":
        return _run_job_sox(job, settings, callback=callback)
    return _run_job_ffmpeg(job, settings, callback=callback)


def _run_job_ffmpeg(job: ConversionJob, settings: BatchSettings, callback: ProgressCallback | None = None) -> RunResult:
    cmd, output_path, action = build_ffmpeg_command(job, settings)
    if action == "skip":
        return RunResult(ok=True, status="Skipped", output_path=output_path)

    if callback:
        callback("command", {"cmd": cmd, "job_id": job.job_id})

    ok, error = _run_ffmpeg_progress_command(cmd, job.duration_seconds, callback, job.job_id)
    if ok:
        return RunResult(ok=True, status="Done", output_path=output_path)
    return RunResult(ok=False, status="Failed", output_path=output_path, error=error)


def _run_job_rubberband(job: ConversionJob, settings: BatchSettings, callback: ProgressCallback | None = None) -> RunResult:
    if shutil.which("rubberband") is None:
        return RunResult(
            ok=False,
            status="Failed",
            error="rubberband CLI not found in PATH (install Rubber Band and try again)",
        )

    if job.audio_stream.stream_index < 0:
        return RunResult(ok=False, status="Failed", error="Job has no valid audio stream")

    plan = _build_output_plan(job, settings)
    if plan.action == "skip":
        return RunResult(ok=True, status="Skipped", output_path=plan.output_path)
    if plan.output_path is None:
        return RunResult(ok=False, status="Failed", error="Could not build output path")

    profile = get_profile(plan.profile_key)
    if profile.key == "none":
        return _run_job_ffmpeg(job, settings, callback=callback)

    if callback:
        callback("progress", {"percent": 1.0, "job_id": job.job_id})

    with tempfile.TemporaryDirectory(prefix="fps_audio_rb_") as tmpdir:
        tmp_root = Path(tmpdir)
        extracted_wav = tmp_root / "input_extract.wav"
        retimed_wav = tmp_root / "retimed.wav"

        extract_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(job.source_path),
            "-map",
            f"0:{job.audio_stream.stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            str(extracted_wav),
        ]
        if callback:
            callback("command", {"cmd": extract_cmd, "job_id": job.job_id})
        ok, error = _run_simple_command(extract_cmd)
        if not ok:
            return RunResult(ok=False, status="Failed", output_path=plan.output_path, error=f"Extract step failed: {error}")
        if callback:
            callback("progress", {"percent": 20.0, "job_id": job.job_id})

        tempo = float(profile.tempo_ratio)
        rb_ok, rb_error = _run_rubberband_cli(extracted_wav, retimed_wav, tempo, callback, job.job_id)
        if not rb_ok:
            return RunResult(
                ok=False,
                status="Failed",
                output_path=plan.output_path,
                error=f"Rubber Band retime failed: {rb_error}",
            )
        if callback:
            callback("progress", {"percent": 75.0, "job_id": job.job_id})

        encode_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y" if settings.overwrite_policy == "overwrite" else "-n",
            "-progress",
            "pipe:1",
            "-i",
            str(retimed_wav),
            "-c:a",
            plan.target_codec,
        ]
        bitrate_arg = _select_bitrate_arg(settings, job, plan.target_codec)
        if codec_needs_bitrate(plan.target_codec) and bitrate_arg:
            encode_cmd += ["-b:a", bitrate_arg]
        encode_cmd.append(str(plan.output_path))
        if callback:
            callback("command", {"cmd": encode_cmd, "job_id": job.job_id})
        ok, error = _run_ffmpeg_progress_command(
            encode_cmd,
            job.duration_seconds,
            callback,
            job.job_id,
            progress_start=75.0,
            progress_end=100.0,
        )
        if not ok:
            return RunResult(ok=False, status="Failed", output_path=plan.output_path, error=f"Encode step failed: {error}")

    return RunResult(ok=True, status="Done", output_path=plan.output_path)


def _run_job_sox(job: ConversionJob, settings: BatchSettings, callback: ProgressCallback | None = None) -> RunResult:
    if shutil.which("sox") is None:
        return RunResult(
            ok=False,
            status="Failed",
            error="sox CLI not found in PATH (install SoX and try again)",
        )

    if job.audio_stream.stream_index < 0:
        return RunResult(ok=False, status="Failed", error="Job has no valid audio stream")

    plan = _build_output_plan(job, settings)
    if plan.action == "skip":
        return RunResult(ok=True, status="Skipped", output_path=plan.output_path)
    if plan.output_path is None:
        return RunResult(ok=False, status="Failed", error="Could not build output path")

    profile = get_profile(plan.profile_key)
    if profile.key == "none":
        return _run_job_ffmpeg(job, settings, callback=callback)

    with tempfile.TemporaryDirectory(prefix="fps_audio_sox_") as tmpdir:
        tmp_root = Path(tmpdir)
        extracted_wav = tmp_root / "input_extract.wav"
        retimed_wav = tmp_root / "retimed.wav"

        if callback:
            callback("progress", {"percent": 1.0, "job_id": job.job_id})

        extract_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(job.source_path),
            "-map",
            f"0:{job.audio_stream.stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            "pcm_s24le",
            str(extracted_wav),
        ]
        if callback:
            callback("command", {"cmd": extract_cmd, "job_id": job.job_id})
        ok, error = _run_simple_command(extract_cmd)
        if not ok:
            return RunResult(ok=False, status="Failed", output_path=plan.output_path, error=f"Extract step failed: {error}")
        if callback:
            callback("progress", {"percent": 20.0, "job_id": job.job_id})

        tempo = float(profile.tempo_ratio)
        sox_ok, sox_error = _run_sox_tempo(extracted_wav, retimed_wav, tempo, callback, job.job_id)
        if not sox_ok:
            return RunResult(ok=False, status="Failed", output_path=plan.output_path, error=f"SoX retime failed: {sox_error}")
        if callback:
            callback("progress", {"percent": 75.0, "job_id": job.job_id})

        encode_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y" if settings.overwrite_policy == "overwrite" else "-n",
            "-progress",
            "pipe:1",
            "-i",
            str(retimed_wav),
            "-c:a",
            plan.target_codec,
        ]
        bitrate_arg = _select_bitrate_arg(settings, job, plan.target_codec)
        if codec_needs_bitrate(plan.target_codec) and bitrate_arg:
            encode_cmd += ["-b:a", bitrate_arg]
        encode_cmd.append(str(plan.output_path))
        if callback:
            callback("command", {"cmd": encode_cmd, "job_id": job.job_id})
        ok, error = _run_ffmpeg_progress_command(
            encode_cmd,
            job.duration_seconds,
            callback,
            job.job_id,
            progress_start=75.0,
            progress_end=100.0,
        )
        if not ok:
            return RunResult(ok=False, status="Failed", output_path=plan.output_path, error=f"Encode step failed: {error}")

    return RunResult(ok=True, status="Done", output_path=plan.output_path)


def _run_rubberband_cli(
    input_wav: Path,
    output_wav: Path,
    tempo: float,
    callback: ProgressCallback | None,
    job_id: str,
) -> tuple[bool, str | None]:
    tempo_str = f"{tempo:.10f}".rstrip("0").rstrip(".")
    attempts = [
        ["rubberband", "--tempo", tempo_str, str(input_wav), str(output_wav)],
        ["rubberband", "-T", tempo_str, str(input_wav), str(output_wav)],
    ]
    last_error = "rubberband failed"
    for cmd in attempts:
        if callback:
            callback("command", {"cmd": cmd, "job_id": job_id})
        ok, error = _run_simple_command(cmd)
        if ok:
            return True, None
        last_error = error or last_error
    return False, last_error


def _run_sox_tempo(
    input_wav: Path,
    output_wav: Path,
    tempo: float,
    callback: ProgressCallback | None,
    job_id: str,
) -> tuple[bool, str | None]:
    # "tempo -s" prefers smoother quality over the default quick mode.
    tempo_str = f"{tempo:.10f}".rstrip("0").rstrip(".")
    attempts = [
        ["sox", str(input_wav), str(output_wav), "tempo", "-s", tempo_str],
        ["sox", str(input_wav), str(output_wav), "tempo", tempo_str],
    ]
    last_error = "sox failed"
    for cmd in attempts:
        if callback:
            callback("command", {"cmd": cmd, "job_id": job_id})
        ok, error = _run_simple_command(cmd)
        if ok:
            return True, None
        last_error = error or last_error
    return False, last_error


def _run_simple_command(cmd: list[str]) -> tuple[bool, str | None]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        return True, None
    return False, (proc.stderr or proc.stdout or f"command exited with code {proc.returncode}").strip()


def _select_bitrate_arg(settings: BatchSettings, job: ConversionJob, target_codec: str) -> str | None:
    if not codec_needs_bitrate(target_codec):
        return None

    requested = (settings.bitrate or "").strip().lower()
    if requested in {"", "auto"}:
        src_bps = job.audio_stream.bit_rate_bps
        if src_bps and src_bps > 0:
            # Round to nearest kbps for FFmpeg-friendly values like 192k / 640k.
            kbps = max(8, int(round(src_bps / 1000.0)))
            return f"{kbps}k"
        return None

    return settings.bitrate.strip()


def _run_ffmpeg_progress_command(
    cmd: list[str],
    duration_seconds: float | None,
    callback: ProgressCallback | None,
    job_id: str,
    *,
    progress_start: float = 0.0,
    progress_end: float = 100.0,
) -> tuple[bool, str | None]:
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    progress_state: dict[str, str] = {}
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        progress_state[key] = value
        if key == "progress":
            pct = _progress_percent(duration_seconds, progress_state)
            scaled = _scale_progress(pct, progress_start, progress_end)
            if callback:
                callback("progress", {"percent": scaled, "state": progress_state.copy(), "job_id": job_id})
            progress_state = {}

    stderr_text = ""
    if proc.stderr is not None:
        stderr_text = proc.stderr.read().strip()
    return_code = proc.wait()
    if return_code == 0:
        if callback:
            callback("progress", {"percent": progress_end, "job_id": job_id})
        return True, None
    return False, stderr_text or f"ffmpeg exited with code {return_code}"


def _scale_progress(percent: float, start: float, end: float) -> float:
    if end <= start:
        return max(0.0, min(100.0, end))
    clamped = max(0.0, min(100.0, percent))
    return start + ((end - start) * (clamped / 100.0))


def _progress_percent(duration_seconds: float | None, progress_state: dict[str, str]) -> float:
    if not duration_seconds or duration_seconds <= 0:
        return 0.0
    out_time_us = progress_state.get("out_time_us")
    out_time_ms = progress_state.get("out_time_ms")
    processed_seconds = None
    if out_time_us and out_time_us.isdigit():
        processed_seconds = int(out_time_us) / 1_000_000
    elif out_time_ms and out_time_ms.isdigit():
        processed_seconds = int(out_time_ms) / 1_000_000
    if processed_seconds is None:
        return 0.0
    pct = (processed_seconds / duration_seconds) * 100
    return max(0.0, min(100.0, pct))
