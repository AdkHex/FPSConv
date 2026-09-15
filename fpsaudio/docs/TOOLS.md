# External tools

The runtime target is **Windows only**. The master prompt's macOS advice —
`afconvert` as the default AAC path — does not apply here and is not used.

Run `fpsaudio doctor` to see what this machine actually has. Run
`fpsaudio doctor --verbose` to see the flags each binary advertised, which is
what the adapters build commands against.

## The matrix

| Stage | Tool | Status | Notes |
| --- | --- | --- | --- |
| Probe | `mediainfo --Output=JSON` | **required** | the only prober that can see Atmos/JOC |
| Probe fallback | `ffprobe` | **required** | cannot see JOC; reports "unknown", never "absent" |
| Demux MKV | `mkvextract` | preferred | preserves `CodecDelay`; ffmpeg `-c copy` fallback, logged |
| Demux M2TS/TS | `tsMuxeR` / `eac3to` | preferred | ffmpeg `-c copy` fallback, logged |
| Decode TrueHD / Atmos | `truehdd` | **required for Atmos** | Rust, open source, DAMF/ADM output |
| Decode DD+ / AC-3 | ffmpeg `-drc_scale 0` | fallback only | no Dolby ref player → JOC objects unavailable |
| Decode FLAC / Opus | `flac`, `opusdec` | preferred | |
| Decode AAC / PCM | ffmpeg | fallback only | bit-exact; logged as ffmpeg use |
| **Resample** | **`soxr` (libsoxr VHQ)** | **required** | in-process, float32, linear phase |
| Time-stretch | `rubberband` R3 | **opt-in only** | never the default |
| **Encode AAC** | **`fdkaac`** | preferred | no Apple DLLs, by design |
| Encode FLAC | `flac -8` + `flac --test` | preferred | |
| Encode Opus | `opusenc` | preferred | |
| Encode WavPack | `wavpack` | preferred | |
| Encode DD / DD+ / DD+ Atmos | — | **refused** | no DEE |
| Encode TrueHD / TrueHD Atmos | — | **hand-off** | DME is GUI-only |
| Encode DTS / DTS-HD MA | — | **out of scope** | |
| Mux | `mkvmerge` | preferred | |
| Loudness | `pyloudnorm`, ffmpeg `ebur128` | preferred | ebur128 for >5 channels |
| Intermediates | RF64 / W64 via `soundfile` | **required** | no 4 GB ceiling |

## Installing

`Install.bat` runs `install.ps1`, which does all of this.

It needs **PowerShell 5.0 or newer** (for `Get-FileHash` and `Expand-Archive`)
and **Python 3.11 or newer** (for `tomllib`). `Install.bat` prefers `pwsh`, then
the full path to Windows PowerShell, so an old 2.0 engine on PATH cannot be
picked up by accident. The installer prints its own PowerShell version and the
folder it resolved before it does anything else.

Individually:

### winget packages

```powershell
winget install --id Gyan.FFmpeg -e                 # ffmpeg + ffprobe
winget install --id MediaArea.MediaInfo.CLI -e     # mediainfo
winget install --id MoritzBunkus.MKVToolNix -e     # mkvmerge + mkvextract
winget install --id Xiph.Flac -e                   # flac
winget install --id Xiph.Opus-tools -e             # opusenc + opusdec
```

### Python packages

All ship Windows wheels for Python 3.11+, so nothing compiles:

```powershell
.venv\Scripts\pip install numpy soxr soundfile pyloudnorm typer textual
```

### No winget package

These have to be fetched manually and put on PATH, or pointed at from config:

| Tool | Where | Needed for |
| --- | --- | --- |
| `fdkaac` | see below | AAC encoding |
| `wavpack` | <https://www.wavpack.com/downloads.html> | WavPack encoding |
| `rubberband` | <https://breakfastquay.com/rubberband/> | `--method stretch` only |
| `truehdd` | <https://github.com/truehdd/truehdd> | Dolby Atmos |
| `tsMuxeR` | <https://github.com/justdan96/tsMuxer> | preferred M2TS demux |
| `eac3to` | <https://forum.doom9.org/showthread.php?t=125966> | preferred Blu-ray extraction |

`install.ps1` has a `$PinnedDownloads` table for these. **The SHA-256 fields are
deliberately empty.** Publishing a checksum that was never verified against the
real artefact is worse than publishing none, and these binaries could not be
fetched from the build machine. The installer will not download anything it
cannot verify: it reports the tool as not installed and moves on.

To enable one, fill in both `Url` and `Sha256` after checking the hash yourself.

Or skip PATH entirely and point at the binary in config:

```toml
[tools]
fdkaac = "C:/tools/fdkaac/fdkaac.exe"
truehdd = "C:/tools/truehdd/truehdd.exe"
```

## Known interop problems

### ffmpeg W64 + libsndfile, three or more channels

Writing a float32 intermediate as `.w64` and reading it with libsndfile returns
`PCM_32` instead of `FLOAT` for ≥3 channels — ffmpeg emits a
`WAVE_FORMAT_EXTENSIBLE` header that libsndfile misparses, so every sample is
read as a reinterpreted bit pattern. Measured on ffmpeg 9.0.1 / libsndfile 1.2.x:

| channels | subtype read back | peak of a −21 dBFS sine |
| --- | --- | --- |
| 2 | FLOAT | 0.088 (correct) |
| 6 | PCM_32 | 0.5625 (garbage) |
| 8 | PCM_32 | 0.5625 (garbage) |

**RF64 is therefore preferred** and is what `intermediate_suffix()` returns
whenever the ffmpeg build advertises it. On top of that, `DecodeStage` asserts
the intermediate reads back as float and hard-fails if it does not, so a
recurrence in some other build cannot corrupt a job quietly.

### fdkaac and 7.1

Whether the standalone `fdkaac` CLI supports 7.1 AAC-LC depends on the build.
The adapter does not assume: it looks for 7.1 / 8-channel in `--help` output and
records `aac_7.1` only if found. Without it, a 7.1 AAC target is refused with
alternatives and an explicit `--accept downmix-to-5.1` escape hatch.

Check yours:

```
fpsaudio doctor --verbose
```

and look for a `!` note under `fdkaac`.

### truehdd is young

The adapter reads `--help`, requires the `decode` subcommand and a DAMF or ADM
output mode to actually appear there, and refuses with the tool's own surface
quoted if they do not. It does not hardcode a command line it cannot prove.

If `doctor --verbose` shows truehdd present but the Atmos path still refuses,
paste that section — the adapter can be matched to your build.

### Opus is always 48 kHz

Opus stores nothing else. fpsaudio resamples to 48 kHz itself with libsoxr VHQ
so the encoder never has to, and a bit-exact `redeclare` to a non-48 kHz rate
with `--codec opus` is refused rather than silently resampled by the encoder.

## Why these choices

**MediaInfo over ffprobe.** ffprobe has no JOC indicator for E-AC-3 at all. A
prober that cannot see object audio will report its absence, and that report
would authorise flattening an Atmos track. MediaInfo reports
`Format_Commercial_IfAny`, `Format_AdditionalFeatures` and
`NumberOfDynamicObjects`; without it, E-AC-3 and TrueHD jobs hit the
"Atmos presence unknown" refusal instead of risking a silent flatten.

**libsoxr in-process over any CLI resampler.** It takes an exact integer rate
pair, so the `Fraction` survives intact. Every CLI boundary in the legacy build
turned the rational into a decimal.

**Headerless PCM into the encoders.** A WAV header cannot describe more than
4 GB; a 2-hour 7.1 24-bit 48 kHz track is 8.29 GB. Raw PCM has no ceiling, and
every encoder here accepts it once told the geometry — with flags checked
against its own `--help`.
