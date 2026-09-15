"""The conversion engine, ported from fps.py, plus an audio-only encode task.

FPS task (from fps.py — same pipeline, same commands, no Telegram):

* AAC source        -> one ffmpeg pass: ``-c:a aac -af atempo=...``
* AC-3 / E-AC-3 / TrueHD source
                    -> ffmpeg to 24-bit 48 kHz WAV with ``-af atempo=...``
                    -> ``python -m deew -f dd|ddp|thd -b <kbps>`` (Dolby Encoding Engine)

Encode task (no speed change, any source, Dolby output):

* DD / DDP          -> ffmpeg to 24-bit 48 kHz WAV (lossless decode of TrueHD,
                       DTS-HD MA, FLAC, AAC …) -> ``deew -f dd|ddp -b <kbps> [-dm N]``
* DDP Atmos         -> ``deezy encode atmos`` (truehdd decodes the TrueHD Atmos
                       objects, DEE 5.2 encodes DD+ JOC). TrueHD Atmos sources only.

Additions over fps.py (all optional, defaults reproduce fps.py exactly):

* pick which audio stream to convert (fps.py always took ``0:a:0``)
* override the output bitrate (fps.py reused the source's)
* overwrite / skip / rename when the output already exists (fps.py: ``-y``)
* explicit tool paths from settings, for machines where ffmpeg is not on PATH
* cancellation kills the running process and removes the partial output
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config

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

# Popen flag so no console window pops up on Windows for each ffmpeg/deew run.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
ATMOS_BITRATES = {
    "streaming": [384, 448, 576, 640, 768, 1024],   # DDP 5.1 + JOC
    "bluray": [1024, 1280, 1536, 1664],              # DDP 7.1 + JOC
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
            notes.append("Atmos unknown (mediainfo not installed): encoding the bed only")
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

def tool(name: str) -> str:
    """Executable for ``ffmpeg`` / ``ffprobe`` / ``mediainfo`` / ``truehdd``: the settings override, else PATH."""
    override = (config.load_settings().get("tools") or {}).get(name, "")
    return override or name


def deew_cmd() -> list[str]:
    python = (config.load_settings().get("tools") or {}).get("deew_python", "") or sys.executable
    return [python, "-m", "deew"]


def deezy_cmd() -> list[str]:
    """DeeZy's console script: the settings override, else the one in this venv, else PATH."""
    override = (config.load_settings().get("tools") or {}).get("deezy", "")
    if override:
        return [override]
    exe = "deezy.exe" if sys.platform == "win32" else "deezy"
    local = Path(sys.executable).parent / exe
    return [str(local) if local.exists() else "deezy"]


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
        sec = float(sec)
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
    try:
        return json.loads(subprocess.check_output(
            [tool("mediainfo"), "--Output=JSON", path],
            text=True, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL, timeout=120,
        ))
    except Exception:
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
    ``atmos`` is True / False from MediaInfo, or None when it is not installed.
    """
    streams: list[dict] = []
    duration = 0.0
    try:
        data = _ffprobe_json(file_path)
        duration = float((data.get("format") or {}).get("duration") or 0)
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
    return {"streams": streams, "duration": duration}


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


# ───────────────────────── TOOL CHECK ─────────────────────────

def _deew_config_path() -> Optional[Path]:
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "deew" / "config.toml" if os.environ.get("APPDATA") else None,
        Path.home() / ".config" / "deew" / "config.toml",
        Path.home() / "Library" / "Application Support" / "deew" / "config.toml",
        Path.cwd() / "config.toml",
    ]
    for cand in candidates:
        if cand and cand.exists():
            return cand
    return None


def _which(name: str) -> Optional[str]:
    return shutil.which(name) or (name if os.path.isfile(name) else None)


_SOXR_CACHE: dict[str, bool] = {}


def ffmpeg_has_soxr(ffmpeg: Optional[str] = None) -> bool:
    """Whether this ffmpeg build links libsoxr (``--enable-libsoxr`` in ``-version``)."""
    ffmpeg = ffmpeg or tool("ffmpeg")
    if ffmpeg not in _SOXR_CACHE:
        try:
            out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=20,
                                 creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL).stdout
            _SOXR_CACHE[ffmpeg] = "--enable-libsoxr" in out
        except Exception:
            _SOXR_CACHE[ffmpeg] = False
    return _SOXR_CACHE[ffmpeg]


def _runs(cmd: list[str]) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                              creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def doctor() -> dict:
    """What is installed. DEE itself is only visible through deew's config."""
    settings = config.load_settings()
    out = {
        "python": sys.version.split()[0],
        "ffmpeg": _which(tool("ffmpeg")),
        "ffprobe": _which(tool("ffprobe")),
        "ffmpeg_soxr": ffmpeg_has_soxr(tool("ffmpeg")) if _which(tool("ffmpeg")) else False,
        "mediainfo": _which(tool("mediainfo")),
        "truehdd": _which(tool("truehdd")),
        "deew": None,
        "deew_python": deew_cmd()[0],
        "deew_config": None,
        "deezy": None,
        "deezy_cmd": deezy_cmd()[0],
        "dee": None,
        "config_dir": str(config.config_dir()),
        "settings": settings,
    }
    out["deew"] = _runs(deew_cmd() + ["--help"]) or None
    out["deezy"] = _runs(deezy_cmd() + ["--version"]) or None
    cfg = _deew_config_path()
    if cfg:
        out["deew_config"] = str(cfg)
        try:
            import tomllib
            data = tomllib.loads(cfg.read_text(encoding="utf-8"))
            dee = str(data.get("dee_path") or "")
            out["dee"] = dee if dee and (os.path.isfile(dee) or shutil.which(dee)) else None
        except Exception:
            out["dee"] = None
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
    finished_at: float = 0.0
    log: list[str] = field(default_factory=list)
    _proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def key(self) -> tuple:
        """What makes two queued jobs "the same" for duplicate detection."""
        base = (self.source, self.conv_type, self.stream_index, self.task)
        if self.task == TASK_ENCODE:
            base += (self.target, self.target_channels, bool(self.atmos), self.bitrate_override, self.drc)
        return base

    def to_dict(self) -> dict:
        return {
            "id": self.id, "source": self.source, "name": os.path.basename(self.source),
            "conv_type": self.conv_type, "out_dir": self.out_dir, "stream_index": self.stream_index,
            "bitrate_override": self.bitrate_override, "overwrite": self.overwrite,
            "task": self.task, "target": self.target, "target_channels": self.target_channels,
            "atmos": self.atmos, "drc": self.drc, "label": self.label or self.conv_type,
            "state": self.state, "step": self.step, "percent": round(self.percent, 1),
            "elapsed": fmt_time(self.elapsed), "eta": fmt_time(self.eta),
            "codec": self.codec, "pretty": self.pretty, "bitrate": self.bitrate, "channels": self.channels,
            "duration": fmt_time(self.duration), "out_path": self.out_path,
            "out_name": os.path.basename(self.out_path) if self.out_path else "",
            "out_size": hr_size(self.out_size) if self.out_size else "",
            "error": self.error, "finished_at": self.finished_at, "log": self.log[-60:],
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


def run_ffmpeg_progress(cmd: list[str], job: Job, step_name: str,
                        total_dur: float, notify: ProgressFn) -> bool:
    if "-progress" not in cmd:
        cmd = cmd[:-1] + ["-progress", "pipe:1", cmd[-1]]
    job.log.append("$ " + " ".join(cmd))
    try:
        proc = _spawn(cmd)
    except OSError as exc:
        job.error = f"cannot start ffmpeg: {exc}"
        job.log.append(job.error)
        return False
    job._proc = proc
    job.step = step_name
    start = time.time()
    last = 0.0
    tail: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        if job._cancel.is_set():
            break
        line = raw.decode("utf-8", "ignore").strip()
        if not line:
            continue
        if not line.startswith(("out_time", "frame=", "fps=", "bitrate=", "total_size=",
                                "stream_", "speed=", "progress=", "drop_", "dup_")):
            tail.append(line)
            tail = tail[-30:]
        if line.startswith("out_time_us="):
            try:
                cur = int(line.split("=")[1]) / 1_000_000.0
            except ValueError:
                continue
            job.percent = min(100.0, cur / total_dur * 100) if total_dur > 0 else 0.0
            now = time.time()
            job.elapsed = now - start
            speed = cur / job.elapsed if job.elapsed > 0 else 0
            job.eta = (total_dur - cur) / speed if speed > 0 else 0
            if now - last >= 0.5:
                notify(job.to_dict())
                last = now
    proc.wait()
    job.elapsed = time.time() - start
    if proc.returncode != 0 and not job._cancel.is_set():
        job.log.extend(tail)
        job.error = _pick_error(tail) or f"ffmpeg exited with code {proc.returncode}"
    return proc.returncode == 0 and not job._cancel.is_set()


_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def run_pct_progress(cmd: list[str], job: Job, step_name: str, notify: ProgressFn,
                     tool_name: str = "deew", steps: Optional[list[str]] = None) -> bool:
    """Drive a tool that prints ``NN%`` (deew, DeeZy — rich progress bars).

    ``steps`` names the phases of a multi-pass tool: every time the percentage
    falls back towards zero the next name becomes the step label.
    """
    job.log.append("$ " + " ".join(cmd))
    try:
        proc = _spawn(cmd)
    except OSError as exc:
        job.error = f"cannot start {tool_name}: {exc}"
        job.log.append(job.error)
        return False
    job._proc = proc
    job.step = steps[0] if steps else step_name
    job.percent = 0.0
    start = time.time()
    last = 0.0
    buffer = ""
    tail = ""
    phase = 0
    prev_pct = 0.0
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(16)          # 16-byte reader: survives \r-only output
        if not chunk:
            break
        if job._cancel.is_set():
            break
        text = chunk.decode("utf-8", "ignore")
        buffer += text
        tail = (tail + text)[-3000:]
        matches = _PCT_RE.findall(buffer)
        now = time.time()
        if matches:
            pct = float(matches[-1])
            buffer = buffer[-50:]
            if steps and pct < prev_pct - 30 and phase < len(steps) - 1:
                phase += 1
                job.step = steps[phase]
                start = now
            prev_pct = pct
            job.percent = min(100.0, pct)
            job.elapsed = now - start
            job.eta = (job.elapsed / pct) * (100 - pct) if pct > 0 else 0
        else:
            job.elapsed = now - start
        if now - last >= 0.5:
            notify(job.to_dict())
            last = now
    proc.wait()
    job.elapsed = time.time() - start
    if proc.returncode != 0 and not job._cancel.is_set():
        lines = [l for l in re.split(r"[\r\n]+", tail) if l.strip()]
        job.log.extend(lines[-15:])
        job.error = _pick_error(lines) or f"{tool_name} exited with code {proc.returncode}"
    return proc.returncode == 0 and not job._cancel.is_set()


def run_deew_progress(cmd: list[str], job: Job, step_name: str, notify: ProgressFn) -> bool:
    return run_pct_progress(cmd, job, step_name, notify, tool_name="deew")


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


def deezy_cmd_atmos(src: str, stream_index: int, enc: Encode, drc: str, work_dir: str, out_path: str) -> list[str]:
    return deezy_cmd() + [
        "encode", "atmos", "--atmos-mode", enc.atmos_mode, "--bitrate", str(enc.bitrate),
        "--track-index", f"a:{stream_index}",
        "--drc-line-mode", drc if drc in DRC_PROFILES else "film_light",
        "--temp-dir", work_dir, "--output", out_path, "--overwrite", src,
    ]


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
            temp_wav = os.path.join(work_dir, "temp_extract.wav")
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
                    job.error = f"deew reported success but {base}{ext} was not produced"
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
    job.log.append(f"source: {stream['pretty']} · {stream['channels']} ch · {stream['sample_rate']} Hz"
                   f"{' · ' + stream['layout'] if stream.get('layout') else ''}")
    job.log.append(f"target: {enc.label}")
    job.log.extend(f"note: {n}" for n in enc.notes)

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
    notify(job.to_dict())

    t0 = time.time()
    success = False
    try:
        if enc.atmos:
            cmd = deezy_cmd_atmos(file_path, job.stream_index, enc, job.drc, work_dir, out_path)
            ok = run_pct_progress(cmd, job, "DeeZy Atmos", notify, tool_name="deezy",
                                  steps=["DeeZy Atmos · truehdd decode", "DeeZy Atmos · DEE encode"])
            if ok and os.path.exists(out_path):
                success = True
            elif ok:
                job.error = f"deezy reported success but {os.path.basename(out_path)} was not produced"
        else:
            temp_wav = os.path.join(work_dir, "temp_extract.wav")
            ffmpeg = tool("ffmpeg")
            soxr = ffmpeg_has_soxr(ffmpeg)
            if int(stream["sample_rate"]) != 48000:
                resampler = "soxr" if soxr else "ffmpeg swresample (no libsoxr in this build)"
                job.log.append(f"note: {stream['sample_rate']} Hz source resampled to 48 kHz with {resampler}")
            wav_ok = run_ffmpeg_progress(wav_cmd_encode(ffmpeg, file_path, job.stream_index, stream, enc, temp_wav, soxr=soxr),
                                         job, "Decoding to WAV (1/2)", job.duration, notify)
            if not wav_ok and soxr and not job._cancel.is_set() and int(stream["sample_rate"]) != 48000:
                # ffmpeg without libsoxr: fall back to its default resampler
                job.log.append("note: soxr resampler unavailable, retrying with ffmpeg's default resampler")
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
                    job.error = f"deew reported success but {base}{enc.ext} was not produced"
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
    if job._cancel.is_set():
        job.state = "cancelled"
        job.error = "cancelled by user"
        _remove(out_path)
    elif success and os.path.exists(out_path):
        job.state = "done"
        job.percent = 100.0
        job.eta = 0.0
        job.out_size = os.path.getsize(out_path)
    else:
        job.state = "failed"
        job.error = job.error or "Conversion failed — check codec / format"
        _remove(out_path)
    notify(job.to_dict())


def _remove(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
