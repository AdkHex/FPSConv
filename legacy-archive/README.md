# Legacy converters — archived for reference

These are the two programs `fpsaudio` replaces. They are kept **only** so the
line references in [`../fpsaudio/docs/AUDIT.md`](../fpsaudio/docs/AUDIT.md) can
be opened and checked. Nothing here is used at runtime.

- `Fps Converter/` — Directory A, "V2.0 by Ionicboy", 152 lines
- `Fps Converter Batch Mode/FPS Converter/` — Directory B, ~1,700 lines

**Do not run either of them.** Both perform the wrong operation: they
time-stretch (pitch-preserving) where a frame-rate change requires a resample
(pitch moves with speed). Directory A additionally ships four wrong ratios that
drift ~300 ms over a two-hour feature, and cannot process TrueHD, DTS, FLAC,
PCM or Opus at all. Directory B silently converts TrueHD 7.1 to ~128 kbps AAC.

## Removed from this archive

The audit lists these as dead weight; they were deleted to take the archive from
7.5 MB to under 100 KB. No source file was touched.

- `Fps Converter Batch Mode/FPS Converter/.venv/` — a checked-in **Windows**
  virtualenv (7.2 MB), created under `C:\Users\Uploader\Documents\...` and
  pointing at `C:\Program Files\Python313`
- `Fps Converter Batch Mode/FPS Converter.zip` — a duplicate of the folder
  beside it
- `Fps Converter Batch Mode/__MACOSX/` — macOS archive metadata
- `__pycache__/` directories
