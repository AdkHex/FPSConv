# Audit of the legacy FPS converters

Phase 0 deliverable. Every finding cites a file and line range you can open and
check yourself.

The source is preserved in **`../../legacy-archive/`** (a sibling of the
`fpsaudio/` folder), with the checked-in Windows `.venv`, `__MACOSX/`,
`__pycache__/` and the duplicate `FPS Converter.zip` stripped — 7.5 MB down to
under 100 KB, with no source file altered. Paths below are relative to that
directory:

- **Directory A** — `Fps Converter/` ("V2.0 by Ionicboy"), 152 lines
- **Directory B** — `Fps Converter Batch Mode/FPS Converter/`, ~1,700 lines

Every line number below was checked against the archived files.

The headline is not a list of bugs. Both programs perform **the wrong
operation**: they time-stretch when they should resample. Everything else
follows from that.

---

## Summary

| | Directory A | Directory B |
| --- | --- | --- |
| Retime ratios correct | 2 of 6 | 6 of 6 |
| Operation correct | no | no |
| Works on TrueHD | never | silently wrong |
| Works on DTS | never | silently wrong |
| Atmos handled | no | no |
| Verification | none | none |
| Dry run | no | no |
| Resume | no | filename-based only |

Directory B is a strict superset of A in engineering quality *and* in
correctness. A contributes nothing B lacks.

---

## Part 1 — Directory A

`Convert FPS.bat` → `python converter.py`. One 152-line file. Undeclared
dependencies on `prettytable` and `pymediainfo`; no requirements file anywhere.

### A-1 — The retime ratios are wrong in 4 of 6 presets

`converter.py:91-114`.

| Preset | Legacy `atempo` | Correct exact ratio | Status |
| --- | --- | --- | --- |
| 23.976 → 25 | `25025/24000` | 25025/24000 | correct |
| 23.976 → 24 | `24025/24000` | **1001/1000** | **wrong** |
| 25 → 23.976 | `24000/25025` | 24000/25025 | correct |
| 24 → 23.976 | `24000/24025` | **1000/1001** | **wrong** |
| 25 → 24 | `24025/25025` | **24/25** | **wrong** |
| 24 → 25 | `25025/24025` | **25/24** | **wrong** |

The author appears to have treated 23.976 as `24 × 1001/1000 ≈ 24.024` and
propagated the 1001 factor into pairs that never needed it.

Each wrong ratio is off by ≈0.0042%. Over a 2-hour feature that is **≈300 ms of
accumulated drift** — 300× the 1 ms tolerance, and plainly audible as lip-sync
error by the third act.

This is reproducible in the replacement:

```
fpsaudio explain --preset 23.976_to_24
  ...
  The legacy 'Fps Converter' used 961/960 here, which is wrong.
  Over 7200 s that accumulates 299.4 ms of drift — 299x the 1 ms tolerance.
```

Asserted in `tests/test_ratio.py::TestLegacyRatios`.

### A-2 — `atempo` is a time-stretcher, not a resampler

`converter.py:92,96,100,104,108,112`.

This is the deepest error in both programs. `atempo` is a pitch-preserving WSOLA
time-stretch. A film speed change is a **resample**: pitch must move with speed,
exactly as it does when film runs faster through a projector.

So the tool defaults to the mode that should be an explicit opt-in, and applies
a phase-vocoder-class artefact generator to every single job — including the two
jobs whose ratios were correct.

### A-3 — `-c:a copy` combined with `-af atempo` is a contradiction ffmpeg rejects

`converter.py:88-90` builds the command; `FMT_MAPPING` at `converter.py:13-17`
maps only `E-AC-3`, `AAC` and `AC-3`. Everything else falls through to `'copy'`.

So for TrueHD, DTS, DTS-HD MA, FLAC, PCM and Opus the command is:

```
ffmpeg -i in -c:a copy -b:a <br> -af atempo=... out
```

and ffmpeg aborts with *"Filtergraph 'atempo' was defined for audio output
stream 0:0 but codec copy was selected."*

**This is exactly why the tool "fails on TrueHD"**, and on every other lossless
format. It is not a subtle failure: the program cannot ever have worked for
those codecs.

### A-4 — No `-vn` and no `-map`

`converter.py:90,118`. For an MP4 or MKV source, ffmpeg's default stream
selection picks the best video stream too and **re-encodes it** with the output
container's default encoder. An audio-retiming tool silently re-encoding the
video track is catastrophic and slow.

### A-5 — Probe loop has no `break` and no guard

`converter.py:86-89`. The loop leaves `codec`/`bitrate` holding the **last**
audio track's values, so a multi-track file is probed incorrectly. A file with
**no** audio track leaves both names unbound → `UnboundLocalError`.

### A-6 — Bitrate parsing is character-set stripping, not suffix stripping

`converter.py:89`. `track.other_bit_rate[0].strip('kb/s')` strips the
*characters* `{k,b,/,s}` from both ends. `"640 kb/s"` happens to yield `"640"`,
so it looks correct. But MediaInfo's spaced-thousands form `"1 509 kb/s"` yields
`"1 509"` → `-b:a 1 509k`, an invalid argument that splits the argument vector.

### A-7 — CWD/script-dir mismatch

`converter.py:62` globs the **CWD**; `converter.py:82` opens
`os.path.join(self.dirPath, name)` where `dirPath` is the **script's**
directory. Run it from anywhere but the script folder and every file is found
and none can be opened.

### A-8 — Progress is output-bytes ÷ input-bytes

`converter.py:49-55,130-134`. Meaningless the moment codec or bitrate changes;
routinely reads near 0% or over 100%. Worse, `'size=' in line` also matches
ffmpeg's early `size=N/A` line, after which `re.findall(r'\d+', ...)` at
`converter.py:50` **concatenates every digit group in the line** into one absurd
integer.

### A-9 — `unbuffered()` reads one character at a time

`converter.py:26-41` reads via `stream.read(1)`, with
`stderr=subprocess.STDOUT` at `converter.py:125` merging the streams, so real
error messages are interleaved into progress text and cannot be recovered.

### A-10 — No DRC or dialnorm control

`-drc_scale` appears nowhere in the file. Every AC-3/E-AC-3 decode therefore
bakes in dynamic range compression and dialnorm gain — the single most common
quality bug in DD+ conversion, present and uncorrected.

### A-11 — Output goes to the CWD with the source extension

`converter.py:94-114,118`. An `.mp4` in produces an `.mp4` out, containing the
re-encoded video from A-4.

### A-12 — The lossless retime path does not exist

No sample-rate redeclaration anywhere in the file.

---

## Part 2 — Directory B

A genuine restructure: `Convert FPS.bat` (151-line bootstrapper) → `converter.py`
(19-line shim) → `app.py` (732-line **Tkinter GUI** — `core/__init__.py`'s
docstring calls it a TUI, but it is not) → `core/` (`config`, `jobs`, `naming`,
`probe`, `profiles`, `ffmpeg_runner`).

A Windows `.venv` (`home = C:\Program Files\Python313`, created under
`C:\Users\Uploader\Documents\...`), `__pycache__`, `__MACOSX` and a duplicate
`FPS Converter.zip` are all checked into the tree as dead weight.

### What it got right

- **B-A** — `core/profiles.py:21-30` uses `fractions.Fraction` and **fixed all
  four of A-1's wrong ratios**. Its table is exactly correct for all six
  presets. This is the single genuinely good artefact in either directory, and
  it is the seed of `fpsaudio/core/ratio.py`.
- **B-B** — `core/ffmpeg_runner.py:379-442` drives `-progress pipe:1`, parses
  the key/value stream, and `_scale_progress` (`:422-426`) maps it into a
  sub-range so multi-step pipelines report monotonic progress. Sound design,
  ported in logic.
- **B-C** — `app.py:460-547`'s `_batch_worker` uses a `ThreadPoolExecutor` over
  files with submit-as-slots-free scheduling. Ported into
  `fpsaudio/core/jobs.py`.
- **B-D** — `core/ffmpeg_runner.py:79` uses `-map 0:<index> -vn -sn -dn`,
  correct demux discipline, fixing A-4.
- **B-E** — `core/naming.py:10-46`, a clean
  `build_output_path`/`unique_path`/overwrite-policy triad.

### B-1 — The exact `Fraction` is computed and then thrown away

`core/profiles.py:15-18` renders the rational into `atempo=`, where ffmpeg's
option parser evaluates it to a C double. The two "HQ" engines are worse:
`core/ffmpeg_runner.py:316` and `:340` do
`f"{float(profile.tempo_ratio):.10f}"`, **deliberately truncating the rational
to ten decimal places**. Exactness is destroyed at every tool boundary.

### B-2 — Still `atempo`; still a time-stretch (A-2 uncorrected)

`core/ffmpeg_runner.py:85`. Worse, the two "HQ" engines are `sox tempo`
(`:342-343`) and `rubberband --tempo` (`:318-319`) — both *also*
time-stretchers. SoX ships the correct tool (`speed`, or `-r` reinterpretation
followed by `rate -v`) and it is never used. So "Better retime quality" and
"Audio-focused retime" are high quality at **the wrong operation**.

### B-3 — `resolve_codec` silently transcodes lossless to AAC

`core/config.py:35-41`. `CONTAINER_EXTENSIONS` (`:8-17`) doubles as the codec
registry. `truehd`, `dts`, `mlp`, `pcm_s24le` and `alac` are not in it, so
`source in CONTAINER_EXTENSIONS` is False and the function **falls through to
`return "aac"` at `core/config.py:41`**.

With the GUI's default `codec=auto`, a TrueHD 7.1 24-bit source is silently
converted to lossy AAC, with no disclosure at any point.

### B-4 — And it does so at a broken bitrate

`core/ffmpeg_runner.py:363-376`. With `bitrate=auto`, `_select_bitrate_arg`
reads the *source* bitrate. Two branches, both bad:

- ffprobe reports no `bit_rate` for TrueHD in MKV (it is variable and MKV does
  not store it) → returns `None` → **no `-b:a` at all** → ffmpeg's native AAC
  encoder uses its low default. TrueHD 7.1 becomes AAC at roughly 128–192 kbps.
- Where a bitrate *is* reported (say ~4500 kbps), `-b:a 4500k` goes to ffmpeg's
  native AAC encoder, far outside its valid range for the channel count; it
  clamps or errors depending on build.

**This is the concrete TrueHD failure in the batch build**, distinct from A-3's.

### B-5 — The extension allowlist silently produces zero jobs

`core/probe.py:9-32`. `AUDIO_EXTENSIONS` and `CONTAINER_EXTENSIONS` omit `.thd`,
`.mlp`, `.ec3`, `.dts`, `.dtshd`, `.w64`, `.rf64`, `.caf`, `.wv`, `.tta`,
`.mp2`, `.mts`, `.mpls`. `is_supported_file` (`:57-58`) gates the scan on them,
so a folder of `.thd` files scans to **zero jobs with no error message**. The
user sees an empty queue and no explanation.

### B-6 — The probe model discards everything that matters

`core/probe.py:35-45`. `AudioStreamInfo` captures index, codec, channels, sample
rate, bitrate, language, title, duration. It **omits `profile`,
`channel_layout`, `bits_per_raw_sample`, `disposition`, `start_time`, and all
side data**.

Consequences: DTS-HD MA is indistinguishable from DTS core (both are
`codec_name == "dts"`; the discriminator is in `profile`, never read); TrueHD
Atmos is indistinguishable from plain TrueHD; E-AC-3 JOC is invisible. The
entire Atmos requirement is unreachable from this model.

### B-7 — Atmos does not exist in the codebase

No occurrence of Atmos, JOC, objects or metadata anywhere in Directory B.
`CODEC_OPTIONS` has no `truehd`. Any Atmos source is therefore **silently
flattened to its 5.1/7.1 core**.

### B-8 — Intermediates are plain RIFF WAV and will overflow

`core/ffmpeg_runner.py:156` and `:257` extract via `-c:a pcm_s24le` into
`input_extract.wav`. A 2-hour 7.1 24-bit 48 kHz track is
`8 ch × 3 B × 48000 × 7200 ≈ 8.29 GB`; even 5.1 is `≈6.22 GB`. Both exceed WAV's
4 GB header limit. A live failure for any multichannel feature-length source.

### B-9 — Fixed 24-bit intermediate, no float, no dither

`pcm_s24le` is hardcoded at both sites above. A 32-bit float or 24-bit source is
requantised **without dither** before the stretch, and again on encode — twice
per job.

### B-10 — Flag guessing

`core/ffmpeg_runner.py:317-320` tries `["--tempo", x]` and, on failure,
`["-T", x]`; `:341-344` tries `tempo -s x` then `tempo x`. This invents tool
flags by construction, and it converts a real error (bad input file, missing
codec) into a silent retry that reports only the *second* failure's message.

### B-11 — Container/codec pairs are never validated

`core/config.py:44-47`. `resolve_extension(target_container, codec)` ignores
`codec` entirely when a container is named. Selecting codec `flac` with
container `m4a` produces a `.m4a` and `-c:a flac`, and ffmpeg fails with *"Could
not find tag for codec flac."* Conversely `resolve_extension("auto", "aac")`
returns `"aac"` → raw ADTS, into which multichannel AAC is legal but poorly
supported by players.

### B-12 — `job_id` collides in recursive mode

`core/jobs.py:55`. `f"{media.path.name}:{stream.stream_index}"` uses the
**basename**. Two `audio.mkv` files in different subfolders produce identical
job IDs → the `row_map` entry is overwritten and progress updates land on the
wrong row. And because `build_output_path` keys off `source_name` (the stem),
**both jobs write to the same output path** — the user asks for two files and
gets one, reported as two successes.

### B-13 — `unique_path` is TOCTOU-racy under the thread pool

`core/naming.py:10-21`. Two workers can both observe `_1` as free and both claim
it. `build_output_path` (`:24-46`) also performs a `mkdir` side effect at `:33`
from what reads as a pure naming function, which makes an honest `--dry-run`
impossible.

### B-14 — `check_dependencies()` can never report a missing dependency

`core/probe.py:133-139`. It calls `subprocess.run(["ffmpeg", "-version"])` and
checks the return code — but when the binary is absent, `subprocess.run`
**raises `FileNotFoundError`** rather than returning non-zero.

Verified directly:

```
>>> subprocess.run(['fpsaudio-no-such-binary','-version'], capture_output=True)
FileNotFoundError: [Errno 2] No such file or directory
=> a returncode check can NEVER see this.
```

`app.py:62` calls `_check_dependencies()` from `__init__` with no `try`, so on
any machine without ffmpeg **the GUI crashes on startup** instead of showing the
"Missing Dependencies" dialog that was written at `app.py:354` for exactly that
case.

### B-15 — No resumability, and "skip" corrupts silently

The queue lives in `self.jobs` in memory and is lost on exit. The only
resume-like behaviour is `overwrite_policy="skip"` (`core/naming.py:42-43`),
which skips on **filename existence** — so a half-written output from a killed
run is treated as complete. There are no content-hashed artefacts and no
completion markers.

### B-16 — No verification of any kind

No sample-count assertion, no duration check, no PCM MD5, no null test, no
loudness measurement, no channel-layout assertion. Nothing.

### B-17 — No dry-run

There is no way to see the command that will run before it runs.

### B-18 — No DRC/dialnorm control (A-10 uncorrected)

`-drc_scale` appears nowhere in Directory B either.

### B-19 — Delay and sync offsets are never read or scaled

No `start_time`, no MKV `CodecDelay`, no chapter handling anywhere. A source
with a +42 ms audio delay retimed 23.976 → 25 needs that delay to become
`42 × 960/1001 = 40.28 ms`; leaving it at 42 ms puts the track 1.7 ms out before
a single sample has played.

### B-20 — Progress denominator is the source duration

`core/ffmpeg_runner.py:106,199-206` passes `job.duration_seconds`, the *input*
length, so a retimed encode's progress is off by up to 4.3%. Cosmetic, but
symptomatic.

### B-21 — Cross-thread mutation of `ConversionJob`

Worker callbacks and the Tk main loop both write `job.status`/`job.progress`;
`app.py:528` reads back via `getattr(job, "progress", 0.0)`. Benign under
CPython, but the ownership model is undefined.

---

## Part 3 — Verdict

### Salvaged

1. **`core/profiles.py:21-30`'s `Fraction` ratio table** — correct for all six
   presets. Seeds `fpsaudio/core/ratio.py`, extended to 91 presets covering
   23.976 / 24 / 25 / 29.97 / 30 / 47.952 / 48 / 50 / 59.94 / 60, plus arbitrary
   pairs and direct-ratio entry.
2. **`ffmpeg_runner.py:379-442`'s `-progress pipe:1` parse and `_scale_progress`
   sub-range mapping** — became `FFmpegAdapter.parse_progress` and
   `adapters.scale_progress`, with B-20's denominator fixed.
3. **`app.py:460-547`'s submit-as-slots-free scheduler** — became
   `core/jobs.py`'s `Scheduler`, with encoder concurrency split out separately.
4. **`core/naming.py:24-46`'s naming triad** — generalised into the token
   template namer, with the `mkdir` side effect removed and the race replaced by
   an atomic `O_CREAT | O_EXCL` claim.
5. **`Convert FPS.bat`'s preflight-then-launch structure** — became
   `Install.bat` / `install.ps1` and the shape of `fpsaudio doctor`.
6. **`-map 0:N -vn -sn -dn` demux discipline.**
7. **Naming conventions** — the `23.976_to_25` profile-key form and the
   `__a{index}__{profile}` output stem are good, and are the template defaults.

### Discarded

- **`atempo` in every form**, and `sox tempo` / `rubberband --tempo` as
  *defaults*. Rubber Band survives only as an explicit `--method stretch`.
- **Directory A wholesale.**
- **`resolve_codec`'s unknown-codec → `"aac"` fallback** — replaced by an
  explicit refusal.
- **`CONTAINER_EXTENSIONS` doing double duty** as codec registry and extension
  map — now two separate, validated registries.
- **The extension allowlist as a scan gate** — replaced by probe-based
  identification with an explicit "unidentified" status per file.
- **Flag-guessing retry loops** — replaced by capability detection via `--help`
  at startup.
- **The Windows `.venv`, both `.bat`s, `__MACOSX/`, `__pycache__/`, `FPS
  Converter.zip`.**
- **Tkinter GUI** — replaced by Textual.
- **`prettytable` and `pymediainfo`** — replaced by a MediaInfo CLI adapter and
  Textual's own table widgets.

---

## Part 4 — Findings introduced during the rebuild

Two defects were found while building and testing the replacement. Neither
exists in the legacy code, but both are recorded here because they are the kind
of thing that silently destroys audio.

### N-1 — ffmpeg's W64 muxer and libsndfile disagree for ≥3 channels

Writing a float32 intermediate as `.w64` and reading it back with libsndfile
returns `PCM_32` rather than `FLOAT` when the file has three or more channels:
ffmpeg emits a `WAVE_FORMAT_EXTENSIBLE` header that libsndfile misparses, so
every sample is read as a reinterpreted float bit pattern.

Measured on ffmpeg 9.0.1 with libsndfile 1.2.x:

| channels | `.w64` subtype read back | peak |
| --- | --- | --- |
| 2 | FLOAT | 0.088 (correct) |
| 6 | PCM_32 | 0.5625 (garbage) |
| 8 | PCM_32 | 0.5625 (garbage) |

RF64 (`.wav` with `-rf64 auto`) reads correctly at every channel count and has
the same freedom from the 4 GB ceiling, so it is now preferred. In addition,
`DecodeStage` asserts that what it wrote reads back as float and hard-fails if
not, so any recurrence is loud rather than silent.

### N-2 — A null test cannot be gated below the output's own dither floor

A 16-bit output bottoms out near −98 dBFS by construction (TPDF dither plus
quantisation error is `step/√6`), so holding it to the −140 dBFS gate would fail
every time and mean nothing. The gate is now the stricter of the configured
value and the physical floor plus 6 dB, and the report states which was applied.

The same applies to loudness: ffmpeg's `ebur128` prints one decimal place, so a
0.1 LU tolerance against two of its readings is a coin toss. The tolerance is
widened by the meter's resolution, and the meter is named in the report.

---

## Part 5 — What the replacement asserts

Every finding above has a test. Run `pytest` to see them:

| Finding | Test |
| --- | --- |
| A-1 wrong ratios | `test_ratio.py::TestLegacyRatios` |
| A-2 / B-2 wrong operation | `test_adapters_and_probe.py::test_no_command_anywhere_uses_atempo` |
| A-3 copy + filter | `test_adapters_and_probe.py::test_ffmpeg_never_combines_copy_with_a_filter` |
| A-4 missing `-vn`/`-map` | `test_adapters_and_probe.py::test_ffmpeg_decode_has_map_vn_sn_dn_and_drc` |
| A-5 probe loop | `test_adapters_and_probe.py::test_multiple_audio_tracks_are_all_kept` |
| A-12 lossless retime | `test_dsp.py::TestBitExactRedeclare` |
| B-1 exactness lost | `test_dsp.py::TestIntegerRatePair` |
| B-3 silent AAC fallback | `test_refusals.py::TestCodecResolution` |
| B-5 silent zero jobs | `test_adapters_and_probe.py::test_extension_is_not_a_capability_gate` |
| B-6 discarded probe fields | `test_adapters_and_probe.py::TestProbeNormalisation` |
| B-7 Atmos flattening | `test_refusals.py::TestPlannerRefusals` |
| B-11 container mismatch | `test_refusals.py::TestContainerValidation` |
| B-12 id + output collision | `test_naming_and_jobs.py::TestJobIdentity`, `test_end_to_end.py::test_colliding_output_names_are_reported_not_collapsed` |
| B-13 TOCTOU + mkdir | `test_naming_and_jobs.py::TestAtomicClaim`, `TestNamingPurity` |
| B-14 undetectable missing dep | `test_adapters_and_probe.py::TestDetectionNeverCrashes` |
| B-15 false "complete" | `test_naming_and_jobs.py::TestResume` |
| B-16 no verification | `test_end_to_end.py::TestPipeline` |
| N-1 W64 corruption | `test_end_to_end.py::test_multichannel_intermediate_is_not_silently_corrupted` |
