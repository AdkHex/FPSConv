"""End-to-end pipeline tests against real binaries and synthetic media.

These run the actual planner, stages, scheduler and verifier.  They skip
cleanly when the tools they need are absent, so the suite is still meaningful
on a machine that has only part of the toolchain.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fpsaudio.core.adapters import get_registry
from fpsaudio.core.contracts import (
    JobSpec,
    OutputSpec,
    RetimeMethod,
    RetimeSpec,
    VerifySpec,
)
from fpsaudio.core.jobs import JobState, QueueStore, Scheduler
from fpsaudio.core.plan import build_plan
from fpsaudio.core.probe import probe_file
from fpsaudio.core.ratio import get_preset

REGISTRY = get_registry()


def have(*names: str) -> bool:
    return all(
        (REGISTRY.get(n) is not None and REGISTRY.get(n).detect().found) for n in names
    )


def make_spec(source: Path, out: Path, **kw) -> JobSpec:
    ratio = get_preset(kw.pop("preset", "23.976_to_25"))
    return JobSpec(
        source=source,
        stream_index=kw.pop("stream_index", 0),
        profile_key=ratio.key,
        retime=RetimeSpec(
            src_fps=ratio.src_fps,
            dst_fps=ratio.dst_fps,
            method=kw.pop("method", RetimeMethod.RESAMPLE),
        ),
        output=OutputSpec(directory=out, codec=kw.pop("codec", "flac")),
        verify=kw.pop("verify", VerifySpec()),
        accepted=kw.pop("accepted", ()),
    )


@unittest.skipUnless(
    have("ffmpeg", "ffprobe", "numpy", "soxr", "soundfile"),
    "the core toolchain is not installed on this machine",
)
class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fpsaudio.core.dsp import synth

        cls.dir = Path(tempfile.mkdtemp(prefix="fpsaudio_e2e_"))
        cls.inputs = cls.dir / "in"
        cls.inputs.mkdir()

        cls.stereo = cls.inputs / "stereo.wav"
        synth.write(
            cls.stereo, synth.sine(2.0, 48000, 997.0, channels=2), 48000, subtype="PCM_24"
        )
        cls.surround = cls.inputs / "surround.wav"
        synth.write(
            cls.surround,
            synth.sweep(2.0, 48000, f1=15000, channels=6),
            48000,
            subtype="PCM_24",
        )

    def _run(self, spec: JobSpec) -> tuple[object, object]:
        from fpsaudio.core.jobs import run_plan

        media = probe_file(spec.source)
        plan = build_plan(
            spec, media, work_dir=self.dir / "work" / spec.short_id, claim_output=False
        )
        outcome = run_plan(plan, keep_work_dir=False)
        return plan, outcome

    # -- the lossless path ------------------------------------------------- #

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_redeclare_is_bit_exact_end_to_end(self) -> None:
        """The Part 6 gate: PCM MD5 identical on the lossless path."""
        spec = make_spec(
            self.stereo,
            self.dir / "out_redeclare",
            preset="24_to_25",
            method=RetimeMethod.REDECLARE,
            codec="flac",
        )
        plan, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)

        checks = {c["name"]: c for c in outcome.verification["checks"]}
        self.assertTrue(checks["pcm_md5"]["ok"], checks["pcm_md5"]["detail"])
        self.assertFalse(checks["pcm_md5"]["skipped"])
        self.assertTrue(checks["sample_count"]["ok"])
        self.assertTrue(checks["duration_drift"]["ok"])
        self.assertTrue(outcome.output.exists())

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_redeclared_output_carries_the_new_sample_rate(self) -> None:
        from fpsaudio.core.audiofile import info

        spec = make_spec(
            self.stereo,
            self.dir / "out_rate",
            preset="24_to_25",
            method=RetimeMethod.REDECLARE,
            codec="flac",
        )
        _, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)
        # 48000 * 25/24 = 50000 exactly.
        self.assertEqual(info(outcome.output).samplerate, 50000)
        self.assertEqual(info(outcome.output).frames, 96000)

    # -- the resample path ------------------------------------------------- #

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_resample_hits_the_exact_frame_count(self) -> None:
        spec = make_spec(self.surround, self.dir / "out_resample", codec="flac")
        plan, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)

        checks = {c["name"]: c for c in outcome.verification["checks"]}
        self.assertTrue(checks["sample_count"]["ok"], checks["sample_count"]["detail"])
        self.assertTrue(checks["channel_layout"]["ok"])
        expected = get_preset("23.976_to_25").output_samples(96000, src_rate=48000)
        self.assertEqual(checks["sample_count"]["measured"], expected)

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_null_test_gate_on_a_24_bit_output(self) -> None:
        spec = make_spec(
            self.surround,
            self.dir / "out_null",
            codec="flac",
            verify=VerifySpec(null_test=True),
        )
        _, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)
        checks = {c["name"]: c for c in outcome.verification["checks"]}
        self.assertTrue(checks["null_test"]["ok"], checks["null_test"]["detail"])
        self.assertLess(checks["null_test"]["measured"], -140.0)

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_multichannel_intermediate_is_not_silently_corrupted(self) -> None:
        """ffmpeg's W64 writer + libsndfile disagree for >=3 channels.

        The decode stage asserts the intermediate reads back as float, so this
        can never degrade silently. A passing null test proves the data survived.
        """
        spec = make_spec(
            self.surround,
            self.dir / "out_w64",
            codec="flac",
            verify=VerifySpec(null_test=True),
        )
        _, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)
        checks = {c["name"]: c for c in outcome.verification["checks"]}
        self.assertTrue(checks["null_test"]["ok"], checks["null_test"]["detail"])

    # -- dry run ----------------------------------------------------------- #

    def test_dry_run_explains_every_step_and_writes_nothing(self) -> None:
        out = self.dir / "out_dry"
        spec = make_spec(self.stereo, out, codec="flac")
        media = probe_file(spec.source)
        plan = build_plan(spec, media, claim_output=False)

        description = plan.describe()
        for expected in ("Source", "Retime", "Method", "Output", "Steps:"):
            self.assertIn(expected, description)
        for stage in plan.stages:
            self.assertTrue(stage.describe(plan.ctx))

        self.assertFalse(out.exists(), "planning must not create the output directory")

    def test_dry_run_names_real_intermediates_not_the_source(self) -> None:
        spec = make_spec(self.stereo, self.dir / "out_dry2", codec="flac")
        plan = build_plan(spec, probe_file(spec.source), claim_output=False)
        encode = [c for c in plan.commands() if c.adapter == "flac"]
        if not encode:
            self.skipTest("flac is not installed")
        # The encoder must read the quantised intermediate, never the source.
        self.assertIn("quantized.pcm", encode[0].rendered())
        self.assertNotIn(str(self.stereo), encode[0].rendered())

    # -- batch, resume, concurrency ---------------------------------------- #

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_batch_runs_and_then_resumes_without_redoing_work(self) -> None:
        out = self.dir / "out_batch"
        state = out / ".fpsaudio"
        specs = [
            make_spec(self.stereo, out, codec="flac"),
            make_spec(self.surround, out, codec="flac"),
        ]

        scheduler = Scheduler(store=QueueStore(state), file_workers=2, encoder_workers=1)
        scheduler.add(specs)
        records = scheduler.run(probe=probe_file)
        self.assertTrue(all(r.state is JobState.DONE for r in records), records[0].error)

        # Second pass: everything is already complete, so nothing re-runs.
        again = Scheduler(store=QueueStore(state), file_workers=2)
        again.add(specs)
        resumed = again.run(probe=probe_file)
        self.assertTrue(all(r.state is JobState.SKIPPED for r in resumed))
        self.assertTrue(all("completed earlier" in r.message for r in resumed))

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_a_deleted_output_makes_resume_rerun_that_job(self) -> None:
        out = self.dir / "out_resume"
        state = out / ".fpsaudio"
        specs = [make_spec(self.stereo, out, codec="flac")]

        scheduler = Scheduler(store=QueueStore(state), file_workers=1)
        scheduler.add(specs)
        first = scheduler.run(probe=probe_file)
        self.assertIs(first[0].state, JobState.DONE, first[0].error)
        first[0].output.unlink()

        again = Scheduler(store=QueueStore(state), file_workers=1)
        again.add(specs)
        second = again.run(probe=probe_file)
        self.assertIs(second[0].state, JobState.DONE, second[0].error)

    def _collide_sources(self) -> Path:
        from fpsaudio.core.dsp import synth

        root = self.dir / "collide"
        if root.exists():
            return root
        for sub in ("a", "b"):
            (root / sub).mkdir(parents=True)
            synth.write(
                root / sub / "audio.wav",
                synth.sine(0.5, 48000, 997.0, channels=2),
                48000,
                subtype="PCM_24",
            )
        return root

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_colliding_output_names_are_reported_not_collapsed(self) -> None:
        """B-12: two audio.mkv files in different folders shared one output.

        The legacy build wrote both jobs to the same path and reported both as
        successes. Producing fewer files than jobs without saying so is the
        failure mode; refusing loudly is the fix.
        """
        root = self._collide_sources()
        out = self.dir / "out_collide"
        specs = [
            make_spec(root / "a" / "audio.wav", out, codec="flac"),
            make_spec(root / "b" / "audio.wav", out, codec="flac"),
        ]
        self.assertNotEqual(specs[0].job_id, specs[1].job_id)

        scheduler = Scheduler(store=QueueStore(out / ".fpsaudio"), file_workers=2)
        scheduler.add(specs)
        records = scheduler.run(probe=probe_file)

        states = sorted(r.state for r in records)
        self.assertEqual(states, sorted([JobState.DONE, JobState.REFUSED]))
        refused = next(r for r in records if r.state is JobState.REFUSED)
        self.assertIn("{parent}", " ".join(refused.refusal["remedies"]))

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_the_parent_token_resolves_the_collision(self) -> None:
        """And the remedy the refusal suggests actually works."""
        root = self._collide_sources()
        out = self.dir / "out_parent"
        specs = []
        for sub in ("a", "b"):
            spec = make_spec(root / sub / "audio.wav", out, codec="flac")
            specs.append(
                JobSpec(
                    source=spec.source,
                    stream_index=spec.stream_index,
                    profile_key=spec.profile_key,
                    retime=spec.retime,
                    output=OutputSpec(
                        directory=out,
                        template="{parent}__{stem}__a{index}__{profile}",
                        codec="flac",
                    ),
                )
            )

        scheduler = Scheduler(store=QueueStore(out / ".fpsaudio"), file_workers=2)
        scheduler.add(specs)
        records = scheduler.run(probe=probe_file)
        for record in records:
            self.assertIs(record.state, JobState.DONE, record.error)
        self.assertEqual(len({r.output for r in records}), 2)

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_skip_policy_does_not_overwrite_an_existing_output(self) -> None:
        """The legacy build resolved 'skip' and then wrote the file anyway."""
        out = self.dir / "out_skip"
        spec = make_spec(self.stereo, out, codec="flac")
        _, first = self._run(spec)
        self.assertIs(first.state, JobState.DONE, first.error)

        target = first.output
        before = target.read_bytes()
        target.write_bytes(b"sentinel content that must survive")

        scheduler = Scheduler(store=QueueStore(out / ".fpsaudio2"), file_workers=1)
        scheduler.add([spec])
        records = scheduler.run(probe=probe_file, resume=False)
        self.assertIs(records[0].state, JobState.SKIPPED)
        self.assertEqual(target.read_bytes(), b"sentinel content that must survive")
        self.assertNotEqual(target.read_bytes(), before)

    @unittest.skipUnless(have("flac"), "flac is not installed")
    def test_a_verification_report_is_written_next_to_the_output(self) -> None:
        import json

        spec = make_spec(self.stereo, self.dir / "out_report", codec="flac")
        _, outcome = self._run(spec)
        self.assertIs(outcome.state, JobState.DONE, outcome.error)
        report = outcome.output.with_name(f"{outcome.output.stem}.verification.json")
        self.assertTrue(report.exists())
        payload = json.loads(report.read_text())
        self.assertIn("verification", payload)
        self.assertEqual(payload["retime"]["speed"], "1001/960")


if __name__ == "__main__":
    unittest.main()
