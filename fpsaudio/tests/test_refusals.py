"""Every refusal path, and the silent-loss behaviours they replace.

Each test here names the legacy finding it exists to prevent regressing.
"""

from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from fpsaudio.core.config import (
    resolve_codec,
    resolve_container,
    validate_pair,
)
from fpsaudio.core.contracts import (
    AtmosInfo,
    AtmosPolicy,
    AudioStream,
    JobSpec,
    MediaFile,
    OutputSpec,
    Refusal,
    RefusalCode,
    RetimeMethod,
    RetimeSpec,
)
from fpsaudio.core.plan import build_plan
from fpsaudio.core.ratio import get_preset


def stream(**overrides) -> AudioStream:
    base = dict(
        stream_index=0,
        codec="truehd",
        channels=8,
        channel_layout="7.1",
        sample_rate=48000,
        bit_depth=24,
        duration_s=100.0,
        sample_count=4800000,
        lossless=True,
    )
    base.update(overrides)
    return AudioStream(**base)


def media(*streams: AudioStream, path: Path | None = None) -> MediaFile:
    return MediaFile(
        path=path or Path("/tmp/example.mkv"),
        container="matroska",
        duration_s=100.0,
        audio=tuple(streams),
        source_tool="mediainfo",
    )


def spec(source: Path, **overrides) -> JobSpec:
    ratio = get_preset(overrides.pop("preset", "23.976_to_25"))
    output = overrides.pop("output", None) or OutputSpec(
        directory=Path("/tmp/out"), codec=overrides.pop("codec", "auto")
    )
    return JobSpec(
        source=source,
        stream_index=overrides.pop("stream_index", 0),
        profile_key=ratio.key,
        retime=RetimeSpec(
            src_fps=ratio.src_fps,
            dst_fps=ratio.dst_fps,
            method=overrides.pop("method", RetimeMethod.RESAMPLE),
        ),
        output=output,
        **overrides,
    )


class TestCodecResolution(unittest.TestCase):
    """B-3: ``resolve_codec`` silently returned "aac" for anything unknown."""

    def test_auto_never_falls_back_to_aac_for_lossless(self) -> None:
        chosen = resolve_codec("auto", "truehd", source_lossless=True)
        self.assertEqual(chosen, "flac")
        self.assertNotEqual(chosen, "aac")

    def test_auto_keeps_a_lossy_source_in_its_own_codec(self) -> None:
        self.assertEqual(resolve_codec("auto", "opus", source_lossless=False), "opus")

    def test_unrecognised_source_is_refused_not_guessed(self) -> None:
        with self.assertRaises(Refusal) as ctx:
            resolve_codec("auto", "some-new-codec", source_lossless=False)
        self.assertEqual(ctx.exception.code, RefusalCode.UNIDENTIFIED_SOURCE)

    def test_dolby_encode_targets_refuse_with_a_reason(self) -> None:
        for codec in ("ac3", "eac3", "truehd"):
            with self.assertRaises(Refusal) as ctx:
                resolve_codec(codec, "flac", source_lossless=True)
            self.assertIn("Dolby", ctx.exception.message)
            self.assertTrue(ctx.exception.remedies)

    def test_dts_encode_is_out_of_scope(self) -> None:
        with self.assertRaises(Refusal) as ctx:
            resolve_codec("dts", "flac", source_lossless=True)
        self.assertEqual(ctx.exception.code, RefusalCode.DTS_OUT_OF_SCOPE)


class TestContainerValidation(unittest.TestCase):
    """B-11: container/codec pairs were never validated."""

    def test_flac_in_m4a_is_refused_before_ffmpeg_sees_it(self) -> None:
        with self.assertRaises(Refusal) as ctx:
            validate_pair("m4a", "flac")
        self.assertEqual(ctx.exception.code, RefusalCode.CONTAINER_CODEC_MISMATCH)
        self.assertTrue(any("flac fits in" in r for r in ctx.exception.remedies))

    def test_valid_pairs_pass(self) -> None:
        validate_pair("m4a", "aac")
        validate_pair("flac", "flac")
        validate_pair("mka", "truehd")

    def test_auto_picks_a_valid_container(self) -> None:
        for codec in ("flac", "aac", "opus", "wavpack", "pcm"):
            container, extension = resolve_container("auto", codec)
            validate_pair(container, codec)
            self.assertTrue(extension)


class TestPlannerRefusals(unittest.TestCase):
    def test_dts_source_is_detected_and_refused(self) -> None:
        source = media(stream(codec="dts", profile="DTS-HD MA", lossless=True))
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path), source)
        self.assertEqual(ctx.exception.code, RefusalCode.DTS_OUT_OF_SCOPE)

    def test_eac3_joc_refuses_and_points_at_the_truehd_track(self) -> None:
        """§3.5 / B-7: DD+ Atmos must never be flattened to its 5.1 core."""
        joc = stream(
            stream_index=1,
            codec="eac3",
            channels=6,
            lossless=False,
            atmos=AtmosInfo(present=True, kind="eac3_joc", certainty="confirmed"),
        )
        thd = stream(stream_index=2, codec="truehd")
        source = media(joc, thd)
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path, stream_index=1), source)
        self.assertEqual(ctx.exception.code, RefusalCode.JOC_DECODER_MISSING)
        self.assertTrue(any("stream #2" in r for r in ctx.exception.remedies))
        self.assertEqual(ctx.exception.override_token, "flatten-atmos")

    def test_truehd_atmos_refuses_unless_a_policy_is_chosen(self) -> None:
        source = media(
            stream(atmos=AtmosInfo(present=True, kind="truehd_atmos", certainty="confirmed"))
        )
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path), source)
        self.assertEqual(ctx.exception.code, RefusalCode.ATMOS_WOULD_FLATTEN)

    def test_unknown_atmos_certainty_is_refused_not_assumed_absent(self) -> None:
        """A guess must never authorise a destructive operation."""
        source = media(
            stream(
                codec="eac3",
                channels=6,
                lossless=False,
                atmos=AtmosInfo(present=False, certainty="unknown", evidence=("no mediainfo",)),
            )
        )
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path), source)
        self.assertEqual(ctx.exception.override_token, "unverified-atmos")

    def test_accepting_the_token_lets_it_through_with_a_warning(self) -> None:
        source = media(
            stream(
                codec="eac3",
                channels=6,
                lossless=False,
                atmos=AtmosInfo(present=False, certainty="unknown"),
            )
        )
        job = spec(source.path, codec="opus", accepted=("unverified-atmos",))
        plan = build_plan(job, source)
        self.assertTrue(any("never verified" in w for w in plan.warnings))

    def test_lossless_to_lossy_needs_confirmation(self) -> None:
        """§3.3 / B-3: the TrueHD -> 128 kbps AAC path, made impossible."""
        source = media(stream(codec="flac", channels=2, lossless=True))
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path, codec="opus"), source)
        self.assertEqual(ctx.exception.code, RefusalCode.LOSSLESS_TO_LOSSY)
        self.assertEqual(ctx.exception.override_token, "lossless-to-lossy")

    def test_lossless_to_lossy_proceeds_once_accepted(self) -> None:
        source = media(stream(codec="flac", channels=2, lossless=True))
        job = spec(source.path, codec="opus", accepted=("lossless-to-lossy",))
        plan = build_plan(job, source)
        self.assertEqual(plan.ctx.target_codec, "opus")

    def test_channel_count_above_a_codecs_ceiling_is_refused(self) -> None:
        source = media(stream(codec="flac", channels=2, lossless=False))
        job = spec(source.path, codec="mp3")
        with self.assertRaises(Refusal):
            build_plan(job, source)

    def test_redeclare_that_is_not_exact_is_refused_with_alternatives(self) -> None:
        source = media(stream(codec="flac", channels=2, sample_rate=48000))
        job = spec(
            source.path,
            preset="24_to_23.976",
            method=RetimeMethod.REDECLARE,
            codec="flac",
        )
        with self.assertRaises(Refusal) as ctx:
            build_plan(job, source)
        self.assertEqual(ctx.exception.code, RefusalCode.REDECLARE_NOT_EXACT)
        self.assertTrue(any("resample" in r for r in ctx.exception.remedies))

    def test_redeclare_that_is_exact_is_allowed_and_flagged_bit_exact(self) -> None:
        source = media(stream(codec="flac", channels=2, sample_rate=48000))
        job = spec(
            source.path, preset="24_to_25", method=RetimeMethod.REDECLARE, codec="flac"
        )
        plan = build_plan(job, source)
        self.assertTrue(plan.ctx.expect_bit_exact)
        self.assertEqual(plan.ctx.target_rate, 50000)
        self.assertTrue(any("Bit-exact" in n for n in plan.notes))

    def test_no_audio_stream_is_a_clear_refusal(self) -> None:
        source = media()
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path), source)
        self.assertEqual(ctx.exception.code, RefusalCode.NO_AUDIO_STREAM)

    def test_unidentified_source_is_refused_with_the_probers_reasons(self) -> None:
        source = MediaFile(
            path=Path("/tmp/mystery.bin"),
            identified=False,
            problems=("mediainfo: not installed", "ffprobe: not installed"),
        )
        with self.assertRaises(Refusal) as ctx:
            build_plan(spec(source.path), source)
        self.assertEqual(ctx.exception.code, RefusalCode.UNIDENTIFIED_SOURCE)


class TestRefusalPresentation(unittest.TestCase):
    def test_every_refusal_renders_its_remedies_and_token(self) -> None:
        refusal = Refusal(
            RefusalCode.LOSSLESS_TO_LOSSY,
            "Source is lossless.",
            remedies=["Use --codec flac."],
            override_token="lossless-to-lossy",
        )
        text = refusal.render()
        self.assertIn("REFUSED [lossless_to_lossy]", text)
        self.assertIn("Use --codec flac.", text)
        self.assertIn("--accept lossless-to-lossy", text)


class TestAtmosHandoff(unittest.TestCase):
    def test_handoff_produces_an_essence_and_a_bundle_not_an_encode(self) -> None:
        source = media(
            stream(atmos=AtmosInfo(present=True, kind="truehd_atmos", certainty="confirmed"))
        )
        job = spec(source.path, atmos_policy=AtmosPolicy.HANDOFF)
        plan = build_plan(job, source)
        stage_ids = [s.id for s in plan.stages]
        self.assertIn("atmos", stage_ids)
        self.assertIn("handoff", stage_ids)
        self.assertEqual(plan.ctx.target_codec, "pcm")
        self.assertTrue(str(plan.output_path).endswith("_essence.w64"))
        self.assertTrue(any("Dolby Media Encoder is GUI-only" in n for n in plan.notes))


if __name__ == "__main__":
    unittest.main()
