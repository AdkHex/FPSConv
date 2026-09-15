"""The synthetic signal suite — Part 7's verification, without any source media.

Sweeps, impulses, silence and full-scale squares, at every channel layout
(mono, stereo, 5.1, 7.1) and bit depth (16, 24, 32f), so the maths is provable
with nothing copyrighted anywhere near it.

The acceptance gates asserted here:

* exact output sample count for every preset and layout
* bit-perfect sample-rate redeclaration (PCM MD5 in == MD5 out)
* null test below -140 dBFS for a float round trip
* the exact rational reaches libsoxr as an integer pair
"""

from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

try:
    import numpy  # noqa: F401
    import soundfile  # noqa: F401
    import soxr  # noqa: F401

    HAVE_DSP = True
except ImportError:  # pragma: no cover
    HAVE_DSP = False

from fpsaudio.core.contracts import DitherMode
from fpsaudio.core.ratio import get_preset, presets, round_half_up

if HAVE_DSP:
    from fpsaudio.core.audiofile import info, rewrite_sample_rate
    from fpsaudio.core.dsp import (
        integer_rate_pair,
        null_test,
        pcm_md5_file,
        quantisation_floor_dbfs,
        quantize_to_raw,
        resample_file,
        synth,
    )

LAYOUTS = (1, 2, 6, 8)
RATE = 48000


@unittest.skipUnless(HAVE_DSP, "numpy / soundfile / soxr are not installed")
class TestIntegerRatePair(unittest.TestCase):
    """B-1: exactness must survive the tool boundary."""

    def test_the_fraction_reaches_soxr_as_integers(self) -> None:
        pair = integer_rate_pair(in_rate=48000, out_rate=48000, speed=Fraction(1001, 960))
        self.assertEqual(pair, (1001, 960))
        self.assertIsInstance(pair[0], int)
        self.assertIsInstance(pair[1], int)

    def test_ratio_is_exactly_preserved_for_every_preset(self) -> None:
        for key, ratio in presets().items():
            if ratio.is_identity:
                continue
            for rate in (44100, 48000, 96000):
                soxr_in, soxr_out = integer_rate_pair(
                    in_rate=rate, out_rate=rate, speed=ratio.speed
                )
                self.assertEqual(
                    Fraction(soxr_in, soxr_out), ratio.speed, f"{key} @ {rate}"
                )

    def test_rate_change_folded_into_the_same_pair(self) -> None:
        ratio = get_preset("23.976_to_25")
        soxr_in, soxr_out = integer_rate_pair(
            in_rate=48000, out_rate=44100, speed=ratio.speed
        )
        self.assertEqual(
            Fraction(soxr_out, soxr_in),
            Fraction(44100) / (Fraction(48000) * ratio.speed),
        )


@unittest.skipUnless(HAVE_DSP, "numpy / soundfile / soxr are not installed")
class TestResampleSampleCounts(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_test_"))

    def test_exact_frame_count_across_layouts_and_presets(self) -> None:
        for channels in LAYOUTS:
            source = self.dir / f"src_{channels}.wav"
            synth.write(source, synth.sweep(2.0, RATE, f1=15000, channels=channels), RATE)
            frames_in = info(source).frames
            for key in ("23.976_to_25", "24_to_25", "25_to_24", "29.97_to_25"):
                ratio = get_preset(key)
                out = self.dir / f"out_{channels}_{key}.wav"
                report = resample_file(source, out, speed=ratio.speed)
                expected = round_half_up(Fraction(frames_in) / ratio.speed)
                self.assertEqual(report.output_frames, expected, f"{key} {channels}ch")
                self.assertEqual(info(out).frames, expected)
                self.assertTrue(report.exact)

    def test_resample_to_a_different_output_rate(self) -> None:
        source = self.dir / "src.wav"
        synth.write(source, synth.sine(1.0, RATE, channels=2), RATE)
        ratio = get_preset("24_to_25")
        out = self.dir / "out44.wav"
        report = resample_file(source, out, speed=ratio.speed, out_rate=44100)
        self.assertEqual(info(out).samplerate, 44100)
        self.assertEqual(report.output_frames, report.expected_frames)


@unittest.skipUnless(HAVE_DSP, "numpy / soundfile / soxr are not installed")
class TestBitExactRedeclare(unittest.TestCase):
    """§3.2 / A-12: the lossless retime that neither legacy program had."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_test_"))

    def test_pcm_md5_is_identical_across_layouts_and_depths(self) -> None:
        for channels in LAYOUTS:
            for subtype, depth in (("PCM_16", 16), ("PCM_24", 24), ("PCM_32", 32)):
                source = self.dir / f"s_{channels}_{depth}.wav"
                synth.write(
                    source, synth.sweep(1.0, RATE, f1=15000, channels=channels), RATE,
                    subtype=subtype,
                )
                out = self.dir / f"r_{channels}_{depth}.wav"
                ratio = get_preset("24_to_25")
                new_rate = int(ratio.redeclared_rate(RATE))
                rewrite_sample_rate(source, out, new_rate)

                self.assertEqual(info(out).samplerate, new_rate)
                self.assertEqual(info(out).frames, info(source).frames)
                self.assertEqual(
                    pcm_md5_file(source, bit_depth=depth),
                    pcm_md5_file(out, bit_depth=depth),
                    f"{channels}ch {subtype}",
                )

    def test_duration_changes_by_exactly_the_ratio(self) -> None:
        source = self.dir / "s.wav"
        synth.write(source, synth.sine(2.0, RATE, channels=2), RATE, subtype="PCM_24")
        ratio = get_preset("24_to_25")
        out = self.dir / "r.wav"
        rewrite_sample_rate(source, out, int(ratio.redeclared_rate(RATE)))
        before = Fraction(info(source).frames, RATE)
        after = Fraction(info(out).frames, info(out).samplerate)
        self.assertEqual(after, before / ratio.speed)


@unittest.skipUnless(HAVE_DSP, "numpy / soundfile / soxr are not installed")
class TestNullTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_test_"))

    def test_float_round_trip_nulls_below_minus_140_dbfs(self) -> None:
        """The Part 6 acceptance gate, at every channel layout."""
        for channels in LAYOUTS:
            source = self.dir / f"n_{channels}.wav"
            synth.write(source, synth.sine(2.0, RATE, 997.0, channels=channels), RATE)
            ratio = get_preset("23.976_to_25")
            forward = self.dir / f"f_{channels}.wav"
            back = self.dir / f"b_{channels}.wav"
            resample_file(source, forward, speed=ratio.speed)
            resample_file(forward, back, speed=1 / ratio.speed)
            result = null_test(source, back)
            self.assertTrue(result["ok"])
            self.assertLess(result["rms_dbfs"], -140.0, f"{channels}ch")

    def test_silence_stays_silent(self) -> None:
        source = self.dir / "silence.wav"
        synth.write(source, synth.silence(1.0, RATE, channels=6), RATE)
        out = self.dir / "silence_out.wav"
        resample_file(source, out, speed=get_preset("24_to_25").speed)
        result = null_test(source, out, max_frames=info(out).frames)
        self.assertEqual(result["peak_dbfs"], float("-inf"))

    def test_length_mismatch_is_reported_not_hidden(self) -> None:
        a = self.dir / "a.wav"
        b = self.dir / "b.wav"
        synth.write(a, synth.sine(1.0, RATE, channels=2), RATE)
        synth.write(b, synth.sine(0.5, RATE, channels=2), RATE)
        result = null_test(a, b)
        self.assertEqual(result["length_delta"], RATE // 2)

    def test_channel_mismatch_is_reported(self) -> None:
        a = self.dir / "a2.wav"
        b = self.dir / "b6.wav"
        synth.write(a, synth.sine(0.5, RATE, channels=2), RATE)
        synth.write(b, synth.sine(0.5, RATE, channels=6), RATE)
        result = null_test(a, b)
        self.assertFalse(result["ok"])
        self.assertIn("channel count differs", result["reason"])


@unittest.skipUnless(HAVE_DSP, "numpy / soundfile / soxr are not installed")
class TestDither(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_test_"))

    def test_quantisation_floor_matches_theory(self) -> None:
        self.assertAlmostEqual(quantisation_floor_dbfs(16), -98.09, places=1)
        self.assertAlmostEqual(quantisation_floor_dbfs(24), -146.24, places=1)

    def test_raw_output_size_matches_the_declared_geometry(self) -> None:
        source = self.dir / "q.wav"
        synth.write(source, synth.sine(1.0, RATE, channels=6), RATE)
        out = self.dir / "q.pcm"
        report = quantize_to_raw(source, out, bit_depth=24, mode=DitherMode.TPDF)
        self.assertEqual(report["frames"], RATE)
        self.assertEqual(out.stat().st_size, RATE * 6 * 3)

    def test_square_just_below_full_scale_does_not_clip(self) -> None:
        source = self.dir / "sq99.wav"
        synth.write(source, synth.square(0.5, RATE, channels=2, amp=0.999), RATE)
        out = self.dir / "sq99.pcm"
        report = quantize_to_raw(source, out, bit_depth=24, mode=DitherMode.TPDF)
        self.assertEqual(report["clipped_samples"], 0)

    def test_full_scale_square_clips_only_its_positive_half(self) -> None:
        """Two's complement is asymmetric; -1.0 fits, +1.0 does not.

        The count must reflect that exactly — over-reporting would send users
        hunting for a problem that is a property of integer PCM.
        """
        source = self.dir / "sq.wav"
        synth.write(source, synth.square(0.5, RATE, channels=2, amp=1.0), RATE)
        out = self.dir / "sq.pcm"
        report = quantize_to_raw(source, out, bit_depth=24, mode=DitherMode.NONE)
        total_samples = report["frames"] * 2
        self.assertAlmostEqual(
            report["clipped_samples"] / total_samples, 0.5, places=2
        )

    def test_no_dither_mode_is_deterministic(self) -> None:
        source = self.dir / "d.wav"
        synth.write(source, synth.sine(0.5, RATE, channels=2), RATE)
        first = self.dir / "d1.pcm"
        second = self.dir / "d2.pcm"
        quantize_to_raw(source, first, bit_depth=24, mode=DitherMode.NONE)
        quantize_to_raw(source, second, bit_depth=24, mode=DitherMode.NONE)
        self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
