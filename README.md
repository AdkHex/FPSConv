# FPSConv

Windows desktop app (plus a command line) with two tasks:

* **FPS change** — the frame-rate speed of audio tracks, 23.976 ↔ 24 ↔ 25, with
  the `fps.py` engine: ffmpeg `atempo` for AAC, deew + Dolby Encoding Engine for
  AC-3 / E-AC-3 / TrueHD.
* **Audio encode** — no speed change: any track → DD / DDP / DDP Atmos for a
  release (TrueHD 7.1 Atmos → DDP 7.1 Atmos, DTS-HD MA → DDP 5.1 …). See
  [Audio encode](#audio-encode).

Every push to `main` builds a new Windows installer on GitHub Actions and
publishes it as a release; installed copies update themselves.

## Install (Windows)

1. Download **`FPSConv-Setup-<version>.exe`** from the
   [latest release](https://github.com/AdkHex/FPSConv/releases/latest) and run it
   (per-user install, no admin needed).
2. Open **FPSConv**. If ffmpeg is not found, click **⚙ → Download ffmpeg for Windows**.
3. For AC-3 / E-AC-3 / TrueHD output: install Dolby Encoding Engine yourself
   (proprietary — not included), then **⚙ → dee.exe path → Save**. FPSConv writes
   deew's `config.toml` for you; deew itself is bundled in the app.

That's the whole setup. Updates arrive on their own (see below).

| Component | Needed for | Comes with the installer? |
| --- | --- | --- |
| Python, pywebview, deew, deezy, pymediainfo | everything | **yes** — bundled in the exe |
| ffmpeg + ffprobe | probing, WAV extraction, AAC encoding | no — one click in ⚙ downloads a static build, or set a path |
| Dolby Encoding Engine (`dee.exe`) | AC-3 / E-AC-3 / TrueHD; every DD / DDP encode | no — licensed from Dolby; point ⚙ at it (5.2.0 / 5.2.1 for Atmos) |
| truehdd | DDP **Atmos** output (audio encode) | no — <https://github.com/truehdd/truehdd>; put it on PATH or set the path in ⚙ |
| Edge WebView2 runtime | the desktop window | ships with Windows 10/11; otherwise the app opens in your browser |

## What it does

| Source codec | Pipeline (exactly as in fps.py) |
| --- | --- |
| AAC (and anything not Dolby: WAV, FLAC, Opus…) | one `ffmpeg` pass: `-c:a aac -b:a <kbps> -af atempo=<ratio>` → `.aac` |
| AC-3 / E-AC-3 / TrueHD | `ffmpeg` → 24-bit 48 kHz WAV with `atempo` → `deew -f dd|ddp|thd` → `.ac3` / `.ec3` / `.thd` |

Conversions `23.976-24`, `23.976-25`, `24-23.976`, `24-25`, `25-23.976`, `25-24`.

**GUI:** single-file or batch queue (N parallel), per-row conversion mode and
audio-stream picker, live progress / ETA, cancel (kills the process, removes the
partial file), retry, open output folder, job log with the exact commands,
overwrite / skip / numbered-copy policy, custom bitrate, history across
restarts, desktop notification when a batch finishes.

**CLI** (`fpsconv-cli.exe` in the install folder, or `python -m fpsconv` from source):

```
fpsconv-cli convert <files or folders…> -m 23.976-25 [-o OUT] [-j N] [-r] [-s STREAM] [-b KBPS] [--overwrite overwrite|skip|rename]
fpsconv-cli encode  <files or folders…> [-f ddp|dd] [-c 0|1|2|6|8] [-b KBPS] [--no-atmos] [--drc film_light|…] [-o OUT] [-j N] [-r] [-s STREAM]
fpsconv-cli doctor            # what is installed / configured
fpsconv-cli dee C:\Dolby\DEE\dee.exe   # write deew's config for this DEE
```

## Audio encode

The second task (**Task → Audio encode** in the sidebar). No `atempo`, no speed
change: the chosen track is decoded — losslessly for TrueHD, DTS-HD MA, FLAC,
PCM; as-is for AAC / DD+ — to 24-bit 48 kHz WAV and handed to DEE through
deew, or, for Atmos, to truehdd + DEE through DeeZy. Both are bundled.

| Source | What you can make | Pipeline |
| --- | --- | --- |
| TrueHD 7.1 **Atmos** (Blu-ray) | **DDP 7.1 Atmos** (1152 – 1664 kbps, default 1536) · **DDP 5.1 Atmos** (384 – 1024, default 768) · plain DDP 7.1 / 5.1 / 2.0 · DD 5.1 / 2.0 | Atmos: `deezy encode atmos` (truehdd → DEE JOC). Plain: ffmpeg WAV → `deew` |
| TrueHD 7.1 / DTS-HD MA 7.1 / FLAC 7.1 | DDP 7.1 (384 – 1664; **1536 kbps is the "1503 kbps" MediaInfo shows**) · DDP 5.1 · DDP 2.0 · DD 5.1 / 2.0 | ffmpeg WAV → `deew -f ddp` (`-dm 6` / `-dm 2` = Dolby downmix in DEE) |
| TrueHD 5.1 / DTS-HD MA 5.1 / AC-3 5.1 / DD+ 5.1 | DDP 5.1 (192 – **1024** max) · DDP 2.0 · DD 5.1 (max 640) · DD 2.0 | same |
| AAC 2.0 / any stereo | DDP 2.0 (96 – 1024, default 256) · DD 2.0 (96 – 640) | same |

Every queued row shows what it will *really* become — `DDP 7.1 Atmos 1536k`,
`DDP 2.0 256k · no upmix: 7.1 requested, source is 2.0` — before you press Start.
Things it tells you rather than does quietly:

* **Lossy → lossy cannot improve quality.** AAC 2.0 → DDP 2.0 is a re-encode; a
  high DDP bitrate (256 kbps) only keeps the extra loss negligible.
* **DDP 5.1 stops at 1024 kbps.** 1280 / 1536 / 1664 exist only for 7.1 (DEE's
  Blu-ray profile, used automatically above 1024). "TrueHD 5.1 → DDP 5.1 at
  1503 kbps" does not exist in DEE; you get 1024.
* **No upmixing.** 7.1 from a 5.1 or 2.0 source gives 5.1 or 2.0. 5.0 / 6.1 / 7.0
  sources are padded with silent channels to 5.1 / 7.1. A bitrate that belongs to
  a bigger layout (1536 on a 2.0 file) falls back to the layout's default.
* **Atmos needs a TrueHD Atmos source** (MediaInfo confirms it; without it TrueHD
  shows *Atmos: unknown* and is encoded as bed only). DTS-HD MA, DTS:X and DD+
  Atmos sources become plain DDP. Atmos output also needs *Keep Atmos* ticked, a
  5.1 or 7.1 target, truehdd and DEE 5.2.x.
* AC-3 / DD+ sources are decoded with `-drc_scale 0` (no decoder DRC baked in);
  96 kHz sources are resampled to 48 kHz with soxr when the ffmpeg build has it.
* DD has no 7.1: a 7.1 source becomes DD 5.1.

Output: `<name>[_a<N>]_<DDP|DD><layout>[Atmos]_<kbps>k.<ec3|ac3>`, e.g.
`movie_DDP7.1Atmos_1536k.ec3`, `movie_a1_DD5.1_640k.ac3` — raw elementary
streams, mux them with mkvmerge.

```
fpsconv-cli encode D:\in -o D:\out -f ddp -c 8 -b 1536        # DDP 7.1 (Atmos kept if the source has it)
fpsconv-cli encode movie.mkv -o D:\out -f ddp -c 6 --no-atmos  # DDP 5.1 1024k, bed only
fpsconv-cli encode song.m4a -o D:\out -f ddp -b 256            # DDP 2.0 256k
fpsconv-cli encode D:\in -o D:\out -f dd -r -j 2               # DD 5.1 / 2.0, whole folder
```

## Which fps is my audio?

Audio has no frame rate of its own — it is "23.976 fps audio" only because it
was cut to a 23.976 fps video. So FPSConv shows, next to every queued file,
the frame rate it is tied to and where that came from:

| Chip | Meaning |
|---|---|
| `25 fps` | the file has a video track at 25 fps (mkv / mp4 / ts …) |
| `23.976 fps ~` | no video track; taken from an fps tag in the container or from the file name (`…23.976fps…`) |
| `fps ?` | audio-only file with nothing to go on |

Pick a **Target video** in the sidebar and FPSConv reads its frame rate,
compares durations, and sets the conversion for every file (`24-25` when a
file is 25/24 longer than the video, and so on). Rows say *suggested* or
*no change needed*; you can still override the dropdown per row.

Same from the CLI:

```
fpsconv-cli probe Movie.Audio.ac3 --target Movie.25fps.mkv
```

## Auto-update

The installed app checks `https://github.com/AdkHex/FPSConv/releases/latest/download/latest.json`
five seconds after start and every six hours. When a newer version exists it
downloads the installer to `%APPDATA%\FPSConv\updates\`, verifies its SHA-256,
waits until no conversion is running, and runs the installer silently
(`/VERYSILENT`). The installer closes the app, replaces the files and relaunches
it. **⚙ → Install updates automatically** turns the last step into a
"Restart to update" button instead; **Check for updates** forces a check.

The version is `MAJOR.MINOR.<build>`: `MAJOR.MINOR` from the `VERSION` file,
`<build>` = the GitHub Actions run number. So every push to `main` is a strictly
newer version and reaches every installed copy.

## Release pipeline

`.github/workflows/release.yml` on every push to `main`:

1. `python -m unittest` on Linux
2. on `windows-latest`: write `fpsconv/_version.py`, run the tests again with deew
   present, **PyInstaller** (`packaging/FPSConv.spec` → `FPSConv.exe` windowed +
   `fpsconv-cli.exe` console, deew and pywebview bundled), smoke-test the frozen
   exe, **Inno Setup** (`packaging/installer.iss`, preinstalled on the runner),
   write `latest.json` + `SHA256SUMS.txt`
3. `gh release create v<version>` with the installer, `latest.json` and the checksums

Nothing to configure: it uses the repository's own `GITHUB_TOKEN`. To ship a
change, push to `main` (or *Run workflow* in the Actions tab). To bump the
major/minor, edit `VERSION`. Pull requests run `ci.yml` (tests on Linux + Windows).

## Run / build from source

```
./run.sh                 # macOS/Linux: GUI (creates .venv, installs deew + deezy + pywebview; Python 3.10–3.13)
run.bat                  # Windows: same
python -m fpsconv doctor
python -m unittest -v
build-windows.bat 1.0.0  # local Windows build → dist\FPSConv-Setup-1.0.0.exe (needs Inno Setup 6)
```

Settings and history: `%APPDATA%\FPSConv\` (Windows), `~/Library/Application Support/FPSConv/`
(macOS), `~/.config/FPSConv/` (Linux). deew's config: platformdirs' user config dir for `deew`
(`%LOCALAPPDATA%\deew\config.toml` on Windows).

## Layout

```
fpsconv/            engine.py (fps.py pipeline + audio-encode task) · queue.py · config.py · server.py (local API)
                    updater.py · window.py (pywebview) · __main__.py (gui/convert/encode/doctor/dee/deew/deezy)
                    static/index.html (GUI)
packaging/          FPSConv.spec (PyInstaller) · installer.iss (Inno Setup) · entry.py · make_icon.py
.github/workflows/  release.yml (build + release on push) · ci.yml (tests on PRs)
tests/              40 unit tests, no ffmpeg/deew/deezy needed
```

## Known limits inherited from the fps.py engine

* `atempo` is a pitch-preserving time-stretch, not a resample: pitch does not move with the speed change.
* Ratios are floats (`25/(24000/1001)`); over 2 h the rounding is well under a millisecond.
* AAC output is raw ADTS, which cannot carry more than 7 channels.
* Dolby sources are resampled to 48 kHz and re-encoded through DEE (lossy → lossy for DD/DD+).
* Metadata and chapters are stripped, as in `fps.py`.

MIT — see `LICENSE`.
