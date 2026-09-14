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


class OverwritePolicy(unittest.TestCase):
    def test_next_free_numbering(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out.aac")
            open(base, "w").close()
            open(os.path.join(tmp, "out_1.aac"), "w").close()
            self.assertEqual(engine._next_free(base), os.path.join(tmp, "out_2.aac"))


if __name__ == "__main__":
    unittest.main()
