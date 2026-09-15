"""Ratio maths — the property tests Part 7 of the plan asks for.

The central assertions:

* ``out_samples == round(in_samples / speed)`` exactly, for every preset and a
  large set of random source/destination pairs.
* No float ever enters the computation.
* The four ratios Directory A got wrong (A-1) are right here, and the drift
  those wrong ratios cause is the ~300 ms the audit claimed.
"""

from __future__ import annotations

import random
import unittest
from fractions import Fraction

from fpsaudio.core.contracts import Refusal
from fpsaudio.core.ratio import (
    CANONICAL_FPS,
    LEGACY_PRESET_KEYS,
    RetimeRatio,
    drift_seconds,
    exact_redeclare_targets,
    format_fps,
    get_preset,
    parse_fps,
    presets,
    resolve_ratio,
    round_half_up,
)

#: What Directory A actually shipped — converter.py:91-114.
LEGACY_A_RATIOS = {
    "23.976_to_25": Fraction(25025, 24000),
    "23.976_to_24": Fraction(24025, 24000),
    "25_to_23.976": Fraction(24000, 25025),
    "24_to_23.976": Fraction(24000, 24025),
    "25_to_24": Fraction(24025, 25025),
    "24_to_25": Fraction(25025, 24025),
}

#: The correct values, per §3.1 and Part 2 of the plan.
CORRECT_RATIOS = {
    "23.976_to_25": Fraction(25025, 24000),
    "23.976_to_24": Fraction(1001, 1000),
    "25_to_23.976": Fraction(24000, 25025),
    "24_to_23.976": Fraction(1000, 1001),
    "25_to_24": Fraction(24, 25),
    "24_to_25": Fraction(25, 24),
}


class TestFpsParsing(unittest.TestCase):
    def test_broadcast_rates_are_exact_rationals(self) -> None:
        self.assertEqual(parse_fps("23.976"), Fraction(24000, 1001))
        self.assertEqual(parse_fps("29.97"), Fraction(30000, 1001))
        self.assertEqual(parse_fps("59.94"), Fraction(60000, 1001))

    def test_23_976_is_not_the_decimal_literal(self) -> None:
        # 2997/125 would be wrong by 1 part in 8 million: ~1.6 ms over 2 hours,
        # which already fails the 1 ms gate on its own.
        self.assertNotEqual(parse_fps("23.976"), Fraction("23.976"))

    def test_integers_and_rationals(self) -> None:
        self.assertEqual(parse_fps("24"), Fraction(24))
        self.assertEqual(parse_fps("24000/1001"), Fraction(24000, 1001))
        self.assertEqual(parse_fps(25), Fraction(25))

    def test_float_input_is_refused(self) -> None:
        # A float has already lost exactness; accepting one silently is how the
        # legacy ratios went wrong.
        with self.assertRaises(Refusal):
            parse_fps(23.976)

    def test_garbage_is_refused(self) -> None:
        with self.assertRaises(Refusal):
            parse_fps("banana")
        with self.assertRaises(Refusal):
            parse_fps("-25")

    def test_round_trip_formatting(self) -> None:
        for label in CANONICAL_FPS:
            self.assertEqual(format_fps(parse_fps(label)), label)


class TestRounding(unittest.TestCase):
    def test_half_up_not_bankers(self) -> None:
        # Python's round() is banker's: round(0.5) == 0, round(2.5) == 2.
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)
        self.assertEqual(round_half_up(Fraction(3, 2)), 2)
        self.assertEqual(round_half_up(Fraction(5, 2)), 3)
        self.assertEqual(round_half_up(Fraction(-1, 2)), -1)
        self.assertEqual(round_half_up(Fraction(-3, 2)), -2)

    def test_exact_integers_unchanged(self) -> None:
        for value in (-5, 0, 1, 48000):
            self.assertEqual(round_half_up(Fraction(value)), value)


class TestLegacyRatios(unittest.TestCase):
    def test_four_of_six_legacy_ratios_were_wrong(self) -> None:
        wrong = [k for k in LEGACY_PRESET_KEYS if LEGACY_A_RATIOS[k] != CORRECT_RATIOS[k]]
        self.assertEqual(
            sorted(wrong),
            sorted(["23.976_to_24", "24_to_23.976", "25_to_24", "24_to_25"]),
        )

    def test_our_presets_match_the_correct_table(self) -> None:
        for key, expected in CORRECT_RATIOS.items():
            self.assertEqual(get_preset(key).speed, expected, key)

    def test_wrong_ratios_drift_about_300ms_over_a_feature(self) -> None:
        for key in ("23.976_to_24", "24_to_23.976", "25_to_24", "24_to_25"):
            drift = abs(drift_seconds(7200, LEGACY_A_RATIOS[key], CORRECT_RATIOS[key]))
            # The audit's claim: ~0.3 s, i.e. 300x the 1 ms tolerance.
            self.assertGreater(float(drift), 0.25, key)
            self.assertLess(float(drift), 0.35, key)


class TestSampleCountProperty(unittest.TestCase):
    """The acceptance gate: out == round(in / speed), exactly, always."""

    def test_every_preset_at_common_rates(self) -> None:
        for key, ratio in presets().items():
            if ratio.is_identity:
                continue
            for rate in (44100, 48000, 96000, 192000):
                for seconds in (1, 7, 3600, 7200):
                    frames = rate * seconds
                    actual = ratio.output_samples(frames, src_rate=rate)
                    expected = round_half_up(Fraction(frames) / ratio.speed)
                    self.assertEqual(actual, expected, f"{key} @ {rate} x {seconds}s")

    def test_random_fps_pairs(self) -> None:
        rng = random.Random(20260814)
        for _ in range(500):
            src = parse_fps(rng.choice(CANONICAL_FPS))
            dst = parse_fps(rng.choice(CANONICAL_FPS))
            if src == dst:
                continue
            ratio = RetimeRatio(src_fps=src, dst_fps=dst, speed=dst / src)
            rate = rng.choice([44100, 48000, 88200, 96000])
            frames = rng.randint(1, 10**9)
            actual = ratio.output_samples(frames, src_rate=rate)
            expected = round_half_up(Fraction(frames) / ratio.speed)
            self.assertEqual(actual, expected)

    def test_rate_conversion_included(self) -> None:
        ratio = get_preset("23.976_to_25")
        frames = 48000 * 10
        actual = ratio.output_samples(frames, src_rate=48000, dst_rate=96000)
        expected = round_half_up(
            Fraction(frames * 96000) / (Fraction(48000) * ratio.speed)
        )
        self.assertEqual(actual, expected)

    def test_no_float_anywhere_in_the_computation(self) -> None:
        for key, ratio in presets().items():
            self.assertIsInstance(ratio.speed, Fraction, key)
            self.assertIsInstance(ratio.src_fps, Fraction, key)
            self.assertIsInstance(ratio.dst_fps, Fraction, key)
            self.assertIsInstance(ratio.duration_scale, Fraction, key)
            self.assertIsInstance(ratio.output_samples(12345, src_rate=48000), int, key)

    def test_round_trip_is_within_one_sample(self) -> None:
        for key, ratio in presets().items():
            if ratio.is_identity:
                continue
            frames = 48000 * 600
            forward = ratio.output_samples(frames, src_rate=48000)
            inverse = RetimeRatio(ratio.dst_fps, ratio.src_fps, 1 / ratio.speed)
            back = inverse.output_samples(forward, src_rate=48000)
            self.assertLessEqual(abs(back - frames), 1, key)


class TestRedeclare(unittest.TestCase):
    def test_24_to_25_is_exact_at_48k(self) -> None:
        ratio = get_preset("24_to_25")
        self.assertTrue(ratio.redeclare_is_exact(48000))
        self.assertEqual(ratio.redeclared_rate(48000), 50000)

    def test_24_to_23_976_is_not_exact_at_48k(self) -> None:
        ratio = get_preset("24_to_23.976")
        self.assertFalse(ratio.redeclare_is_exact(48000))

    def test_23_976_to_24_is_exact_at_48k(self) -> None:
        ratio = get_preset("23.976_to_24")
        self.assertEqual(ratio.redeclared_rate(48000), 48048)

    def test_exact_targets_are_all_really_exact(self) -> None:
        for key, rate in exact_redeclare_targets(48000):
            self.assertEqual(get_preset(key).redeclared_rate(48000), rate)


class TestResolveRatio(unittest.TestCase):
    def test_explicit_rates_beat_preset(self) -> None:
        ratio = resolve_ratio(preset="24_to_25", src_fps="25", dst_fps="24")
        self.assertEqual(ratio.speed, Fraction(24, 25))

    def test_direct_ratio_wins(self) -> None:
        ratio = resolve_ratio(preset="24_to_25", ratio="1001/1000")
        self.assertEqual(ratio.speed, Fraction(1001, 1000))

    def test_half_a_pair_is_refused(self) -> None:
        with self.assertRaises(Refusal):
            resolve_ratio(src_fps="24")

    def test_unknown_preset_is_refused_not_silently_ignored(self) -> None:
        # The legacy get_profile() fell back to "no retime" on a typo, which
        # silently produced a job that did nothing.
        with self.assertRaises(Refusal):
            get_preset("23.976_to_26")

    def test_no_arguments_means_identity(self) -> None:
        self.assertTrue(resolve_ratio().is_identity)


class TestTimestampScaling(unittest.TestCase):
    def test_delay_scales_by_the_same_exact_ratio(self) -> None:
        ratio = get_preset("23.976_to_25")
        # A +42 ms delay must become 42 * 960/1001 ms.
        scaled = ratio.scale_timestamp(Fraction(42, 1000))
        self.assertEqual(scaled, Fraction(42, 1000) * Fraction(960, 1001))
        self.assertIsInstance(scaled, Fraction)


if __name__ == "__main__":
    unittest.main()
