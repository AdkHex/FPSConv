"""fpsaudio command line — full parity with the TUI.

Both surfaces build the same :class:`~fpsaudio.core.contracts.JobSpec`, so
``--dry-run``, ``--manifest`` and ``--dump-manifest`` all operate on the same
artefact and a job started in the TUI can be finished from the CLI.

Design notes worth stating:

* ``--dry-run`` explains **every** operation, including ones whose tool is
  missing.  A dry run that dies because a binary is absent tells you nothing.
* Refusals print their remedies and the exact ``--accept`` token, and exit 3.
  Nothing is ever silently downgraded to make a command succeed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from ..core.config import (
    CODECS,
    CONTAINERS,
    AppConfig,
    encodable_codecs,
    load_config,
)
from ..core.contracts import (
    AtmosPolicy,
    DitherMode,
    JobSpec,
    MediaFile,
    OutputSpec,
    Refusal,
    RefusalCode,
    RetimeMethod,
    RetimeSpec,
    VerifySpec,
    DecodeSpec,
)
from ..core.jobs import JobState, QueueStore, Scheduler, auto_workers
from ..core.plan import build_plan
from ..core.probe import discover_files, probe_file
from ..core.ratio import (
    LEGACY_PRESET_KEYS,
    exact_redeclare_targets,
    format_fps,
    iter_presets,
    presets,
    resolve_ratio,
)

try:
    import typer
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "fpsaudio's CLI needs typer.\n"
        "  Windows: .venv\\Scripts\\pip install typer\n"
        "  Or re-run install.ps1, which installs everything."
    ) from exc


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VERIFY_FAILED = 2
EXIT_REFUSED = 3

app = typer.Typer(
    name="fpsaudio",
    help="Retime audio between frame rates, exactly and without silent loss.",
    add_completion=False,
    no_args_is_help=True,
)


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def echo(text: str = "") -> None:
    typer.echo(text)


def warn(text: str) -> None:
    typer.echo(typer.style(text, fg=typer.colors.YELLOW), err=True)


def fail(text: str) -> None:
    typer.echo(typer.style(text, fg=typer.colors.RED), err=True)


def handle_refusal(refusal: Refusal) -> None:
    fail(refusal.render())
    raise typer.Exit(EXIT_REFUSED)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #

@app.command()
def doctor(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show detected flags."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    config_file: Optional[Path] = typer.Option(None, "--config", help="Config file."),
) -> None:
    """Report every tool this machine has, and what is missing.

    This is the command to run on the Windows box first. Paste its output back
    and the toolchain can be matched to what is actually installed.
    """
    from ..core.adapters import get_registry, set_tool_overrides

    config, used = load_config(config_file)
    if config.tools:
        set_tool_overrides(config.tools)

    registry = get_registry()

    if as_json:
        payload = {
            "config_files": [str(p) for p in used],
            "tools": {name: result.to_dict() for name, result in registry.detect_all().items()},
            "missing_essential": list(registry.missing_essential()),
        }
        echo(json.dumps(payload, indent=2))
        raise typer.Exit(EXIT_OK if not registry.missing_essential() else EXIT_ERROR)

    echo("fpsaudio doctor")
    echo("=" * 60)
    echo(f"Python      : {sys.version.split()[0]} ({sys.executable})")
    echo(f"Platform    : {sys.platform}")
    if used:
        echo(f"Config      : {', '.join(str(p) for p in used)}")
    else:
        echo("Config      : none found (using built-in defaults)")
    echo()

    for title, adapters in registry.groups():
        echo(f"{title}")
        for adapter in adapters:
            result = adapter.detect()
            mark = typer.style("  OK  ", fg=typer.colors.GREEN) if result.found else typer.style(
                "  --  ", fg=typer.colors.RED
            )
            line = f"{mark}{result.name:<12}"
            if result.found:
                line += f" {result.version or 'version unknown'}"
                if verbose and result.path:
                    line += f"\n            path: {result.path}"
            else:
                line += f" {result.error or 'not found'}"
                if result.required_for:
                    line += f"\n            needed for: {', '.join(result.required_for)}"
            echo(line)
            for note in result.capabilities.notes:
                warn(f"            ! {note}")
            if verbose and result.found and result.capabilities.flags:
                flags = " ".join(sorted(result.capabilities.flags)[:40])
                echo(f"            flags: {flags}")
            if verbose and result.found and result.capabilities.features:
                echo(f"            features: {' '.join(sorted(result.capabilities.features))}")
        echo()

    missing = registry.missing_essential()
    if missing:
        fail(f"Missing essential tools: {', '.join(missing)}")
        echo()
        echo("Install them with install.ps1, or individually:")
        for name in missing:
            echo(f"  {_install_hint(name)}")
        raise typer.Exit(EXIT_ERROR)

    echo(typer.style("All essential tools present.", fg=typer.colors.GREEN))

    optional = [
        a.name for a in registry if not a.detect().found and a.name not in missing
    ]
    if optional:
        echo()
        echo("Optional tools not installed (features that need them will refuse):")
        for name in optional:
            adapter = registry.get(name)
            need = ", ".join(adapter.required_for) if adapter and adapter.required_for else ""
            echo(f"  {name:<12} {need}")


_INSTALL_HINTS: dict[str, str] = {
    "ffmpeg": "winget install --id Gyan.FFmpeg -e",
    "ffprobe": "winget install --id Gyan.FFmpeg -e   (ships with ffmpeg)",
    "mediainfo": "winget install --id MediaArea.MediaInfo -e",
    "mkvmerge": "winget install --id MoritzBunkus.MKVToolNix -e",
    "mkvextract": "winget install --id MoritzBunkus.MKVToolNix -e",
    "flac": "winget install --id Xiph.Flac -e",
    "opusenc": "winget install --id Xiph.Opus-tools -e",
    "opusdec": "winget install --id Xiph.Opus-tools -e",
    "numpy": ".venv\\Scripts\\pip install numpy",
    "soxr": ".venv\\Scripts\\pip install soxr",
    "soundfile": ".venv\\Scripts\\pip install soundfile",
    "pyloudnorm": ".venv\\Scripts\\pip install pyloudnorm",
    "fdkaac": "no winget package; see docs/TOOLS.md for a pinned download",
    "wavpack": "no winget package; see docs/TOOLS.md",
    "rubberband": "no winget package; see docs/TOOLS.md",
    "truehdd": "no winget package; see docs/TOOLS.md",
}


def _install_hint(name: str) -> str:
    return f"{name:<12} {_INSTALL_HINTS.get(name, 'see docs/TOOLS.md')}"


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #

@app.command()
def inspect(
    paths: list[Path] = typer.Argument(..., help="Files or directories to probe."),
    recursive: bool = typer.Option(False, "--recursive", "-r"),
    as_json: bool = typer.Option(False, "--json"),
    prober: str = typer.Option("mediainfo", "--prober", help="mediainfo | ffprobe"),
) -> None:
    """Show what is actually in a file: every stream, with Atmos findings."""
    files: list[Path] = []
    for path in paths:
        files.extend(discover_files(path, recursive=recursive))

    results = [probe_file(path, prefer=prober) for path in files]

    if as_json:
        echo(json.dumps([m.to_dict() for m in results], indent=2))
        return

    for media in results:
        echo(typer.style(str(media.path), bold=True))
        if not media.identified:
            fail(f"  unidentified: {'; '.join(media.problems)}")
            echo()
            continue
        echo(
            f"  container {media.container}"
            f"{f', {media.duration_s:.3f} s' if media.duration_s else ''}"
            f"{', has video' if media.has_video else ''}"
            f"   (probed by {media.source_tool})"
        )
        if not media.audio:
            warn("  no audio streams")
        for stream in media.audio:
            echo(f"  {stream.label}")
            if stream.lossless:
                echo("      lossless")
            if stream.atmos.present:
                detail = f"      OBJECT AUDIO: {stream.atmos.kind} ({stream.atmos.certainty})"
                if stream.atmos.objects:
                    detail += f", {stream.atmos.objects} objects"
                echo(typer.style(detail, fg=typer.colors.CYAN))
                for evidence in stream.atmos.evidence:
                    echo(f"        evidence: {evidence}")
            elif stream.atmos.certainty == "unknown":
                warn(
                    f"      Atmos presence UNKNOWN — {'; '.join(stream.atmos.evidence)}"
                )
            if stream.start_time_s:
                echo(f"      delay {stream.start_time_s * 1000:+.3f} ms")
        if media.chapters:
            echo(f"  {len(media.chapters)} chapters")
        echo()


# --------------------------------------------------------------------------- #
# presets / formats / explain
# --------------------------------------------------------------------------- #

@app.command(name="presets")
def presets_command(
    all_presets: bool = typer.Option(False, "--all", help="Show all 90 generated presets."),
    tokens: bool = typer.Option(False, "--tokens", help="List output-template tokens."),
) -> None:
    """List retime presets and their exact ratios."""
    if tokens:
        from ..core.naming import TOKENS

        echo("Output template tokens:")
        for name, description in sorted(TOKENS.items()):
            echo(f"  {{{name}}}".ljust(14) + description)
        return

    echo(f"{'key':<20} {'label':<22} {'exact speed':<16} {'duration x'}")
    echo("-" * 76)
    shown = 0
    for key, ratio in iter_presets():
        if not all_presets and key not in LEGACY_PRESET_KEYS:
            continue
        echo(
            f"{key:<20} {ratio.label:<22} {ratio.speed_str:<16} "
            f"{ratio.duration_scale.numerator}/{ratio.duration_scale.denominator}"
        )
        shown += 1
    if not all_presets:
        echo()
        echo(f"Showing the {shown} classic presets. --all lists all {len(presets()) - 1}.")
        echo("Any pair also works directly: --from 29.97 --to 25")


@app.command()
def formats() -> None:
    """Show which formats can be read, written, and why some cannot."""
    echo("Encode targets available in this build:")
    for codec in encodable_codecs():
        info = CODECS[codec]
        kind = "lossless" if info.lossless else "lossy"
        echo(f"  {info.label:<22} {kind:<9} .{info.default_extension}")
        if info.notes:
            echo(f"      {info.notes}")
    echo()
    echo("Decode-only (these can be retimed, but not written back):")
    for codec, info in sorted(CODECS.items()):
        if info.encodable or info.decoder is None:
            continue
        echo(f"  {info.label:<22} {info.encode_blocked_reason}")
    echo()
    echo("Out of scope entirely:")
    for codec, info in sorted(CODECS.items()):
        if info.decoder is None:
            echo(f"  {info.label:<22} {info.encode_blocked_reason}")
    echo()
    echo("Containers:")
    for name, container in sorted(CONTAINERS.items()):
        echo(f"  .{name:<6} {', '.join(sorted(container.codecs))}")


@app.command()
def explain(
    preset: Optional[str] = typer.Option(None, "--preset", "-p"),
    src_fps: Optional[str] = typer.Option(None, "--from"),
    dst_fps: Optional[str] = typer.Option(None, "--to"),
    ratio: Optional[str] = typer.Option(None, "--ratio"),
    rate: int = typer.Option(48000, "--rate", help="Sample rate to reason about."),
    duration: float = typer.Option(7200.0, "--duration", help="Seconds, for drift figures."),
) -> None:
    """Show the exact maths for a retime, and what a wrong ratio would cost."""
    from fractions import Fraction

    try:
        retime = resolve_ratio(preset=preset, src_fps=src_fps, dst_fps=dst_fps, ratio=ratio)
    except Refusal as refusal:
        handle_refusal(refusal)
        return

    echo(typer.style(retime.label, bold=True))
    echo(f"  exact speed ratio    {retime.speed_str}   ({retime.percent_approx():+.6f}%)")
    echo(
        f"  duration multiplier  {retime.duration_scale.numerator}/"
        f"{retime.duration_scale.denominator}"
    )
    echo(f"  source fps           {format_fps(retime.src_fps)}  = {retime.src_fps}")
    echo(f"  target fps           {format_fps(retime.dst_fps)}  = {retime.dst_fps}")
    echo()

    frames_in = int(rate * duration)
    frames_out = retime.output_samples(frames_in, src_rate=rate)
    echo(f"  At {rate} Hz over {duration:g} s:")
    echo(f"    {frames_in} frames in  ->  {frames_out} frames out")
    echo(f"    duration {duration:g} s  ->  {float(retime.output_seconds(Fraction(int(duration)))):.6f} s")
    echo()

    new_rate = retime.redeclared_rate(rate)
    if new_rate.denominator == 1:
        echo(
            typer.style(
                f"  Bit-exact retime IS possible at {rate} Hz: redeclare as "
                f"{int(new_rate)} Hz. Not one sample changes.",
                fg=typer.colors.GREEN,
            )
        )
    else:
        echo(
            f"  Bit-exact retime is NOT possible at {rate} Hz "
            f"({rate} x {retime.speed_str} = {new_rate}, not a whole number of Hz)."
        )
        exact = list(exact_redeclare_targets(rate))
        if exact:
            echo(f"  Presets that ARE bit-exact at {rate} Hz:")
            for key, target in exact[:12]:
                echo(f"    {key:<20} -> {target} Hz")
    echo()

    # What the legacy tool would have done, where it differs.
    legacy = _LEGACY_RATIOS.get(retime.key)
    if legacy is not None and legacy != retime.speed:
        drift = duration / float(legacy) - duration / float(retime.speed)
        echo(
            typer.style(
                f"  The legacy 'Fps Converter' used {legacy.numerator}/{legacy.denominator} "
                f"here, which is wrong.",
                fg=typer.colors.RED,
            )
        )
        echo(
            f"  Over {duration:g} s that accumulates {abs(drift) * 1000:.1f} ms of drift "
            f"— {abs(drift) * 1000:.0f}x the 1 ms tolerance."
        )


#: The ratios Directory A shipped, for the comparison above (converter.py:91-114).
_LEGACY_RATIOS: dict[str, Any] = {}


def _init_legacy_ratios() -> None:
    from fractions import Fraction

    _LEGACY_RATIOS.update(
        {
            "23.976_to_25": Fraction(25025, 24000),
            "23.976_to_24": Fraction(24025, 24000),
            "25_to_23.976": Fraction(24000, 25025),
            "24_to_23.976": Fraction(24000, 24025),
            "25_to_24": Fraction(24025, 25025),
            "24_to_25": Fraction(25025, 24025),
        }
    )


_init_legacy_ratios()


# --------------------------------------------------------------------------- #
# convert
# --------------------------------------------------------------------------- #

@app.command()
def convert(
    inputs: list[Path] = typer.Argument(..., help="Files or directories."),
    output: Path = typer.Option(Path("."), "--output", "-o", help="Output directory."),
    preset: Optional[str] = typer.Option(None, "--preset", "-p"),
    src_fps: Optional[str] = typer.Option(None, "--from"),
    dst_fps: Optional[str] = typer.Option(None, "--to"),
    ratio: Optional[str] = typer.Option(None, "--ratio", help="Direct speed ratio."),
    method: str = typer.Option(
        "resample", "--method",
        help="resample (default) | redeclare (bit-exact) | stretch (pitch-preserving, opt-in)",
    ),
    codec: str = typer.Option("auto", "--codec", "-c"),
    container: str = typer.Option("auto", "--container"),
    bitrate: Optional[str] = typer.Option(None, "--bitrate", "-b"),
    bit_depth: Optional[int] = typer.Option(None, "--bit-depth"),
    sample_rate: Optional[int] = typer.Option(None, "--sample-rate"),
    dither: str = typer.Option("tpdf", "--dither", help="tpdf | shaped | none"),
    template: Optional[str] = typer.Option(None, "--template"),
    stream: Optional[int] = typer.Option(None, "--stream", help="Only this stream index."),
    all_streams: bool = typer.Option(False, "--all-streams"),
    recursive: bool = typer.Option(False, "--recursive", "-r"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    rename: bool = typer.Option(False, "--rename"),
    atmos_policy: str = typer.Option("refuse", "--atmos-policy", help="refuse | handoff | flatten"),
    accept: list[str] = typer.Option([], "--accept", help="Accept a documented loss by token."),
    jobs: int = typer.Option(0, "--jobs", "-j", help="Parallel files (0 = auto)."),
    encoder_jobs: int = typer.Option(0, "--encoder-jobs", help="Parallel encodes (0 = auto)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Explain everything; write nothing."),
    dump_manifest: Optional[Path] = typer.Option(None, "--dump-manifest"),
    keep_intermediates: bool = typer.Option(False, "--keep-intermediates"),
    null_test: bool = typer.Option(False, "--null-test"),
    loudness: bool = typer.Option(False, "--loudness"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    state_dir: Optional[Path] = typer.Option(None, "--state-dir"),
    config_file: Optional[Path] = typer.Option(None, "--config"),
    prober: str = typer.Option("mediainfo", "--prober"),
) -> None:
    """Retime one or more files."""
    from ..core.adapters import set_tool_overrides

    config, _ = load_config(config_file)
    if config.tools:
        set_tool_overrides(config.tools)

    try:
        specs, media_by_path = _collect_specs(
            inputs=inputs,
            output=output,
            preset=preset or (config.preset if not (src_fps or ratio) else None),
            src_fps=src_fps,
            dst_fps=dst_fps,
            ratio=ratio,
            method=method,
            codec=codec,
            container=container,
            bitrate=bitrate,
            bit_depth=bit_depth,
            sample_rate=sample_rate,
            dither=dither,
            template=template or config.template,
            stream=stream,
            all_streams=all_streams,
            recursive=recursive,
            overwrite=("overwrite" if overwrite else "rename" if rename else "skip"),
            atmos_policy=atmos_policy,
            accept=accept,
            keep_intermediates=keep_intermediates,
            null_test=null_test,
            loudness=loudness,
            prober=prober,
        )
    except Refusal as refusal:
        handle_refusal(refusal)
        return

    if not specs:
        fail("No audio streams found in the given inputs.")
        raise typer.Exit(EXIT_ERROR)

    if dump_manifest:
        payload = {
            "version": 1,
            "jobs": [spec.to_dict() for spec in specs],
        }
        dump_manifest.parent.mkdir(parents=True, exist_ok=True)
        dump_manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        echo(f"Wrote {len(specs)} job(s) to {dump_manifest}")

    if dry_run:
        _render_dry_run(specs, media_by_path)
        return

    _execute(
        specs,
        media_by_path=media_by_path,
        jobs=jobs or config.file_workers,
        encoder_jobs=encoder_jobs or config.encoder_workers,
        resume=resume,
        state_dir=state_dir or (output / ".fpsaudio"),
        keep_intermediates=keep_intermediates,
        prober=prober,
    )


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

@app.command()
def manifest(
    path: Path = typer.Argument(..., help="A manifest written by --dump-manifest."),
    dry_run: bool = typer.Option(False, "--dry-run"),
    jobs: int = typer.Option(0, "--jobs", "-j"),
    encoder_jobs: int = typer.Option(0, "--encoder-jobs"),
    resume: bool = typer.Option(True, "--resume/--no-resume"),
    state_dir: Optional[Path] = typer.Option(None, "--state-dir"),
    prober: str = typer.Option("mediainfo", "--prober"),
) -> None:
    """Run the jobs in a manifest — the same artefact the TUI produces."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        specs = [JobSpec.from_dict(entry) for entry in payload.get("jobs", ())]
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        fail(f"Cannot read manifest {path}: {exc}")
        raise typer.Exit(EXIT_ERROR)
    except Refusal as refusal:
        handle_refusal(refusal)
        return

    if not specs:
        fail("Manifest contains no jobs.")
        raise typer.Exit(EXIT_ERROR)

    media_by_path = {spec.source: probe_file(spec.source, prefer=prober) for spec in specs}

    if dry_run:
        _render_dry_run(specs, media_by_path)
        return

    _execute(
        specs,
        media_by_path=media_by_path,
        jobs=jobs,
        encoder_jobs=encoder_jobs,
        resume=resume,
        state_dir=state_dir or (specs[0].output.directory / ".fpsaudio"),
        keep_intermediates=False,
        prober=prober,
    )


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #

@app.command()
def watch(
    directory: Path = typer.Argument(..., help="Folder to watch."),
    output: Path = typer.Option(..., "--output", "-o"),
    preset: str = typer.Option("23.976_to_25", "--preset", "-p"),
    interval: float = typer.Option(5.0, "--interval", help="Seconds between scans."),
    settle: float = typer.Option(10.0, "--settle", help="Seconds a file must stop growing."),
    codec: str = typer.Option("auto", "--codec", "-c"),
    method: str = typer.Option("resample", "--method"),
    accept: list[str] = typer.Option([], "--accept"),
    prober: str = typer.Option("mediainfo", "--prober"),
) -> None:
    """Watch a folder and retime anything new that lands in it.

    A file is only picked up once its size has stopped changing for ``--settle``
    seconds, so a partially-copied file is never processed.
    """
    import time

    store = QueueStore(output / ".fpsaudio")
    store.ensure()
    seen: dict[Path, tuple[int, float]] = {}
    echo(f"Watching {directory} (Ctrl-C to stop)")

    try:
        while True:
            for path in discover_files(directory, recursive=False):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                previous = seen.get(path)
                now = time.monotonic()
                if previous is None or previous[0] != size:
                    seen[path] = (size, now)
                    continue
                if now - previous[1] < settle:
                    continue
                if previous[1] == -1.0:
                    continue

                echo(f"\nPicked up {path.name}")
                seen[path] = (size, -1.0)
                try:
                    specs, media_by_path = _collect_specs(
                        inputs=[path],
                        output=output,
                        preset=preset,
                        src_fps=None, dst_fps=None, ratio=None,
                        method=method, codec=codec, container="auto",
                        bitrate=None, bit_depth=None, sample_rate=None,
                        dither="tpdf", template=None, stream=None,
                        all_streams=False, recursive=False, overwrite="skip",
                        atmos_policy="refuse", accept=accept,
                        keep_intermediates=False, null_test=False, loudness=False,
                        prober=prober,
                    )
                    _execute(
                        specs,
                        media_by_path=media_by_path,
                        jobs=1, encoder_jobs=1, resume=True,
                        state_dir=output / ".fpsaudio",
                        keep_intermediates=False,
                        prober=prober,
                        exit_on_failure=False,
                    )
                except Refusal as refusal:
                    fail(refusal.render())
            time.sleep(interval)
    except KeyboardInterrupt:
        echo("\nStopped.")


# --------------------------------------------------------------------------- #
# tui
# --------------------------------------------------------------------------- #

@app.command()
def tui() -> None:
    """Launch the keyboard-driven terminal UI."""
    try:
        from ..tui.app import run as run_tui
    except ImportError as exc:
        fail(
            "The TUI needs textual.\n"
            "  Windows: .venv\\Scripts\\pip install textual\n"
            f"  ({exc})"
        )
        raise typer.Exit(EXIT_ERROR)
    run_tui()


# --------------------------------------------------------------------------- #
# Shared machinery
# --------------------------------------------------------------------------- #

def _collect_specs(
    *,
    inputs: list[Path],
    output: Path,
    preset: Optional[str],
    src_fps: Optional[str],
    dst_fps: Optional[str],
    ratio: Optional[str],
    method: str,
    codec: str,
    container: str,
    bitrate: Optional[str],
    bit_depth: Optional[int],
    sample_rate: Optional[int],
    dither: str,
    template: Optional[str],
    stream: Optional[int],
    all_streams: bool,
    recursive: bool,
    overwrite: str,
    atmos_policy: str,
    accept: list[str],
    keep_intermediates: bool,
    null_test: bool,
    loudness: bool,
    prober: str,
) -> tuple[list[JobSpec], dict[Path, MediaFile]]:
    retime = resolve_ratio(preset=preset, src_fps=src_fps, dst_fps=dst_fps, ratio=ratio)

    try:
        retime_method = RetimeMethod(method)
    except ValueError:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Unknown --method {method!r}.",
            remedies=[
                "resample  — the default: libsoxr VHQ, pitch moves with speed.",
                "redeclare — bit-exact, where the sample rate allows it.",
                "stretch   — pitch-preserving, opt-in, not a real speed change.",
            ],
        )

    files: list[Path] = []
    for path in inputs:
        files.extend(discover_files(path, recursive=recursive))

    specs: list[JobSpec] = []
    media_by_path: dict[Path, MediaFile] = {}
    unidentified: list[Path] = []

    for path in files:
        media = probe_file(path, prefer=prober)
        media_by_path[path] = media
        if not media.identified or not media.audio:
            unidentified.append(path)
            continue

        candidates = media.audio
        if stream is not None:
            candidates = tuple(s for s in media.audio if s.stream_index == stream)
            if not candidates:
                warn(f"{path.name}: no stream with index {stream}; skipping")
                continue
        elif not all_streams:
            candidates = (_default_stream(media),)

        for audio in candidates:
            specs.append(
                JobSpec(
                    source=path,
                    stream_index=audio.stream_index,
                    profile_key=retime.key,
                    retime=RetimeSpec(
                        src_fps=retime.src_fps,
                        dst_fps=retime.dst_fps,
                        method=retime_method,
                        target_sample_rate=sample_rate,
                    ),
                    output=OutputSpec(
                        directory=output,
                        template=template or "{stem}__a{index}__{profile}",
                        container=container,
                        codec=codec,
                        bitrate=bitrate,
                        bit_depth=bit_depth,
                        dither=DitherMode(dither),
                        overwrite=overwrite,
                        keep_intermediates=keep_intermediates,
                    ),
                    decode=DecodeSpec(),
                    verify=VerifySpec(null_test=null_test, loudness=loudness),
                    atmos_policy=AtmosPolicy(atmos_policy),
                    accepted=tuple(accept),
                )
            )

    # B-5: a file the scan cannot identify is reported, never silently dropped.
    for path in unidentified:
        media = media_by_path[path]
        warn(f"unidentified: {path.name} — {'; '.join(media.problems) or 'no audio streams'}")

    return specs, media_by_path


def _default_stream(media: MediaFile):
    """Pick the stream a user most likely means: default flag, then best quality."""
    for stream in media.audio:
        if stream.default:
            return stream
    return max(
        media.audio,
        key=lambda s: (s.lossless, s.channels or 0, s.bit_rate_bps or 0),
    )


def _render_dry_run(specs: list[JobSpec], media_by_path: dict[Path, MediaFile]) -> None:
    refused = 0
    for index, spec in enumerate(specs, 1):
        echo(typer.style(f"[{index}/{len(specs)}] {spec.source.name}", bold=True))
        try:
            plan = build_plan(spec, media_by_path[spec.source], claim_output=False)
        except Refusal as refusal:
            fail(refusal.render())
            refused += 1
            echo()
            continue
        echo(plan.describe())
        echo()
        echo("  Commands that would run:")
        echo(plan.render_commands())
        echo()

    echo(f"Dry run: {len(specs) - refused} job(s) would run, {refused} refused.")
    echo("Nothing was written.")
    if refused:
        raise typer.Exit(EXIT_REFUSED)


def _execute(
    specs: list[JobSpec],
    *,
    media_by_path: dict[Path, MediaFile],
    jobs: int,
    encoder_jobs: int,
    resume: bool,
    state_dir: Path,
    keep_intermediates: bool,
    prober: str,
    exit_on_failure: bool = True,
) -> None:
    store = QueueStore(state_dir)
    scheduler = Scheduler(
        store=store,
        file_workers=jobs or auto_workers("files"),
        encoder_workers=encoder_jobs or auto_workers("encoders"),
        keep_work_dir=keep_intermediates,
        on_event=_make_reporter(),
    )
    scheduler.add(specs)

    def probe(path: Path) -> MediaFile:
        cached = media_by_path.get(path)
        return cached if cached is not None else probe_file(path, prefer=prober)

    try:
        records = scheduler.run(probe=probe, resume=resume)
    except KeyboardInterrupt:
        scheduler.stop()
        warn("\nInterrupted. Progress is saved; re-run the same command to resume.")
        raise typer.Exit(EXIT_ERROR)

    echo()
    echo("Results")
    echo("-" * 60)
    failed = refused = verify_failed = 0
    for record in records:
        colour = {
            JobState.DONE: typer.colors.GREEN,
            JobState.SKIPPED: typer.colors.BLUE,
            JobState.FAILED: typer.colors.RED,
            JobState.REFUSED: typer.colors.MAGENTA,
        }.get(record.state, typer.colors.WHITE)
        echo(
            typer.style(f"  {record.state.value:<9}", fg=colour)
            + f" {record.spec.source.name} #{record.spec.stream_index}"
            + (f" -> {record.output.name}" if record.output else "")
        )
        if record.message and record.state is JobState.SKIPPED:
            echo(f"            {record.message}")
        for warning in record.warnings:
            warn(f"            ! {warning}")
        if record.refusal:
            refused += 1
            for line in record.error.splitlines():
                echo(f"            {line}")
            for remedy in record.refusal.get("remedies", ()):
                echo(f"            - {remedy}")
            token = record.refusal.get("override_token")
            if token:
                echo(f"            override with: --accept {token}")
        elif record.state is JobState.FAILED:
            failed += 1
            echo(f"            {record.error}")
        if record.verification:
            for check in record.verification.get("checks", ()):
                if check.get("skipped"):
                    echo(f"            ~ {check['name']}: skipped ({check['skip_reason']})")
                elif not check.get("ok"):
                    verify_failed += 1
                    fail(f"            FAIL {check['name']}: {check['detail']}")
                else:
                    echo(f"            PASS {check['name']}: {check['detail']}")

    echo()
    summary = scheduler.summary()
    echo(" | ".join(f"{key}={value}" for key, value in sorted(summary.items())))

    if not exit_on_failure:
        return
    if refused:
        raise typer.Exit(EXIT_REFUSED)
    if verify_failed:
        raise typer.Exit(EXIT_VERIFY_FAILED)
    if failed:
        raise typer.Exit(EXIT_ERROR)


def _make_reporter():
    state: dict[str, Any] = {"last": ""}

    def report(kind: str, payload: dict[str, Any]) -> None:
        if kind == "batch_start":
            echo(
                f"Starting {payload['total']} job(s): {payload['workers']} files in "
                f"parallel, {payload['encoder_slots']} encoder slots."
            )
        elif kind == "progress":
            line = (
                f"  {payload['job_id'][:8]} {payload['stage']:<16} "
                f"{payload['percent']:5.1f}%  {payload['message'][:48]}"
            )
            if line != state["last"]:
                typer.echo(line + " " * 8, nl=False)
                typer.echo("\r", nl=False)
                state["last"] = line
        elif kind == "batch_done":
            echo()

    return report


def main() -> None:
    try:
        app()
    except Refusal as refusal:
        fail(refusal.render())
        raise SystemExit(EXIT_REFUSED)


if __name__ == "__main__":
    main()
