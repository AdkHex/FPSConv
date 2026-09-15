"""The actual signal processing: exact-ratio resampling, dither, measurement.

This module is where the central correction to both legacy programs lives.

A frame-rate change is a **speed change**, and a speed change is a **resample**:
the pitch moves with the rate, exactly as it does when film is projected faster.
``atempo`` (A-2), ``sox tempo`` and ``rubberband --tempo`` (B-2) are all
pitch-preserving time-stretchers — the wrong operation, applied by default, to
every job.

Exactness survives all the way into libsoxr.  The legacy code rendered the
``Fraction`` into a string for ffmpeg's option parser to evaluate as a C double,
and deliberately truncated to ten decimal places for the SoX and Rubber Band
engines (B-1).  Here the speed ratio is converted into a *pair of integers*
that libsoxr consumes directly:

    ratio = out_rate / (in_rate x speed)
          = (out_rate x speed.denominator) / (in_rate x speed.numerator)

Both sides are integers, reduced by their GCD, so the resampler is driven by the
exact rational and the output sample count is asserted against it afterwards.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from fractions import Fraction
from math import gcd
from pathlib import Path
from typing import Any, Callable

from .audiofile import BLOCK_FRAMES, info, open_reader, open_writer
from .contracts import DitherMode, Refusal, RefusalCode
from .ratio import round_half_up

__all__ = [
    "DEFAULT_QUALITY",
    "ResampleReport",
    "dither_and_quantize",
    "integer_rate_pair",
    "measure_loudness",
    "null_test",
    "measure_loudness_ffmpeg",
    "pcm_md5_file",
    "quantisation_floor_dbfs",
    "quantize_to_raw",
    "resample_file",
    "synth",
]

#: libsoxr's very-high-quality linear-phase setting.  Anything less is a
#: deliberate quality reduction and would have to be argued for.
DEFAULT_QUALITY = "VHQ"

ProgressFn = Callable[[float], None]


def _numpy() -> Any:
    from .adapters import get_registry

    return get_registry().adapters["numpy"].load()  # type: ignore[attr-defined]


def _soxr() -> Any:
    from .adapters import get_registry

    return get_registry().soxr.load()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# The exact ratio → integer rate pair
# --------------------------------------------------------------------------- #

def integer_rate_pair(
    *, in_rate: int, out_rate: int, speed: Fraction
) -> tuple[int, int]:
    """Reduce the resample ratio to the smallest exact integer pair.

    Returns ``(soxr_in_rate, soxr_out_rate)`` whose quotient equals
    ``in_rate x speed / out_rate`` exactly.  libsoxr only ever sees integers.

    >>> integer_rate_pair(in_rate=48000, out_rate=48000, speed=Fraction(1001, 960))
    (1001, 960)
    """
    if in_rate <= 0 or out_rate <= 0:
        raise ValueError("sample rates must be positive")
    if speed <= 0:
        raise ValueError("speed must be positive")
    numerator = in_rate * speed.numerator      # effective input rate
    denominator = out_rate * speed.denominator  # effective output rate
    divisor = gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


@dataclass(frozen=True, slots=True)
class ResampleReport:
    input_frames: int
    output_frames: int
    expected_frames: int
    in_rate: int
    out_rate: int
    soxr_in_rate: int
    soxr_out_rate: int
    quality: str
    trimmed: int = 0
    padded: int = 0

    @property
    def exact(self) -> bool:
        return self.output_frames == self.expected_frames

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_frames": self.input_frames,
            "output_frames": self.output_frames,
            "expected_frames": self.expected_frames,
            "in_rate": self.in_rate,
            "out_rate": self.out_rate,
            "soxr_ratio": f"{self.soxr_out_rate}/{self.soxr_in_rate}",
            "quality": self.quality,
            "trimmed": self.trimmed,
            "padded": self.padded,
        }


def resample_file(
    source: Path,
    destination: Path,
    *,
    speed: Fraction,
    out_rate: int | None = None,
    quality: str = DEFAULT_QUALITY,
    subtype: str = "FLOAT",
    progress: ProgressFn | None = None,
) -> ResampleReport:
    """Speed-change ``source`` into ``destination`` by the exact ``speed``.

    Streamed in blocks, so a feature-length 7.1 track never has to fit in RAM.
    The realised frame count is forced to the exact expected value: libsoxr's
    output length can differ by a frame or two from the ideal rational result
    because of filter delay, and a sample-count contract that is "off by one
    sometimes" is not a contract.
    """
    np = _numpy()
    soxr = _soxr()

    meta = info(source)
    in_rate = meta.samplerate
    target_rate = int(out_rate) if out_rate else in_rate
    soxr_in, soxr_out = integer_rate_pair(
        in_rate=in_rate, out_rate=target_rate, speed=speed
    )

    expected = round_half_up(
        Fraction(meta.frames * target_rate, 1) / (Fraction(in_rate) * speed)
    )

    stream = _make_stream(soxr, soxr_in, soxr_out, meta.channels, quality)
    written = 0
    read_frames = 0

    with open_reader(source) as reader, open_writer(
        destination, samplerate=target_rate, channels=meta.channels, subtype=subtype
    ) as writer:
        while True:
            block = reader.read(BLOCK_FRAMES, dtype="float32", always_2d=True)
            last = len(block) == 0 or read_frames + len(block) >= meta.frames
            if len(block):
                read_frames += len(block)
            out = _resample_chunk(stream, np, block, last=last)
            if out is not None and len(out):
                # Never write more than the contract allows.
                room = expected - written
                if room <= 0:
                    pass
                else:
                    if len(out) > room:
                        out = out[:room]
                    writer.write(out)
                    written += len(out)
            if progress is not None and meta.frames:
                progress(min(100.0, read_frames / meta.frames * 100.0))
            if last:
                break

        trimmed = 0
        padded = 0
        if written > expected:
            trimmed = written - expected
        elif written < expected:
            padded = expected - written
            writer.write(np.zeros((padded, meta.channels), dtype="float32"))
            written = expected

    return ResampleReport(
        input_frames=meta.frames,
        output_frames=written,
        expected_frames=expected,
        in_rate=in_rate,
        out_rate=target_rate,
        soxr_in_rate=soxr_in,
        soxr_out_rate=soxr_out,
        quality=quality,
        trimmed=trimmed,
        padded=padded,
    )


def _make_stream(soxr: Any, in_rate: int, out_rate: int, channels: int, quality: str) -> Any:
    if not hasattr(soxr, "ResampleStream"):
        return None
    return soxr.ResampleStream(
        in_rate, out_rate, channels, dtype="float32", quality=quality
    )


def _resample_chunk(stream: Any, np: Any, block: Any, *, last: bool) -> Any:
    if stream is None:
        return None if not len(block) else block
    if not len(block) and not last:
        return None
    return stream.resample_chunk(block, last=last)


def resample_array(
    data: Any, *, in_rate: int, out_rate: int, speed: Fraction, quality: str = DEFAULT_QUALITY
) -> Any:
    """One-shot resample of an in-memory array.  Used by tests and the null test."""
    soxr = _soxr()
    soxr_in, soxr_out = integer_rate_pair(in_rate=in_rate, out_rate=out_rate, speed=speed)
    return soxr.resample(data, soxr_in, soxr_out, quality=quality)


# --------------------------------------------------------------------------- #
# Quantisation and dither  (§3.4: quantise once, dither only at the final step)
# --------------------------------------------------------------------------- #

_SUBTYPES = {16: "PCM_16", 24: "PCM_24", 32: "PCM_32"}


def _subtype(bit_depth: int) -> str:
    subtype = _SUBTYPES.get(bit_depth)
    if subtype is None:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Cannot quantise to {bit_depth} bits.",
            remedies=["Choose 16, 24 or 32."],
        )
    return subtype


def _quantize_stream(
    source: Path,
    open_destination: Callable[[int, int, str], Any],
    *,
    bit_depth: int,
    mode: DitherMode,
    seed: int,
    progress: ProgressFn | None,
) -> dict[str, Any]:
    """Shared dither+quantise loop.

    TPDF (triangular probability density function) at 1 LSB peak-to-peak is the
    textbook default: it fully decorrelates the quantisation error from the
    signal, at the cost of ~4.8 dB of noise floor.  ``SHAPED`` adds a
    first-order highpass to the dither noise, moving it where the ear is less
    sensitive.
    """
    np = _numpy()
    meta = info(source)
    subtype = _subtype(bit_depth)
    full_scale = float(2 ** (bit_depth - 1))
    lsb = 1.0 / full_scale
    rng = np.random.default_rng(seed)
    written = clipped = 0
    error_state = None

    with open_reader(source) as reader, open_destination(
        meta.samplerate, meta.channels, subtype
    ) as writer:
        while True:
            block = reader.read(BLOCK_FRAMES, dtype="float64", always_2d=True)
            if len(block) == 0:
                break
            if mode is DitherMode.NONE:
                shaped = block
            else:
                # TPDF = difference of two independent uniform LSB/2 variates.
                noise = (rng.random(block.shape) - rng.random(block.shape)) * lsb
                if mode is DitherMode.SHAPED:
                    if error_state is None or error_state.shape != (block.shape[1],):
                        error_state = np.zeros(block.shape[1], dtype="float64")
                    previous = np.empty_like(noise)
                    previous[0] = error_state
                    previous[1:] = noise[:-1]
                    noise = noise - previous
                    error_state = noise[-1].copy()
                shaped = block + noise

            quantised = np.rint(shaped * full_scale)
            # Two's complement is asymmetric: -full_scale is representable,
            # +full_scale is not. Counting |x| > full_scale-1 would report every
            # negative full-scale sample as clipped when nothing was lost.
            clipped += int(
                np.count_nonzero(
                    (quantised > full_scale - 1) | (quantised < -full_scale)
                )
            )
            np.clip(quantised, -full_scale, full_scale - 1, out=quantised)
            writer.write(quantised / full_scale)
            written += len(block)
            if progress is not None and meta.frames:
                progress(min(100.0, written / meta.frames * 100.0))

    return {
        "frames": written,
        "bit_depth": bit_depth,
        "dither": mode.value,
        "clipped_samples": clipped,
        "sample_rate": meta.samplerate,
        "channels": meta.channels,
    }


def dither_and_quantize(
    source: Path,
    destination: Path,
    *,
    bit_depth: int,
    mode: DitherMode = DitherMode.TPDF,
    seed: int = 0x5EED,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Write ``source`` at ``bit_depth`` into a normal container, dithered once.

    The legacy build quantised to 24-bit without dither *before* the stretch and
    again on encode — twice per job, both times undithered (B-9).
    """

    def opener(rate: int, channels: int, subtype: str) -> Any:
        return open_writer(
            destination, samplerate=rate, channels=channels, subtype=subtype
        )

    return _quantize_stream(
        source, opener, bit_depth=bit_depth, mode=mode, seed=seed, progress=progress
    )


def quantize_to_raw(
    source: Path,
    destination: Path,
    *,
    bit_depth: int,
    mode: DitherMode = DitherMode.TPDF,
    seed: int = 0x5EED,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Same, but into a **headerless** little-endian PCM stream.

    This is what the external encoders are fed.  A WAV header cannot describe
    more than 4 GB and a 2-hour 7.1 24-bit 48 kHz track is 8.29 GB (B-8); a raw
    stream has no such ceiling.
    """
    from .adapters import get_registry

    sf = get_registry().soundfile.load()
    destination.parent.mkdir(parents=True, exist_ok=True)

    def opener(rate: int, channels: int, subtype: str) -> Any:
        return sf.SoundFile(
            str(destination),
            mode="w",
            samplerate=rate,
            channels=channels,
            subtype=subtype,
            format="RAW",
            endian="LITTLE",
        )

    return _quantize_stream(
        source, opener, bit_depth=bit_depth, mode=mode, seed=seed, progress=progress
    )


# --------------------------------------------------------------------------- #
# Measurement / verification primitives
# --------------------------------------------------------------------------- #

def pcm_md5_file(path: Path, *, bit_depth: int | None = None) -> str:
    """MD5 of the decoded PCM sample values.

    Used for the bit-perfect assertion on the lossless retime path: a
    redeclaration must not change a single sample, so this hash must match the
    source's exactly.

    ``bit_depth`` makes the hash **representation-independent**, which is what
    the assertion actually needs.  The retime chain decodes to float32, retimes,
    then writes integer PCM; hashing the raw bytes would compare float32 against
    int24 and report a difference that is not there.  With ``bit_depth`` set,
    both sides are resolved to the same integer grid first, so the hash answers
    the real question: are these the same samples?
    """
    np = _numpy()
    meta = info(path)
    digest = hashlib.md5()  # noqa: S324 - integrity check, not a security primitive

    if bit_depth is None:
        dtype = "int32" if meta.subtype.upper().startswith("PCM_") else "float64"
        with open_reader(path) as reader:
            while True:
                block = reader.read(BLOCK_FRAMES, dtype=dtype, always_2d=True)
                if len(block) == 0:
                    break
                digest.update(block.tobytes(order="C"))
        return digest.hexdigest()

    full_scale = float(2 ** (bit_depth - 1))
    with open_reader(path) as reader:
        while True:
            block = reader.read(BLOCK_FRAMES, dtype="float64", always_2d=True)
            if len(block) == 0:
                break
            digest.update(
                np.rint(block * full_scale).astype("int64").tobytes(order="C")
            )
    return digest.hexdigest()


#: Frames excluded at each end of a null test.  A resampler's filter ramps in
#: and out at the signal boundaries, and the forward pass truncates its tail at
#: the exact expected frame count, so the first and last few dozen frames of a
#: round trip can never null.  Measured on this build: 260 frames out of 192000
#: exceed -140 dBFS, all within ~130 frames of each edge.  The interior figure
#: is the one that says whether the chain is transparent.
NULL_TEST_EDGE_FRAMES = 1024


def null_test(
    reference: Path,
    candidate: Path,
    *,
    max_frames: int | None = None,
    skip_edge_frames: int = NULL_TEST_EDGE_FRAMES,
) -> dict[str, Any]:
    """Subtract two files and report the residual.

    Reports both the whole-file residual and the interior residual (edges
    excluded).  The −140 dBFS acceptance gate applies to the interior; the edge
    figure is reported separately rather than quietly folded in, because a
    number that includes an unavoidable boundary transient is not a measure of
    transparency.

    Length mismatch is reported rather than silently truncated away.
    """
    np = _numpy()
    ref_meta = info(reference)
    cand_meta = info(candidate)

    if ref_meta.channels != cand_meta.channels:
        return {
            "ok": False,
            "reason": (
                f"channel count differs: {ref_meta.channels} vs {cand_meta.channels}"
            ),
        }
    if ref_meta.samplerate != cand_meta.samplerate:
        return {
            "ok": False,
            "reason": (
                f"sample rate differs: {ref_meta.samplerate} vs {cand_meta.samplerate}"
            ),
        }

    frames = min(ref_meta.frames, cand_meta.frames)
    if max_frames:
        frames = min(frames, max_frames)

    edge = max(0, min(skip_edge_frames, frames // 4))
    interior_start, interior_end = edge, frames - edge

    sum_sq = peak = 0.0
    count = 0
    inner_sum_sq = inner_peak = 0.0
    inner_count = 0
    position = 0

    with open_reader(reference) as ref, open_reader(candidate) as cand:
        remaining = frames
        while remaining > 0:
            take = min(BLOCK_FRAMES, remaining)
            a = ref.read(take, dtype="float64", always_2d=True)
            b = cand.read(take, dtype="float64", always_2d=True)
            if len(a) == 0 or len(b) == 0:
                break
            n = min(len(a), len(b))
            diff = a[:n] - b[:n]
            sum_sq += float(np.sum(diff * diff))
            peak = max(peak, float(np.max(np.abs(diff))) if diff.size else 0.0)
            count += diff.size

            lo = max(interior_start - position, 0)
            hi = min(interior_end - position, n)
            if hi > lo:
                inner = diff[lo:hi]
                inner_sum_sq += float(np.sum(inner * inner))
                inner_peak = max(
                    inner_peak, float(np.max(np.abs(inner))) if inner.size else 0.0
                )
                inner_count += inner.size

            position += n
            remaining -= n

    rms = math.sqrt(sum_sq / count) if count else 0.0
    inner_rms = math.sqrt(inner_sum_sq / inner_count) if inner_count else 0.0
    return {
        "ok": True,
        "rms_dbfs": _dbfs(inner_rms),
        "peak_dbfs": _dbfs(inner_peak),
        "rms_dbfs_full": _dbfs(rms),
        "peak_dbfs_full": _dbfs(peak),
        "frames_compared": frames,
        "edge_frames_excluded": edge,
        "length_delta": ref_meta.frames - cand_meta.frames,
    }


def _dbfs(value: float) -> float:
    if value <= 0:
        return -float("inf")
    return 20.0 * math.log10(value)


def quantisation_floor_dbfs(bit_depth: int) -> float:
    """Theoretical RMS noise floor of a TPDF-dithered N-bit quantiser, in dBFS.

    With full scale at +/-1 the step is ``2^(1-N)``; TPDF dither plus
    quantisation error has RMS ``step / sqrt(6)``.  A null test can never go
    below this, so it is what the acceptance gate has to be measured against for
    an integer output: 16-bit bottoms out near -98 dBFS no matter how good the
    resampler is.
    """
    step = 2.0 ** (1 - bit_depth)
    return _dbfs(step / math.sqrt(6.0))


def measure_loudness_ffmpeg(path: Path, registry: Any = None) -> dict[str, Any]:
    """Integrated loudness via ffmpeg's ``ebur128`` filter.

    Used where pyloudnorm cannot go: it rejects layouts with more than five
    channels, which rules out every 5.1 and 7.1 track — i.e. most of what this
    tool exists to handle.  ffmpeg's implementation is also streaming, so it
    does not need the whole file in memory.
    """
    from .adapters import get_registry

    reg = registry if registry is not None else get_registry()
    ffmpeg = reg.get("ffmpeg")
    if ffmpeg is None or not ffmpeg.detect().found:
        raise RuntimeError("ffmpeg is not available for loudness measurement")

    outcome = ffmpeg.run(ffmpeg.loudness_command(path))
    blob = f"{outcome.stdout}\n{outcome.stderr}"

    import re

    loudness = re.search(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", blob, re.MULTILINE)
    peak = re.search(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", blob, re.MULTILINE)
    if not loudness:
        raise RuntimeError(
            outcome.message if not outcome.ok else "ffmpeg reported no integrated loudness"
        )
    return {
        "lufs": float(loudness.group(1)),
        "true_peak_dbfs": float(peak.group(1)) if peak else None,
        "truncated": False,
        "tool": "ffmpeg ebur128",
        # ffmpeg's summary prints one decimal place, so a delta between two of
        # its readings is only meaningful to +/-0.1 LU.
        "resolution_lu": 0.1,
    }


def measure_loudness(path: Path, *, max_seconds: float | None = None) -> dict[str, Any]:
    """Integrated loudness in LUFS via pyloudnorm (BS.1770-4).

    pyloudnorm needs the whole signal in memory, so very long files are
    measured over a bounded window and the report says so — a truncated
    measurement that admits it is more useful than an out-of-memory crash.
    """
    np = _numpy()
    from .adapters import get_registry

    pyln = get_registry().pyloudnorm.load()  # type: ignore[attr-defined]
    meta = info(path)
    limit = meta.frames
    truncated = False
    if max_seconds:
        cap = int(max_seconds * meta.samplerate)
        if cap < limit:
            limit = cap
            truncated = True

    with open_reader(path) as reader:
        data = reader.read(limit, dtype="float64", always_2d=True)

    meter = pyln.Meter(meta.samplerate)
    if data.shape[1] == 1:
        data = data[:, 0]
    loudness = float(meter.integrated_loudness(data))
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    return {
        "lufs": loudness,
        "true_peak_dbfs": _dbfs(peak),
        "seconds_measured": limit / meta.samplerate if meta.samplerate else 0.0,
        "truncated": truncated,
        "tool": "pyloudnorm",
        "resolution_lu": 0.01,
    }


# --------------------------------------------------------------------------- #
# Synthetic test signals — the whole verification suite, no copyrighted source
# --------------------------------------------------------------------------- #

class synth:
    """Generators for the synthetic signal suite (Part 7 of the plan).

    Sweeps, impulses, silence and full-scale squares, at every channel layout
    and bit depth, so the maths is provable without any copyrighted material.
    """

    @staticmethod
    def sine(seconds: float, rate: int, freq: float = 997.0, channels: int = 1, amp: float = 0.5) -> Any:
        np = _numpy()
        t = np.arange(int(seconds * rate), dtype="float64") / rate
        wave = amp * np.sin(2 * np.pi * freq * t)
        return np.tile(wave[:, None], (1, channels)).astype("float32")

    @staticmethod
    def sweep(
        seconds: float, rate: int, f0: float = 20.0, f1: float | None = None,
        channels: int = 1, amp: float = 0.5,
    ) -> Any:
        """Exponential sine sweep — the standard resampler stress signal."""
        np = _numpy()
        f1 = f1 if f1 is not None else rate * 0.45
        n = int(seconds * rate)
        t = np.arange(n, dtype="float64") / rate
        k = np.log(f1 / f0)
        phase = 2 * np.pi * f0 * seconds / k * (np.exp(t * k / seconds) - 1.0)
        wave = amp * np.sin(phase)
        return np.tile(wave[:, None], (1, channels)).astype("float32")

    @staticmethod
    def impulse(seconds: float, rate: int, channels: int = 1, position: float = 0.5) -> Any:
        np = _numpy()
        n = int(seconds * rate)
        data = np.zeros((n, channels), dtype="float32")
        data[min(n - 1, int(n * position)), :] = 1.0
        return data

    @staticmethod
    def silence(seconds: float, rate: int, channels: int = 1) -> Any:
        np = _numpy()
        return np.zeros((int(seconds * rate), channels), dtype="float32")

    @staticmethod
    def square(
        seconds: float, rate: int, freq: float = 100.0, channels: int = 1,
        amp: float = 1.0,
    ) -> Any:
        """Square wave — worst case for intersample peaks.

        At ``amp=1.0`` the positive half sits at exactly +1.0, which is *not*
        representable in signed integer PCM (two's complement runs -2^(N-1) to
        +2^(N-1)-1). Those samples legitimately clip; that is a property of the
        format, not a defect, and the quantiser counts them.
        """
        np = _numpy()
        n = int(seconds * rate)
        t = np.arange(n, dtype="float64") / rate
        wave = np.where(np.sin(2 * np.pi * freq * t) >= 0, amp, -amp)
        return np.tile(wave[:, None], (1, channels)).astype("float32")

    @staticmethod
    def write(path: Path, data: Any, rate: int, subtype: str = "FLOAT") -> Path:
        with open_writer(
            path, samplerate=rate, channels=data.shape[1], subtype=subtype
        ) as writer:
            writer.write(data)
        return path
