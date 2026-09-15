# Audio FPS Retiming Toolchain — Phase 0 Audit + Build Plan

## Context

Two legacy Windows programs in this folder attempt to "convert audio between frame rates."
Both are broken in ways that go beyond bugs — the core operation they perform is the wrong
operation. They must be replaced by a single production-grade application with a library core,
a full CLI, and a keyboard-driven TUI.

I have read all 1,869 lines of legacy source and probed the environment. This document records
the Phase 0 audit findings, the scope decisions locked in by your answers, and the build plan.

Per §2 of the master prompt, the first executable step is to write `docs/AUDIT.md` and **stop
for review**. Everything after that is roadmap, not authorisation.

### Scope decisions locked in

| Decision | Value | Consequence |
| --- | --- | --- |
| Target platform | **Windows only**, `.bat` launcher | macOS `afconvert` is unavailable; §4.2's macOS advice is void |
| DTS / DTS-HD MA / DTS:X | **Excluded** | Detect and refuse with a clear message; never silently transcode |
| DEE (Dolby Encoding Engine) | **Not present** | DD / DD+ / DD+ Atmos *encode* hard-fails with a specific error |
| Dolby Media Encoder | **GUI only, no CLI** | TrueHD encode cannot be automated → ship a *hand-off* mode instead |
| AAC encoder | **fdkaac**, no Apple DLLs | Best non-Apple AAC; quality caveat stated plainly in UI and docs |
| Test media | **Synthetic only** | Sweeps, impulses, silence, full-scale squares. No copyrighted source |
| Dev / verify loop | Build here on macOS, ship `doctor` | You run `doctor` on the Windows box and paste output |

---

## Part 1 — Environment as detected

Detected on this Mac (the dev machine, **not** the runtime target):

- macOS 26.6.1 arm64, Homebrew 6.0.15
- Present: `mediainfo` 26.05, `afconvert`/`afinfo`, Python 3.14.6 (brew), Python 3.9.6 (system), `pipx`
- **Missing: `ffmpeg`, `ffprobe`**, `sox`, `flac`, `opusenc`/`opusdec`, `mkvmerge`/`mkvextract`,
  `rubberband`, `wavpack`, `truehdd`, `dee`/`deew`, `tsMuxeR`, `eac3to`, `qaac`, `fdkaac`, `exhale`,
  `loudness-scanner`, `MP4Box`
- No Python packages: `soundfile`, `soxr`, `textual`
- No Dolby or DTS applications in `/Applications`

Because ffmpeg is absent here, **both legacy programs fail at their first external call on this
machine** — which surfaced a real bug independent of environment (see B-14 below).

The Windows runtime environment is **unknown and undetectable from here**. The legacy `.bat` does
`where ffmpeg` / `where ffprobe`, implying ffmpeg is installed there, but nothing else can be
assumed. This is the reason `fpsaudio doctor` is a Phase 1 deliverable rather than a nicety.

---

## Part 2 — Audit: `Fps Converter/` (Directory A, "V2.0 by Ionicboy")

**Shape.** `Convert FPS.bat` → `python converter.py`. One 152-line file. Undeclared deps on
`prettytable` and `pymediainfo` (no requirements file anywhere).

**Control flow.** `main()` → `_fetch_files()` globs the CWD for
`*.eac3 *.m4a *.mp4 *.mka *.ac3 *.aac` → for each file, `_change(name)` prints a table and
**prompts interactively for the FPS pair, per file**. A "batch" of 50 files asks 50 questions.

**Pipeline as implemented.** MediaInfo probe → pick codec from a 3-entry map → build one
ffmpeg command → run it → parse `size=` from merged stdout/stderr for a progress bar.

### Findings

**A-1 — The retime ratios are wrong in 4 of 6 presets.**

| Preset | Legacy `atempo` | Correct exact ratio | Status |
| --- | --- | --- | --- |
| 23.976 → 25 | `25025/24000` | 25025/24000 | correct |
| 23.976 → 24 | `24025/24000` | **1001/1000** | **wrong** |
| 25 → 23.976 | `24000/25025` | 24000/25025 | correct |
| 24 → 23.976 | `24000/24025` | **1000/1001** | **wrong** |
| 25 → 24 | `24025/25025` | **24/25** | **wrong** |
| 24 → 25 | `25025/24025` | **25/24** | **wrong** |

The author appears to have treated 23.976 as `24 × 1001/1000 ≈ 24.024` and propagated the 1001
factor into pairs that never needed it. Each wrong ratio is off by ≈0.0042%, which over a 2-hour
feature is **≈0.3 s (300 ms) of accumulated drift** — 300× the 1 ms tolerance in §6, and plainly
visible as lip-sync error by the third act.

**A-2 — `atempo` is a time-stretcher, not a resampler.** This is the deepest error in both
programs. `atempo` is a pitch-preserving WSOLA time-stretch. A film speed change is a *resample*:
pitch must move with speed (§3.7). So the legacy tool defaults to the mode §3.7 says must be an
explicit opt-in, and applies a phase-vocoder-class artefact generator to every single job.

**A-3 — `-c:a copy` combined with `-af atempo` is a contradiction that ffmpeg rejects.**
`FMT_MAPPING` maps only `E-AC-3`, `AAC`, `AC-3`. Everything else falls through to `'copy'`.
So for TrueHD, DTS, DTS-HD MA, FLAC, PCM, Opus the command is
`ffmpeg -i in -c:a copy -b:a <br> -af atempo=... out`, and ffmpeg aborts with
*"Filtergraph 'atempo' was defined for audio output stream 0:0 but codec copy was selected."*
**This is exactly why the tool "fails on TrueHD" and on every other lossless format.** It is not
a subtle failure — the program cannot ever have worked for those codecs.

**A-4 — No `-vn` and no `-map`.** For an MP4 or MKV source, ffmpeg's default stream selection
picks the best video stream too and **re-encodes it** with the output container's default encoder.
An audio-retiming tool silently re-encoding the video track is catastrophic and slow.

**A-5 — Probe loop has no `break` and no guard.** The `for track in file_fmt.tracks` loop leaves
`codec`/`bitrate` holding the **last** audio track's values, so a multi-track file is probed
incorrectly; and a file with **no** audio track leaves both names unbound → `UnboundLocalError`.

**A-6 — Bitrate parsing is character-set stripping, not suffix stripping.**
`track.other_bit_rate[0].strip('kb/s')` strips the *characters* `{k,b,/,s}` from both ends.
`"640 kb/s"` happens to yield `"640"`. But MediaInfo's spaced-thousands form `"1 509 kb/s"`
yields `"1 509"` → `-b:a 1 509k`, an invalid argument that splits the arg vector.

**A-7 — CWD/script-dir mismatch.** `_fetch_files()` globs the **CWD**; `_change()` opens
`os.path.join(self.dirPath, name)` where `dirPath` is the **script's** directory. Run it from
anywhere other than the script folder and every file is found but none can be opened.

**A-8 — Progress is output-bytes ÷ input-bytes.** Meaningless the moment codec or bitrate changes;
routinely reads near 0% or over 100%. Also, `'size=' in line` matches ffmpeg's early `size=N/A`
line, after which `re.findall(r'\d+', ...)` **concatenates every digit group in the line** into one
absurd integer.

**A-9 — `unbuffered()` reads one character at a time** via `stream.read(1)`, with
`stderr=subprocess.STDOUT` merging the streams so real errors are interleaved into progress text.

**A-10 — No DRC or dialnorm control.** `-drc_scale 0` appears nowhere. Every AC-3/E-AC-3 decode
bakes in dynamic range compression and dialnorm gain — §3.3's named "single most common quality
bug in DD+ conversion", present and uncorrected.

**A-11 — Output goes to the CWD with the source extension** and a `23.976_to_25_` prefix, so an
`.mp4` in produces an `.mp4` out (with the re-encoded video from A-4).

**A-12 — The lossless retime path (§3.2) does not exist.** No sample-rate redeclaration anywhere.

---

## Part 3 — Audit: `Fps Converter Batch Mode/FPS Converter/` (Directory B)

**Shape.** A genuine restructure. `Convert FPS.bat` (151-line Windows bootstrapper: locates Python
via `py -3` then `python`, creates `.venv`, upgrades pip, installs requirements, `where ffmpeg` /
`where ffprobe` preflight, launches) → `converter.py` (19-line import shim) → `app.py` (732-line
**Tkinter GUI** — `core/__init__.py`'s docstring calls it a TUI, but it is not) → `core/`
(`config`, `jobs`, `naming`, `probe`, `profiles`, `ffmpeg_runner`).

A Windows `.venv` (`home = C:\Program Files\Python313`, created under
`C:\Users\Uploader\Documents\...`) plus `__pycache__`, `__MACOSX` and a duplicate
`FPS Converter.zip` are all checked into the tree as dead weight.

### What it got right

**B-A — `core/profiles.py` uses `fractions.Fraction`** and **fixed all four of A-1's wrong ratios**.
Its table matches §3.1 exactly for all six presets. This is the single genuinely good artefact in
either directory.

**B-B — `_run_ffmpeg_progress_command`** drives `-progress pipe:1`, parses the `key=value` stream,
reads `out_time_us`/`out_time_ms`, normalises to a percentage, and `_scale_progress` maps it into a
sub-range so multi-step pipelines report monotonic progress. Sound design.

**B-C — `_batch_worker`** uses a `ThreadPoolExecutor` over **files** with submit-as-slots-free
scheduling (`wait(..., return_when=FIRST_COMPLETED, timeout=0.2)`), default
`max(1, min(4, cpu_count()))`. This matches §5's concurrency requirement.

**B-D — `-map 0:<index> -vn -sn -dn`** — correct demux discipline, fixing A-4.

**B-E — `core/naming.py`** — clean `build_output_path` / `unique_path` / overwrite-policy triad.

### Findings

**B-1 — The exact `Fraction` is computed and then thrown away.** `atempo_value` renders
`"1001/1000"` into `-af atempo=`, where ffmpeg's option parser evaluates it to a C double. The
sox/rubberband engines are worse: `f"{float(profile.tempo_ratio):.10f}"` deliberately truncates the
rational to ten decimal places. Exactness is destroyed at every tool boundary.

**B-2 — Still `atempo`. Still a time-stretch (A-2 uncorrected).** Worse, the two "HQ" engines are
`sox tempo` and `rubberband --tempo` — both *also* time-stretchers. SoX ships the correct tool
(`speed`, or a `-r` reinterpretation followed by `rate -v`) and it is never used. So the "Better
retime quality" and "Audio-focused retime" options are high quality at **the wrong operation**.

**B-3 — `resolve_codec` silently transcodes lossless to AAC.** `CONTAINER_EXTENSIONS` in
`config.py` doubles as the codec registry. `truehd`, `dts`, `mlp`, `pcm_s24le`, `alac` are not in
it, so `source in CONTAINER_EXTENSIONS` is False and the function **falls through to
`return "aac"`**. With the GUI's default `codec=auto`, a TrueHD 7.1 24-bit source is silently
converted to lossy AAC with no disclosure — precisely what §3.3 forbids.

**B-4 — And it does so at a broken bitrate.** With `bitrate=auto`, `_select_bitrate_arg` reads the
*source* bitrate. Two branches, both bad:
- ffprobe reports no `bit_rate` for TrueHD in MKV (it is variable and MKV does not store it) →
  returns `None` → **no `-b:a` at all** → ffmpeg's native AAC encoder uses its low default.
  TrueHD 7.1 → AAC at roughly 128–192 kbps, silently.
- Where a bitrate *is* reported (e.g. ~4500 kbps), `-b:a 4500k` is handed to ffmpeg's native AAC
  encoder, far outside its valid range for the channel count; it clamps or errors by build.

**This is the concrete TrueHD failure in the batch build**, distinct from A-3's failure.

**B-5 — The extension allowlist silently produces zero jobs.** `AUDIO_EXTENSIONS` /
`CONTAINER_EXTENSIONS` in `probe.py` omit `.thd`, `.mlp`, `.ec3`, `.dts`, `.dtshd`, `.w64`,
`.rf64`, `.caf`, `.wv`, `.tta`, `.mp2`, `.mts`, `.mpls`. A folder of `.thd` files scans to **zero
jobs with no error message** — the user sees an empty queue and no explanation.

**B-6 — The probe model discards everything that matters.** `AudioStreamInfo` captures index,
codec, channels, sample rate, bitrate, language, title, duration. It **omits `profile`,
`channel_layout`, `bits_per_raw_sample`, `disposition`, `start_time`, and any side-data**.
Consequences: DTS-HD MA is indistinguishable from DTS core (both are `codec_name == "dts"`;
the discriminator is in `profile`, never read); TrueHD Atmos is indistinguishable from plain
TrueHD; E-AC-3 JOC is invisible. §3.5's entire Atmos requirement is unreachable from this model.

**B-7 — Atmos does not exist in the codebase.** No occurrence of Atmos, JOC, objects, or metadata
anywhere. `CODEC_OPTIONS` has no `truehd`. Any Atmos source is therefore **silently flattened to
its 5.1/7.1 core** — the exact failure §3.5 prohibits.

**B-8 — Intermediates are plain RIFF WAV and will overflow.** Both HQ engines extract via
`ffmpeg ... -c:a pcm_s24le input_extract.wav`. A 2-hour 7.1 24-bit 48 kHz track is
`8 ch × 3 B × 48000 × 7200 ≈ 8.29 GB`; even 5.1 is `≈6.22 GB`. Both exceed WAV's 4 GB header
limit. §4.3 names this exact trap, and it is a live failure for any multichannel feature-length
source. RF64/W64 is the fix.

**B-9 — Fixed 24-bit intermediate, no float, no dither.** `pcm_s24le` is hardcoded. A 32-bit float
or 24-bit source is requantised **without dither** before the stretch, and again on encode.
§3.4's "32-bit float end to end, quantise once, dither only at the final quantise" is violated
twice per job.

**B-10 — Flag guessing.** `_run_rubberband_cli` tries `["--tempo", x]` and, on failure,
`["-T", x]`; `_run_sox_tempo` tries `tempo -s x` then `tempo x`. This violates "no invented tool
flags" by construction, and it converts a real error (bad input file, missing codec) into a silent
retry that reports only the *second* failure's message.

**B-11 — Container/codec pairs are never validated.** `resolve_extension(target_container, codec)`
ignores `codec` entirely when a container is named. Selecting codec `flac` with container `m4a`
produces a `.m4a` and `-c:a flac`, and ffmpeg fails with *"Could not find tag for codec flac."*
Conversely `resolve_extension("auto", "aac")` returns `"aac"` → raw ADTS, into which multichannel
AAC is legal but poorly supported by players.

**B-12 — `job_id` collides in recursive mode.** `f"{media.path.name}:{stream.stream_index}"` uses
the **basename**. Two `audio.mkv` files in different subfolders produce identical job IDs → the
`row_map` entry is overwritten, progress updates land on the wrong row, and because
`build_output_path` keys off `source_name` (the stem), **both jobs write to the same output path**.

**B-13 — `unique_path` is TOCTOU-racy under the thread pool.** Two workers can both observe
`_1` as free and both claim it. `build_output_path` also performs a `mkdir` side effect from what
reads as a pure naming function.

**B-14 — `check_dependencies()` can never report a missing dependency.** It calls
`subprocess.run(["ffmpeg", "-version"])` and checks the return code — but when the binary is
absent, `subprocess.run` **raises `FileNotFoundError`** rather than returning non-zero. Verified
on this machine. `app.py._check_dependencies()` calls it from `__init__` with no `try`, so on any
machine without ffmpeg **the GUI crashes on startup** instead of showing the "Missing
Dependencies" dialog that was written for exactly that case.

**B-15 — No resumability, and "skip" corrupts silently.** The queue lives in `self.jobs` in memory
and is lost on exit. The only resume-like behaviour is `overwrite_policy="skip"`, which skips on
**filename existence** — so a half-written output from a killed run is treated as complete. §5's
content-hashed artefacts and completion markers are absent.

**B-16 — No verification of any kind.** No sample-count assertion, no duration check, no PCM MD5,
no null test, no loudness measurement, no channel-layout assertion. Nothing from §6 exists.

**B-17 — No dry-run.** There is no way to see the command that will run before it runs.

**B-18 — No DRC/dialnorm control (A-10 uncorrected).** `-drc_scale 0` appears nowhere.

**B-19 — Delay and sync offsets are never read or scaled.** §3.6 entirely absent — no
`start_time`, no MKV `CodecDelay`, no chapter handling.

**B-20 — Progress denominator is the source duration.** `job.duration_seconds` is the input length,
so a retimed encode's progress is off by up to 4.3%. Cosmetic, but symptomatic.

**B-21 — Cross-thread mutation of `ConversionJob`.** Worker callbacks and the Tk main loop both
write `job.status`/`job.progress`; `_batch_worker` reads back via
`getattr(job, "progress", 0.0)`. Benign under CPython, but the ownership model is undefined.

### Overlap and unique capability

| | Directory A | Directory B |
| --- | --- | --- |
| ffmpeg-driven, `atempo`-based | yes | yes |
| Six FPS presets, same labels | yes | yes (ratios corrected) |
| Windows `.bat` launcher | trivial | full bootstrapper |
| Interactive per-file CLI prompt | **only here** | — |
| `pymediainfo` probing | **only here** | — |
| `Fraction` ratio table | — | **only here** |
| ffprobe JSON probe model | — | **only here** |
| Output naming + overwrite policy | — | **only here** |
| Parallel executor over files | — | **only here** |
| `-progress pipe:1` parsing | — | **only here** |
| Engine abstraction (3 engines) | — | **only here** |
| Recursive scan | — | **only here** |

Directory B is a strict superset of A in engineering quality and a strict superset in correctness.
A contributes nothing that B lacks.

---

## Part 4 — Verdict

### Salvage

1. **`core/profiles.py`'s `Fraction` ratio table** — correct for all six presets. Becomes the seed
   of `core/ratio.py`, extended with 29.97/30/59.94 pairs, arbitrary src/dst, and direct-ratio entry.
2. **`_run_ffmpeg_progress_command` + `_scale_progress`** — the `-progress pipe:1` key/value parse
   and sub-range mapping become the `parse_progress` half of the adapter contract.
3. **`_batch_worker`'s submit-as-slots-free `ThreadPoolExecutor` over files** — becomes the
   `core/jobs.py` scheduler, with encoder concurrency split out separately per §5.
4. **`core/naming.py`'s `build_output_path` / `unique_path` / overwrite-policy triad** —
   generalised into §8's token-template namer, with the `mkdir` side effect and the TOCTOU race
   removed.
5. **`Convert FPS.bat`'s preflight-then-launch structure** — becomes `Install.bat` / `install.ps1`
   and the `doctor` command shape.
6. **`-map 0:N -vn -sn -dn` demux discipline.**
7. **Naming conventions** — the `23.976_to_25` profile-key form and the `__a{index}__{profile}`
   output-stem pattern are good and worth keeping as template defaults.

### Discard

- **`atempo` in every form** — wrong operation, and the same applies to `sox tempo` and
  `rubberband --tempo` as *defaults*. Rubber Band survives only as §3.7's explicit opt-in.
- **Directory A wholesale** — four wrong ratios, the `-c:a copy` + `-af` contradiction, no `-vn`,
  CWD output, character-set bitrate stripping, per-file prompting, byte-ratio progress.
- **`resolve_codec`'s unknown-codec → `"aac"` fallback** — replaced by an explicit refusal.
- **`CONTAINER_EXTENSIONS` doing double duty** as codec registry and extension map.
- **The extension allowlist as a scan gate** — replaced by probe-based identification with an
  explicit "unidentified" status per file.
- **Flag-guessing retry loops** — replaced by capability detection via `--help` at startup.
- **The Windows `.venv`, both `.bat`s, `__MACOSX/`, `__pycache__/`, `FPS Converter.zip`.**
- **Tkinter GUI** — replaced by Textual.
- **`prettytable` and `pymediainfo`** — replaced by a MediaInfo CLI adapter and Textual's own
  table widgets.

---

## Part 5 — Corrected toolchain for Windows

§4.2 of the master prompt assumes macOS and names `afconvert` as the default AAC path. **That is
void** — the runtime target is Windows only. Revised matrix:

| Stage | Windows tool | Status | Notes |
| --- | --- | --- | --- |
| Probe | `mediainfo --Output=JSON` | preferred | best Atmos/JOC detection; `ffprobe` as fallback |
| Demux MKV | `mkvextract` | preferred | MKVToolNix; ffmpeg `-c copy` fallback, logged |
| Demux M2TS/TS | `tsMuxeR` / `eac3to` | preferred | ffmpeg `-c copy` fallback, logged |
| Decode TrueHD / Atmos | `truehdd.exe` | **required for Atmos** | Rust, open source, DAMF/ADM output |
| Decode DD+ / AC-3 | ffmpeg `-drc_scale 0` | fallback only | no Dolby ref player → **JOC objects unavailable** |
| Decode FLAC / Opus | `flac`, `opusdec` | preferred | |
| Decode AAC / PCM | ffmpeg | fallback only | bit-exact; logged as ffmpeg use |
| **Resample** | **`soxr` Python bindings (libsoxr VHQ)** | preferred | in-process, 32-bit float, linear phase; Windows wheels exist |
| Time-stretch | `rubberband` R3 | **opt-in only** | never the default |
| **Encode AAC** | **`fdkaac.exe`** | preferred | no Apple DLLs per your decision |
| Encode FLAC | `flac -8` + `flac -t` | preferred | |
| Encode Opus | `opusenc` | preferred | |
| Encode WavPack | `wavpack` | preferred | |
| Encode DD / DD+ / DD+ Atmos | — | **refuse** | no DEE |
| Encode TrueHD / TrueHD Atmos | — | **hand-off** | DME is GUI-only; see below |
| Encode DTS / DTS-HD MA | — | **out of scope** | |
| Mux | `mkvmerge` | preferred | |
| Loudness | `pyloudnorm` | preferred | pure Python, one fewer binary |
| Intermediates | RF64 / W64 via `soundfile` | required | fixes B-8 |

### Three consequences you should see stated plainly

**DME GUI-only → a "hand-off" mode, not a refusal.** Because Dolby Media Encoder has no CLI, a
TrueHD or TrueHD Atmos *output* cannot be automated. Rather than simply refusing, the tool will
produce everything DME needs and stop cleanly: the retimed PCM essence as RF64/W64, the retimed
Atmos master (DAMF or ADM BWF) with all metadata timestamps rescaled by the same exact `Fraction`,
the verification report, and a written recipe listing the exact DME settings to select. You finish
the last step by hand in the GUI. This is honest, useful, and does not pretend to an automation
that does not exist.

**fdkaac's multichannel ceiling is unverified.** The standalone `fdkaac` CLI's support for 7.1
AAC-LC needs confirming against the actual binary on your Windows box. `doctor` will run
`fdkaac --help` and report. If 7.1 is unsupported, the tool **refuses** and offers an explicit,
typed-confirmation downmix to 5.1 — it will never downmix silently (§3.3).

**No JOC decoder means DD+ Atmos sources refuse.** ffmpeg decodes E-AC-3 JOC to the 5.1 core and
drops the objects. Per §3.5 the tool will refuse and offer (a) a TrueHD Atmos track from the same
file if one exists, or (b) typed confirmation to flatten. Never silent.

### `install.sh` → `install.ps1`

§9 lists `install.sh`. On a Windows-only target that is wrong; the deliverable becomes
`install.ps1` plus an `Install.bat` wrapper, using `winget` where packages exist (ffmpeg,
MKVToolNix) and pinned direct downloads with checksum verification for the rest (`fdkaac`, `flac`,
`opus-tools`, `rubberband`, `truehdd`, `tsMuxeR`). Python 3.11+ required; the installer creates
`.venv` and installs `soxr soundfile numpy textual typer pyloudnorm`, all of which ship Windows
wheels.

---

## Part 6 — Build plan

### Execution mode — full build in one pass

**You have overridden §2's stop-after-audit gate.** All phases are authorised now, built in one
pass, parallelised across subagents. `docs/AUDIT.md` is written as Phase 0's artefact but is no
longer a review checkpoint.

Parallelisation plan — four agents on disjoint file sets, so there are no write conflicts:

| Agent | Owns | Depends on |
| --- | --- | --- |
| **A — Foundation** | `core/ratio.py`, `core/probe.py`, `core/config.py`, `core/naming.py`, `core/jobspec.py`, `docs/AUDIT.md` | nothing (starts first, defines shared models) |
| **B — Adapters** | `core/adapters/*` (base contract, mediainfo, ffmpeg, flac, fdkaac, opus, wavpack, mkvtoolnix, truehdd, rubberband, soxr), `doctor` | A's models (contract agreed up front) |
| **C — Pipeline** | `core/plan.py`, `core/stages/*`, `core/verify.py`, `core/jobs.py` | A + B interfaces |
| **D — Surfaces** | `cli/*`, `tui/*`, `presets/*.toml`, `install.ps1`, `Install.bat`, `Run FPS Audio.bat`, `README.md`, `docs/QUALITY.md`, `docs/TOOLS.md`, `docs/ARCHITECTURE.md` | A's `JobSpec` shape |

To let all four start immediately, I write the shared contracts first — `JobSpec`, `MediaInfo`,
`Adapter`, `Stage`, `VerifyResult` — as a single frozen interface module every agent codes against.

| Phase | Deliverable | Owner |
| --- | --- | --- |
| 0 | `docs/AUDIT.md` — Parts 1–5 with per-finding file/line refs | A |
| 1 | `core/ratio.py`, `core/probe.py`, adapter framework, `doctor`, `core/verify.py`, synthetic tests | A + B |
| 2 | Lossless retime — sample-rate redeclaration for PCM/FLAC/WavPack | C |
| 3 | Decode → resample → encode: FLAC, AAC (fdkaac), Opus, WavPack; decode for AC-3/DD+/TrueHD | C |
| 4 | Textual TUI — five screens, "what will happen" panel first | D |
| 5 | Batch, queue persistence, resume, watch folders, manifests | C + D |
| 6 | Atmos — `truehdd` DAMF/ADM, metadata retiming, DME hand-off, JOC refusal | C |
| 7 | Docs, `install.ps1`, `Run FPS Audio.bat`, polish | D |

Acceptance gates are unchanged and all still apply: ratio math exact-rational throughout, PCM MD5
identical on the lossless path, null tests below −140 dBFS, loudness delta under 0.1 LU, batch
resume across a kill, and `--dry-run` explaining every operation.

### Architecture

Per §5, with the Windows-driven adjustments:

```
core/
  ratio.py          exact-rational FPS math, preset table, validation   [seeded from profiles.py]
  probe.py          mediainfo/ffprobe → normalized MediaInfo model      [rewritten, not reused]
  plan.py           JobSpec → ordered Stage list; the planner
  stages/           demux decode resample retime_lossless atmos encode mux verify
  adapters/         one module per binary; detect/version/capabilities/build_argv/parse_progress
  verify.py         PCM MD5, sample-count, loudness delta, duration drift, layout assertions
  jobs.py           queue, persistence, resume, concurrency             [scheduler from app.py]
  naming.py         token-template output paths                        [generalized from naming.py]
  config.py         TOML config + presets
tui/                Textual app — presentation only, zero business logic
cli/                typer entry point, full parity with the TUI
```

`JobSpec` is a serialisable TOML/JSON object built identically by the TUI and the CLI, so
`--dry-run`, `--manifest` and `--dump-manifest` all operate on the same artefact.

### Files to be created (representative)

- `core/ratio.py` — seeded from `Fps Converter Batch Mode/FPS Converter/core/profiles.py:21-30`,
  the one table that is already correct
- `core/adapters/base.py` — the `detect / version / capabilities / build_argv / parse_progress`
  contract; `parse_progress` for the ffmpeg adapter ports
  `core/ffmpeg_runner.py:379-442` verbatim in logic
- `core/jobs.py` — scheduler ported from `app.py:460-547`, with persistence and content-hashed
  completion markers added
- `core/naming.py` — from `core/naming.py:24-46`, with the `mkdir` side effect removed and the
  `unique_path` race fixed by an atomic `O_EXCL` claim
- `docs/AUDIT.md`, `docs/QUALITY.md`, `docs/TOOLS.md`, `docs/ARCHITECTURE.md`, `README.md`
- `install.ps1`, `Install.bat`, `Run FPS Audio.bat`
- `presets/*.toml`, `tests/`

### Files to be deleted

Both legacy directories, after `docs/AUDIT.md` is approved — including the checked-in Windows
`.venv`, `__MACOSX/`, `__pycache__/`, and `FPS Converter.zip`.

---

## Part 7 — Verification

**For Step 0** (the only authorised step): `docs/AUDIT.md` is reviewed by you. Every finding cites
a file and line range you can open and check yourself.

**For the phases that follow**, verification is built before the TUI per §6:

- Synthetic signal suite — sweeps, impulses, silence, full-scale squares — at every channel layout
  (mono, stereo, 5.1, 7.1) and bit depth (16, 24, 32f), so the maths is provable without any
  copyrighted source.
- Ratio math property tests: for every preset and a large set of random src/dst pairs, assert
  `out_samples == round(in_samples / speed)` exactly and that no float ever enters the computation.
- Bit-perfect assertion for the lossless retime path: PCM MD5 in == MD5 out, hard fail otherwise.
- Null test for lossless→lossless with resample: decode output, resample back, report residual RMS
  in dBFS; expect < −140.
- Loudness delta via `pyloudnorm`, expect < 0.1 LU.
- Adapter contract tests that assert `build_argv` output against flags confirmed by `--help` on the
  real binary — never against remembered flags.
- `doctor` output from your Windows box, pasted back, is the ground truth for what is installable.

---

## Part 8 — Open risks

1. **The Windows environment is unverified.** Everything past Step 0 depends on `doctor` output
   from the real box. If ffmpeg is absent there too, Phase 1 stalls on installation.
2. **`fdkaac` 7.1 support is unconfirmed** — see Part 5. May force a refusal-or-downmix decision
   for 7.1 AAC targets.
3. **`truehdd` is a young project.** Its DAMF/ADM output modes need verification against a real
   TrueHD Atmos file, which per your answer we do not have. Phase 6 will be built and unit-tested
   against synthetic metadata, but cannot be proven end to end until a real file exists.
4. **Every Dolby encode target refuses.** With no DEE, the tool's *output* format list is FLAC,
   AAC, Opus, WavPack and PCM. DD/DD+/TrueHD are decode-and-retime-only, plus the DME hand-off.
   This is a large reduction from the master prompt's ambition and should be confirmed as
   acceptable before Phase 3 is scoped.
5. **The master prompt's §11 definition of done cannot be fully met** as written: its DD+ Atmos
   and TrueHD output criteria require encoders we do not have. The lossless-retime, null-test,
   DRC/dialnorm, refusal-path, dry-run and batch-resume criteria all remain fully achievable.
