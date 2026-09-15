# Architecture

```
fpsaudio/
  core/
    contracts.py      frozen shared models; zero third-party imports
    ratio.py          exact-rational FPS maths, preset table    [seeded from profiles.py]
    probe.py          mediainfo/ffprobe -> normalised MediaFile [rewritten]
    config.py         codec/container registries + TOML config
    naming.py         token-template output paths               [generalised from naming.py]
    plan.py           JobSpec -> ordered Stage list; every refusal decided here
    dsp.py            libsoxr resampling, dither, null test, measurement
    audiofile.py      RF64/W64 streaming I/O
    verify.py         sample count, PCM MD5, null test, loudness, layout
    jobs.py           queue, persistence, resume, concurrency   [scheduler from app.py]
    adapters/         one module per binary
      base.py         detect / version / capabilities / build_argv / parse_progress
      ffmpeg.py mediainfo.py encoders.py mkvtoolnix.py truehdd.py
      rubberband.py soxr.py demuxers.py
    stages/
      demux decode retime encode atmos mux verify_stage
  cli/                typer entry point, full parity with the TUI
  tui/                Textual app — presentation only, zero business logic
```

## The three rules everything else follows from

### 1. A frame-rate change is a resample, not a time-stretch

This is the correction the whole project exists for. Playing 23.976 fps content
at 25 fps makes it run 4.27% fast; the audio must run 4.27% fast too, and its
pitch rises with it — exactly as it does in a cinema projector.

`atempo`, `sox tempo` and `rubberband --tempo` all preserve pitch. They are the
wrong operation. Rubber Band survives as `--method stretch` for the one case
where somebody deliberately wants pitch left alone, and it warns when used.

### 2. Exactness is never lost at a boundary

The speed ratio is a `fractions.Fraction` from end to end. It reaches libsoxr as
a *pair of integers*, never as a decimal:

```
ratio = out_rate / (in_rate × speed)
      = (out_rate × speed.denominator) / (in_rate × speed.numerator)
```

Both sides are integers; reduce by the GCD and hand them to the resampler. The
realised frame count is then asserted against the exact rational.

### 3. Nothing is lost silently

Every operation that would discard information raises a `Refusal` carrying what
would be lost, the alternatives, and the exact token to type if the user really
wants it. There is no code path that quietly downmixes, flattens, transcodes or
truncates.

## Planning is separate from running

```
JobSpec + MediaFile ──build_plan()──> Plan ──run_plan()──> JobOutcome
                         │                    │
                    all refusals          all side effects
                    all naming
                    all validation
```

`Plan.describe()` and `Plan.commands()` are pure. That is what makes `--dry-run`
total rather than best-effort: it shows the same refusals a real run would hit,
and it names the real intermediate paths, because the planner walks the stages
on a snapshot context where each stage projects what it *would* produce.

A dry run on a machine missing a tool still explains the whole plan; the
unresolvable step is reported through `Plan.unresolved()` rather than aborting
the description.

## The stage pipeline

A typical decode → resample → encode job:

| Stage | What it does |
| --- | --- |
| `demux` | (Atmos only) lift the elementary stream out, bit-exactly |
| `atmos` | (Atmos only) `truehdd` → DAMF/ADM, then rescale object timestamps |
| `decode` | ffmpeg → float32 RF64/W64, `-drc_scale 0`, `-map 0:N -vn -sn -dn` |
| `retime_lossless` \| `resample` \| `stretch` | the speed change |
| `quantize` | the single dithered quantisation in the whole chain |
| `encode` | flac / fdkaac / opusenc / wavpack, fed headerless PCM |
| `handoff` | (Atmos only) write the DME bundle and recipe |
| `mux` | (mka only) mkvmerge, applying the rescaled delay |
| `verify` | the acceptance gates, written to `<output>.verification.json` |

Every stage implements `describe(ctx)`, `commands(ctx)`, `advance(ctx)` and
`run(ctx, progress)`. `advance` is the plan-time projection; `run` is the only
one with side effects.

## The adapter contract

```python
detect()         -> DetectResult(found, path, version, capabilities)
capabilities     -> flags observed in --help, plus semantic features
build_argv(...)  -> Command(argv, purpose, weight)
parse_progress() -> percent or None
```

Adapters are the only place that knows a tool's syntax, and they learn it from
the tool. `require_flag()` asserts a flag exists before a command is built
around it, so a missing capability is a refusal naming the tool and the purpose
— not a second command whose error message hides the first one's.

`detect()` catches `FileNotFoundError`, which is what `subprocess.run` raises
for a missing binary. Checking the return code, as the legacy code did, can
never see it.

## Concurrency

Two independent limits, because a decode and an encode saturate different
resources:

- **file workers** — how many jobs run at once (default `min(4, cpus)`)
- **encoder slots** — how many `quantize`/`encode` stages run at once
  (default `min(4, cpus/2)`)

The scheduler submits as slots free up rather than in fixed waves. Worker
threads never mutate shared state: they return an immutable `JobOutcome` and the
scheduler thread applies it.

## Resume

A job is complete only when a **content-hashed completion marker** exists whose
name is the SHA-256 of the entire `JobSpec`, *and* the recorded output still
exists at the recorded size. Change the preset, codec, bitrate or template and
the hash changes, so the job correctly re-runs.

Filename existence is not evidence of completion — a half-written file from a
killed run has the right name.

## Memory

Everything streams in 1 Mi-frame blocks. A 2-hour 7.1 float32 track would be
~11 GB if read whole; peak RSS stays flat instead. Intermediates are RF64 or
W64, never plain RIFF WAV, whose 4 GB header limit that same track would blow
past at 8.29 GB.

External encoders are fed headerless raw PCM, which has no size ceiling at all.
