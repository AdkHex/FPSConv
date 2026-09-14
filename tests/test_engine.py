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
