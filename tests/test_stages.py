"""The per-job stage list behind the Progress view."""

from __future__ import annotations

import unittest

from fpsconv import engine
from fpsconv.engine import Job


def _states(job: Job) -> list[tuple[str, str]]:
    return [(s["name"], s["state"]) for s in job.stages]


class StageList(unittest.TestCase):
    def test_plan_then_run_through(self):
        job = Job(source="a.thd", conv_type="encode", out_dir="/out")
        job.plan_stages(["Extracting TrueHD", "truehdd decode", "DEE encode", "Finishing"])
        job.stage_skip("Extracting TrueHD", "source is already a .thd")
        self.assertEqual(job.overall(), 0.0)

        job.stage_begin("truehdd decode")
        job.stage_progress(50)
        self.assertEqual(_states(job), [("Extracting TrueHD", "skip"), ("truehdd decode", "running"),
                                        ("DEE encode", "wait"), ("Finishing", "wait")])
        w = engine.STAGE_WEIGHT
        total = w["truehdd decode"] + w["DEE encode"] + w["Finishing"]      # the skipped one does not count
        self.assertAlmostEqual(job.overall(), w["truehdd decode"] * 0.5 / total * 100, places=3)

        job.stage_begin("DEE encode")                                       # the previous one closes itself
        self.assertEqual(job.stages[1]["state"], "done")
        self.assertEqual(job.stages[1]["pct"], 100.0)
        self.assertGreater(job.stages[1]["finished"], 0)
        job.stage_begin("DEE encode")                                       # same stage again: no-op
        self.assertEqual(job.stages[2]["state"], "running")

        job.stage_begin("Finishing")
        job.stage_close(True)
        self.assertTrue(all(s["state"] in ("done", "skip") for s in job.stages))
        self.assertEqual(job.overall(), 100.0)

    def test_unplanned_stage_slots_in_before_the_waiting_ones(self):
        job = Job(source="a.wav", conv_type="encode", out_dir="/out")
        job.plan_stages(["Decoding to WAV", "DEE: measure", "DEE: encode", "Finishing"])
        job.stage_begin("Decoding to WAV")
        job.stage_begin("ffmpeg")                     # deew's own pre-step the plan did not list
        self.assertEqual([s["name"] for s in job.stages],
                         ["Decoding to WAV", "ffmpeg", "DEE: measure", "DEE: encode", "Finishing"])
        self.assertEqual(job.stages[1]["state"], "running")

    def test_failure_and_cancel_mark_the_running_stage(self):
        job = Job(source="a.thd", conv_type="encode", out_dir="/out")
        job.plan_stages(["truehdd decode", "DEE encode"])
        job.stage_begin("truehdd decode")
        job.stage_progress(31)
        job.stage_close(False)
        self.assertEqual(job.stages[0]["state"], "failed")
        self.assertEqual(job.stages[0]["pct"], 31)                          # keeps where it stopped
        self.assertEqual(job.stages[1]["state"], "wait")

        job2 = Job(source="b.thd", conv_type="encode", out_dir="/out")
        job2.plan_stages(["truehdd decode"])
        job2.stage_begin("truehdd decode")
        job2.cancel()
        job2.stage_close(False)
        self.assertEqual(job2.stages[0]["state"], "cancelled")

    def test_no_plan_falls_back_to_percent(self):
        job = Job(source="a.wav", conv_type="23.976-25", out_dir="/out")
        job.percent = 42.0
        self.assertEqual(job.overall(), 42.0)
        job.stage_begin("ffmpeg atempo + AAC")         # runners still create stages on the fly
        self.assertEqual(_states(job), [("ffmpeg atempo + AAC", "running")])

    def test_history_roundtrip_and_snapshot_fields(self):
        job = Job(source="a.thd", conv_type="encode", out_dir="/out")
        job.plan_stages(["truehdd decode", "Finishing"])
        job.stage_begin("truehdd decode")
        d = job.to_dict()
        self.assertIn("stages", d)
        self.assertIn("overall", d)
        self.assertIn("now", d)
        back = Job.from_dict(d)
        self.assertEqual(_states(back), _states(job))
        self.assertEqual(Job.from_dict({"source": "old.mka", "state": "done"}).stages, [])   # pre-stages history

    def test_stage_of_strips_step_numbering(self):
        self.assertEqual(engine.stage_of("Extracting TrueHD (1/5)"), "Extracting TrueHD")
        self.assertEqual(engine.stage_of("DEE flat 7.1 (3/5–4/5)"), "DEE flat 7.1")
        self.assertEqual(engine.stage_of("Encoding AAC Engine"), "ffmpeg atempo + AAC")


if __name__ == "__main__":
    unittest.main()
