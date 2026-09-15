"""Verify stage — runs §6's checks against the finished output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from ..contracts import Command, ProgressSink, StageResult
from ..verify import verify_output
from .base import BaseStage
from .context import RunContext

__all__ = ["VerifyStage"]


@dataclass
class VerifyStage(BaseStage):
    id: str = "verify"
    title: str = "Verify the output"

    def describe(self, ctx: RunContext) -> str:
        spec = ctx.spec.verify
        wanted = [
            name
            for name, on in (
                ("sample count", spec.sample_count),
                (f"duration drift < {spec.duration_tolerance_ms} ms", spec.duration_drift),
                ("channel layout", spec.channel_layout),
                ("PCM MD5 (bit-exact paths only)", spec.pcm_md5),
                (f"null test < {spec.null_test_max_dbfs} dBFS", spec.null_test),
                (f"loudness delta < {spec.loudness_tolerance_lu} LU", spec.loudness),
            )
            if on
        ]
        return "Verify: " + ", ".join(wanted) + "."

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return ()

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        ctx.emit(progress, self.id, 5.0, "verifying")

        result = verify_output(
            source_stream=ctx.stream,
            output_path=ctx.output_path,
            ratio=ctx.ratio,
            spec=ctx.spec.verify,
            source_frames=ctx.metrics.get("source_frames"),
            source_rate=ctx.metrics.get("source_rate"),
            expected_frames=ctx.current_frames,
            expected_rate=ctx.current_rate,
            expect_bit_exact=ctx.expect_bit_exact,
            source_pcm_md5=ctx.source_pcm_md5,
            pcm_md5_bits=ctx.pcm_md5_bits,
            output_bit_depth=ctx.target_bit_depth,
            reference_for_null=ctx.artifacts.get("decoded"),
            registry=ctx.registry,
        )

        report_path = ctx.output_path.with_name(f"{ctx.output_path.stem}.verification.json")
        payload = {
            "job_id": ctx.spec.job_id,
            "source": str(ctx.media.path),
            "stream_index": ctx.stream.stream_index,
            "output": str(ctx.output_path),
            "retime": ctx.ratio.to_dict(),
            "method": ctx.spec.retime.method.value,
            "notes": ctx.notes,
            "warnings": ctx.warnings,
            "metrics": ctx.metrics,
            "verification": result.to_dict(),
        }
        try:
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            ctx.artifacts["verification"] = report_path
        except OSError as exc:
            ctx.warn(f"could not write {report_path.name}: {exc}")

        # A hand-off bundle wants the report next to the essence.
        handoff = ctx.artifacts.get("handoff")
        if handoff and handoff.is_dir():
            try:
                (handoff / "verification.json").write_text(
                    json.dumps(payload, indent=2), encoding="utf-8"
                )
            except OSError:
                pass

        ctx.emit(progress, self.id, 100.0, "verified")
        ctx.metrics["verification"] = result.to_dict()

        failures = result.failures
        return StageResult(
            stage_id=self.id,
            ok=not failures,
            outputs=(report_path,) if report_path.exists() else (),
            error=(
                "; ".join(f"{c.name}: {c.detail}" for c in failures) if failures else None
            ),
            notes=tuple(c.render().strip() for c in result.checks),
            metrics={"verification": result.to_dict()},
        )
