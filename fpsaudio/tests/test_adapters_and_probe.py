"""Adapter contract tests and probe normalisation.

Part 7 of the plan: ``build_argv`` output is asserted against flags confirmed by
``--help`` on the real binary, never against remembered flags.  Where a binary
is not installed, the test skips and says so rather than asserting a guess.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from fpsaudio.core.adapters import get_registry
from fpsaudio.core.adapters.base import BinaryAdapter, extract_flags
from fpsaudio.core.contracts import Refusal
from fpsaudio.core.probe import (
    canonical_codec,
    normalize_ffprobe,
    normalize_mediainfo,
)


class TestDetectionNeverCrashes(unittest.TestCase):
    """B-14: check_dependencies() could never report a missing dependency.

    ``subprocess.run`` raises ``FileNotFoundError`` when a binary is absent; it
    does not return a non-zero code.  The legacy code checked the return code,
    so the exception propagated and crashed the GUI on startup.
    """

    def test_a_missing_binary_reports_found_false_instead_of_raising(self) -> None:
        adapter = BinaryAdapter(
            name="definitely-not-installed",
            binary="fpsaudio-no-such-binary-xyzzy",
        )
        result = adapter.detect()
        self.assertFalse(result.found)
        self.assertIsNotNone(result.error)
        self.assertIn("not found", result.error)

    def test_the_whole_registry_can_be_detected_without_raising(self) -> None:
        registry = get_registry()
        results = registry.detect_all()
        self.assertTrue(results)
        for name, result in results.items():
            self.assertEqual(result.name, name)
            self.assertIsInstance(result.found, bool)

    def test_requiring_a_missing_tool_refuses_with_install_advice(self) -> None:
        adapter = BinaryAdapter(name="ghost", binary="fpsaudio-no-such-binary-xyzzy")
        with self.assertRaises(Refusal) as ctx:
            adapter.resolved_path()
        self.assertTrue(ctx.exception.remedies)


class TestFlagExtraction(unittest.TestCase):
    def test_flags_are_pulled_out_of_help_text(self) -> None:
        flags = extract_flags("  -b, --bitrate <n>   set bitrate\n  --raw-channels N")
        self.assertIn("--bitrate", flags)
        self.assertIn("-b", flags)
        self.assertIn("--raw-channels", flags)

    def test_prose_is_not_mistaken_for_a_flag(self) -> None:
        flags = extract_flags("this is a well-known thing")
        self.assertEqual(flags, frozenset())


class TestRealBinaryContracts(unittest.TestCase):
    """Assert built argv against what the installed binary actually advertises."""

    def setUp(self) -> None:
        self.registry = get_registry()

    def _require(self, name: str) -> BinaryAdapter:
        adapter = self.registry.get(name)
        if adapter is None or not adapter.detect().found:
            self.skipTest(f"{name} is not installed on this machine")
        return adapter

    def test_ffmpeg_decode_has_map_vn_sn_dn_and_drc(self) -> None:
        """A-4 and A-10, asserted on the built command."""
        ffmpeg = self._require("ffmpeg")
        command = ffmpeg.decode_command(
            Path("/tmp/in.mkv"), 3, Path("/tmp/out.wav"), codec="eac3"
        )
        argv = list(command.argv)
        self.assertIn("-map", argv)
        self.assertEqual(argv[argv.index("-map") + 1], "0:3")
        for flag in ("-vn", "-sn", "-dn"):
            self.assertIn(flag, argv)
        self.assertIn("-drc_scale", argv)
        self.assertEqual(argv[argv.index("-drc_scale") + 1], "0")
        self.assertIn("pcm_f32le", argv)

    def test_ffmpeg_never_combines_copy_with_a_filter(self) -> None:
        """A-3: '-c:a copy' plus '-af atempo' is a contradiction ffmpeg rejects."""
        ffmpeg = self._require("ffmpeg")
        copy_cmd = ffmpeg.demux_copy_command(Path("/tmp/in.mkv"), 0, Path("/tmp/o.thd"))
        argv = list(copy_cmd.argv)
        self.assertIn("copy", argv)
        self.assertNotIn("-af", argv)
        self.assertNotIn("-filter:a", argv)

    def test_no_command_anywhere_uses_atempo(self) -> None:
        """A-2 / B-2: atempo is a time-stretcher and must not appear at all."""
        ffmpeg = self._require("ffmpeg")
        for command in (
            ffmpeg.decode_command(Path("/a"), 0, Path("/b.wav"), codec="ac3"),
            ffmpeg.demux_copy_command(Path("/a"), 0, Path("/b.ac3")),
            ffmpeg.encode_command(Path("/a"), Path("/b.flac"), codec="flac"),
        ):
            self.assertNotIn("atempo", command.rendered())

    def test_ffmpeg_reports_a_usable_intermediate_container(self) -> None:
        ffmpeg = self._require("ffmpeg")
        suffix = ffmpeg.intermediate_suffix()
        self.assertIn(suffix, (".wav", ".w64"))

    def test_flac_raw_flags_exist_on_this_build(self) -> None:
        flac = self._require("flac")
        from fpsaudio.core.adapters.encoders import RawFormat

        command = flac.encode_command(
            Path("/tmp/in.pcm"), Path("/tmp/out.flac"),
            raw=RawFormat(rate=48000, channels=6, bits=24),
        )
        rendered = command.rendered()
        for expected in ("--force-raw-format", "--channels=6", "--bps=24", "--sample-rate=48000"):
            self.assertIn(expected, rendered)

    def test_flac_test_flag_is_confirmed_before_use(self) -> None:
        flac = self._require("flac")
        command = flac.test_command(Path("/tmp/out.flac"))
        self.assertIn("--test", command.rendered())

    def test_mediainfo_probe_argv_requests_json(self) -> None:
        mediainfo = self._require("mediainfo")
        command = mediainfo.command(mediainfo.probe_argv(Path("/tmp/a.mkv")))
        self.assertIn("--Output=JSON", command.rendered())

    def test_fdkaac_refuses_7_1_when_unconfirmed(self) -> None:
        """Part 5's open question, resolved by refusal rather than assumption."""
        fdkaac = self._require("fdkaac")
        if fdkaac.supports_channels(8):
            self.skipTest("this fdkaac build confirms 7.1; nothing to refuse")
        with self.assertRaises(Refusal):
            fdkaac.encode_command(Path("/a.pcm"), Path("/b.m4a"), channels=8)


class TestProbeNormalisation(unittest.TestCase):
    def test_codec_canonicalisation(self) -> None:
        self.assertEqual(canonical_codec("E-AC-3"), "eac3")
        self.assertEqual(canonical_codec("MLP FBA"), "truehd")
        self.assertEqual(canonical_codec("pcm_s24le"), "pcm")
        self.assertEqual(canonical_codec("DTS"), "dts")
        self.assertEqual(canonical_codec(None), "unknown")

    def test_mediainfo_truehd_atmos_is_detected(self) -> None:
        """B-6/B-7: the fields that make Atmos visible are read and kept."""
        payload = {
            "media": {
                "track": [
                    {"@type": "General", "Format": "Matroska", "Duration": "7200.0"},
                    {
                        "@type": "Audio",
                        "StreamOrder": "1",
                        "Format": "MLP FBA",
                        "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
                        "Format_AdditionalFeatures": "16-ch",
                        "NumberOfDynamicObjects": "13",
                        "BedChannelCount": "8",
                        "Channels": "8",
                        "SamplingRate": "48000",
                        "BitDepth": "24",
                        "Language": "en",
                    },
                ]
            }
        }
        media = normalize_mediainfo(payload, Path("/tmp/a.mkv"))
        stream = media.audio[0]
        self.assertEqual(stream.codec, "truehd")
        self.assertEqual(stream.stream_index, 1)
        self.assertTrue(stream.lossless)
        self.assertTrue(stream.atmos.present)
        self.assertEqual(stream.atmos.kind, "truehd_atmos")
        self.assertEqual(stream.atmos.objects, 13)
        self.assertEqual(stream.atmos.certainty, "confirmed")

    def test_mediainfo_eac3_joc_is_detected(self) -> None:
        payload = {
            "media": {
                "track": [
                    {"@type": "General", "Format": "Matroska"},
                    {
                        "@type": "Audio",
                        "StreamOrder": "1",
                        "Format": "E-AC-3",
                        "Format_AdditionalFeatures": "JOC",
                        "Format_Commercial_IfAny": "Dolby Digital Plus with Dolby Atmos",
                        "Channels": "6",
                        "SamplingRate": "48000",
                    },
                ]
            }
        }
        stream = normalize_mediainfo(payload, Path("/tmp/a.mkv")).audio[0]
        self.assertEqual(stream.atmos.kind, "eac3_joc")
        self.assertTrue(stream.atmos.present)

    def test_dts_hd_ma_is_distinguished_from_dts_core(self) -> None:
        """B-6: both are codec_name 'dts'; the discriminator is the profile."""
        def build(commercial: str) -> object:
            payload = {
                "media": {
                    "track": [
                        {"@type": "General", "Format": "Matroska"},
                        {
                            "@type": "Audio",
                            "StreamOrder": "1",
                            "Format": "DTS",
                            "Format_Commercial_IfAny": commercial,
                            "Channels": "8",
                        },
                    ]
                }
            }
            return normalize_mediainfo(payload, Path("/tmp/a.mkv")).audio[0]

        self.assertTrue(build("DTS-HD Master Audio").lossless)
        self.assertFalse(build("DTS").lossless)

    def test_ffprobe_cannot_see_joc_and_says_so(self) -> None:
        """An honest 'unknown' beats a confident wrong 'absent'."""
        payload = {
            "format": {"format_name": "matroska", "duration": "100.0"},
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "eac3",
                    "channels": 6,
                    "sample_rate": "48000",
                    "duration": "100.0",
                }
            ],
        }
        stream = normalize_ffprobe(payload, Path("/tmp/a.mkv")).audio[0]
        self.assertFalse(stream.atmos.present)
        self.assertEqual(stream.atmos.certainty, "unknown")
        self.assertTrue(stream.atmos.evidence)

    def test_ffprobe_keeps_the_fields_the_legacy_model_discarded(self) -> None:
        payload = {
            "format": {"format_name": "matroska"},
            "streams": [
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "truehd",
                    "profile": "Dolby TrueHD + Atmos",
                    "channels": 8,
                    "channel_layout": "7.1",
                    "sample_rate": "48000",
                    "bits_per_raw_sample": "24",
                    "start_time": "0.042",
                    "disposition": {"default": 1, "forced": 0},
                    "tags": {"language": "eng", "title": "Main"},
                }
            ],
        }
        stream = normalize_ffprobe(payload, Path("/tmp/a.mkv")).audio[0]
        self.assertEqual(stream.profile, "Dolby TrueHD + Atmos")
        self.assertEqual(stream.channel_layout, "7.1")
        self.assertEqual(stream.bit_depth, 24)
        self.assertEqual(stream.start_time_s, 0.042)
        self.assertTrue(stream.default)
        self.assertEqual(stream.title, "Main")
        self.assertTrue(stream.atmos.present)

    def test_multiple_audio_tracks_are_all_kept(self) -> None:
        """A-5: the legacy probe loop kept only the last track's values."""
        payload = {
            "format": {"format_name": "matroska"},
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264"},
                {"index": 1, "codec_type": "audio", "codec_name": "truehd", "channels": 8},
                {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6},
            ],
        }
        media = normalize_ffprobe(payload, Path("/tmp/a.mkv"))
        self.assertEqual(len(media.audio), 2)
        self.assertEqual([s.stream_index for s in media.audio], [1, 2])
        self.assertTrue(media.has_video)

    def test_a_file_with_no_audio_does_not_raise(self) -> None:
        """A-5: the legacy code left names unbound and raised UnboundLocalError."""
        payload = {
            "format": {"format_name": "matroska"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        }
        media = normalize_ffprobe(payload, Path("/tmp/a.mkv"))
        self.assertEqual(media.audio, ())
        self.assertIn("no audio streams reported", media.problems)


class TestScanning(unittest.TestCase):
    def test_extension_is_not_a_capability_gate(self) -> None:
        """B-5: a folder of .thd files scanned to zero jobs with no message."""
        import tempfile

        root = Path(tempfile.mkdtemp())
        for name in ("a.thd", "b.mlp", "c.ec3", "d.dtshd", "e.w64", "f.mp2"):
            (root / name).write_bytes(b"\x00" * 16)
        (root / "notes.txt").write_text("ignore me")

        from fpsaudio.core.probe import discover_files

        found = {p.name for p in discover_files(root)}
        self.assertIn("a.thd", found)
        self.assertIn("b.mlp", found)
        self.assertIn("d.dtshd", found)
        self.assertNotIn("notes.txt", found)


if __name__ == "__main__":
    unittest.main()
