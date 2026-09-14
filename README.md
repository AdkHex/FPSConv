# FPSConv

Windows desktop app (plus a command line) for changing the frame-rate speed of
audio tracks — 23.976 ↔ 24 ↔ 25 — with the `fps.py` engine: ffmpeg `atempo`
for AAC, and deew + Dolby Encoding Engine for AC-3 / E-AC-3 / TrueHD.

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
| Python, pywebview, deew | everything | **yes** — bundled in the exe |
| ffmpeg + ffprobe | probing, WAV extraction, AAC encoding | no — one click in ⚙ downloads a static build, or set a path |
| Dolby Encoding Engine (`dee.exe`) | AC-3 / E-AC-3 / TrueHD | no — licensed from Dolby; point ⚙ at it |
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
fpsconv-cli doctor            # what is installed / configured
fpsconv-cli dee C:\Dolby\DEE\dee.exe   # write deew's config for this DEE
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
./run.sh                 # macOS/Linux: GUI (creates .venv, installs deew + pywebview)
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
fpsconv/            engine.py (fps.py pipeline) · queue.py · config.py · server.py (local API)
                    updater.py · window.py (pywebview) · __main__.py (gui/convert/doctor/dee/deew)
                    static/index.html (GUI)
packaging/          FPSConv.spec (PyInstaller) · installer.iss (Inno Setup) · entry.py · make_icon.py
.github/workflows/  release.yml (build + release on push) · ci.yml (tests on PRs)
tests/              16 unit tests, no ffmpeg/deew needed
```

## Known limits inherited from the fps.py engine

* `atempo` is a pitch-preserving time-stretch, not a resample: pitch does not move with the speed change.
* Ratios are floats (`25/(24000/1001)`); over 2 h the rounding is well under a millisecond.
* AAC output is raw ADTS, which cannot carry more than 7 channels.
* Dolby sources are resampled to 48 kHz and re-encoded through DEE (lossy → lossy for DD/DD+).
* Metadata and chapters are stripped, as in `fps.py`.

MIT — see `LICENSE`.
