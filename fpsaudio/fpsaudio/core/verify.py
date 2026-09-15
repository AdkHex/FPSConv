"""Verification: sample counts, PCM MD5, null tests, loudness, layout.

The legacy build had **no verification of any kind** — no sample-count
assertion, no duration check, no PCM MD5, no null test, no loudness
measurement, no channel-layout assertion (B-16).  Nothing from §6 existed.

Every check here reports one of three outcomes, and the distinction matters:

* **pass / fail** — the check ran and the answer is known.
* **skipped** — the check could not run (a tool is missing, the path does not
  support it) and *says why*.  A skipped check is never counted as a pass.

The acceptance gates, from Part 6 of the plan:

* ratio math exact-rational throughout
* PCM MD5 identical on the lossless path
* null tests below −140 dBFS
* loudness delta under 0.1 LU
* duration drift under 1 ms
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .contracts import (
    AudioStream,
    Severity,
    VerifyCheck,
    VerifyResult,
    VerifySpec,
)
from .ratio import RetimeRatio

__all__ = ["verify_output"]


def verify_output(
    *,
    source_stream: AudioStream,
    output_path: Path,
    ratio: RetimeRatio,
    spec: VerifySpec,
    source_frames: int | None = None,
    source_rate: int | None = None,
    expected_frames: int | None = None,
    expected_rate: int | None = None,
    expect_bit_exact: bool = False,
    source_pcm_md5: str | None = None,
    pcm_md5_bits: int | None = None,
    output_bit_depth: int | None = None,
    reference_for_null: Path | None = None,
    registry: Any = None,
) -> VerifyResult:
    """Run every requested check against a finished output."""
    from .adapters import get_registry

    reg = registry if registry is not None else get_registry()
    checks: list[VerifyCheck] = []

    meta = _read_meta(output_path, reg, checks)

    if spec.sample_count:
        checks.append(
            _check_sample_count(
                meta=meta,
                ratio=ratio,
                source_frames=source_frames or source_stream.sample_count,
                source_rate=source_rate or source_stream.sample_rate,
                expected_frames=expected_frames,
                expected_rate=expected_rate,
            )
        )

    if spec.duration_drift:
        checks.append(
            _check_duration(
                meta=meta,
                ratio=ratio,
                source_stream=source_stream,
                source_frames=source_frames,
                source_rate=source_rate,
                tolerance_ms=spec.duration_tolerance_ms,
            )
        )

    if spec.channel_layout:
        checks.append(_check_layout(meta, source_stream))

    if spec.pcm_md5:
        checks.append(
            _check_pcm_md5(
                output_path=output_path,
                expect_bit_exact=expect_bit_exact,
                source_pcm_md5=source_pcm_md5,
                pcm_md5_bits=pcm_md5_bits,
                meta=meta,
            )
        )

    if spec.null_test:
        checks.append(
            _check_null(
                output_path=output_path,
                reference=reference_for_null,
                ratio=ratio,
                max_dbfs=spec.null_test_max_dbfs,
                output_bit_depth=output_bit_depth,
                registry=reg,
            )
        )

    if spec.loudness:
        checks.append(
            _check_loudness(
                output_path=output_path,
                reference=reference_for_null,
                tolerance_lu=spec.loudness_tolerance_lu,
                registry=reg,
            )
        )

    return VerifyResult(checks=tuple(checks))


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class _Meta:
    ok: bool
    frames: int | None = None
    samplerate: int | None = None
    channels: int | None = None
    subtype: str | None = None
    reason: str | None = None


def _read_meta(path: Path, registry: Any, checks: list[VerifyCheck]) -> _Meta:
    """Read the output's geometry, degrading gracefully if we cannot."""
    if not path.exists():
        return _Meta(False, reason=f"{path} was not created")
    try:
        from .audiofile import info

        meta = info(path)
        return _Meta(
            True,
            frames=meta.frames,
            samplerate=meta.samplerate,
            channels=meta.channels,
            subtype=meta.subtype,
        )
    except Exception as exc:  # noqa: BLE001
        # Compressed formats libsndfile cannot open (AAC, Opus in some builds)
        # fall back to ffprobe.
        probe = registry.get("ffprobe")
        if probe is not None and probe.detect().found:
            try:
                payload = probe.probe(path)
                for stream in payload.get("streams", ()):
                    if stream.get("codec_type") != "audio":
                        continue
                    rate = int(stream.get("sample_rate") or 0) or None
                    frames = stream.get("duration_ts")
                    return _Meta(
                        True,
                        frames=int(frames) if frames not in (None, "N/A") else None,
                        samplerate=rate,
                        channels=int(stream.get("channels") or 0) or None,
                        subtype=stream.get("codec_name"),
                    )
            except Exception as probe_exc:  # noqa: BLE001
                return _Meta(False, reason=f"ffprobe could not read it either: {probe_exc}")
        return _Meta(False, reason=str(exc))


def _check_sample_count(
    *,
    meta: _Meta,
    ratio: RetimeRatio,
    source_frames: int | None,
    source_rate: int | None,
    expected_frames: int | None,
    expected_rate: int | None,
) -> VerifyCheck:
    if expected_frames is None:
        if not (source_frames and source_rate):
            return VerifyCheck(
                "sample_count",
                ok=False,
                skipped=True,
                skip_reason="the source frame count was not reported by any prober",
            )
        expected_frames = ratio.output_samples(
            source_frames, src_rate=source_rate, dst_rate=expected_rate or source_rate
        )
    if not meta.ok or meta.frames is None:
        return VerifyCheck(
            "sample_count",
            ok=False,
            skipped=True,
            skip_reason=meta.reason or "the output's frame count could not be read",
            expected=expected_frames,
        )
    delta = meta.frames - expected_frames
    return VerifyCheck(
        "sample_count",
        ok=delta == 0,
        detail=(
            f"{meta.frames} frames, exactly as the rational demands"
            if delta == 0
            else f"{meta.frames} frames, expected {expected_frames} ({delta:+d})"
        ),
        measured=meta.frames,
        expected=expected_frames,
    )


def _check_duration(
    *,
    meta: _Meta,
    ratio: RetimeRatio,
    source_stream: AudioStream,
    source_frames: int | None,
    source_rate: int | None,
    tolerance_ms: float,
) -> VerifyCheck:
    if not meta.ok or not meta.frames or not meta.samplerate:
        return VerifyCheck(
            "duration_drift",
            ok=False,
            skipped=True,
            skip_reason=meta.reason or "the output's duration could not be read",
        )

    frames = source_frames or source_stream.sample_count
    rate = source_rate or source_stream.sample_rate
    if frames and rate:
        expected_s = Fraction(frames, rate) / ratio.speed
    elif source_stream.duration_s:
        expected_s = (
            Fraction(source_stream.duration_s).limit_denominator(10**9) / ratio.speed
        )
    else:
        return VerifyCheck(
            "duration_drift",
            ok=False,
            skipped=True,
            skip_reason="the source duration was not reported by any prober",
        )

    actual_s = Fraction(meta.frames, meta.samplerate)
    drift_ms = float(actual_s - expected_s) * 1000.0
    return VerifyCheck(
        "duration_drift",
        ok=abs(drift_ms) <= tolerance_ms,
        detail=(
            f"{drift_ms:+.4f} ms against an exact target of "
            f"{float(expected_s):.6f} s (tolerance {tolerance_ms} ms)"
        ),
        measured=round(drift_ms, 6),
        expected=0.0,
        tolerance=tolerance_ms,
    )


def _check_layout(meta: _Meta, source_stream: AudioStream) -> VerifyCheck:
    if not meta.ok or meta.channels is None:
        return VerifyCheck(
            "channel_layout",
            ok=False,
            skipped=True,
            skip_reason=meta.reason or "the output's channel count could not be read",
        )
    expected = source_stream.channels
    if expected is None:
        return VerifyCheck(
            "channel_layout",
            ok=True,
            severity=Severity.WARN,
            detail=f"output has {meta.channels} channels; the source count was unknown",
            measured=meta.channels,
        )
    return VerifyCheck(
        "channel_layout",
        ok=meta.channels == expected,
        detail=(
            f"{meta.channels} channels, matching the source"
            if meta.channels == expected
            else f"{meta.channels} channels, but the source had {expected} — "
                 f"channels were added or dropped"
        ),
        measured=meta.channels,
        expected=expected,
    )


def _check_pcm_md5(
    *,
    output_path: Path,
    expect_bit_exact: bool,
    source_pcm_md5: str | None,
    pcm_md5_bits: int | None,
    meta: _Meta,
) -> VerifyCheck:
    if not expect_bit_exact:
        return VerifyCheck(
            "pcm_md5",
            ok=True,
            skipped=True,
            skip_reason=(
                "this path resamples, so the PCM must change; a bit-exact assertion "
                "only applies to the sample-rate redeclaration path"
            ),
        )
    if not source_pcm_md5:
        return VerifyCheck(
            "pcm_md5",
            ok=False,
            skipped=True,
            skip_reason="no source PCM MD5 was captured before the retime",
        )
    if not meta.ok:
        return VerifyCheck(
            "pcm_md5",
            ok=False,
            skipped=True,
            skip_reason=meta.reason or "the output could not be read",
        )
    try:
        from .dsp import pcm_md5_file

        actual = pcm_md5_file(output_path, bit_depth=pcm_md5_bits)
    except Exception as exc:  # noqa: BLE001
        return VerifyCheck(
            "pcm_md5", ok=False, skipped=True, skip_reason=f"could not hash output: {exc}"
        )
    return VerifyCheck(
        "pcm_md5",
        ok=actual == source_pcm_md5,
        detail=(
            f"bit-exact: {actual}"
            if actual == source_pcm_md5
            else f"PCM CHANGED on a path that promised not to: {source_pcm_md5} -> {actual}"
        ),
        measured=actual,
        expected=source_pcm_md5,
    )


def _check_null(
    *,
    output_path: Path,
    reference: Path | None,
    ratio: RetimeRatio,
    max_dbfs: float,
    output_bit_depth: int | None,
    registry: Any,
) -> VerifyCheck:
    if reference is None:
        return VerifyCheck(
            "null_test",
            ok=False,
            skipped=True,
            skip_reason="no pre-retime reference was kept (use --keep-intermediates)",
        )
    if not reference.exists():
        return VerifyCheck(
            "null_test", ok=False, skipped=True,
            skip_reason=f"reference {reference.name} no longer exists",
        )

    import tempfile

    try:
        from .dsp import null_test, resample_file

        with tempfile.TemporaryDirectory(prefix="fpsaudio_null_") as tmp:
            reverted = Path(tmp) / "reverted.w64"
            resample_file(output_path, reverted, speed=1 / ratio.speed)
            result = null_test(reference, reverted)
    except Exception as exc:  # noqa: BLE001
        return VerifyCheck(
            "null_test", ok=False, skipped=True, skip_reason=f"could not run: {exc}"
        )

    if not result.get("ok"):
        return VerifyCheck(
            "null_test", ok=False,
            detail=f"could not compare: {result.get('reason')}",
        )

    # An integer output cannot null below its own dither floor, however good the
    # resampler is: 16-bit bottoms out near -98 dBFS. Holding a 16-bit output to
    # -140 dBFS would fail every time and mean nothing, so the gate is relaxed to
    # the physical floor plus a small margin when the output is quantised — and
    # the report says which gate was actually applied.
    effective = max_dbfs
    floor_note = ""
    if output_bit_depth:
        from .dsp import quantisation_floor_dbfs

        floor = quantisation_floor_dbfs(output_bit_depth)
        if floor + _NULL_TEST_MARGIN_DB > max_dbfs:
            effective = floor + _NULL_TEST_MARGIN_DB
            floor_note = (
                f"; gate relaxed from {max_dbfs} to {effective:.1f} dBFS because a "
                f"{output_bit_depth}-bit output's dither floor is {floor:.1f} dBFS"
            )

    rms = result["rms_dbfs"]
    return VerifyCheck(
        "null_test",
        ok=rms <= effective,
        detail=(
            f"residual {rms:.1f} dBFS RMS in the interior "
            f"({result['edge_frames_excluded']} edge frames excluded; "
            f"whole-file {result['rms_dbfs_full']:.1f} dBFS), gate {effective:.1f} dBFS"
            f"{floor_note}"
        ),
        measured=round(rms, 3),
        tolerance=round(effective, 3),
    )


#: Headroom above the theoretical dither floor before a null test is called bad.
_NULL_TEST_MARGIN_DB = 6.0


def _check_loudness(
    *,
    output_path: Path,
    reference: Path | None,
    tolerance_lu: float,
    registry: Any,
) -> VerifyCheck:
    if reference is None or not reference.exists():
        return VerifyCheck(
            "loudness",
            ok=False,
            skipped=True,
            skip_reason="no pre-retime reference was kept (use --keep-intermediates)",
        )
    from .dsp import measure_loudness, measure_loudness_ffmpeg

    def measure(path: Path) -> dict[str, Any]:
        # pyloudnorm rejects layouts above five channels, which rules out every
        # 5.1 and 7.1 track. ffmpeg's ebur128 handles them and streams, so it is
        # the fallback rather than a skipped check.
        errors: list[str] = []
        if registry.pyloudnorm.detect().found:
            try:
                return measure_loudness(path, max_seconds=1800)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"pyloudnorm: {exc}")
        try:
            return measure_loudness_ffmpeg(path, registry)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ffmpeg ebur128: {exc}")
        raise RuntimeError("; ".join(errors) or "no loudness meter available")

    try:
        before = measure(reference)
        after = measure(output_path)
    except Exception as exc:  # noqa: BLE001
        return VerifyCheck(
            "loudness", ok=False, skipped=True, skip_reason=f"could not measure: {exc}"
        )

    delta = after["lufs"] - before["lufs"]

    # A tolerance tighter than the meter's own resolution is not a measurement,
    # it is a coin toss: ffmpeg's ebur128 prints one decimal, so two readings can
    # differ by exactly 0.1 LU with no real change in loudness. Widen the gate by
    # the meter's resolution and say which meter produced the number.
    resolution = max(
        float(before.get("resolution_lu") or 0.0),
        float(after.get("resolution_lu") or 0.0),
    )
    effective = tolerance_lu + resolution
    tool = after.get("tool") or before.get("tool") or "unknown meter"
    note = " (measurement truncated)" if before.get("truncated") or after.get("truncated") else ""

    return VerifyCheck(
        "loudness",
        ok=abs(delta) <= effective,
        detail=(
            f"{before['lufs']:.2f} -> {after['lufs']:.2f} LUFS, delta {delta:+.3f} LU "
            f"(tolerance {tolerance_lu} LU + {resolution:g} LU {tool} resolution "
            f"= {effective:g} LU){note}"
        ),
        measured=round(delta, 4),
        expected=0.0,
        tolerance=round(effective, 4),
    )
