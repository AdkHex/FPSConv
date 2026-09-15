"""Atmos metadata retiming and the Dolby Media Encoder hand-off.

Per Part 8 risk 3 of the plan: no real TrueHD Atmos file was available, so this
is built and tested against **synthetic metadata**. What is proven here is that
object timestamps are rescaled by exactly the same rational as the audio, and
that the hand-off bundle is complete and honest. What is *not* proven is
truehdd's real output format — that needs a real file.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from fpsaudio.core.contracts import (
    AtmosInfo,
    AtmosPolicy,
    AudioStream,
    JobSpec,
    MediaFile,
    OutputSpec,
    RetimeSpec,
)
from fpsaudio.core.plan import build_plan
from fpsaudio.core.ratio import get_preset
from fpsaudio.core.stages.atmos import retime_atmos_metadata


class TestMetadataRetiming(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_atmos_"))

    def test_object_timestamps_scale_by_the_exact_ratio(self) -> None:
        master = self.dir / "master.atmos"
        payload = {
            "version": "1.0",
            "objects": [
                {"id": 1, "startTime": 10.0, "duration": 5.0, "gain": -3.0},
                {"id": 2, "start_time": 100.0, "endTime": 120.0},
            ],
            "bed": {"channels": 8, "offset": 0.5},
            "events": [{"timestamp": 42.0, "position": [0.5, 0.5, 0.5]}],
        }
        master.write_text(json.dumps(payload), encoding="utf-8")

        speed = get_preset("23.976_to_25").speed  # 1001/960
        report = retime_atmos_metadata(master, speed)

        result = json.loads(master.read_text(encoding="utf-8"))
        scale = Fraction(1) / speed  # 960/1001

        # Compare against the exact rational, converted to float exactly once.
        # `value * float(scale)` would be a *less* accurate expectation than
        # what the implementation produces, which multiplies as Fractions and
        # converts at the very end.
        def expected(value: float) -> float:
            return float(Fraction(value).limit_denominator(10**9) * scale)

        self.assertEqual(result["objects"][0]["startTime"], expected(10.0))
        self.assertEqual(result["objects"][0]["duration"], expected(5.0))
        self.assertEqual(result["objects"][1]["start_time"], expected(100.0))
        self.assertEqual(result["objects"][1]["endTime"], expected(120.0))
        self.assertEqual(result["bed"]["offset"], expected(0.5))
        self.assertEqual(result["events"][0]["timestamp"], expected(42.0))

        self.assertEqual(report["timestamps_scaled"], 6)
        self.assertEqual(report["scale"], "960/1001")

    def test_non_time_fields_are_left_alone(self) -> None:
        """Rewriting something we do not understand is worse than leaving it."""
        master = self.dir / "m.atmos"
        payload = {
            "objects": [{"id": 7, "gain": -3.0, "position": [0.1, 0.2, 0.3], "size": 0.5}],
            "name": "reel 1",
        }
        master.write_text(json.dumps(payload), encoding="utf-8")

        retime_atmos_metadata(master, get_preset("24_to_25").speed)
        result = json.loads(master.read_text(encoding="utf-8"))

        self.assertEqual(result["objects"][0]["id"], 7)
        self.assertEqual(result["objects"][0]["gain"], -3.0)
        self.assertEqual(result["objects"][0]["position"], [0.1, 0.2, 0.3])
        self.assertEqual(result["objects"][0]["size"], 0.5)
        self.assertEqual(result["name"], "reel 1")

    def test_integer_sample_positions_stay_integers(self) -> None:
        master = self.dir / "s.atmos"
        master.write_text(json.dumps({"samplePosition": 48000}), encoding="utf-8")
        retime_atmos_metadata(master, get_preset("24_to_25").speed)  # 25/24
        result = json.loads(master.read_text(encoding="utf-8"))
        self.assertIsInstance(result["samplePosition"], int)
        self.assertEqual(result["samplePosition"], 46080)  # 48000 * 24/25

    def test_the_scale_matches_the_audio_exactly(self) -> None:
        """Bed and objects must not drift apart — same Fraction, both sides."""
        ratio = get_preset("23.976_to_25")
        master = self.dir / "x.atmos"
        master.write_text(json.dumps({"startTime": 3600.0}), encoding="utf-8")
        retime_atmos_metadata(master, ratio.speed)
        scaled = json.loads(master.read_text(encoding="utf-8"))["startTime"]

        audio_seconds = float(ratio.output_seconds(3600))
        self.assertAlmostEqual(scaled, audio_seconds, places=9)

    def test_a_missing_or_unreadable_master_does_not_raise(self) -> None:
        report = retime_atmos_metadata(self.dir / "nope.atmos", Fraction(25, 24))
        self.assertEqual(report["timestamps_scaled"], 0)


class TestHandoffPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_handoff_"))
        self.source = self.dir / "movie.mkv"
        self.source.write_bytes(b"\x00" * 32)

        self.stream = AudioStream(
            stream_index=1,
            codec="truehd",
            profile="Dolby TrueHD + Atmos",
            commercial_name="Dolby TrueHD with Dolby Atmos",
            channels=8,
            channel_layout="7.1",
            sample_rate=48000,
            bit_depth=24,
            duration_s=7200.0,
            sample_count=48000 * 7200,
            language="eng",
            lossless=True,
            atmos=AtmosInfo(
                present=True, kind="truehd_atmos", objects=13,
                bed_channels=8, certainty="confirmed",
            ),
        )
        self.media = MediaFile(
            path=self.source, container="matroska", duration_s=7200.0,
            audio=(self.stream,), source_tool="mediainfo",
        )

    def _plan(self, policy: AtmosPolicy = AtmosPolicy.HANDOFF):
        ratio = get_preset("23.976_to_25")
        spec = JobSpec(
            source=self.source,
            stream_index=1,
            profile_key=ratio.key,
            retime=RetimeSpec(src_fps=ratio.src_fps, dst_fps=ratio.dst_fps),
            output=OutputSpec(directory=self.dir / "out"),
            atmos_policy=policy,
        )
        return build_plan(spec, self.media, claim_output=False)

    def test_the_handoff_pipeline_has_the_right_stages_in_order(self) -> None:
        plan = self._plan()
        ids = [s.id for s in plan.stages]
        self.assertEqual(
            ids,
            ["demux", "atmos", "decode", "resample", "encode", "handoff", "verify"],
        )

    def test_no_truehd_encode_is_attempted(self) -> None:
        plan = self._plan()
        self.assertEqual(plan.ctx.target_codec, "pcm")
        rendered = plan.render_commands()
        self.assertNotIn("truehd", rendered.lower().replace("truehdd", ""))

    def test_the_output_is_an_essence_inside_a_bundle(self) -> None:
        plan = self._plan()
        self.assertTrue(plan.output_path.name.endswith("_essence.w64"))
        self.assertTrue(plan.output_path.parent.name.endswith("_DME"))
        self.assertEqual(plan.ctx.handoff_dir, plan.output_path.parent)

    def test_the_plan_says_plainly_that_dme_is_manual(self) -> None:
        plan = self._plan()
        text = plan.describe()
        self.assertIn("GUI-only", text)
        self.assertIn("hand-off", text.lower())

    def test_the_recipe_carries_the_real_numbers(self) -> None:
        from fpsaudio.core.stages.atmos import _recipe_text

        plan = self._plan()
        ctx = plan.ctx
        ctx.current_frames = 331444555
        ctx.current_rate = 48000
        ctx.current_channels = 8

        recipe = _recipe_text(ctx, Path("essence.w64"))

        self.assertIn("1001/960", recipe)          # the exact ratio
        self.assertIn("13 objects", recipe)         # the real object count
        self.assertIn("331444555", recipe)          # the realised frame count
        self.assertIn("48000 Hz", recipe)
        self.assertIn("7.1", recipe)
        self.assertIn("960/1001", recipe)           # the metadata scale
        self.assertIn("drc_scale 0", recipe)
        self.assertIn("Do **not** let DME resample", recipe)

    def test_flatten_still_needs_an_explicit_token(self) -> None:
        from fpsaudio.core.contracts import Refusal

        with self.assertRaises(Refusal) as ctx:
            self._plan(AtmosPolicy.FLATTEN)
        self.assertEqual(ctx.exception.override_token, "flatten-atmos")


if __name__ == "__main__":
    unittest.main()
