"""Naming purity, the TOCTOU claim, job identity, and resume semantics."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from fpsaudio.core.contracts import (
    AudioStream,
    JobSpec,
    OutputSpec,
    Refusal,
    RetimeMethod,
    RetimeSpec,
)
from fpsaudio.core.jobs import JobOutcome, JobState, QueueStore
from fpsaudio.core.naming import (
    TOKENS,
    build_name,
    claim,
    render_template,
    resolve_output,
    sanitize,
)
from fpsaudio.core.ratio import get_preset


def a_stream(index: int = 0) -> AudioStream:
    return AudioStream(
        stream_index=index, codec="truehd", channels=8, channel_layout="7.1",
        sample_rate=48000, bit_depth=24, language="eng", lossless=True,
    )


def a_spec(source: Path, out: Path, **kw) -> JobSpec:
    ratio = get_preset(kw.pop("preset", "23.976_to_25"))
    return JobSpec(
        source=source,
        stream_index=kw.pop("stream_index", 0),
        profile_key=ratio.key,
        retime=RetimeSpec(src_fps=ratio.src_fps, dst_fps=ratio.dst_fps),
        output=OutputSpec(directory=out, **kw),
    )


class TestSanitize(unittest.TestCase):
    def test_windows_reserved_characters_are_replaced(self) -> None:
        self.assertNotIn(":", sanitize("a:b"))
        self.assertNotIn("?", sanitize("what?"))

    def test_windows_device_names_are_escaped(self) -> None:
        # CON.flac is unopenable on Windows regardless of extension.
        for name in ("con", "CON", "nul", "lpt1", "com3"):
            self.assertTrue(sanitize(name).startswith("_"), name)

    def test_trailing_dots_and_spaces_are_stripped(self) -> None:
        self.assertEqual(sanitize("name. "), "name")

    def test_empty_becomes_something_openable(self) -> None:
        self.assertEqual(sanitize("   "), "untitled")


class TestTemplates(unittest.TestCase):
    def test_all_documented_tokens_render(self) -> None:
        values = {name: "x" for name in TOKENS}
        for name in TOKENS:
            self.assertEqual(render_template(f"{{{name}}}", values), "x")

    def test_unknown_token_is_refused_with_the_list(self) -> None:
        with self.assertRaises(Refusal) as ctx:
            render_template("{nonsense}", {})
        self.assertIn("Available tokens", " ".join(ctx.exception.remedies))

    def test_default_template_matches_the_legacy_convention(self) -> None:
        # The legacy __a{index}__{profile} stem is worth keeping (salvage item 7).
        name = build_name(
            template="{stem}__a{index}__{profile}",
            source=Path("/x/Movie.mkv"),
            stream=a_stream(1),
            profile_key="23.976_to_25",
            codec="flac",
            method="resample",
        )
        self.assertEqual(name, "Movie__a1__23_976_to_25")

    def test_rich_template(self) -> None:
        name = build_name(
            template="{stem}.{lang}.{channels}ch.{rate}.{codec}.{method}",
            source=Path("/x/Movie.mkv"),
            stream=a_stream(0),
            profile_key="24_to_25",
            codec="flac",
            method="redeclare",
            sample_rate=50000,
        )
        self.assertEqual(name, "Movie.eng.8ch.50000.flac.redeclare")


class TestNamingPurity(unittest.TestCase):
    """B-13: build_output_path created directories from a 'naming' function."""

    def test_resolving_a_name_creates_nothing_on_disk(self) -> None:
        root = Path(tempfile.mkdtemp()) / "does" / "not" / "exist"
        result = resolve_output(directory=root, stem="x", extension="flac")
        self.assertEqual(result.action, "write")
        self.assertFalse(root.exists(), "naming must not touch the filesystem")

    def test_skip_policy_reports_skip(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "x.flac").write_text("existing")
        result = resolve_output(directory=root, stem="x", extension="flac", overwrite="skip")
        self.assertEqual(result.action, "skip")

    def test_fail_policy_reports_fail(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "x.flac").write_text("existing")
        result = resolve_output(directory=root, stem="x", extension="flac", overwrite="fail")
        self.assertEqual(result.action, "fail")

    def test_unknown_policy_is_refused(self) -> None:
        with self.assertRaises(Refusal):
            resolve_output(
                directory=Path("/tmp"), stem="x", extension="flac", overwrite="nonsense"
            )


class TestAtomicClaim(unittest.TestCase):
    """B-13: two workers could both observe '_1' as free and both take it."""

    def test_claim_is_exclusive_under_threads(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "out.flac"
        claimed: list[Path] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(16)

        def worker() -> None:
            try:
                barrier.wait()
                claimed.append(claim(target, overwrite="rename"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(claimed), 16)
        self.assertEqual(
            len(set(claimed)), 16, "every worker must get a distinct path"
        )

    def test_claim_refuses_rather_than_clobbering(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "out.flac"
        claim(target, overwrite="skip")
        with self.assertRaises(Refusal):
            claim(target, overwrite="skip")

    def test_overwrite_policy_returns_the_same_path(self) -> None:
        root = Path(tempfile.mkdtemp())
        target = root / "out.flac"
        target.write_text("x")
        self.assertEqual(claim(target, overwrite="overwrite"), target)


class TestJobIdentity(unittest.TestCase):
    """B-12: job_id keyed on the basename collided across subfolders."""

    def test_same_basename_in_different_folders_gets_different_ids(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "a").mkdir()
        (root / "b").mkdir()
        first = root / "a" / "audio.mkv"
        second = root / "b" / "audio.mkv"
        first.write_text("x")
        second.write_text("x")
        out = root / "out"
        self.assertNotEqual(
            a_spec(first, out).job_id,
            a_spec(second, out).job_id,
        )

    def test_stream_index_changes_the_id(self) -> None:
        root = Path(tempfile.mkdtemp())
        source = root / "a.mkv"
        source.write_text("x")
        self.assertNotEqual(
            a_spec(source, root, stream_index=0).job_id,
            a_spec(source, root, stream_index=1).job_id,
        )

    def test_id_is_stable_across_runs(self) -> None:
        root = Path(tempfile.mkdtemp())
        source = root / "a.mkv"
        source.write_text("x")
        self.assertEqual(a_spec(source, root).job_id, a_spec(source, root).job_id)


class TestManifestRoundTrip(unittest.TestCase):
    def test_spec_survives_json_exactly(self) -> None:
        root = Path(tempfile.mkdtemp())
        source = root / "a.mkv"
        source.write_text("x")
        spec = a_spec(source, root, codec="flac", bitrate="640k")
        restored = JobSpec.from_dict(json.loads(spec.to_json()))
        self.assertEqual(restored.retime.src_fps, spec.retime.src_fps)
        self.assertEqual(restored.retime.dst_fps, spec.retime.dst_fps)
        self.assertEqual(restored.job_id, spec.job_id)
        self.assertEqual(restored.content_key(), spec.content_key())

    def test_a_future_spec_version_is_refused(self) -> None:
        with self.assertRaises(Refusal):
            JobSpec.from_dict(
                {
                    "spec_version": 99,
                    "source": "/x/a.mkv",
                    "stream_index": 0,
                    "retime": {"src_fps": "24", "dst_fps": "25"},
                    "output": {"directory": "/x"},
                }
            )


class TestResume(unittest.TestCase):
    """B-15: 'skip' keyed on filename treated a half-written file as complete."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.store = QueueStore(self.root / ".fpsaudio")
        self.source = self.root / "a.mkv"
        self.source.write_text("x")
        self.output = self.root / "out.flac"
        self.output.write_bytes(b"0" * 100)
        self.spec = a_spec(self.source, self.root, codec="flac")

    def _complete(self) -> None:
        self.store.mark_complete(
            self.spec, JobOutcome(self.spec.job_id, JobState.DONE, output=self.output)
        )

    def test_no_marker_means_not_complete(self) -> None:
        complete, reason = self.store.is_complete(self.spec)
        self.assertFalse(complete)
        self.assertIn("no completion marker", reason)

    def test_marker_makes_it_resumable(self) -> None:
        self._complete()
        complete, _ = self.store.is_complete(self.spec)
        self.assertTrue(complete)

    def test_a_changed_setting_invalidates_the_marker(self) -> None:
        """The marker is the hash of the whole spec, not the filename."""
        self._complete()
        different = a_spec(self.source, self.root, codec="opus")
        complete, _ = self.store.is_complete(different)
        self.assertFalse(complete)

    def test_a_deleted_output_invalidates_the_marker(self) -> None:
        self._complete()
        self.output.unlink()
        complete, reason = self.store.is_complete(self.spec)
        self.assertFalse(complete)
        self.assertIn("no longer exists", reason)

    def test_a_truncated_output_is_detected(self) -> None:
        """A half-written file from a killed run must not count as done."""
        self._complete()
        self.output.write_bytes(b"0" * 10)
        complete, reason = self.store.is_complete(self.spec)
        self.assertFalse(complete)
        self.assertIn("changed size", reason)

    def test_queue_state_survives_a_round_trip(self) -> None:
        from fpsaudio.core.jobs import JobRecord

        records = [JobRecord(spec=self.spec, state=JobState.DONE, output=self.output)]
        self.store.save(records)
        loaded = self.store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].job_id, self.spec.job_id)
        self.assertIs(loaded[0].state, JobState.DONE)

    def test_state_file_is_written_atomically(self) -> None:
        from fpsaudio.core.jobs import JobRecord

        self.store.save([JobRecord(spec=self.spec)])
        payload = json.loads(self.store.state_file.read_text())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["jobs"]), 1)


if __name__ == "__main__":
    unittest.main()
