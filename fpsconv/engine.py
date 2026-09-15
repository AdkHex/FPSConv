"""The conversion engine, ported from fps.py.

Same pipeline, same commands, no Telegram:

* AAC source        -> one ffmpeg pass: ``-c:a aac -af atempo=...``
* AC-3 / E-AC-3 / TrueHD source
                    -> ffmpeg to 24-bit 48 kHz WAV with ``-af atempo=...``
                    -> ``deew -f dd|ddp|thd -b <kbps>`` (Dolby Encoding Engine)

Additions over fps.py (all optional, defaults reproduce fps.py exactly):

* pick which audio stream to convert (fps.py always took ``0:a:0``)
* override the output bitrate (fps.py reused the source's)
* overwrite / skip / rename when the output already exists (fps.py: ``-y``)
* explicit tool paths from settings, an ffmpeg downloader for Windows, and
  deew's own ``config.toml`` written for the user from the DEE path in Settings
* cancellation kills the running process and removes the partial output

Encode task (no speed change, any source, Dolby output — see ``convert_encode``):

* DD / DDP          -> ffmpeg lossless decode to 24-bit 48 kHz WAV (TrueHD,
                       DTS-HD MA, FLAC, AAC …) -> ``deew -f dd|ddp -b <kbps> [-dm N]``
* DDP Atmos         -> ``deezy encode atmos`` (truehdd decodes the TrueHD Atmos
                       objects, DEE 5.2 encodes DD+ JOC). TrueHD Atmos sources only.
"""

from __future__ import annotations

import io
import json
import os
import queue as _queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config, log as applog

LOG = applog.get("engine")

# ───────────────────────── CONFIGURATION (from fps.py) ─────────────────────────

DEE_CODEC_MAP = {
    "eac3":   ("-f", "ddp", ".ec3"),
    "ac3":    ("-f", "dd",  ".ac3"),
    "truehd": ("-f", "thd", ".thd"),
}

FPS_CONVERSIONS = {
    "23.976-24": 24 / (24000 / 1001),
    "23.976-25": 25 / (24000 / 1001),
    "24-23.976": (24000 / 1001) / 24,
    "24-25": 25 / 24,
    "25-23.976": (24000 / 1001) / 25,
    "25-24": 24 / 25,
}

AUDIO_EXTS = {
    ".mka", ".mkv", ".mp4", ".m4a", ".mov", ".ts", ".m2ts", ".webm",
    ".ac3", ".ec3", ".eac3", ".eb3", ".thd", ".truehd", ".mlp", ".dts", ".dtshd",
    ".aac", ".wav", ".w64", ".flac", ".ogg", ".opus",
}
#: What the "Target video" picker shows: anything ffprobe can read a frame rate from.
VIDEO_EXTS = AUDIO_EXTS | {".avi", ".m4v", ".wmv", ".mpg", ".mpeg", ".flv", ".vob"}

# Popen flag so no console window pops up on Windows for each ffmpeg/deew run.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FROZEN = bool(getattr(sys, "frozen", False))
#: Static ffmpeg build the Windows downloader fetches (gyan.dev "essentials").
FFMPEG_WIN_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


# ───────────────────────── ENCODE TASK (no fps change) ─────────────────────────

TASK_FPS = "fps"
TASK_ENCODE = "encode"
ENCODE_CONV = "encode"          # conv_type of an encode job: speed ratio 1.0, no atempo

TARGETS = ("ddp", "dd")
TARGET_CHANNELS = (0, 1, 2, 6, 8)   # 0 = same as the source
DRC_PROFILES = ("film_light", "film_standard", "music_light", "music_standard", "speech")
LAYOUT_NAMES = {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}
FORMAT_NAMES = {"ddp": "DDP", "dd": "DD"}

# What DEE accepts, copied from deew/bitrates.py. 7.1 is deew's "combined" list:
# up to 1024 kbps DEE runs in the standard 7.1 mode, above it in Blu-ray mode.
BITRATES = {
    ("dd", 1): [96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640],
    ("dd", 2): [96, 112, 128, 160, 192, 224, 256, 320, 384, 448, 512, 576, 640],
    ("dd", 6): [224, 256, 320, 384, 448, 512, 576, 640],
    ("ddp", 1): [32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144, 160, 176, 192, 200,
                 208, 216, 224, 232, 240, 248, 256, 272, 288, 304, 320, 336, 352, 368, 384, 400,
                 448, 512, 576, 640, 704, 768, 832, 896, 960, 1008, 1024],
    ("ddp", 2): [96, 104, 112, 120, 128, 144, 160, 176, 192, 200, 208, 216, 224, 232, 240, 248,
                 256, 272, 288, 304, 320, 336, 352, 368, 384, 400, 448, 512, 576, 640, 704, 768,
                 832, 896, 960, 1008, 1024],
    ("ddp", 6): [192, 200, 208, 216, 224, 232, 240, 248, 256, 272, 288, 304, 320, 336, 352, 368,
                 384, 400, 448, 512, 576, 640, 704, 768, 832, 896, 960, 1008, 1024],
    ("ddp", 8): [384, 448, 576, 640, 704, 768, 832, 896, 960, 1008, 1024, 1280, 1536, 1664],
}
# DD+ JOC rates DeeZy passes to DEE (deezy/enums/atmos.py).
ATMOS_BITRATES = {
    "streaming": [384, 448, 512, 576, 640, 768, 1024],      # DDP 5.1 + JOC
    "bluray": [1152, 1280, 1408, 1512, 1536, 1664],         # DDP 7.1 + JOC
}
DEFAULT_BITRATE = {
    ("dd", 1): 192, ("dd", 2): 256, ("dd", 6): 640,
    ("ddp", 1): 128, ("ddp", 2): 256, ("ddp", 6): 1024, ("ddp", 8): 1536,
    ("atmos", 6): 768, ("atmos", 8): 1536,
}


def allowed_bitrates(fmt: str, channels: int, atmos: bool = False) -> list[int]:
    if atmos:
        return ATMOS_BITRATES["bluray" if channels == 8 else "streaming"]
    return BITRATES[(fmt, channels)]


def snap_bitrate(fmt: str, channels: int, kbps: int, atmos: bool = False) -> int:
    """The closest bitrate DEE can actually do for this layout.

    0 / None picks the layout's default. So does a request outside the layout's
    whole range (1536 kbps on a 2.0 file): that number belongs to another
    layout, and the layout's default is more sensible than its maximum.
    """
    allowed = allowed_bitrates(fmt, channels, atmos)
    default = DEFAULT_BITRATE[("atmos", 8 if channels == 8 else 6) if atmos else (fmt, channels)]
    kbps = int(kbps or 0)
    if not kbps or kbps < allowed[0] or kbps > allowed[-1]:
        return default
    return min(allowed, key=lambda b: abs(b - kbps))


def pad_layout(channels: int) -> int:
    """deew/DEE only take 1, 2, 6 or 8 channel WAVs; odd layouts get padded up."""
    channels = int(channels or 2)
    if channels <= 1:
        return 1
    if channels == 2:
        return 2
    if channels <= 6:
        return 6
    return 8


@dataclass
class Encode:
    fmt: str                  # ddp | dd
    src_channels: int
    wav_channels: int         # what ffmpeg writes (padded layout)
    out_channels: int         # what DEE writes
    atmos: bool
    atmos_mode: str           # streaming | bluray | ""
    bitrate: int
    notes: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return ".ac3" if self.fmt == "dd" else ".ec3"

    @property
    def label(self) -> str:
        return (f"{FORMAT_NAMES[self.fmt]} {LAYOUT_NAMES[self.out_channels]}"
                f"{' Atmos' if self.atmos else ''} {self.bitrate}k")

    def to_dict(self) -> dict:
        return {"fmt": self.fmt, "src_channels": self.src_channels, "wav_channels": self.wav_channels,
                "out_channels": self.out_channels, "atmos": self.atmos, "atmos_mode": self.atmos_mode,
                "bitrate": self.bitrate, "label": self.label, "notes": self.notes}


def resolve_encode(target: str, target_channels: int, keep_atmos: bool, bitrate: int,
                   src_channels: int, src_atmos: Optional[bool], src_codec: str = "") -> Encode:
    """Decide what one encode job will really produce, and why.

    Never upmixes: a 7.1 request on a 2.0 source yields 2.0. Atmos survives only
    for a DDP 5.1 / 7.1 target from a source that MediaInfo confirmed as Atmos.
    """
    fmt = target if target in TARGETS else "ddp"
    notes: list[str] = []
    wav = pad_layout(src_channels)
    if wav != src_channels:
        notes.append(f"{src_channels}-channel source padded to {LAYOUT_NAMES[wav]} with silent channels")
    want = int(target_channels or 0) or wav
    out = min(want, wav)
    if want > wav:
        notes.append(f"no upmix: {LAYOUT_NAMES.get(want, want)} requested, source is {LAYOUT_NAMES[wav]}")
    if fmt == "dd" and out > 6:
        out = 6
        notes.append("DD has no 7.1: downmixed to 5.1")
    atmos = False
    if keep_atmos:
        if src_atmos is True and fmt == "ddp" and out >= 6:
            atmos = True
        elif src_atmos is True and fmt == "dd":
            notes.append("Atmos dropped: DD cannot carry Atmos")
        elif src_atmos is True:
            notes.append("Atmos dropped: needs a 5.1 or 7.1 target")
        elif src_atmos is None and src_codec == "truehd":
            notes.append("Atmos unknown (mediainfo not available): encoding the bed only")
    mode = ("bluray" if out == 8 else "streaming") if atmos else ""
    kbps = snap_bitrate(fmt, out, bitrate, atmos)
    if bitrate and kbps != int(bitrate):
        lo, hi = allowed_bitrates(fmt, out, atmos)[0], allowed_bitrates(fmt, out, atmos)[-1]
        what = f"{FORMAT_NAMES[fmt]} {LAYOUT_NAMES[out]}{' Atmos' if atmos else ''}"
        if int(bitrate) < lo or int(bitrate) > hi:
            notes.append(f"{bitrate} kbps is outside {what} ({lo}–{hi}): using the default {kbps}")
        else:
            notes.append(f"{bitrate} kbps is not a DEE {what} rate: using {kbps}")
    if fmt == "ddp" and out == 8 and not atmos and kbps > 1024:
        notes.append("DDP 7.1 above 1024 kbps: Blu-ray profile")
    return Encode(fmt, int(src_channels or 2), wav, out, atmos, mode, kbps, notes)


def encode_output_name(file_path: str, stream_index: int, enc: Encode) -> str:
    stem = os.path.splitext(os.path.basename(file_path))[0]
    track = f"_a{stream_index}" if stream_index else ""
    tag = f"{FORMAT_NAMES[enc.fmt]}{LAYOUT_NAMES[enc.out_channels]}{'Atmos' if enc.atmos else ''}"
    return f"{stem}{track}_{tag}_{enc.bitrate}k{enc.ext}"


# ───────────────────────── TOOL PATHS ─────────────────────────

def _bin_dir() -> Path:
    """Where the in-app ffmpeg download lands."""
    return config.config_dir() / "bin"


def tool(name: str) -> str:
    """Executable for ``ffmpeg`` / ``ffprobe`` / ``mediainfo`` / ``truehdd``.

    Order: explicit path from Settings → the in-app download → PATH.
    """
    override = (config.load_settings().get("tools") or {}).get(name, "")
    if override:
        return override
    exe = _bin_dir() / (f"{name}.exe" if sys.platform == "win32" else name)
    if exe.exists():
        return str(exe)
    return name


def _deew_importable() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("deew") is not None
    except Exception:  # noqa: BLE001
        return False


def deew_cmd() -> list[str]:
    """How to run deew.

    * A Python configured in Settings → ``python -m deew``
    * The installed Windows build bundles deew → ``FPSConv.exe deew`` (see
      ``__main__``), so nothing has to be installed on the machine
    * From source → this interpreter's ``-m deew``
    """
    python = (config.load_settings().get("tools") or {}).get("deew_python", "")
    if python:
        return [python, "-m", "deew"]
    if FROZEN and _deew_importable():
        # Prefer the console-subsystem exe: with CREATE_NO_WINDOW its children
        # (dee.exe, ffmpeg) inherit a hidden console instead of popping one up.
        exe_dir = Path(sys.executable).parent
        cli = exe_dir / ("fpsconv-cli.exe" if sys.platform == "win32" else "fpsconv-cli")
        return [str(cli if cli.exists() else sys.executable), "deew"]
    return [sys.executable, "-m", "deew"]


def _deezy_importable() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("deezy") is not None
    except Exception:  # noqa: BLE001
        return False


def deezy_cmd() -> list[str]:
    """How to run DeeZy (DDP Atmos).

    DeeZy cannot be bundled next to deew (it needs rich >= 14, deew rich < 14),
    so it is an external tool like truehdd:

    * ``deezy`` path from Settings (its standalone exe) → that
    * a Python configured in Settings (``deezy_python``) → ``python -m deezy``
    * ``deezy`` on PATH → that
    * otherwise this interpreter's ``-m deezy`` (source installs with deezy in the venv)
    """
    tools = config.load_settings().get("tools") or {}
    exe = tools.get("deezy", "")
    if exe:
        return [exe]
    python = tools.get("deezy_python", "")
    if python:
        return [python, "-m", "deezy"]
    if shutil.which("deezy"):
        return ["deezy"]
    if FROZEN and _deezy_importable():
        exe_dir = Path(sys.executable).parent
        cli = exe_dir / ("fpsconv-cli.exe" if sys.platform == "win32" else "fpsconv-cli")
        return [str(cli if cli.exists() else sys.executable), "deezy"]
    return [sys.executable, "-m", "deezy"]


def deew_config_path() -> Path:
    """deew reads ``config.toml`` from platformdirs' user_config_dir('deew')."""
    try:
        from platformdirs import PlatformDirs
        return Path(PlatformDirs("deew", False).user_config_dir) / "config.toml"
    except Exception:  # noqa: BLE001 - platformdirs ships with deew; mirror its rules
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "deew" / "config.toml"


def read_deew_config() -> dict:
    path = deew_config_path()
    if not path.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def write_deew_config(dee_path: str) -> Path:
    """Create / update deew's config.toml so the user never edits it by hand.

    Keeps everything deew's own generator writes, points it at our ffmpeg /
    ffprobe, turns the logo off (it is not a terminal) and sets the DEE path.
    """
    path = deew_config_path()
    current = read_deew_config()
    bitrates = current.get("default_bitrates") or {
        "dd_1_0": 128, "dd_2_0": 256, "dd_5_1": 640,
        "ddp_1_0": 128, "ddp_2_0": 256, "ddp_5_1": 1024, "ddp_7_1": 1536,
    }
    ffmpeg = shutil.which(tool("ffmpeg")) or tool("ffmpeg")
    ffprobe = shutil.which(tool("ffprobe")) or tool("ffprobe")
    # A temp folder we own. Left empty, the bundled deew would use "temp" next to
    # FPSConv.exe inside Program Files / Programs, which is the wrong place.
    temp_dir = config.config_dir() / "deew-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def q(v: str) -> str:
        return json.dumps(str(v))  # a TOML basic string; escapes backslashes and quotes

    text = f"""# Written by FPSConv. Edit the DEE path in FPSConv's Settings instead of here.
ffmpeg_path = {q(ffmpeg)}
ffprobe_path = {q(ffprobe)}
dee_path = {q(dee_path)}
temp_path = {q(str(temp_dir))}
logo = 0
max_instances = {q(current.get('max_instances', '50%'))}

[default_bitrates]
""" + "".join(f"    {k} = {v}\n" for k, v in bitrates.items()) + """
[summary_sections]
    deew_info = false
    binaries = false
    input_info = false
    output_info = false
    other = false
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def ensure_deew_config() -> None:
    """Migrate a deew config written by an older FPSConv (no temp_path, logo on)."""
    current = read_deew_config()
    dee = str(current.get("dee_path") or "")
    if not dee:
        return
    wanted = str(config.config_dir() / "deew-temp")
    if current.get("temp_path") != wanted or current.get("logo") != 0:
        try:
            write_deew_config(dee)
            LOG.info("updated deew config: temp folder %s", wanted)
        except OSError as exc:
            LOG.warning("could not update deew config: %s", exc)


def download_ffmpeg(progress: Optional[Callable[[float, str], None]] = None) -> dict:
    """Windows only: fetch a static ffmpeg build into the app's bin folder."""
    if sys.platform != "win32":
        raise RuntimeError("the in-app ffmpeg download is for Windows; use brew/apt elsewhere")
    dest = _bin_dir()
    dest.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(FFMPEG_WIN_ZIP, headers={"User-Agent": "FPSConv"})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total * 90, "downloading ffmpeg")
    found: dict[str, str] = {}
    with zipfile.ZipFile(buf) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            if base.lower() in ("ffmpeg.exe", "ffprobe.exe"):
                target = dest / base.lower()
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                found[base.lower()[:-4]] = str(target)
    if progress:
        progress(100, "done")
    if "ffmpeg" not in found or "ffprobe" not in found:
        raise RuntimeError("the downloaded archive did not contain ffmpeg.exe and ffprobe.exe")
    return found


# ───────────────────────── UTILS (from fps.py) ─────────────────────────

def hr_size(size) -> str:
    try:
        size = float(size)
    except (TypeError, ValueError):
        return "0 B"
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {u}"
        size /= 1024
    return f"{size:.2f} PB"


def fmt_time(sec) -> str:
    try:
        sec = max(0.0, float(sec))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"
    except (TypeError, ValueError):
        return "00:00:00"


def _ffprobe_json(path: str) -> dict:
    return json.loads(subprocess.check_output(
        [tool("ffprobe"), "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        text=True, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
    ))


def get_duration(path: str) -> float:
    try:
        return float(_ffprobe_json(path)["format"].get("duration", 0))
    except Exception:
        return 0.0


# Frame rates the app knows how to convert between, plus the common ones
# worth naming when they show up in a video track.
_FPS_LABELS = (
    (23.976, "23.976"), (24.0, "24"), (25.0, "25"), (29.97, "29.97"),
    (30.0, "30"), (50.0, "50"), (59.94, "59.94"), (60.0, "60"),
)
# "…23.976fps…", "…25 fps…", "…29.97…" in a file name.  A bare 24/25/30
# is not trusted (S01E24) unless "fps" follows it.
_FPS_IN_NAME = re.compile(r"(?<!\d)(23\.976|23\.98|29\.97|59\.94|(?:24|25|30|50|60)(?=[\s._-]?fps))[\s._-]?(?:fps)?(?!\d)", re.I)


def fps_label(value: float) -> Optional[str]:
    """24.0 → "24", 23.976023… → "23.976"; None for 0 / nonsense."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not v or v != v or v > 1000:
        return None
    for ref, label in _FPS_LABELS:
        if abs(v - ref) < 0.015:
            return label
    return f"{v:.3f}".rstrip("0").rstrip(".")


def _parse_rate(text: str) -> float:
    """ffprobe rates are "24000/1001" or "25/1"."""
    try:
        num, _, den = str(text).partition("/")
        return float(num) / (float(den) if den else 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _fps_from_tags(*tag_dicts: dict) -> Optional[str]:
    for tags in tag_dicts:
        for k, v in (tags or {}).items():
            if k.lower().replace("_", "").replace("-", "") in ("fps", "framerate", "originalfps", "sourcefps", "videofps"):
                label = fps_label(_parse_rate(v))
                if label:
                    return label
    return None


def detect_fps(data: dict, file_path: str = "") -> tuple[Optional[str], str]:
    """(fps label, where it came from) for a probed file.

    Audio has no frame rate of its own: it is only "23.976 fps audio" because
    it was cut to a 23.976 fps video.  So we look, in order, at the video track
    it is muxed with, at fps-like container / stream tags, and finally at the
    file name.  ("", "") when nothing tells us.
    """
    for s in data.get("streams", []):
        if s.get("codec_type") != "video" or s.get("disposition", {}).get("attached_pic"):
            continue
        for key in ("avg_frame_rate", "r_frame_rate"):
            label = fps_label(_parse_rate(s.get(key, "")))
            if label:
                return label, "video"
    fmt = data.get("format") or {}
    tag_fps = _fps_from_tags(fmt.get("tags"), *[s.get("tags") for s in data.get("streams", [])])
    if tag_fps:
        return tag_fps, "tag"
    m = _FPS_IN_NAME.search(os.path.basename(file_path))
    if m:
        return fps_label(float(m.group(1))) or m.group(1), "name"
    return None, ""


def suggest_conversion(src_fps: Optional[str], src_duration: float,
                       ref_fps: Optional[str], ref_duration: float,
                       tolerance: float = 0.0005) -> Optional[dict]:
    """Which FPS_CONVERSIONS key turns this audio into one that fits the reference video.

    * both frame rates known → "<src>-<ref>" if the app has it
    * otherwise compare durations: audio cut at 24 fps played against a 25 fps
      video is 25/24 longer, so the conversion whose speed ratio matches
      ``src_duration / ref_duration`` (within ``tolerance``, 0.05 %) is the one.
    Returns {"conv_type", "reason", "delta"} or None.
    """
    if src_fps and ref_fps:
        key = f"{src_fps}-{ref_fps}"
        if key in FPS_CONVERSIONS:
            return {"conv_type": key, "reason": f"audio is {src_fps} fps, video is {ref_fps} fps", "delta": 0.0}
        if src_fps == ref_fps:
            return {"conv_type": None, "reason": f"already {ref_fps} fps – no conversion needed", "delta": 0.0}
    if src_duration > 0 and ref_duration > 0:
        want = src_duration / ref_duration
        best, best_err = None, tolerance
        for key, ratio in FPS_CONVERSIONS.items():
            if ref_fps and not key.endswith("-" + ref_fps):
                continue
            err = abs(ratio - want) / want
            if err < best_err:
                best, best_err = key, err
        if best:
            delta = abs(src_duration - ref_duration * FPS_CONVERSIONS[best])
            return {"conv_type": best,
                    "reason": f"audio runs {fmt_time(src_duration)} against a {fmt_time(ref_duration)} video (×{want:.5f})",
                    "delta": round(delta, 2)}
        if abs(want - 1) < tolerance:
            return {"conv_type": None, "reason": "durations already match – no conversion needed", "delta": round(abs(src_duration - ref_duration), 2)}
    return None


def probe_video(file_path: str) -> dict:
    """Frame rate + duration of a reference video (no audio needed)."""
    data = _ffprobe_json(file_path)
    fps, source = detect_fps(data, file_path)
    duration = float((data.get("format") or {}).get("duration") or 0)
    return {"path": file_path, "name": os.path.basename(file_path), "fps": fps,
            "fps_source": source, "duration": fmt_time(duration), "duration_s": duration}


def _classify(codec_name: str) -> str:
    c = (codec_name or "aac").lower()
    if "eac3" in c:
        return "eac3"
    if "ac3" in c:
        return "ac3"
    if "truehd" in c:
        return "truehd"
    return "aac"


def _mediainfo_json(path: str) -> Optional[dict]:
    """MediaInfo's JSON for a file: the ``mediainfo`` CLI, else pymediainfo
    (bundled with deezy, DLL included on Windows). ``None`` when neither works."""
    try:
        return json.loads(subprocess.check_output(
            [tool("mediainfo"), "--Output=JSON", path],
            text=True, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL, timeout=120,
        ))
    except Exception:  # noqa: BLE001 - fall through to the library
        pass
    try:
        from pymediainfo import MediaInfo
        return json.loads(MediaInfo.parse(path, output="JSON"))
    except Exception:  # noqa: BLE001
        return None


def _parse_mediainfo_audio(data: Optional[dict]) -> Optional[list[dict]]:
    """Per audio track (in container order): ``{"atmos": bool, "commercial": str}``.

    ``None`` when MediaInfo was not available — ffprobe cannot see Atmos, so
    "unknown" must stay distinguishable from "no".
    """
    if not data:
        return None
    tracks = ((data.get("media") or {}).get("track")) or []
    out = []
    for t in tracks:
        if t.get("@type") != "Audio":
            continue
        commercial = str(t.get("Format_Commercial_IfAny") or "")
        extra = str(t.get("Format_AdditionalFeatures") or "")
        atmos = "atmos" in commercial.lower() or "joc" in extra.lower() or "16-ch" in extra.lower()
        out.append({"atmos": atmos, "commercial": commercial})
    return out


def _mediainfo_atmos(path: str) -> Optional[list[dict]]:
    return _parse_mediainfo_audio(_mediainfo_json(path))


def pretty_codec(stream: dict) -> str:
    """``TrueHD Atmos``, ``DTS-HD MA``, ``E-AC-3``, ``AAC`` … for the queue row."""
    name = (stream.get("codec_name") or "?").lower()
    profile = stream.get("profile") or ""
    base = {"truehd": "TrueHD", "eac3": "E-AC-3", "ac3": "AC-3", "aac": "AAC", "dts": "DTS",
            "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis", "mlp": "MLP"}.get(name)
    if base is None:
        base = "PCM" if name.startswith("pcm") else name.upper()
    if name == "dts" and profile:
        base = profile                     # ffprobe says "DTS-HD MA", "DTS-HD HRA", "DTS:X" …
    if stream.get("atmos") is True:
        base += " Atmos"
    return base


def probe_streams(file_path: str, with_mediainfo: bool = True) -> dict:
    """Every audio stream of a file, plus container duration.

    ``codec`` is the fps.py family (aac / ac3 / eac3 / truehd) — anything that
    is not Dolby is treated as AAC and goes through ffmpeg, exactly as fps.py did.
    ``atmos`` is True / False from MediaInfo, or None when it is not available.
    """
    streams: list[dict] = []
    duration = 0.0
    fps, fps_source = None, ""
    try:
        data = _ffprobe_json(file_path)
        duration = float((data.get("format") or {}).get("duration") or 0)
        fps, fps_source = detect_fps(data, file_path)
        fmt_bitrate = int((data.get("format") or {}).get("bit_rate") or 0) // 1000
        for s in data.get("streams", []):
            if s.get("codec_type") != "audio":
                continue
            tags = s.get("tags") or {}
            bitrate = int(s["bit_rate"]) // 1000 if s.get("bit_rate") else fmt_bitrate or 640
            streams.append({
                "index": len(streams),                     # position among audio streams (0:a:N)
                "codec": _classify(s.get("codec_name")),
                "codec_name": s.get("codec_name", "?"),
                "profile": s.get("profile", ""),
                "bitrate": bitrate,
                "channels": int(s.get("channels", 2)),
                "layout": s.get("channel_layout", ""),
                "sample_rate": int(s.get("sample_rate") or 48000),
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
                "atmos": None,
                "commercial": "",
            })
    except Exception:
        pass
    if streams and with_mediainfo:
        info = _mediainfo_atmos(file_path)
        if info is not None:
            for s in streams:
                mi = info[s["index"]] if s["index"] < len(info) else {"atmos": False, "commercial": ""}
                s["atmos"], s["commercial"] = mi["atmos"], mi["commercial"]
    for s in streams:
        s["pretty"] = pretty_codec(s)
    return {"streams": streams, "duration": duration, "fps": fps, "fps_source": fps_source}


def find_stream(file_path: str, stream_index: int, with_mediainfo: bool = False) -> Optional[dict]:
    info = probe_streams(file_path, with_mediainfo=with_mediainfo)
    for s in info["streams"]:
        if s["index"] == stream_index:
            return s
    return info["streams"][0] if info["streams"] else None


def detect_audio_info(file_path: str, stream_index: int = 0) -> tuple[str, int, int]:
    """(codec, bitrate_kbps, channels) of one audio stream — fps.py defaults if unknown."""
    s = find_stream(file_path, stream_index)
    if s:
        return s["codec"], s["bitrate"], s["channels"]
    return "aac", 640, 2


def atempo_chain(ratio) -> str:
    parts = []
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return "atempo=1.0"
    if abs(r - 1.0) < 0.0001:
        return "anull"
    while r > 2.0:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5")
        r *= 2.0
    parts.append(f"atempo={r:.6f}")
    return ",".join(parts)


def output_name(file_path: str, conv_type: str, codec: str, stream_index: int = 0) -> str:
    ext = ".aac" if codec == "aac" else DEE_CODEC_MAP.get(codec, ("-f", "ddp", ".ec3"))[2]
    stem = os.path.splitext(os.path.basename(file_path))[0]
    track = f"_a{stream_index}" if stream_index else ""
    return f"{stem}{track}_{conv_type.replace('.', '_')}{ext}"


def list_audio_files(folder: str, recursive: bool = False) -> list[str]:
    root = Path(folder)
    it = root.rglob("*") if recursive else root.iterdir()
    return sorted(
        str(p) for p in it
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")
    )


def _next_free(path: str) -> str:
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


def open_folder(path: str) -> bool:
    """Reveal a folder in Explorer / Finder / the desktop's file manager."""
    if not os.path.isdir(path):
        return False
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except OSError:
        return False


_SOXR_CACHE: dict[str, bool] = {}


def ffmpeg_has_soxr(ffmpeg: Optional[str] = None) -> bool:
    """Whether this ffmpeg build links libsoxr (``--enable-libsoxr`` in ``-version``)."""
    ffmpeg = ffmpeg or tool("ffmpeg")
    if ffmpeg not in _SOXR_CACHE:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=20,
                                 creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL).stdout
            _SOXR_CACHE[ffmpeg] = "--enable-libsoxr" in out
        except Exception:  # noqa: BLE001
            _SOXR_CACHE[ffmpeg] = False
    return _SOXR_CACHE[ffmpeg]


# ───────────────────────── TOOL CHECK ─────────────────────────

def doctor() -> dict:
    """What is installed. DEE itself is only visible through deew's config."""
    settings = config.load_settings()
    ffmpeg = tool("ffmpeg")
    ffprobe = tool("ffprobe")
    cmd = deew_cmd()
    dz = deezy_cmd()
    mediainfo = tool("mediainfo")
    truehdd = tool("truehdd")
    out = {
        "python": sys.version.split()[0],
        "frozen": FROZEN,
        "ffmpeg": shutil.which(ffmpeg) or (ffmpeg if os.path.isfile(ffmpeg) else None),
        "ffprobe": shutil.which(ffprobe) or (ffprobe if os.path.isfile(ffprobe) else None),
        "ffmpeg_soxr": False,
        "deew": None,
        "deew_via": "bundled" if cmd[:2] == [sys.executable, "deew"] else cmd[0],
        "deew_config": None,
        "dee": None,
        "dee_path": "",
        "mediainfo": shutil.which(mediainfo) or (mediainfo if os.path.isfile(mediainfo) else None),
        "pymediainfo": False,
        "truehdd": shutil.which(truehdd) or (truehdd if os.path.isfile(truehdd) else None),
        "deezy": None,
        "deezy_via": "bundled" if dz[-1] == "deezy" and len(dz) == 2 else dz[0],
        "config_dir": str(config.config_dir()),
        "settings": settings,
        "can_download_ffmpeg": sys.platform == "win32",
    }
    if out["ffmpeg"]:
        out["ffmpeg_soxr"] = ffmpeg_has_soxr(ffmpeg)
    try:
        import importlib.util
        out["pymediainfo"] = importlib.util.find_spec("pymediainfo") is not None
    except Exception:  # noqa: BLE001
        pass
    for key, argv in (("deew", cmd), ("deezy", dz)):
        try:
            out[key] = subprocess.run(
                argv + ["--version"], capture_output=True, text=True, timeout=30,
                creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
            ).returncode == 0 or None
        except Exception:  # noqa: BLE001
            out[key] = None
    cfg = deew_config_path()
    if cfg.exists():
        out["deew_config"] = str(cfg)
        dee = str(read_deew_config().get("dee_path") or "")
        out["dee_path"] = dee
        out["dee"] = dee if dee and (os.path.isfile(dee) or shutil.which(dee)) else None
    return out


# ───────────────────────── JOB MODEL ─────────────────────────

ProgressFn = Callable[[dict], None]


@dataclass
class Job:
    source: str
    conv_type: str
    out_dir: str
    stream_index: int = 0
    bitrate_override: int = 0          # 0 = source bitrate (fps.py) / DEE default (encode)
    overwrite: str = "overwrite"       # overwrite | skip | rename
    task: str = TASK_FPS               # fps | encode
    target: str = "ddp"                # encode: ddp | dd
    target_channels: int = 0           # encode: 0 = same as source, else 1 / 2 / 6 / 8
    atmos: bool = True                 # encode: keep Atmos when the source has it
    drc: str = "film_light"            # encode: DEE DRC profile
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "queued"              # queued | running | done | failed | cancelled | skipped
    step: str = ""
    percent: float = 0.0
    elapsed: float = 0.0
    eta: float = 0.0
    codec: str = ""
    pretty: str = ""                   # "TrueHD Atmos", "DTS-HD MA" … (encode task)
    label: str = ""                    # "DDP 7.1 Atmos 1536k" (encode task)
    bitrate: int = 0
    channels: int = 0
    duration: float = 0.0
    out_path: str = ""
    out_size: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    detail: str = ""                   # one-line "what is happening now"
    log: list[str] = field(default_factory=list)
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _last_progress_line: int = field(default=-1, repr=False)

    def key(self) -> tuple:
        """What makes two queued jobs "the same" for duplicate detection."""
        base = (self.source, self.conv_type, self.stream_index, self.task)
        if self.task == TASK_ENCODE:
            base += (self.target, self.target_channels, bool(self.atmos), self.bitrate_override, self.drc)
        return base

    # -- log helpers -------------------------------------------------------- #

    def say(self, text: str, level: str = "info") -> None:
        """Append a timestamped line to the job log (and the app log)."""
        self.log.append(f"{applog.stamp()}  {text}")
        self._last_progress_line = -1
        getattr(LOG, level if level in ("info", "warning", "error", "debug") else "info")(
            "[%s] %s", os.path.basename(self.source)[:40], text)

    def progress_line(self, text: str) -> None:
        """A live progress line: replaces the previous one instead of piling up."""
        line = f"{applog.stamp()}  {text}"
        if 0 <= self._last_progress_line < len(self.log):
            self.log[self._last_progress_line] = line
        else:
            self.log.append(line)
            self._last_progress_line = len(self.log) - 1

    def to_dict(self) -> dict:
        elapsed = (time.time() - self.started_at) if self.state == "running" and self.started_at else self.elapsed
        return {
            "id": self.id, "source": self.source, "name": os.path.basename(self.source),
            "conv_type": self.conv_type, "out_dir": self.out_dir, "stream_index": self.stream_index,
            "bitrate_override": self.bitrate_override, "overwrite": self.overwrite,
            "task": self.task, "target": self.target, "target_channels": self.target_channels,
            "atmos": self.atmos, "drc": self.drc, "label": self.label or self.conv_type,
            "state": self.state, "step": self.step, "detail": self.detail, "percent": round(self.percent, 1),
            "elapsed": fmt_time(elapsed), "eta": fmt_time(self.eta) if self.eta > 0 else "",
            "codec": self.codec, "pretty": self.pretty, "bitrate": self.bitrate, "channels": self.channels,
            "duration": fmt_time(self.duration), "out_path": self.out_path,
            "out_name": os.path.basename(self.out_path) if self.out_path else "",
            "out_size": hr_size(self.out_size) if self.out_size else "",
            "error": self.error, "finished_at": self.finished_at, "log": self.log[-400:],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        job = cls(
            source=d.get("source", ""), conv_type=d.get("conv_type", ""), out_dir=d.get("out_dir", ""),
            stream_index=int(d.get("stream_index", 0)), bitrate_override=int(d.get("bitrate_override", 0)),
            overwrite=d.get("overwrite", "overwrite"), id=d.get("id") or uuid.uuid4().hex[:12],
            task=d.get("task") or TASK_FPS, target=d.get("target") or "ddp",
            target_channels=int(d.get("target_channels") or 0), atmos=bool(d.get("atmos", True)),
            drc=d.get("drc") or "film_light",
        )
        job.state = d.get("state", "done")
        job.step = d.get("step", "")
        job.percent = 100.0 if job.state == "done" else 0.0
        job.codec, job.bitrate, job.channels = d.get("codec", ""), int(d.get("bitrate", 0)), int(d.get("channels", 0))
        job.pretty, job.label = d.get("pretty", ""), d.get("label", "")
        job.out_path = d.get("out_path", "")
        job.out_size = os.path.getsize(job.out_path) if job.out_path and os.path.exists(job.out_path) else 0
        job.error = d.get("error", "")
        job.finished_at = float(d.get("finished_at", 0))
        job.log = list(d.get("log", []))
        return job

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


# ───────────────────────── PROGRESS ENGINES (from fps.py) ─────────────────────────

def _spawn(cmd: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
    )


_ERR_RE = re.compile(r"not supported|no such|not found|no module|invalid|error|denied", re.IGNORECASE)


def _pick_error(lines: list[str]) -> str:
    """The most informative line of a failed tool's output, not just the last one."""
    for line in lines:
        if _ERR_RE.search(line) and "Conversion failed" not in line:
            return re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
    return lines[-1].strip() if lines else ""


_FFMPEG_NOISE = re.compile(r"^(ffmpeg version|built with|configuration:|lib(avutil|avcodec|avformat|avdevice|avfilter|swscale|swresample|postproc)\s)")


def run_ffmpeg_progress(cmd: list[str], job: Job, step_name: str,
                        total_dur: float, notify: ProgressFn) -> bool:
    if "-progress" not in cmd:
        cmd = cmd[:-1] + ["-progress", "pipe:1", cmd[-1]]
    job.say(f"{step_name}: $ " + " ".join(cmd))
    try:
        proc = _spawn(cmd)
    except OSError as exc:
        job.error = f"cannot start ffmpeg: {exc}"
        job.say(job.error, "error")
        return False
    job._proc = proc
    job.step = step_name
    job.detail = "starting ffmpeg"
    start = time.time()
    last = 0.0
    tail: list[str] = []
    state: dict[str, str] = {}
    assert proc.stdout is not None
    for raw in proc.stdout:
        if job._cancel.is_set():
            break
        line = raw.decode("utf-8", "ignore").strip()
        if not line:
            continue
        if "=" in line and line.split("=", 1)[0] in ("out_time_us", "out_time_ms", "out_time", "speed",
                                                    "bitrate", "total_size", "frame", "fps", "progress",
                                                    "drop_frames", "dup_frames", "stream_0_0_q"):
            key, value = line.split("=", 1)
            state[key] = value.strip()
            if key != "progress":            # "progress=" closes each block; speed/bitrate are in by then
                continue
            try:
                cur = int(state.get("out_time_us", "0")) / 1_000_000.0
            except ValueError:
                continue
            job.percent = min(100.0, cur / total_dur * 100) if total_dur > 0 else 0.0
            now = time.time()
            job.elapsed = now - start
            speed = cur / job.elapsed if job.elapsed > 0 else 0
            job.eta = (total_dur - cur) / speed if speed > 0 else 0
            job.detail = (f"ffmpeg {fmt_time(cur)} / {fmt_time(total_dur)}"
                          f"  ·  {state.get('speed', '')}  ·  {state.get('bitrate', '')}")
            if now - last >= 1.0:
                job.progress_line(f"{step_name}: {job.detail}  ·  {job.percent:.1f}%")
                notify(job.to_dict())
                last = now
            continue
        tail.append(line)
        tail = tail[-30:]
        if _FFMPEG_NOISE.match(line):
            continue                          # version banner / build config: not useful in a job log
        job.say(f"ffmpeg: {line}", "warning" if _ERR_RE.search(line) else "info")
    proc.wait()
    job.elapsed = time.time() - start
    if proc.returncode != 0 and not job._cancel.is_set():
        job.error = _pick_error(tail) or f"ffmpeg exited with code {proc.returncode}"
        job.say(f"ffmpeg exited with code {proc.returncode}: {job.error}", "error")
    else:
        job.say(f"{step_name}: finished in {fmt_time(job.elapsed)}")
    return proc.returncode == 0 and not job._cancel.is_set()


_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_STAGE_RE = re.compile(r"\[\s*([^\]|]+?)\s*\|")
#: deew's stages and the share of the job each one gets on the progress bar.
_DEEW_STAGES = {"ffmpeg": (0, 10), "DEE: measure": (10, 40), "DEE: encode": (40, 100)}


def run_deew_progress(cmd: list[str], job: Job, step_name: str, notify: ProgressFn) -> bool:
    """Run deew and turn its rich progress bars into stage + percent.

    deew draws its bars with *rich*, which only refreshes live on a terminal.
    ``FORCE_COLOR=1`` makes rich treat the pipe as a terminal, so the bar is
    redrawn continuously (carriage returns + ANSI codes) and we can read
    ``[ DEE: measure | file ] ━━━ 42.10%`` as it happens.
    """
    job.say(f"{step_name}: $ " + " ".join(cmd))
    env = dict(os.environ, FORCE_COLOR="1", TERM="xterm-256color", COLUMNS="120",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, env=env)
    except OSError as exc:
        job.error = f"cannot start deew: {exc}"
        job.say(job.error, "error")
        return False
    job._proc = proc
    job.step = step_name
    job.detail = "starting deew"
    job.percent = 0.0
    start = time.time()
    last = 0.0
    buffer = b""
    tail: list[str] = []
    seen_plain: set[str] = set()
    stage = ""
    stage_started = start
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(16)            # 16-byte reader: survives \r-only output
        if not chunk:
            break
        if job._cancel.is_set():
            break
        buffer += chunk
        # Split on CR/LF; keep the unfinished remainder for the next chunk.
        parts = re.split(rb"[\r\n]+", buffer)
        buffer = parts.pop()
        for raw in parts:
            text = _ANSI_RE.sub("", raw.decode("utf-8", "ignore")).strip()
            if not text:
                continue
            m = _PCT_RE.search(text)
            st = _STAGE_RE.search(text)
            if m and st:
                name = st.group(1).strip()
                pct = float(m.group(1))
                if name != stage:
                    if stage:
                        job.say(f"deew: {stage} finished in {fmt_time(time.time() - stage_started)}")
                    stage, stage_started = name, time.time()
                    job.say(f"deew: {name} started")
                lo, hi = _DEEW_STAGES.get(name, (0, 100))
                job.percent = min(100.0, lo + (hi - lo) * pct / 100.0)
                now = time.time()
                job.elapsed = now - start
                stage_el = now - stage_started
                job.eta = (stage_el / pct) * (100 - pct) if pct > 0 else 0
                job.detail = f"{name}  ·  {pct:.1f}%"
                if now - last >= 1.0:
                    job.progress_line(f"deew: {name}  {pct:.1f}%  ·  {fmt_time(stage_el)} in this stage")
                    notify(job.to_dict())
                    last = now
                continue
            if text in seen_plain:
                continue
            seen_plain.add(text)
            tail.append(text)
            tail = tail[-40:]
            job.say(f"deew: {text}", "warning" if _ERR_RE.search(text) else "info")
    if buffer.strip():
        text = _ANSI_RE.sub("", buffer.decode("utf-8", "ignore")).strip()
        if text and text not in seen_plain:
            tail.append(text)
            job.say(f"deew: {text}")
    proc.wait()
    job.elapsed = time.time() - start
    if stage:
        job.say(f"deew: {stage} finished in {fmt_time(time.time() - stage_started)}")
    if proc.returncode != 0 and not job._cancel.is_set():
        job.error = _pick_error(tail) or f"deew exited with code {proc.returncode}"
        job.say(f"deew exited with code {proc.returncode}: {job.error}", "error")
    else:
        job.say(f"{step_name}: deew exited with code {proc.returncode} after {fmt_time(job.elapsed)}")
    return proc.returncode == 0 and not job._cancel.is_set()


_DEEZY_LINE = re.compile(r"^(?:[\w-]+:\s*)?(.+?)\s*\((\d+) of (\d+)\)\s+(\d+(?:\.\d+)?)%")
#: truehdd's own ``--progress`` output, relayed by DeeZy at debug level as ``[truehdd-err] …``.
_TRUEHDD_PCT = re.compile(r"^\[truehdd[^\]]*\].*?(\d+(?:\.\d+)?)\s*%")
#: DeeZy's Atmos stages and the share of the job each one gets on the progress bar.
_DEEZY_STAGES = {"truehdd": (0, 35), "TrueHD extract & decode": (0, 35), "DEE measure": (35, 55),
                 "DEE encode": (55, 100), "FFMPEG": (0, 20)}


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def run_deezy_progress(cmd: list[str], job: Job, step_name: str, notify: ProgressFn,
                       work_dir: str = "") -> bool:
    """Run DeeZy and turn its output into stage + percent + a live detail line.

    Without a terminal DeeZy logs plain lines such as ``truehdd (1 of 3)  42.0%``,
    ``DEE measure (2 of 3) …`` and ``DEE encode (3 of 3) …``. For a raw ``.thd``
    DeeZy knows no duration, so the truehdd stage prints no percentage at all;
    while DeeZy is silent the detail line shows how much truehdd / DEE have
    written into ``work_dir`` so a long decode is visibly alive.
    """
    job.say(f"{step_name}: $ " + " ".join(cmd))
    env = dict(os.environ, DEEZY_NO_PROGRESS="1", NO_COLOR="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, env=env)
    except OSError as exc:
        job.error = f"cannot start deezy: {exc}"
        job.say(job.error, "error")
        return False
    job._proc = proc
    job.step = step_name
    job.detail = "starting deezy"
    job.percent = 0.0
    start = time.time()
    last = 0.0
    tail: list[str] = []
    stage = ""
    stage_started = start
    last_text = ""
    lines: "_queue.Queue[Optional[bytes]]" = _queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=reader, daemon=True, name="deezy-reader").start()

    def set_stage(name: str) -> None:
        nonlocal stage, stage_started
        if name != stage:
            if stage:
                job.say(f"deezy: {stage} finished in {fmt_time(time.time() - stage_started)}")
            stage, stage_started = name, time.time()
            job.say(f"deezy: {name} started")

    def progress(name: str, pct: float) -> None:
        nonlocal last
        set_stage(name)
        lo, hi = _DEEZY_STAGES.get(name, (0, 100))
        job.percent = min(100.0, lo + (hi - lo) * pct / 100.0)
        now = time.time()
        job.elapsed = now - start
        stage_el = now - stage_started
        job.eta = (stage_el / pct) * (100 - pct) if pct > 0 else 0
        job.detail = f"{name}  ·  {pct:.1f}%"
        if now - last >= 1.0:
            job.progress_line(f"deezy: {name}  {pct:.1f}%  ·  {fmt_time(stage_el)} in this stage")
            notify(job.to_dict())
            last = now

    while True:
        if job._cancel.is_set():
            break
        try:
            raw = lines.get(timeout=1.0)
        except _queue.Empty:
            # DeeZy is quiet (truehdd decoding a raw .thd): show that work is happening
            now = time.time()
            job.elapsed = now - start
            written = _dir_size(work_dir) if work_dir and os.path.isdir(work_dir) else 0
            what = stage or "truehdd decode"
            job.detail = f"{what}  ·  {hr_size(written)} written to temp  ·  {fmt_time(now - stage_started)} in this stage"
            if now - last >= 5.0:
                job.progress_line(f"deezy: {job.detail}")
                notify(job.to_dict())
                last = now
            continue
        if raw is None:
            break
        text = _ANSI_RE.sub("", raw.decode("utf-8", "ignore")).strip()
        if not text or text == last_text:          # debug level repeats every info line
            continue
        last_text = text
        m = _DEEZY_LINE.match(text)
        if m:
            progress(m.group(1).strip(), float(m.group(4)))
            continue
        t = _TRUEHDD_PCT.match(text)
        if t:
            progress("truehdd", float(t.group(1)))
            continue
        if text.startswith("[truehdd") and not stage:
            set_stage("truehdd")
        tail.append(text)
        tail = tail[-40:]
        job.say(f"deezy: {text}", "warning" if _ERR_RE.search(text) else "info")
    proc.wait()
    job.elapsed = time.time() - start
    if stage:
        job.say(f"deezy: {stage} finished in {fmt_time(time.time() - stage_started)}")
    if proc.returncode != 0 and not job._cancel.is_set():
        job.error = _pick_error(tail) or f"deezy exited with code {proc.returncode}"
        job.say(f"deezy exited with code {proc.returncode}: {job.error}", "error")
    else:
        job.say(f"{step_name}: deezy exited with code {proc.returncode} after {fmt_time(job.elapsed)}")
    return proc.returncode == 0 and not job._cancel.is_set()


def _deew_output(work_dir: str, base: str) -> Optional[str]:
    """deew names DDP 7.1 Blu-ray-profile output ``.eb3``; accept every extension it can write."""
    for ext in (".ec3", ".eb3", ".ac3", ".thd"):
        cand = os.path.join(work_dir, f"{base}{ext}")
        if os.path.exists(cand):
            return cand
    return None


# ───────────────────────── COMMAND BUILDERS (encode task) ─────────────────────────

def wav_cmd_encode(ffmpeg: str, src: str, stream_index: int, stream: dict, enc: Encode,
                   temp_wav: str, soxr: bool = True) -> list[str]:
    """Lossless decode to 24-bit 48 kHz WAV, no speed change.

    * ``-drc_scale 0`` for AC-3 / E-AC-3 sources — ffmpeg applies the stream's
      DRC metadata by default, which is wrong for a transcode.
    * a source that is not 48 kHz (96 kHz TrueHD) is resampled with soxr.
    """
    cmd = [ffmpeg, "-y", "-nostdin"]
    if stream.get("codec") in ("ac3", "eac3"):
        cmd += ["-drc_scale", "0"]
    cmd += ["-i", src, "-map", f"0:a:{stream_index}", "-vn", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c:a", "pcm_s24le", "-ac", str(enc.wav_channels)]
    if int(stream.get("sample_rate") or 48000) != 48000 and soxr:
        cmd += ["-af", "aresample=out_sample_rate=48000:resampler=soxr:precision=28"]
    cmd += ["-ar", "48000", "-rf64", "auto", temp_wav]
    return cmd


def deew_cmd_encode(temp_wav: str, enc: Encode, drc: str, out_dir: str) -> list[str]:
    cmd = deew_cmd() + ["-i", temp_wav, "-f", enc.fmt, "-b", str(enc.bitrate),
                        "-r", drc if drc in DRC_PROFILES else "film_light", "-o", out_dir, "-np"]
    if enc.out_channels < enc.wav_channels:
        cmd += ["-dm", str(enc.out_channels)]
    return cmd


def deezy_cmd_atmos(src: str, stream_index: int, enc: Encode, drc: str, work_dir: str, out_path: str,
                    tools: Optional[dict] = None) -> list[str]:
    """``deezy encode atmos …``; tool paths are passed explicitly so nobody has to
    write DeeZy's own config (``tools`` = {"ffmpeg", "dee", "truehdd"}, empty = its default).

    Only ``--no-progress-bars`` is a top-level DeeZy option; ``--ffmpeg`` /
    ``--dee`` / ``--truehdd`` belong to the ``encode atmos`` sub-parser.

    ``--working-dir`` (DeeZy's logs / batch-results) goes under our config
    folder: left alone, DeeZy creates ``deezy_work`` next to its own exe, which
    is Program Files for a normal install and not writable.
    """
    cmd = deezy_cmd() + ["--no-progress-bars", "--log-level", "debug", "encode", "atmos"]
    for flag in ("ffmpeg", "dee", "truehdd"):
        value = (tools or {}).get(flag) or ""
        if value:
            cmd += [f"--{flag}", value]
    return cmd + [
        "--atmos-mode", enc.atmos_mode, "--bitrate", str(enc.bitrate),
        "--track-index", f"a:{stream_index}",
        "--drc-line-mode", drc if drc in DRC_PROFILES else "film_light",
        "--working-dir", deezy_work_dir(), "--temp-dir", os.path.abspath(work_dir),
        "--output", out_path, "--overwrite", src,
    ]


def deezy_work_dir() -> str:
    """A writable folder for DeeZy's logs and batch results (``<config dir>/deezy-work``)."""
    path = config.config_dir() / "deezy-work"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(path)


def encode_plan(enc: Encode, stream: dict) -> list[str]:
    """What the job is about to do, step by step, for the job log."""
    src = stream.get("pretty") or stream.get("codec", "").upper()
    if enc.atmos:
        mode = "7.1 (Blu-ray mode)" if enc.atmos_mode == "bluray" else "5.1 (streaming mode)"
        return [
            f"1/4 ffmpeg copies the {src} stream unchanged into truehdd (no decode by ffmpeg)",
            "2/4 truehdd decodes the bed and the Atmos objects into a Dolby Atmos master (DAMF: "
            ".atmos / .atmos.audio / .atmos.metadata) in the job's temp folder — the slow step; "
            "for a raw .thd no percentage is available, so the row shows how much has been written",
            f"3/4 DEE measures loudness (dialnorm) on that master, then encodes DD+ JOC {mode} at "
            f"{enc.bitrate} kbps, DRC profile as chosen",
            "4/4 the .ec3 is moved to the output folder and the temp folder is deleted",
        ]
    decode = ("lossless decode" if stream.get("codec") in ("truehd",) or (stream.get("codec_name") or "") in ("dts", "flac", "mlp") or (stream.get("codec_name") or "").startswith("pcm")
              else "decode (lossy source: quality can only stay the same)")
    resample = "" if int(stream.get("sample_rate") or 48000) == 48000 else f", resampled from {stream.get('sample_rate')} Hz to 48 kHz"
    dm = ""
    if enc.out_channels < enc.wav_channels:
        dm = f" with DEE's own {LAYOUT_NAMES[enc.wav_channels]} → {LAYOUT_NAMES[enc.out_channels]} downmix"
    profile = " (Blu-ray profile)" if enc.fmt == "ddp" and enc.out_channels == 8 and enc.bitrate > 1024 else ""
    return [
        f"1/3 ffmpeg: {decode} of the {src} stream to 24-bit 48 kHz {LAYOUT_NAMES[enc.wav_channels]} WAV{resample}",
        f"2/3 deew writes DEE's XML job; DEE measures loudness (dialnorm), then encodes "
        f"{FORMAT_NAMES[enc.fmt]} {LAYOUT_NAMES[enc.out_channels]} at {enc.bitrate} kbps{profile}{dm}",
        f"3/3 the {enc.ext} is moved to the output folder and the temp folder is deleted",
    ]


def deezy_tools() -> dict:
    """Explicit tool paths for DeeZy: our ffmpeg, DEE from deew's config, truehdd from Settings / PATH."""
    ffmpeg = tool("ffmpeg")
    dee = str(read_deew_config().get("dee_path") or "")
    truehdd = tool("truehdd")
    return {
        "ffmpeg": shutil.which(ffmpeg) or (ffmpeg if os.path.isfile(ffmpeg) else ""),
        "dee": dee if dee and (os.path.isfile(dee) or shutil.which(dee)) else "",
        "truehdd": shutil.which(truehdd) or (truehdd if os.path.isfile(truehdd) else ""),
    }


# ───────────────────────── MAIN CONVERSION (from fps.py) ─────────────────────────

def convert(job: Job, notify: ProgressFn, work_root: str = ".temp_jobs") -> Job:
    """Run one job to completion (or cancellation). Mutates and returns ``job``."""
    if job.task == TASK_ENCODE:
        return convert_encode(job, notify, work_root)

    file_path = job.source
    conv_type = job.conv_type

    def finish(state: str, error: str = "") -> Job:
        job.state, job.error = state, error
        job.finished_at = time.time()
        notify(job.to_dict())
        return job

    if conv_type not in FPS_CONVERSIONS:
        return finish("failed", f"Invalid FPS conversion type: {conv_type}")
    if not os.path.exists(file_path):
        return finish("failed", f"File not found: {file_path}")

    ratio = FPS_CONVERSIONS[conv_type]
    job.codec, src_bitrate, job.channels = detect_audio_info(file_path, job.stream_index)
    job.bitrate = job.bitrate_override or src_bitrate
    job.duration = get_duration(file_path)

    codec = job.codec
    fmt, ext = "ddp", ".ec3"
    if codec != "aac":
        _, fmt, ext = DEE_CODEC_MAP.get(codec, ("-f", "ddp", ".ec3"))
        dee = str(read_deew_config().get("dee_path") or "")
        if not dee or not (os.path.isfile(dee) or shutil.which(dee)):
            return finish("failed", f"{codec.upper()} output needs Dolby Encoding Engine: "
                                    "set the dee.exe path in Settings (⚙) first")
    out_path = os.path.join(job.out_dir, output_name(file_path, conv_type, codec, job.stream_index))

    if os.path.exists(out_path):
        if job.overwrite == "skip":
            job.out_path = out_path
            job.out_size = os.path.getsize(out_path)
            job.percent = 100.0
            return finish("skipped", "output already exists")
        if job.overwrite == "rename":
            out_path = _next_free(out_path)
    job.out_path = out_path

    work_dir = os.path.join(work_root, f"fpsjob_{job.id}")
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(job.out_dir, exist_ok=True)
    job.state = "running"
    job.step = "Initializing"
    job.started_at = time.time()
    job.say(f"job started: {os.path.basename(file_path)}  →  {os.path.basename(out_path)}")
    job.say(f"source: {codec.upper()} · {job.bitrate} kbps · {job.channels} ch · {fmt_time(job.duration)} · "
            f"stream #{job.stream_index} · mode {conv_type} (atempo {atempo_chain(ratio)})")
    notify(job.to_dict())

    t0 = time.time()
    success = False
    ffmpeg = tool("ffmpeg")
    a_map = f"0:a:{job.stream_index}"
    try:
        if codec == "aac":
            ffmpeg_cmd = [
                ffmpeg, "-y", "-nostdin", "-i", file_path,
                "-map", a_map, "-vn", "-sn", "-dn",
                "-map_metadata", "-1", "-map_chapters", "-1",
                "-c:a", "aac", "-b:a", f"{job.bitrate}k",
                "-ac", str(job.channels),
                "-af", atempo_chain(ratio),
                out_path,
            ]
            success = run_ffmpeg_progress(ffmpeg_cmd, job, "Encoding AAC Engine",
                                          job.duration, notify)
        else:
            # Unique name: deew writes its own wav/xml under ONE shared temp folder
            # using this basename, so two parallel jobs called "temp_extract"
            # would delete each other's files (FileNotFoundError in deew's cleanup).
            temp_wav = os.path.join(work_dir, f"fpsconv_{job.id}.wav")
            wav_cmd = [
                ffmpeg, "-y", "-nostdin", "-i", file_path,
                "-map", a_map, "-vn", "-sn", "-dn",
                "-map_metadata", "-1", "-map_chapters", "-1",
                "-c:a", "pcm_s24le", "-ar", "48000", "-ac", str(job.channels),
                "-af", atempo_chain(ratio), "-rf64", "auto",
                temp_wav,
            ]
            wav_ok = run_ffmpeg_progress(wav_cmd, job, "Extracting WAV (1/2)",
                                         job.duration, notify)
            if wav_ok and os.path.exists(temp_wav) and not job._cancel.is_set():
                cmd = deew_cmd() + ["-i", temp_wav, "-f", fmt, "-b", str(job.bitrate),
                                    "-o", work_dir, "-np"]
                dee_ok = run_deew_progress(cmd, job, "Dolby DEE Encoding (2/2)", notify)
                base = os.path.splitext(os.path.basename(temp_wav))[0]
                deew_out = _deew_output(work_dir, base)
                if dee_ok and deew_out:
                    shutil.move(deew_out, out_path)
                    success = True
                elif dee_ok:
                    job.error = (f"deew reported success but {base}{ext} was not produced — "
                                 f"check the DEE path and the deew lines above")
    except Exception as exc:  # noqa: BLE001 - a job must never take the app down
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        _settle(job, out_path, work_dir, t0, success, notify)
    return job


def convert_encode(job: Job, notify: ProgressFn, work_root: str = ".temp_jobs") -> Job:
    """Audio-only encode: source track -> DD / DDP / DDP Atmos, no speed change."""
    file_path = job.source

    def finish(state: str, error: str = "") -> Job:
        job.state, job.error = state, error
        job.finished_at = time.time()
        notify(job.to_dict())
        return job

    job.conv_type = ENCODE_CONV
    if not os.path.exists(file_path):
        return finish("failed", f"File not found: {file_path}")
    stream = find_stream(file_path, job.stream_index, with_mediainfo=True)
    if stream is None:
        return finish("failed", "no audio stream found")
    if stream["index"] != job.stream_index:
        return finish("failed", f"audio stream #{job.stream_index} not found")

    enc = resolve_encode(job.target, job.target_channels, job.atmos, job.bitrate_override,
                         stream["channels"], stream["atmos"], stream["codec"])
    job.codec, job.pretty = stream["codec"], stream["pretty"]
    job.bitrate, job.channels, job.label = enc.bitrate, enc.out_channels, enc.label
    job.duration = get_duration(file_path)

    dee = str(read_deew_config().get("dee_path") or "")
    if not dee or not (os.path.isfile(dee) or shutil.which(dee)):
        return finish("failed", f"{enc.label} needs Dolby Encoding Engine: set the dee.exe path in Settings (⚙) first")
    if enc.atmos and not deezy_tools()["truehdd"]:
        return finish("failed", "DDP Atmos needs truehdd: set its path in Settings (⚙) or untick Keep Atmos")

    out_path = os.path.join(job.out_dir, encode_output_name(file_path, job.stream_index, enc))
    if os.path.exists(out_path):
        if job.overwrite == "skip":
            job.out_path = out_path
            job.out_size = os.path.getsize(out_path)
            job.percent = 100.0
            return finish("skipped", "output already exists")
        if job.overwrite == "rename":
            out_path = _next_free(out_path)
    job.out_path = out_path

    work_dir = os.path.join(work_root, f"fpsjob_{job.id}")
    os.makedirs(work_dir, exist_ok=True)
    os.makedirs(job.out_dir, exist_ok=True)
    job.state = "running"
    job.step = "Initializing"
    job.started_at = time.time()
    job.say(f"job started: {os.path.basename(file_path)}  →  {os.path.basename(out_path)}")
    job.say(f"source: {stream['pretty']} · {stream['channels']} ch · {stream['sample_rate']} Hz"
            f"{' · ' + stream['layout'] if stream.get('layout') else ''} · {fmt_time(job.duration)} · stream #{job.stream_index}")
    job.say(f"target: {enc.label}")
    for note in enc.notes:
        job.say(f"note: {note}")
    for line in encode_plan(enc, stream):
        job.say(f"plan: {line}")
    notify(job.to_dict())

    t0 = time.time()
    success = False
    try:
        if enc.atmos:
            cmd = deezy_cmd_atmos(file_path, job.stream_index, enc, job.drc, work_dir, out_path, deezy_tools())
            ok = run_deezy_progress(cmd, job, "DeeZy Atmos (truehdd → DEE)", notify, work_dir=work_dir)
            if ok and os.path.exists(out_path):
                success = True
            elif ok:
                job.error = f"deezy reported success but {os.path.basename(out_path)} was not produced"
        else:
            # Unique name: deew writes its own wav/xml under ONE shared temp folder
            # using this basename (see convert()).
            temp_wav = os.path.join(work_dir, f"fpsconv_{job.id}.wav")
            ffmpeg = tool("ffmpeg")
            soxr = ffmpeg_has_soxr(ffmpeg)
            if int(stream["sample_rate"]) != 48000:
                resampler = "soxr" if soxr else "ffmpeg swresample (no libsoxr in this build)"
                job.say(f"note: {stream['sample_rate']} Hz source resampled to 48 kHz with {resampler}")
            wav_ok = run_ffmpeg_progress(wav_cmd_encode(ffmpeg, file_path, job.stream_index, stream, enc, temp_wav, soxr=soxr),
                                         job, "Decoding to WAV (1/2)", job.duration, notify)
            if not wav_ok and soxr and not job._cancel.is_set() and int(stream["sample_rate"]) != 48000:
                job.say("note: soxr resampler failed, retrying with ffmpeg's default resampler", "warning")
                job.error = ""
                wav_ok = run_ffmpeg_progress(wav_cmd_encode(ffmpeg, file_path, job.stream_index, stream, enc,
                                                            temp_wav, soxr=False),
                                             job, "Decoding to WAV (1/2)", job.duration, notify)
            if wav_ok and os.path.exists(temp_wav) and not job._cancel.is_set():
                cmd = deew_cmd_encode(temp_wav, enc, job.drc, work_dir)
                dee_ok = run_deew_progress(cmd, job, "Dolby DEE Encoding (2/2)", notify)
                base = os.path.splitext(os.path.basename(temp_wav))[0]
                deew_out = _deew_output(work_dir, base)
                if dee_ok and deew_out:
                    shutil.move(deew_out, out_path)
                    success = True
                elif dee_ok:
                    job.error = (f"deew reported success but {base}{enc.ext} was not produced — "
                                 f"check the DEE path and the deew lines above")
    except Exception as exc:  # noqa: BLE001 - a job must never take the app down
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        _settle(job, out_path, work_dir, t0, success, notify)
    return job


def _settle(job: Job, out_path: str, work_dir: str, t0: float, success: bool, notify: ProgressFn) -> None:
    """Common tail of a job: clean up, set the terminal state, remove partial output."""
    shutil.rmtree(work_dir, ignore_errors=True)
    job.elapsed = time.time() - t0
    job._proc = None
    job.finished_at = time.time()
    job.detail = ""
    if job._cancel.is_set():
        job.state = "cancelled"
        job.error = "cancelled by user"
        _remove(out_path)
        job.say("cancelled — partial output removed", "warning")
    elif success and os.path.exists(out_path):
        job.state = "done"
        job.percent = 100.0
        job.eta = 0.0
        job.out_size = os.path.getsize(out_path)
        job.say(f"done in {fmt_time(job.elapsed)}: {out_path} ({hr_size(job.out_size)})")
    else:
        job.state = "failed"
        job.error = job.error or "Conversion failed — check codec / format"
        _remove(out_path)
        job.say(f"failed: {job.error}", "error")
    notify(job.to_dict())


def _remove(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
