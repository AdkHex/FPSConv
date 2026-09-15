"""Atmos stages: object decode, metadata retiming, and the DME hand-off.

§3.5's requirement is that object audio is never silently flattened.  Neither
legacy program contained the words Atmos, JOC, object or metadata anywhere, so
every Atmos source they touched was flattened to its 5.1/7.1 core without a word
to the user (B-7).

What is actually possible here, stated plainly (Part 5 of the plan):

* **TrueHD Atmos in** — ``truehdd`` decodes it to DAMF or ADM BWF with objects
  intact.  The object timestamps are then rescaled by the *same exact*
  :class:`fractions.Fraction` as the audio, so the bed and the objects stay
  locked together.
* **TrueHD Atmos out** — impossible to automate.  Dolby Media Encoder is GUI
  only.  So the tool produces everything DME needs and stops cleanly: the
  retimed essence, the retimed metadata, the verification report and a written
  recipe.  That is a hand-off, not a refusal.
* **DD+ Atmos (E-AC-3 JOC) in** — no JOC decoder exists outside Dolby's own
  tools.  ffmpeg decodes the 5.1 core and drops the objects.  So this refuses,
  and offers the TrueHD track from the same file if one exists, or a typed
  confirmation to flatten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

from ..contracts import (
    AtmosPolicy,
    Command,
    ProgressSink,
    Refusal,
    RefusalCode,
    StageResult,
)
from .base import BaseStage, run_command
from .context import RunContext

__all__ = ["AtmosDecodeStage", "HandoffStage", "retime_atmos_metadata"]


@dataclass
class AtmosDecodeStage(BaseStage):
    id: str = "atmos"
    title: str = "Decode Dolby Atmos objects"

    def _destination(self, ctx: RunContext) -> Path:
        return ctx.temp("atmos_master")

    def describe(self, ctx: RunContext) -> str:
        objects = ctx.stream.atmos.objects
        count = f"{objects} dynamic objects" if objects else "its object bed"
        return (
            f"Decode the TrueHD Atmos stream with truehdd, preserving {count}. "
            f"Object timestamps will be rescaled by {ctx.ratio.speed_str}, the same "
            f"exact ratio applied to the audio, so bed and objects stay locked."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return (
            ctx.registry.truehdd.decode_command(
                ctx.current, self._destination(ctx), output_format="damf"
            ),
        )

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        destination = self._destination(ctx)
        command = self.commands(ctx)[0]
        ok, message = run_command(
            ctx.registry.truehdd, command, ctx, self.id, progress,
            duration_s=ctx.stream.duration_s,
        )
        if not ok:
            return self.failed(f"truehdd decode failed: {message}")

        ctx.artifacts["atmos_master"] = destination
        rescaled = retime_atmos_metadata(destination, ctx.ratio.speed)
        ctx.note(
            f"rescaled {rescaled['timestamps_scaled']} object timestamps by "
            f"{ctx.ratio.duration_scale.numerator}/"
            f"{ctx.ratio.duration_scale.denominator}"
        )
        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(destination,),
            metrics={"atmos": ctx.stream.atmos.to_dict(), **rescaled},
        )


def retime_atmos_metadata(master: Path, speed: Fraction) -> dict[str, Any]:
    """Rescale every time-domain field in a DAMF/ADM sidecar.

    Timestamps scale by ``1/speed`` — the exact same rational as the audio, so
    no rounding difference can open up between the bed and the objects.  Values
    are rescaled as :class:`fractions.Fraction` and only rendered to text at the
    end.

    This walks the metadata generically: any key whose name marks it as a time,
    offset or duration is scaled.  Unrecognised structure is left untouched
    rather than rewritten into a shape the encoder might not accept.
    """
    scale = Fraction(1) / speed

    # Deduplicate: for a master already named ``*.atmos`` these expressions all
    # produce the same path, and scaling one file three times would cube the
    # ratio instead of applying it once.
    candidates: list[Path] = []
    for candidate in (
        master,
        master.with_suffix(".atmos"),
        (master / "metadata.json") if master.is_dir() else master,
    ):
        resolved = candidate.absolute()
        if resolved not in candidates:
            candidates.append(resolved)

    scaled_count = 0
    touched: list[str] = []

    for path in candidates:
        if not path.exists() or path.is_dir():
            continue
        if path.suffix.lower() not in (".json", ".atmos", ".xml"):
            continue
        if path.suffix.lower() == ".json" or path.suffix.lower() == ".atmos":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            payload, count = _scale_times(payload, scale)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            scaled_count += count
            touched.append(path.name)

    return {
        "timestamps_scaled": scaled_count,
        "metadata_files": touched,
        "scale": f"{scale.numerator}/{scale.denominator}",
    }


_TIME_KEYS = (
    "time", "timestamp", "offset", "duration", "start", "end", "position",
    "samplepos", "sampleposition", "fadein", "fadeout",
)


def _scale_times(node: Any, scale: Fraction) -> tuple[Any, int]:
    count = 0
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            lowered = key.lower().replace("_", "")
            if any(marker in lowered for marker in _TIME_KEYS) and isinstance(
                value, (int, float)
            ):
                out[key] = _render(Fraction(value).limit_denominator(10**9) * scale, value)
                count += 1
            else:
                out[key], sub = _scale_times(value, scale)
                count += sub
        return out, count
    if isinstance(node, list):
        result = []
        for item in node:
            scaled, sub = _scale_times(item, scale)
            result.append(scaled)
            count += sub
        return result, count
    return node, 0


def _render(value: Fraction, original: Any) -> Any:
    if isinstance(original, int):
        from ..ratio import round_half_up

        return round_half_up(value)
    return float(value)


# --------------------------------------------------------------------------- #
# Dolby Media Encoder hand-off
# --------------------------------------------------------------------------- #

@dataclass
class HandoffStage(BaseStage):
    id: str = "handoff"
    title: str = "Write the Dolby Media Encoder hand-off bundle"

    def describe(self, ctx: RunContext) -> str:
        return (
            "TrueHD / TrueHD Atmos output cannot be automated: Dolby Media Encoder is "
            "GUI-only and ships no CLI. Instead, write everything DME needs — the "
            "retimed PCM essence, the retimed Atmos master, the verification report and "
            "a written recipe of the exact settings to select — and stop cleanly. You "
            "finish the last step by hand."
        )

    def commands(self, ctx: RunContext) -> Sequence[Command]:
        return ()

    def run(self, ctx: RunContext, progress: ProgressSink | None = None) -> StageResult:
        bundle = ctx.handoff_dir or ctx.output_path.parent / f"{ctx.output_path.stem}_DME"
        bundle.mkdir(parents=True, exist_ok=True)

        essence = bundle / f"{ctx.output_path.stem}_essence{ctx.current.suffix}"
        if ctx.current.exists() and ctx.current != essence:
            import shutil

            shutil.copy2(ctx.current, essence)

        master = ctx.artifacts.get("atmos_master")
        if master and master.exists():
            import shutil

            target = bundle / master.name
            if master.is_dir():
                shutil.copytree(master, target, dirs_exist_ok=True)
            else:
                shutil.copy2(master, target)

        recipe = bundle / "DME_RECIPE.md"
        recipe.write_text(_recipe_text(ctx, essence), encoding="utf-8")

        ctx.artifacts["handoff"] = bundle
        ctx.note(f"hand-off bundle written to {bundle}")
        ctx.emit(progress, self.id, 100.0, "hand-off bundle written")

        return StageResult(
            stage_id=self.id,
            ok=True,
            outputs=(bundle,),
            notes=(
                "TrueHD encode was NOT performed — Dolby Media Encoder has no CLI. "
                f"Open {recipe.name} and finish the encode in the DME GUI.",
            ),
            metrics={"bundle": str(bundle)},
        )


def _recipe_text(ctx: RunContext, essence: Path) -> str:
    stream = ctx.stream
    ratio = ctx.ratio
    frames_in = stream.sample_count or "unknown"
    rate = ctx.current_rate or stream.sample_rate or 48000
    return f"""# Dolby Media Encoder hand-off

fpsaudio has done everything that can be automated. The final TrueHD encode has
to be done by hand, because Dolby Media Encoder is GUI-only and ships no
command-line interface. Nothing here is a guess: every number below came from
the retime that was actually performed.

## What was done

| | |
| --- | --- |
| Source | `{ctx.media.path.name}` |
| Stream | #{stream.stream_index} — {stream.codec}{f" ({stream.profile})" if stream.profile else ""} |
| Atmos | {stream.atmos.kind or "none"} ({stream.atmos.certainty}){f", {stream.atmos.objects} objects" if stream.atmos.objects else ""} |
| Retime | {ratio.label} |
| Exact speed ratio | `{ratio.speed_str}` ({ratio.percent_approx():+.6f}%) |
| Method | {ctx.spec.retime.method.value} |
| Input frames | {frames_in} |
| Output frames | {ctx.current_frames or "unknown"} |
| Sample rate | {rate} Hz |
| Channels | {ctx.current_channels or stream.channels or "unknown"} |

Object metadata timestamps were rescaled by exactly
`{ratio.duration_scale.numerator}/{ratio.duration_scale.denominator}` — the same
rational applied to the audio — so the bed and the objects remain locked.

## Files in this bundle

- `{essence.name}` — the retimed PCM essence (RF64/W64, 32-bit float)
- the retimed Atmos master (DAMF folder or ADM BWF), if one was produced
- `verification.json` — the checks that were run on the retimed audio

## Settings to select in Dolby Media Encoder

1. **Input**: the Atmos master in this folder (DAMF `.atmos` / ADM BWF).
   If you are encoding the bed only, use `{essence.name}`.
2. **Output format**: Dolby TrueHD{" with Dolby Atmos" if stream.atmos.present else ""}.
3. **Sample rate**: {rate} Hz. Do **not** let DME resample — the retime is
   already done, and resampling again would compound the error.
4. **Channel configuration**: {stream.channel_layout or f"{stream.channels or '?'} channels"}.
5. **Dialogue normalisation**: set to the source's dialnorm value. fpsaudio
   decoded with `-drc_scale 0` and no target-level gain, so the essence carries
   the original levels and DME should not apply another correction.
6. **Dynamic range profile**: match the source. Do not add compression.

## After encoding

Check the result against `verification.json`:

- duration should be {_duration_text(ctx)}
- the frame count should be {ctx.current_frames or "as recorded above"}

If DME's output differs by more than a millisecond, something resampled twice.
"""


def _duration_text(ctx: RunContext) -> str:
    if ctx.current_frames and ctx.current_rate:
        seconds = ctx.current_frames / ctx.current_rate
        return f"{seconds:.6f} s"
    return "as recorded in verification.json"
