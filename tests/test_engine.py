"""Unit tests for the parts of the engine that need no ffmpeg or deew."""

import os
import tempfile
import unittest
from unittest import mock

from fpsconv import config, engine


class AtempoChain(unittest.TestCase):
    def test_fps_py_ratios_are_single_atempo(self):
        for key, ratio in engine.FPS_CONVERSIONS.items():
            self.assertRegex(engine.atempo_chain(ratio), r"^atempo=\d\.\d{6}$", key)

    def test_identity_is_anull(self):
        self.assertEqual(engine.atempo_chain(1.0), "anull")

    def test_large_ratio_is_chained(self):
        self.assertEqual(engine.atempo_chain(4.0), "atempo=2.0,atempo=2.000000")
        self.assertEqual(engine.atempo_chain(0.2), "atempo=0.5,atempo=0.5,atempo=0.800000")

    def test_garbage_falls_back(self):
        self.assertEqual(engine.atempo_chain("x"), "atempo=1.0")


class OutputName(unittest.TestCase):
    def test_matches_fps_py(self):
        self.assertEqual(engine.output_name("/x/movie.mka", "23.976-25", "ac3"), "movie_23_976-25.ac3")
        self.assertEqual(engine.output_name("/x/movie.mka", "24-25", "aac"), "movie_24-25.aac")
        self.assertEqual(engine.output_name("/x/movie.mka", "24-25", "truehd"), "movie_24-25.thd")

    def test_second_stream_is_tagged(self):
        self.assertEqual(engine.output_name("/x/m.mkv", "24-25", "eac3", 1), "m_a1_24-25.ec3")


class ErrorPicking(unittest.TestCase):
    def test_prefers_informative_line(self):
        lines = ["[adts @ 0x1] channelConfiguration > 7 is not supported in ADTS",
                 "size=0KiB time=N/A", "Conversion failed!"]
        self.assertEqual(engine._pick_error(lines), "channelConfiguration > 7 is not supported in ADTS")

    def test_falls_back_to_last_line(self):
        self.assertEqual(engine._pick_error(["a", "b"]), "b")
        self.assertEqual(engine._pick_error([]), "")


class Settings(unittest.TestCase):
    def test_roundtrip_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "config_dir", return_value=__import__("pathlib").Path(tmp)):
                self.assertEqual(config.load_settings()["conv_type"], "23.976-25")
                saved = config.save_settings({"conv_type": "24-25", "tools": {"ffmpeg": "/x/ffmpeg"}, "junk": 1})
                self.assertEqual(saved["conv_type"], "24-25")
                self.assertEqual(saved["tools"]["ffmpeg"], "/x/ffmpeg")
                self.assertNotIn("junk", saved)
                self.assertEqual(config.load_settings()["tools"]["ffmpeg"], "/x/ffmpeg")
                config.save_history([{"id": "a"}] * 300)
                self.assertEqual(len(config.load_history()), 200)


class DeewConfig(unittest.TestCase):
    def test_config_uses_our_temp_dir_and_quotes_paths(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "config_dir", return_value=Path(tmp)), \
                 mock.patch.object(engine, "deew_config_path", return_value=Path(tmp) / "deew" / "config.toml"):
                path = engine.write_deew_config(r"C:\Dolby\DEE\dee.exe")
                data = engine.read_deew_config()
                self.assertEqual(data["dee_path"], r"C:\Dolby\DEE\dee.exe")
                self.assertEqual(data["temp_path"], str(Path(tmp) / "deew-temp"))
                self.assertTrue((Path(tmp) / "deew-temp").is_dir())
                self.assertEqual(data["logo"], 0)
                self.assertTrue(path.exists())


class BitrateSnapping(unittest.TestCase):
    def test_tables_match_dee(self):
        self.assertEqual(engine.snap_bitrate("ddp", 6, 1536), 1024)      # DDP 5.1 has no 1536: its default (= max)
        self.assertEqual(engine.snap_bitrate("ddp", 8, 1503), 1536)      # "1503 kbps" is 1536 nominal
        self.assertEqual(engine.snap_bitrate("ddp", 6, 300), 304)        # in range: nearest
        self.assertEqual(engine.snap_bitrate("dd", 6, 1024), 640)
        self.assertEqual(engine.snap_bitrate("dd", 2, 0), 256)
        self.assertEqual(engine.snap_bitrate("ddp", 2, 0), 256)
        self.assertEqual(engine.snap_bitrate("ddp", 2, 1536), 256)       # a 7.1 rate on a 2.0 file: the 2.0 default

    def test_atmos_tables(self):
        self.assertEqual(engine.snap_bitrate("ddp", 6, 1536, atmos=True), 768)    # 7.1 rate on 5.1 Atmos: default
        self.assertEqual(engine.snap_bitrate("ddp", 6, 1000, atmos=True), 1024)   # in range: nearest
        self.assertEqual(engine.snap_bitrate("ddp", 8, 0, atmos=True), 1536)
        self.assertEqual(engine.snap_bitrate("ddp", 8, 1503, atmos=True), 1512)   # DeeZy's Blu-ray JOC list
        self.assertEqual(engine.snap_bitrate("ddp", 6, 0, atmos=True), 768)

    def test_pad_layout(self):
        self.assertEqual([engine.pad_layout(c) for c in (1, 2, 3, 5, 6, 7, 8, 10)], [1, 2, 6, 6, 6, 8, 8, 8])


class ResolveEncode(unittest.TestCase):
    def test_downmix_71_to_51(self):
        e = engine.resolve_encode("ddp", 6, True, 0, 8, False)
        self.assertEqual((e.wav_channels, e.out_channels, e.bitrate, e.atmos), (8, 6, 1024, False))
        self.assertEqual(e.label, "DDP 5.1 1024k")
        self.assertIn("-dm", engine.deew_cmd_encode("/t.wav", e, "film_light", "/w"))

    def test_never_upmixes(self):
        e = engine.resolve_encode("ddp", 8, True, 1536, 2, False)
        self.assertEqual((e.out_channels, e.bitrate), (2, 256))
        self.assertTrue(any("no upmix" in n for n in e.notes))
        self.assertTrue(any("outside DDP 2.0" in n for n in e.notes))
        self.assertNotIn("-dm", engine.deew_cmd_encode("/t.wav", e, "film_light", "/w"))

    def test_odd_layout_is_padded(self):
        e = engine.resolve_encode("ddp", 0, True, 0, 5, False)
        self.assertEqual((e.wav_channels, e.out_channels), (6, 6))
        self.assertTrue(any("padded" in n for n in e.notes))

    def test_dd_has_no_71(self):
        e = engine.resolve_encode("dd", 0, True, 0, 8, True, "truehd")
        self.assertEqual((e.out_channels, e.bitrate, e.atmos, e.ext), (6, 640, False, ".ac3"))
        self.assertTrue(any("DD cannot carry Atmos" in n for n in e.notes))

    def test_atmos_only_when_confirmed(self):
        yes = engine.resolve_encode("ddp", 0, True, 0, 8, True, "truehd")
        self.assertEqual((yes.atmos, yes.atmos_mode, yes.bitrate, yes.label), (True, "bluray", 1536, "DDP 7.1 Atmos 1536k"))
        streaming = engine.resolve_encode("ddp", 6, True, 0, 8, True, "truehd")
        self.assertEqual((streaming.atmos, streaming.atmos_mode, streaming.bitrate), (True, "streaming", 768))
        stereo = engine.resolve_encode("ddp", 2, True, 0, 8, True, "truehd")
        self.assertFalse(stereo.atmos)
        unknown = engine.resolve_encode("ddp", 0, True, 0, 8, None, "truehd")
        self.assertFalse(unknown.atmos)
        self.assertTrue(any("mediainfo" in n for n in unknown.notes))
        off = engine.resolve_encode("ddp", 0, False, 0, 8, True, "truehd")
        self.assertEqual((off.atmos, off.label), (False, "DDP 7.1 1536k"))

    def test_bluray_profile_note(self):
        self.assertTrue(any("Blu-ray" in n for n in engine.resolve_encode("ddp", 8, False, 1536, 8, False).notes))
        self.assertFalse(any("Blu-ray" in n for n in engine.resolve_encode("ddp", 8, False, 1024, 8, False).notes))


class EncodeNaming(unittest.TestCase):
    def test_names(self):
        e = engine.resolve_encode("ddp", 0, True, 0, 8, True, "truehd")
        self.assertEqual(engine.encode_output_name("/x/movie.mkv", 0, e), "movie_DDP7.1Atmos_1536k.ec3")
        e = engine.resolve_encode("dd", 0, True, 0, 6, False)
        self.assertEqual(engine.encode_output_name("/x/movie.mkv", 1, e), "movie_a1_DD5.1_640k.ac3")
        e = engine.resolve_encode("ddp", 0, True, 0, 2, False)
        self.assertEqual(engine.encode_output_name("/x/song.m4a", 0, e), "song_DDP2.0_256k.ec3")


class CommandBuilders(unittest.TestCase):
    def setUp(self):
        self.settings = {"tools": {"ffmpeg": "", "ffprobe": "", "deew_python": "/venv/python",
                                   "deezy": r"C:\Tools\deezy.exe", "deezy_python": "", "mediainfo": "", "truehdd": ""}}
        patcher = mock.patch.object(config, "load_settings", return_value=self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deew_argv(self):
        e = engine.resolve_encode("ddp", 6, True, 0, 8, False)
        self.assertEqual(engine.deew_cmd_encode("/w/t.wav", e, "film_light", "/w"),
                         ["/venv/python", "-m", "deew", "-i", "/w/t.wav", "-f", "ddp", "-b", "1024",
                          "-r", "film_light", "-o", "/w", "-np", "-dm", "6"])
        e = engine.resolve_encode("ddp", 8, True, 1536, 8, False)
        cmd = engine.deew_cmd_encode("/w/t.wav", e, "bogus", "/w")
        self.assertEqual(cmd[5:10], ["-f", "ddp", "-b", "1536", "-r"])
        self.assertIn("film_light", cmd)                       # unknown DRC falls back

    def test_deezy_argv(self):
        e = engine.resolve_encode("ddp", 0, True, 0, 8, True, "truehd")
        cmd = engine.deezy_cmd_atmos("/in/m.mkv", 1, e, "film_standard", "/w", "/o/m.ec3",
                                     {"ffmpeg": "/bin/ffmpeg", "dee": r"C:\DEE\dee.exe", "truehdd": ""})
        self.assertEqual(cmd, [r"C:\Tools\deezy.exe", "--no-progress-bars",
                               "--ffmpeg", "/bin/ffmpeg", "--dee", r"C:\DEE\dee.exe",
                               "encode", "atmos", "--atmos-mode", "bluray", "--bitrate", "1536",
                               "--track-index", "a:1", "--drc-line-mode", "film_standard",
                               "--temp-dir", "/w", "--output", "/o/m.ec3", "--overwrite", "/in/m.mkv"])

    def test_deezy_cmd_prefers_exe_then_python(self):
        self.assertEqual(engine.deezy_cmd(), [r"C:\Tools\deezy.exe"])
        self.settings["tools"]["deezy"] = ""
        self.settings["tools"]["deezy_python"] = "/venv/python"
        self.assertEqual(engine.deezy_cmd(), ["/venv/python", "-m", "deezy"])

    def test_wav_argv(self):
        e = engine.resolve_encode("ddp", 0, True, 0, 6, False)
        cmd = engine.wav_cmd_encode("ffmpeg", "/in/m.mkv", 0, {"codec": "eac3", "sample_rate": 48000}, e, "/w/t.wav")
        self.assertEqual(cmd[:5], ["ffmpeg", "-y", "-nostdin", "-drc_scale", "0"])   # no decoder DRC on Dolby sources
        self.assertNotIn("-af", cmd)
        self.assertEqual(cmd[cmd.index("-ac") + 1], "6")
        cmd = engine.wav_cmd_encode("ffmpeg", "/in/m.mkv", 0, {"codec": "truehd", "sample_rate": 96000}, e, "/w/t.wav")
        self.assertNotIn("-drc_scale", cmd)
        self.assertIn("aresample=out_sample_rate=48000:resampler=soxr:precision=28", cmd)
        cmd = engine.wav_cmd_encode("ffmpeg", "/in/m.mkv", 0, {"codec": "truehd", "sample_rate": 96000}, e, "/w/t.wav", soxr=False)
        self.assertNotIn("-af", cmd)
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

    def test_deew_output_accepts_eb3(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(engine._deew_output(tmp, "fpsconv_x"))
            open(os.path.join(tmp, "fpsconv_x.eb3"), "w").close()
            self.assertEqual(engine._deew_output(tmp, "fpsconv_x"), os.path.join(tmp, "fpsconv_x.eb3"))

    def test_deezy_progress_line(self):
        m = engine._DEEZY_LINE.match("truehdd (1 of 3)       42.5%")
        self.assertEqual((m.group(1), m.group(4)), ("truehdd", "42.5"))
        m = engine._DEEZY_LINE.match("W1: DEE encode (3 of 3) 80.0%")
        self.assertEqual((m.group(1), m.group(4)), ("DEE encode", "80.0"))
        self.assertIsNone(engine._DEEZY_LINE.match("Dee job: something 100% done"))


class MediaInfoAtmos(unittest.TestCase):
    FIXTURE = {"media": {"track": [
        {"@type": "General"},
        {"@type": "Video"},
        {"@type": "Audio", "Format": "MLP FBA", "Format_Commercial_IfAny": "Dolby TrueHD with Dolby Atmos",
         "Format_AdditionalFeatures": "16-ch"},
        {"@type": "Audio", "Format": "E-AC-3", "Format_Commercial_IfAny": "Dolby Digital Plus with Dolby Atmos",
         "Format_AdditionalFeatures": "JOC"},
        {"@type": "Audio", "Format": "DTS", "Format_Commercial_IfAny": "DTS-HD Master Audio"},
        {"@type": "Text"},
    ]}}

    def test_parses_per_audio_track(self):
        info = engine._parse_mediainfo_audio(self.FIXTURE)
        self.assertEqual([t["atmos"] for t in info], [True, True, False])
        self.assertEqual(info[2]["commercial"], "DTS-HD Master Audio")

    def test_missing_mediainfo_is_unknown(self):
        self.assertIsNone(engine._parse_mediainfo_audio(None))
        with mock.patch.object(engine, "_mediainfo_json", return_value=None):
            self.assertIsNone(engine._mediainfo_atmos("/x.mkv"))

    def test_pretty_names(self):
        self.assertEqual(engine.pretty_codec({"codec_name": "truehd", "atmos": True}), "TrueHD Atmos")
        self.assertEqual(engine.pretty_codec({"codec_name": "dts", "profile": "DTS-HD MA", "atmos": False}), "DTS-HD MA")
        self.assertEqual(engine.pretty_codec({"codec_name": "eac3", "atmos": None}), "E-AC-3")
        self.assertEqual(engine.pretty_codec({"codec_name": "pcm_s24le"}), "PCM")


class JobModel(unittest.TestCase):
    def test_old_history_defaults_to_fps(self):
        job = engine.Job.from_dict({"source": "/a.mkv", "conv_type": "24-25", "out_dir": "/o", "state": "done"})
        self.assertEqual((job.task, job.target, job.target_channels, job.atmos, job.drc),
                         ("fps", "ddp", 0, True, "film_light"))
        self.assertEqual(job.to_dict()["label"], "24-25")

    def test_encode_fields_roundtrip(self):
        job = engine.Job(source="/a.mkv", conv_type="encode", out_dir="/o", task="encode", target="dd",
                         target_channels=6, atmos=False, drc="speech", bitrate_override=448)
        job.label, job.pretty = "DD 5.1 448k", "TrueHD"
        back = engine.Job.from_dict(job.to_dict())
        self.assertEqual(back.key(), job.key())
        self.assertEqual((back.label, back.pretty), ("DD 5.1 448k", "TrueHD"))

    def test_key_distinguishes_encode_settings(self):
        a = engine.Job(source="/a.mkv", conv_type="encode", out_dir="/o", task="encode", target_channels=6)
        b = engine.Job(source="/a.mkv", conv_type="encode", out_dir="/o", task="encode", target_channels=8)
        self.assertNotEqual(a.key(), b.key())
        fps1 = engine.Job(source="/a.mkv", conv_type="24-25", out_dir="/o", bitrate_override=0)
        fps2 = engine.Job(source="/a.mkv", conv_type="24-25", out_dir="/o", bitrate_override=640)
        self.assertEqual(fps1.key(), fps2.key())      # fps duplicate rule unchanged: source + mode + stream

    def test_nested_encode_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "config_dir", return_value=__import__("pathlib").Path(tmp)):
                saved = config.save_settings({"task": "encode", "encode": {"target": "dd", "channels": "6", "atmos": 0, "junk": 1},
                                              "tools": {"truehdd": "/x/truehdd"}})
                self.assertEqual(saved["encode"], {"target": "dd", "channels": 6, "bitrate": 0, "atmos": False, "drc": "film_light"})
                self.assertEqual(config.load_settings()["tools"]["truehdd"], "/x/truehdd")
                self.assertEqual(config.load_settings()["task"], "encode")


class OverwritePolicy(unittest.TestCase):
    def test_next_free_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out.aac")
            open(base, "w").close()
            open(os.path.join(tmp, "out_1.aac"), "w").close()
            self.assertEqual(engine._next_free(base), os.path.join(tmp, "out_2.aac"))


if __name__ == "__main__":
    unittest.main()


# ───────────────────────── fps detection & suggestion ─────────────────────────

def test_fps_label_rounds_to_known_rates():
    assert engine.fps_label(24000 / 1001) == "23.976"
    assert engine.fps_label(24.0) == "24"
    assert engine.fps_label(30000 / 1001) == "29.97"
    assert engine.fps_label(0) is None
    assert engine.fps_label("x") is None


def test_detect_fps_prefers_video_track_then_tags_then_name():
    video = {"streams": [{"codec_type": "audio"}, {"codec_type": "video", "avg_frame_rate": "24000/1001"}]}
    assert engine.detect_fps(video, "movie.25fps.mkv") == ("23.976", "video")
    tagged = {"format": {"tags": {"FPS": "25"}}, "streams": [{"codec_type": "audio"}]}
    assert engine.detect_fps(tagged, "a.mka") == ("25", "tag")
    assert engine.detect_fps({"streams": []}, "Movie.2019.23.976fps.DDP5.1.mka") == ("23.976", "name")
    assert engine.detect_fps({"streams": []}, "Show.S01E24.mka") == (None, "")
    # a cover-art "video" stream is not a frame rate
    art = {"streams": [{"codec_type": "video", "avg_frame_rate": "90000/1", "disposition": {"attached_pic": 1}}]}
    assert engine.detect_fps(art, "x.m4a") == (None, "")


def test_suggest_from_known_frame_rates():
    sg = engine.suggest_conversion("23.976", 0, "25", 0)
    assert sg["conv_type"] == "23.976-25"
    assert engine.suggest_conversion("25", 0, "25", 0)["conv_type"] is None


def test_suggest_from_durations_when_audio_has_no_fps():
    video = 5400.0                       # 1 h 30 at 25 fps
    audio = video * 25 / 24              # the same cut at 24 fps runs longer
    sg = engine.suggest_conversion(None, audio, "25", video)
    assert sg["conv_type"] == "24-25"
    # tells 23.976 and 24 apart (they differ by 0.1 %)
    sg = engine.suggest_conversion(None, video * 25 / (24000 / 1001), "25", video)
    assert sg["conv_type"] == "23.976-25"
    # nothing sensible when the durations are unrelated
    assert engine.suggest_conversion(None, 100.0, "25", 5400.0) is None
    # same length already
    assert engine.suggest_conversion(None, 5400.5, None, 5400.0)["conv_type"] is None
