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
"""

from __future__ import annotations

import io
import json
import os
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
    ".ac3", ".ec3", ".eac3", ".thd", ".truehd", ".aac", ".wav", ".flac", ".ogg", ".opus",
}
#: What the "Target video" picker shows: anything ffprobe can read a frame rate from.
VIDEO_EXTS = AUDIO_EXTS | {".avi", ".m4v", ".wmv", ".mpg", ".mpeg", ".flv", ".vob"}

# Popen flag so no console window pops up on Windows for each ffmpeg/deew run.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

FROZEN = bool(getattr(sys, "frozen", False))
#: Static ffmpeg build the Windows downloader fetches (gyan.dev "essentials").
FFMPEG_WIN_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


# ───────────────────────── TOOL PATHS ─────────────────────────

def _bin_dir() -> Path:
    """Where the in-app ffmpeg download lands."""
    return config.config_dir() / "bin"


def tool(name: str) -> str:
    """Executable for ``ffmpeg`` / ``ffprobe``.

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

    def q(v: str) -> str:
        return json.dumps(str(v))  # a TOML basic string; escapes backslashes and quotes

    text = f"""# Written by FPSConv. Edit the DEE path in FPSConv's Settings instead of here.
ffmpeg_path = {q(ffmpeg)}
ffprobe_path = {q(ffprobe)}
dee_path = {q(dee_path)}
temp_path = {q(current.get('temp_path', ''))}
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


def probe_streams(file_path: str) -> dict:
    """Every audio stream of a file, plus container duration.

    ``codec`` is the fps.py family (aac / ac3 / eac3 / truehd) — anything that
    is not Dolby is treated as AAC and goes through ffmpeg, exactly as fps.py did.
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
                "bitrate": bitrate,
                "channels": int(s.get("channels", 2)),
                "layout": s.get("channel_layout", ""),
                "language": tags.get("language", ""),
                "title": tags.get("title", ""),
            })
    except Exception:
        pass
    return {"streams": streams, "duration": duration, "fps": fps, "fps_source": fps_source}


def detect_audio_info(file_path: str, stream_index: int = 0) -> tuple[str, int, int]:
    """(codec, bitrate_kbps, channels) of one audio stream — fps.py defaults if unknown."""
    info = probe_streams(file_path)
    for s in info["streams"]:
        if s["index"] == stream_index:
            return s["codec"], s["bitrate"], s["channels"]
    if info["streams"]:
        s = info["streams"][0]
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

def doctor() -> dict:
    """What is installed. DEE itself is only visible through deew's config."""
    settings = config.load_settings()
    ffmpeg = tool("ffmpeg")
    ffprobe = tool("ffprobe")
    cmd = deew_cmd()
    out = {
        "python": sys.version.split()[0],
        "frozen": FROZEN,
        "ffmpeg": shutil.which(ffmpeg) or (ffmpeg if os.path.isfile(ffmpeg) else None),
        "ffprobe": shutil.which(ffprobe) or (ffprobe if os.path.isfile(ffprobe) else None),
        "deew": None,
        "deew_via": "bundled" if cmd[:2] == [sys.executable, "deew"] else cmd[0],
        "deew_config": None,
        "dee": None,
        "dee_path": "",
        "config_dir": str(config.config_dir()),
        "settings": settings,
        "can_download_ffmpeg": sys.platform == "win32",
    }
    try:
        out["deew"] = subprocess.run(
            cmd + ["--version"], capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
        ).returncode == 0 or None
    except Exception:  # noqa: BLE001
        out["deew"] = None
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
    bitrate_override: int = 0          # 0 = source bitrate (fps.py)
    overwrite: str = "overwrite"       # overwrite | skip | rename
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "queued"              # queued | running | done | failed | cancelled | skipped
    step: str = ""
    percent: float = 0.0
    elapsed: float = 0.0
    eta: float = 0.0
    codec: str = ""
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
            "state": self.state, "step": self.step, "detail": self.detail, "percent": round(self.percent, 1),
            "elapsed": fmt_time(elapsed), "eta": fmt_time(self.eta) if self.eta > 0 else "",
            "codec": self.codec, "bitrate": self.bitrate, "channels": self.channels,
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
        )
        job.state = d.get("state", "done")
        job.step = d.get("step", "")
        job.percent = 100.0 if job.state == "done" else 0.0
        job.codec, job.bitrate, job.channels = d.get("codec", ""), int(d.get("bitrate", 0)), int(d.get("channels", 0))
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


# ───────────────────────── MAIN CONVERSION (from fps.py) ─────────────────────────

def convert(job: Job, notify: ProgressFn, work_root: str = ".temp_jobs") -> Job:
    """Run one job to completion (or cancellation). Mutates and returns ``job``."""
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
                deew_out = os.path.join(work_dir, f"{base}{ext}")
                if dee_ok and os.path.exists(deew_out):
                    shutil.move(deew_out, out_path)
                    success = True
                elif dee_ok:
                    job.error = (f"deew reported success but {base}{ext} was not produced — "
                                 f"check the DEE path and the deew lines above")
    except Exception as exc:  # noqa: BLE001 - a job must never take the app down
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
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
    return job


def _remove(path: str) -> None:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
