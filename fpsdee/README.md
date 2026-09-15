# FPS Converter (fpsdee)

Desktop app + command line with two tasks:

* **FPS change** — retime audio tracks between frame rates (23.976 ↔ 24 ↔ 25).
  The engine is the one from `fps.py` (the Telegram bot), lifted out unchanged
  and given a GUI, a batch queue, settings and a CLI.
* **Audio encode** — no speed change: re-encode any track to DD / DDP / DDP
  Atmos for a release (TrueHD 7.1 Atmos → DDP 7.1 Atmos, DTS-HD MA → DDP 5.1 …).
  See [Audio encode](#audio-encode).

| Source codec | Pipeline (exactly as in fps.py) |
| --- | --- |
| AAC (and anything that is not Dolby: WAV, FLAC, Opus …) | one `ffmpeg` pass: `-c:a aac -b:a <kbps> -af atempo=<ratio>` → `.aac` |
| AC-3 | `ffmpeg` → 24-bit 48 kHz WAV with `atempo` → `deew -f dd` (Dolby Encoding Engine) → `.ac3` |
| E-AC-3 | same → `deew -f ddp` → `.ec3` |
| TrueHD | same → `deew -f thd` → `.thd` |

Conversions: `23.976-24`, `23.976-25`, `24-23.976`, `24-25`, `25-23.976`, `25-24`
(ratios are the values from `fps.py`, e.g. 23.976→25 = ×1.042708).

## Requirements

| Component | Needed for | How to get it |
| --- | --- | --- |
| **Python 3.10+** (3.12 recommended) | everything | Windows: `winget install --id Python.Python.3.12 -e` (tick *Add to PATH*). macOS: `brew install python`. Linux: your package manager. |
| **ffmpeg + ffprobe** | probing, WAV extraction, AAC encoding, progress | Windows: `winget install --id Gyan.FFmpeg -e` (`Install.bat` does this). macOS: `brew install ffmpeg`. Linux: `apt install ffmpeg`. If it is not on PATH, set the path in *Settings ⚙*. |
| **deew** (pip) | AC-3 / E-AC-3 / TrueHD output; every DD / DDP encode | installed by `Install.bat` / `run.sh` from `requirements.txt` (`pip install deew`). Needs Python 3.10 – 3.13. |
| **Dolby Encoding Engine (DEE)** | AC-3 / E-AC-3 / TrueHD output; every DD / DDP encode | **Proprietary, licensed from Dolby — not included, not downloadable by this app.** Install it yourself, run `python -m deew` once to create deew's `config.toml`, then set `dee_path`, `ffmpeg_path` and `ffprobe_path` in it. *Settings ⚙* shows a green DEE dot when it is found. |
| **mediainfo** (CLI) | audio encode: detecting Dolby Atmos in a source | Windows: `winget install --id MediaArea.MediaInfo.CLI -e` (`Install.bat` does this). macOS: `brew install mediainfo`. Without it TrueHD sources show *Atmos: unknown* and are encoded as plain DDP. |
| **deezy** (pip) + **truehdd** | audio encode: DDP **Atmos** output | `pip install deezy` (optional step in `Install.bat` / `run.sh`; Python 3.10 – 3.13), `truehdd` from <https://github.com/truehdd/truehdd> on PATH, DEE **5.2.0 / 5.2.1**. Run `deezy config generate` once and set `dee`, `ffmpeg`, `truehdd` in its `deezy-conf.toml`. |
| A web browser | the GUI window | any modern browser; the app opens `http://127.0.0.1:8765/` (local only). |

Nothing else — the GUI, server, queue, settings and CLI are standard library only.

Without DEE the app still runs: AAC/WAV/FLAC sources convert; Dolby sources fail
with a clear `No module named deew` / DEE message and are marked *Failed* (retry
later with one click once DEE is set up).

## Setup

**Windows**
1. Double-click `Install.bat` — finds Python, creates `.venv`, installs `deew`,
   installs ffmpeg via winget if missing, prints `doctor`.
2. Double-click `Run FPS Converter.bat` — starts the app and opens the GUI in your browser.
   With arguments it is the CLI: `"Run FPS Converter.bat" convert D:\in -o D:\out -m 23.976-25 -j 2`

**macOS / Linux**
```
./run.sh                    # GUI
./run.sh doctor             # what is installed
./run.sh convert ~/in -o ~/out -m 25-23.976 -j 2 -r
```
Or directly from this folder: `python -m fpsdee [gui|doctor|convert …]`.

Run `doctor` first — it tells you exactly which of the four components above are missing.

## Using the GUI

| Area | What it does |
| --- | --- |
| **Task** | *FPS change* (the six fps modes) or *Audio encode* (DD / DDP / DDP Atmos, no speed change) |
| **Conversion** | fps task: the six fps modes; the exact speed ratio and `atempo` filter are shown underneath |
| **Format / Channels / Bitrate / Keep Atmos / DRC** | encode task: what to produce; each queued row shows what it will *really* become (`DDP 7.1 Atmos 1536k`, `DDP 2.0 256k (no upmix)`) |
| **Output folder** | where results go — remembered between launches |
| **Mode** | *Single file* (one file at a time) or *Batch* (many files, N in parallel) |
| **Advanced** | output bitrate (same as source, or a custom kbps) · what to do when the output already exists (overwrite / skip / numbered copy) |
| **+ Add** | *Add Files…* (multi-select with checkboxes), *Add Folder…* (optionally with sub-folders), *Paste a path…* |
| **Queue** | added files sit as *Ready* until you press **Start**; each row can have its own conversion mode and, for files with several audio tracks, its own stream |
| **Progress** | per-job bar, step name, elapsed, ETA, plus a thin overall bar under the toolbar |
| **Row actions** | ✕ Cancel (kills ffmpeg/deew, deletes the partial output) · ↻ Retry · 📁 Open output folder · ✕ Remove |
| **Log** | click a row (or the log icon) to see the exact commands and tool output; *Copy* puts it on the clipboard |
| **⚙ Settings** | tool status (ffmpeg, ffprobe, deew, DEE), explicit paths for ffmpeg/ffprobe and the Python that has deew, *Open output folder*, *Quit app* |
| Keyboard | `Delete` removes ticked rows, `Esc` closes dialogs |

Finished jobs stay in the list across restarts (history), so you can see what
was converted yesterday, open its folder, or retry it. Settings live in
`%APPDATA%\fpsdee` (Windows), `~/Library/Application Support/fpsdee` (macOS)
or `~/.config/fpsdee` (Linux).

## Audio encode

The second task. No `atempo`, no speed change: the chosen track is decoded
(losslessly for TrueHD, DTS-HD MA, FLAC, PCM; as-is for AAC / DD+) to 24-bit
48 kHz WAV and handed to DEE through deew, or — for Atmos — to truehdd + DEE
through DeeZy.

| Source | What you can make | Pipeline |
| --- | --- | --- |
| TrueHD 7.1 **Atmos** (Blu-ray) | **DDP 7.1 Atmos** (1024 – 1664 kbps, default 1536) · **DDP 5.1 Atmos** (384 – 1024, default 768) · plain DDP 7.1 / 5.1 / 2.0 · DD 5.1 / 2.0 | Atmos: `deezy encode atmos` (truehdd → DEE JOC). Plain: ffmpeg WAV → `deew` |
| TrueHD 7.1 / DTS-HD MA 7.1 / FLAC 7.1 | DDP 7.1 (384 – 1664; **1536 kbps is the "1503 kbps" you see in MediaInfo**) · DDP 5.1 · DDP 2.0 · DD 5.1 / 2.0 | ffmpeg WAV → `deew -f ddp` (`-dm 6` / `-dm 2` for the Dolby downmix) |
| TrueHD 5.1 / DTS-HD MA 5.1 / AC-3 5.1 / DD+ 5.1 | DDP 5.1 (192 – **1024** max) · DDP 2.0 · DD 5.1 (max 640) · DD 2.0 | same |
| AAC 2.0 / any stereo | DDP 2.0 (96 – 1024, default 256) · DD 2.0 (96 – 640) | same |

Things it will tell you rather than do quietly:

* **Lossy → lossy cannot improve quality.** AAC 2.0 → DDP 2.0 is a re-encode;
  a high DDP bitrate (256 kbps) only keeps the extra loss negligible.
* **DDP 5.1 stops at 1024 kbps.** 1280 / 1536 / 1664 exist only for 7.1
  (DEE's Blu-ray profile, used automatically above 1024). "TrueHD 5.1 → DDP 5.1
  at 1503 kbps" does not exist in DEE; you get 1024.
* **No upmixing.** Asking for 7.1 from a 5.1 or 2.0 source gives 5.1 or 2.0,
  and the row says `(no upmix)`. 5.0 / 6.1 / 7.0 sources are padded with silent
  channels to 5.1 / 7.1.
* **Atmos needs a TrueHD Atmos source.** DTS-HD MA, DTS:X and DD+ Atmos sources
  become plain DDP (bed only). Atmos output also needs *Keep Atmos* ticked, a
  5.1 or 7.1 target, mediainfo (to see the Atmos flag), truehdd, deezy and DEE 5.2.x.
* AC-3 / DD+ sources are decoded with `-drc_scale 0` (no decoder DRC baked in);
  96 kHz sources are resampled to 48 kHz with soxr when the ffmpeg build has it.
* DD has no 7.1: a 7.1 source becomes DD 5.1.

Output: `<name>[_a<N>]_<DDP|DD><layout>[Atmos]_<kbps>k.<ec3|ac3>`, e.g.
`movie_DDP7.1Atmos_1536k.ec3`, `movie_a1_DD5.1_640k.ac3`. Raw elementary
streams — mux them with mkvmerge.

```
python -m fpsdee encode D:\in -o D:\out -f ddp -c 8 -b 1536          # DDP 7.1 (Atmos kept if the source has it)
python -m fpsdee encode movie.mkv -o D:\out -f ddp -c 6 --no-atmos    # DDP 5.1 1024k, bed only
python -m fpsdee encode song.m4a -o D:\out -f ddp -b 256              # DDP 2.0 256k
python -m fpsdee encode D:\in -o D:\out -f dd -r -j 2                 # DD 5.1 / 2.0, whole folder
```

## Command line

```
python -m fpsdee convert <files or folders…> -m 23.976-25 [-o OUT] [-j N] [-r]
                                             [-s STREAM] [-b KBPS] [--overwrite overwrite|skip|rename]
python -m fpsdee encode  <files or folders…> [-f ddp|dd] [-c 0|1|2|6|8] [-b KBPS] [--no-atmos]
                                             [--drc film_light|film_standard|music_light|music_standard|speech]
                                             [-o OUT] [-j N] [-r] [-s STREAM] [--overwrite …]
python -m fpsdee doctor
python -m fpsdee gui [--port 8765] [--no-browser] [-j N]
```
Exit code 0 when every job is done or skipped, 1 otherwise, 130 on Ctrl-C.

## Output naming

FPS task: `<name>_<mode with . replaced by _><ext>` — `movie.mka` + `23.976-25` → `movie_23_976-25.ac3`.
A non-first audio stream adds `_a<N>`: `movie_a1_23_976-25.ac3`.
With the *numbered copy* policy an existing file yields `movie_23_976-25_1.ac3`, `_2`, …
Encode task: see [Audio encode](#audio-encode).

## Tests

```
python -m unittest -v tests.test_engine
```
31 tests covering the atempo chain, output naming, error extraction, settings
persistence, the rename policy, and the encode task (DEE bitrate tables,
downmix / no-upmix resolution, Atmos gating, deew / deezy / ffmpeg command
lines, MediaInfo Atmos parsing) — none of them need ffmpeg or deew.

## Known limits inherited from the fps.py engine

* `atempo` is a pitch-preserving **time-stretch**, not a resample: pitch does not move with the speed change the way a film speed-up does. That is what `fps.py` does and what this app does.
* Ratios are floats (`25/(24000/1001)`), not exact rationals; over a 2 h track the rounding is well under a millisecond.
* AAC output is raw ADTS, which cannot carry more than 7 channels — a 7.1 AAC/WAV source fails with `channelConfiguration > 7 is not supported in ADTS`.
* Dolby sources are always resampled to 48 kHz and re-encoded through DEE (lossy → lossy for DD/DD+).
* Metadata and chapters are stripped (`-map_metadata -1 -map_chapters -1`), as in `fps.py`.
