# fpsaudio

Retime audio between frame rates — exactly, verifiably, and without ever losing
information silently.

Replaces two legacy converters that both performed the wrong operation. See
[docs/AUDIT.md](docs/AUDIT.md) for what was wrong and where.

---

## The short version

Playing 23.976 fps content at 25 fps makes it run 4.27% fast. The audio has to
run 4.27% fast too — and its pitch rises with it, exactly as it does when film
runs faster through a projector.

That is a **resample**. It is not a time-stretch. Both legacy tools used
`atempo` / `sox tempo` / `rubberband --tempo`, all of which preserve pitch, on
every job by default.

```
fpsaudio convert D:\in -o D:\out --preset 23.976_to_25
```

## Install

Double-click **`Install.bat`**. It installs the Python packages and external
tools, then runs `fpsaudio doctor` to show you exactly what is present and what
is missing.

Requirements:

- **Python 3.11 or newer** (for `tomllib`)
- **PowerShell 5.0 or newer** — Windows 10/11 ship 5.1 as "Windows PowerShell".
  `Install.bat` picks the right one for you and the installer checks its own
  version before doing anything.

The installer prints the PowerShell version and the folder it resolved on its
first two lines, so if anything goes wrong those tell you where you are. If it
cannot find a suitable Python it lists every candidate it tried and what each
one said — including the Microsoft Store alias stub, which is the usual reason a
Windows box looks like it has Python and does not.

Python installed somewhere unusual? Point at it directly:

```
powershell -ExecutionPolicy Bypass -File install.ps1 -PythonExe "D:\Python312\python.exe"
```

## Use

Double-click **`Run FPS Audio.bat`** for the keyboard-driven TUI, or pass
arguments to use it as a command line:

```
"Run FPS Audio.bat" doctor
"Run FPS Audio.bat" convert D:\in -o D:\out --preset 23.976_to_25
```

### Start here

```
fpsaudio doctor                     what this machine has, and what it lacks
fpsaudio inspect D:\movie.mkv       every stream, with Atmos findings
fpsaudio explain --preset 24_to_25  the exact maths, and whether it can be bit-exact
fpsaudio presets                    the six classic presets (--all for 90 more)
fpsaudio formats                    what can be written, and why some things cannot
```

### Convert

```
# See exactly what would happen. Writes nothing.
fpsaudio convert D:\in -o D:\out --preset 23.976_to_25 --dry-run

# Do it.
fpsaudio convert D:\in -o D:\out --preset 23.976_to_25

# Bit-exact, where the arithmetic allows it: not one sample changes.
fpsaudio convert D:\in -o D:\out --preset 24_to_25 --method redeclare --codec flac

# Any pair of rates, not just the presets.
fpsaudio convert D:\in -o D:\out --from 29.97 --to 25

# Full scrutiny on a master you intend to keep.
fpsaudio convert D:\in -o D:\out --preset 23.976_to_25 --null-test --loudness
```

### Batch

```
fpsaudio convert D:\library -o D:\out -r --jobs 4 --preset 23.976_to_25
```

Interrupt it and re-run the same command: it resumes. Completion is tracked by
a content hash of the whole job spec, not by filename, so a half-written file
from a killed run is correctly redone — and changing the preset or codec
correctly re-runs everything.

```
fpsaudio watch D:\dropbox -o D:\out --preset 23.976_to_25
```

Picks a file up only once its size has stopped changing, so a
partially-copied file is never processed.

## What you get

Every job writes `<output>.verification.json` and prints its checks:

```
done      movie.mkv #1 -> movie__a1__23_976_to_25.flac
          PASS sample_count: 184136 frames, exactly as the rational demands
          PASS duration_drift: +0.0028 ms against an exact target of 3.836164 s
          PASS channel_layout: 6 channels, matching the source
          PASS null_test: residual -142.1 dBFS RMS in the interior, gate -140.0 dBFS
          PASS loudness: -0.70 -> -0.80 LUFS, delta -0.100 LU
```

A check that could not run says so. **It is never counted as a pass.**

## Refusals

fpsaudio stops rather than quietly losing something. Every refusal names what
would have been lost, what the alternatives are, and the exact token to type if
you want it anyway:

```
REFUSED [lossless_to_lossy]: The source is lossless (Dolby TrueHD) and the
target is lossy (AAC-LC). That is a one-way loss of quality.
  - --codec flac or --codec wavpack keeps it lossless.
  - Or confirm you want the lossy encode.
  To override anyway, re-run with --accept lossless-to-lossy
```

The things that refuse, and why:

| Situation | Why |
| --- | --- |
| DTS in any form | out of scope by decision — never silently transcoded |
| DD / DD+ output | needs Dolby Encoding Engine, which is not available |
| TrueHD output | Dolby Media Encoder is GUI-only → hand-off bundle instead |
| DD+ Atmos source | no JOC decoder exists; ffmpeg would discard the objects |
| TrueHD Atmos source | pick `--atmos-policy handoff` to keep the objects |
| Atmos presence unknown | MediaInfo missing, so it cannot be ruled out |
| Lossless → lossy | one-way quality loss |
| 7.1 AAC on an fdkaac that does not confirm 7.1 | would fail or silently downmix |
| flac in an .m4a | ffmpeg would fail later; caught first |
| Two sources → one output name | you would get fewer files than jobs |

## Dolby Atmos

TrueHD Atmos input works: `truehdd` decodes it with objects intact, the essence
is retimed, and **every object timestamp is rescaled by the same exact
`Fraction`** as the audio, so bed and objects stay locked.

TrueHD Atmos *output* cannot be automated — Dolby Media Encoder ships no CLI. So
`--atmos-policy handoff` produces everything DME needs and stops cleanly:

- the retimed essence as RF64/W64
- the retimed Atmos master (DAMF or ADM BWF)
- `verification.json`
- `DME_RECIPE.md` — the exact settings to select, with the real numbers from
  the retime that was actually performed

You finish the last step by hand. That is honest about what is possible.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | everything succeeded |
| 1 | a job failed |
| 2 | a verification check failed |
| 3 | an operation was refused |

## Documentation

- [docs/AUDIT.md](docs/AUDIT.md) — what the legacy programs got wrong, with line references
- [docs/QUALITY.md](docs/QUALITY.md) — the acceptance gates, and their caveats stated plainly
- [docs/TOOLS.md](docs/TOOLS.md) — every external tool, and known interop problems
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — how it fits together

## Presets

Ready-made configurations in `presets/`:

| File | For |
| --- | --- |
| `film-to-pal.toml` | 23.976 → 25, the common case |
| `pal-to-film.toml` | 25 → 23.976 |
| `bit-exact.toml` | sample-rate redeclaration, not one sample altered |
| `atmos-handoff.toml` | TrueHD Atmos → Dolby Media Encoder |
| `archival.toml` | every check on, for a master you keep |

```
fpsaudio convert D:\in -o D:\out --config presets\film-to-pal.toml
```

## Tests

```
.venv\Scripts\python -m pytest
```

141 tests, all against synthetic signals — sweeps, impulses, silence and
full-scale squares at mono/stereo/5.1/7.1 and 16/24/32-bit — so the maths is
provable without any copyrighted source. Tests needing an absent tool skip and
say which.
