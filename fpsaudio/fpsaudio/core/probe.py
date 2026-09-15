"""Normalised media probing.

Rewritten rather than reused.  The legacy model (B-6) captured index, codec,
channels, sample rate, bitrate, language, title and duration — and discarded
``profile``, ``channel_layout``, ``bits_per_raw_sample``, ``disposition``,
``start_time`` and all side data.  That made DTS-HD MA indistinguishable from
DTS core, TrueHD Atmos indistinguishable from plain TrueHD, and E-AC-3 JOC
invisible, which in turn made §3.5's Atmos requirement unreachable.

Two things this module refuses to do:

* **Gate a scan on a file-extension allowlist.**  B-5 is a folder of ``.thd``
  files scanning to zero jobs with no message.  Here every regular file is
  offered to the prober, and anything the prober cannot identify is surfaced as
  an explicit ``identified=False`` entry rather than vanishing.
* **Guess.**  Atmos findings carry a ``certainty``; nothing destructive is ever
  decided from a guess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts import (
    AtmosInfo,
    AudioStream,
    Chapter,
    MediaFile,
    Refusal,
    RefusalCode,
)

__all__ = [
    "LOSSLESS_CODECS",
    "OBJECT_AUDIO_CODECS",
    "SKIP_SUFFIXES",
    "canonical_codec",
    "discover_files",
    "normalize_ffprobe",
    "normalize_mediainfo",
    "probe_file",
    "probe_many",
]


# --------------------------------------------------------------------------- #
# Codec identification
# --------------------------------------------------------------------------- #

#: MediaInfo ``Format`` / ffprobe ``codec_name`` → canonical id.
_CODEC_MAP: dict[str, str] = {
    # Dolby
    "ac-3": "ac3",
    "ac3": "ac3",
    "e-ac-3": "eac3",
    "eac3": "eac3",
    "e-ac-3 joc": "eac3",
    "truehd": "truehd",
    "mlp fba": "truehd",
    "mlp": "truehd",
    # DTS
    "dts": "dts",
    "dts-hd": "dts",
    "dca": "dts",
    # Lossless / uncompressed
    "flac": "flac",
    "alac": "alac",
    "wavpack": "wavpack",
    "wv": "wavpack",
    "tta": "tta",
    "tak": "tak",
    "monkey's audio": "ape",
    "pcm": "pcm",
    "pcm_s16le": "pcm",
    "pcm_s24le": "pcm",
    "pcm_s32le": "pcm",
    "pcm_f32le": "pcm",
    "pcm_s16be": "pcm",
    "pcm_s24be": "pcm",
    "pcm_bluray": "pcm",
    "pcm_dvd": "pcm",
    # Lossy
    "aac": "aac",
    "aac lc": "aac",
    "he-aac": "aac",
    "mpeg audio": "mp3",
    "mp3": "mp3",
    "mp2": "mp2",
    "opus": "opus",
    "vorbis": "vorbis",
    "wma": "wma",
}

LOSSLESS_CODECS: frozenset[str] = frozenset(
    {"truehd", "flac", "alac", "wavpack", "tta", "tak", "ape", "pcm"}
)

#: Codecs that can carry object audio.  Presence here does not mean objects are
#: *present*; that is what :class:`AtmosInfo` decides.
OBJECT_AUDIO_CODECS: frozenset[str] = frozenset({"truehd", "eac3", "dts"})

#: Files never worth probing.  This is a *noise* filter, not a capability gate:
#: anything not listed here is probed, and unidentified results are reported.
SKIP_SUFFIXES: frozenset[str] = frozenset(
    {
        ".txt", ".md", ".nfo", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
        ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup",
        ".zip", ".rar", ".7z", ".tar", ".gz", ".xz",
        ".exe", ".dll", ".so", ".dylib", ".msi",
        ".py", ".pyc", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".log",
        ".ds_store", ".url", ".lnk", ".sfv", ".par2", ".torrent", ".pdf",
    }
)


def canonical_codec(raw: str | None) -> str:
    if not raw:
        return "unknown"
    key = str(raw).strip().lower()
    if key in _CODEC_MAP:
        return _CODEC_MAP[key]
    if key.startswith("pcm"):
        return "pcm"
    if "truehd" in key or key.startswith("mlp"):
        return "truehd"
    if "e-ac-3" in key or "eac3" in key:
        return "eac3"
    if "ac-3" in key or key == "ac3":
        return "ac3"
    if "dts" in key:
        return "dts"
    if "aac" in key:
        return "aac"
    return key.replace(" ", "_")


def _is_lossless(codec: str, profile: str | None, commercial: str | None) -> bool:
    if codec in LOSSLESS_CODECS:
        return True
    haystack = f"{profile or ''} {commercial or ''}".lower()
    # DTS-HD MA is lossless; DTS core and DTS-HD HRA are not.
    if codec == "dts" and ("master audio" in haystack or re.search(r"\bma\b", haystack)):
        return True
    return False


# --------------------------------------------------------------------------- #
# Atmos / object-audio detection
# --------------------------------------------------------------------------- #

def _detect_atmos_mediainfo(track: Mapping[str, Any], codec: str) -> AtmosInfo:
    evidence: list[str] = []
    commercial = str(track.get("Format_Commercial_IfAny") or "")
    additional = str(track.get("Format_AdditionalFeatures") or "")
    profile = str(track.get("Format_Profile") or "")
    objects_raw = track.get("NumberOfDynamicObjects")
    bed_raw = track.get("BedChannelCount")

    haystack = f"{commercial} {additional} {profile}".lower()

    kind: str | None = None
    if "atmos" in haystack:
        evidence.append(f"Format_Commercial_IfAny={commercial!r}")
        kind = "truehd_atmos" if codec == "truehd" else "eac3_joc"
    if "joc" in haystack:
        evidence.append(f"Format_AdditionalFeatures={additional!r}")
        kind = "eac3_joc"
    if objects_raw not in (None, "", "0"):
        evidence.append(f"NumberOfDynamicObjects={objects_raw}")
        kind = kind or ("truehd_atmos" if codec == "truehd" else "eac3_joc")
    if codec == "dts" and ("dts:x" in haystack or "xll x" in haystack or " x " in f" {additional.lower()} "):
        evidence.append(f"Format_AdditionalFeatures={additional!r}")
        kind = "dtsx"

    if not kind:
        return AtmosInfo(present=False, certainty="absent")

    return AtmosInfo(
        present=True,
        kind=kind,
        objects=_as_int(objects_raw),
        bed_channels=_as_int(bed_raw),
        certainty="confirmed",
        evidence=tuple(evidence),
    )


def _detect_atmos_ffprobe(stream: Mapping[str, Any], codec: str) -> AtmosInfo:
    """ffprobe is a much weaker witness than MediaInfo for object audio.

    ffprobe reports ``profile`` for TrueHD/DTS but has no JOC indicator for
    E-AC-3 at all.  Rather than declare "no Atmos" — which is how a JOC track
    gets silently flattened — an E-AC-3 stream probed only by ffprobe is
    reported as ``certainty="unknown"``, and the planner treats unknown as
    "must not act destructively".
    """
    profile = str(stream.get("profile") or "")
    haystack = profile.lower()
    evidence: list[str] = []

    if "atmos" in haystack:
        evidence.append(f"profile={profile!r}")
        return AtmosInfo(
            present=True,
            kind="truehd_atmos" if codec == "truehd" else "eac3_joc",
            certainty="confirmed",
            evidence=tuple(evidence),
        )
    if codec == "dts" and ("dts:x" in haystack or "x " in haystack):
        return AtmosInfo(
            present=True, kind="dtsx", certainty="likely",
            evidence=(f"profile={profile!r}",),
        )
    for side in stream.get("side_data_list") or ():
        stype = str(side.get("side_data_type", "")).lower()
        if "joc" in stype or "atmos" in stype:
            return AtmosInfo(
                present=True, kind="eac3_joc", certainty="confirmed",
                evidence=(f"side_data_type={stype!r}",),
            )

    if codec in ("eac3", "truehd"):
        return AtmosInfo(
            present=False,
            certainty="unknown",
            evidence=("ffprobe cannot report JOC/Atmos presence; install MediaInfo",),
        )
    return AtmosInfo(present=False, certainty="absent")


# --------------------------------------------------------------------------- #
# Scalar coercion
# --------------------------------------------------------------------------- #

def _as_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(str(value).replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "yes", "true"}


def _first(track: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = track.get(key)
        if value not in (None, "", "N/A"):
            return value
    return None


# --------------------------------------------------------------------------- #
# MediaInfo JSON  →  MediaFile
# --------------------------------------------------------------------------- #

def normalize_mediainfo(payload: Mapping[str, Any], path: Path) -> MediaFile:
    """Normalise ``mediainfo --Output=JSON`` output.

    MediaInfo is the preferred prober precisely because it is the only one of
    the two that can see Atmos/JOC (Part 5 of the plan).
    """
    media = payload.get("media") or {}
    tracks: Sequence[Mapping[str, Any]] = media.get("track") or []

    general: Mapping[str, Any] = {}
    audio_tracks: list[Mapping[str, Any]] = []
    has_video = False
    for track in tracks:
        kind = str(track.get("@type", "")).lower()
        if kind == "general":
            general = track
        elif kind == "audio":
            audio_tracks.append(track)
        elif kind == "video":
            has_video = True

    streams: list[AudioStream] = []
    for ordinal, track in enumerate(audio_tracks):
        # MediaInfo's StreamOrder is the container-wide index ffmpeg's -map
        # wants.  ID is 1-based and container-specific; fall back only if
        # StreamOrder is absent, and record which we used.
        stream_index = _as_int(_first(track, "StreamOrder"))
        if stream_index is None:
            stream_index = _as_int(_first(track, "ID"))
            if stream_index is not None:
                stream_index -= 1
        if stream_index is None:
            stream_index = ordinal

        fmt = _first(track, "Format")
        codec = canonical_codec(fmt)
        profile = _first(track, "Format_Profile", "Format_AdditionalFeatures")
        commercial = _first(track, "Format_Commercial_IfAny", "Format_Commercial")
        channels = _as_int(_first(track, "Channels"))
        layout = _first(track, "ChannelLayout", "ChannelPositions")
        rate = _as_int(_first(track, "SamplingRate"))
        depth = _as_int(_first(track, "BitDepth"))
        bitrate = _as_int(_first(track, "BitRate", "BitRate_Nominal", "BitRate_Maximum"))
        samples = _as_int(_first(track, "SamplingCount"))
        delay = _as_float(_first(track, "Delay"))
        video_delay = _as_float(_first(track, "Video_Delay"))

        atmos = _detect_atmos_mediainfo(track, codec)
        streams.append(
            AudioStream(
                stream_index=stream_index,
                codec=codec,
                codec_long=str(fmt) if fmt else None,
                profile=str(profile) if profile else None,
                commercial_name=str(commercial) if commercial else None,
                channels=channels,
                channel_layout=str(layout) if layout else None,
                sample_rate=rate,
                bit_depth=depth,
                bit_rate_bps=bitrate,
                bit_rate_mode=_first(track, "BitRate_Mode"),
                duration_s=_as_float(_first(track, "Duration")),
                start_time_s=(delay if delay is not None else video_delay),
                codec_delay_ns=None,
                sample_count=samples,
                language=_first(track, "Language"),
                title=_first(track, "Title"),
                default=_as_bool(track.get("Default")),
                forced=_as_bool(track.get("Forced")),
                lossless=_is_lossless(codec, str(profile or ""), str(commercial or "")),
                atmos=atmos,
                source_tool="mediainfo",
                identified=codec != "unknown",
                raw=dict(track),
            )
        )

    chapters = _chapters_from_mediainfo(tracks)
    container = str(_first(general, "Format") or path.suffix.lstrip(".")).lower()
    return MediaFile(
        path=path,
        container=container,
        format_name=str(_first(general, "Format") or ""),
        duration_s=_as_float(_first(general, "Duration")),
        size_bytes=_as_int(_first(general, "FileSize")),
        has_video=has_video,
        audio=tuple(streams),
        chapters=chapters,
        source_tool="mediainfo",
        identified=bool(streams) or bool(general),
        problems=() if streams else ("no audio tracks reported",),
    )


def _chapters_from_mediainfo(tracks: Sequence[Mapping[str, Any]]) -> tuple[Chapter, ...]:
    for track in tracks:
        if str(track.get("@type", "")).lower() != "menu":
            continue
        extra = track.get("extra") or {}
        chapters: list[Chapter] = []
        for idx, (stamp, title) in enumerate(extra.items()):
            seconds = _timecode_to_seconds(stamp)
            if seconds is None:
                continue
            chapters.append(Chapter(index=idx, start_s=seconds, end_s=seconds, title=str(title)))
        if chapters:
            return tuple(chapters)
    return ()


_TIMECODE_RE = re.compile(r"_?(\d{2})_(\d{2})_(\d{2})[._](\d{1,3})")


def _timecode_to_seconds(stamp: str) -> float | None:
    match = _TIMECODE_RE.search(str(stamp))
    if not match:
        return None
    hh, mm, ss, ms = (int(g) for g in match.groups())
    return hh * 3600 + mm * 60 + ss + ms / 1000.0


# --------------------------------------------------------------------------- #
# ffprobe JSON  →  MediaFile
# --------------------------------------------------------------------------- #

def normalize_ffprobe(payload: Mapping[str, Any], path: Path) -> MediaFile:
    fmt = payload.get("format") or {}
    streams: Sequence[Mapping[str, Any]] = payload.get("streams") or []

    audio: list[AudioStream] = []
    has_video = False
    for stream in streams:
        if stream.get("codec_type") == "video":
            has_video = True
            continue
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        codec = canonical_codec(stream.get("codec_name"))
        profile = stream.get("profile")
        duration = _as_float(stream.get("duration")) or _as_float(fmt.get("duration"))
        rate = _as_int(stream.get("sample_rate"))
        samples = _as_int(stream.get("duration_ts")) if rate else None

        audio.append(
            AudioStream(
                stream_index=_as_int(stream.get("index")) or 0,
                codec=codec,
                codec_long=stream.get("codec_long_name"),
                profile=str(profile) if profile not in (None, "unknown", -99) else None,
                commercial_name=None,
                channels=_as_int(stream.get("channels")),
                channel_layout=stream.get("channel_layout"),
                sample_rate=rate,
                bit_depth=_as_int(
                    stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")
                ),
                bit_rate_bps=_as_int(stream.get("bit_rate")),
                bit_rate_mode=None,
                duration_s=duration,
                start_time_s=_as_float(stream.get("start_time")),
                codec_delay_ns=_as_int(stream.get("initial_padding")),
                sample_count=samples,
                language=tags.get("language"),
                title=tags.get("title"),
                default=bool(disposition.get("default")),
                forced=bool(disposition.get("forced")),
                lossless=_is_lossless(codec, str(profile or ""), None),
                atmos=_detect_atmos_ffprobe(stream, codec),
                source_tool="ffprobe",
                identified=codec != "unknown",
                raw=dict(stream),
            )
        )

    chapters = tuple(
        Chapter(
            index=idx,
            start_s=_as_float(ch.get("start_time")) or 0.0,
            end_s=_as_float(ch.get("end_time")) or 0.0,
            title=(ch.get("tags") or {}).get("title"),
        )
        for idx, ch in enumerate(payload.get("chapters") or ())
    )

    return MediaFile(
        path=path,
        container=str(fmt.get("format_name") or path.suffix.lstrip(".")).lower(),
        format_name=fmt.get("format_long_name") or fmt.get("format_name"),
        duration_s=_as_float(fmt.get("duration")),
        size_bytes=_as_int(fmt.get("size")),
        has_video=has_video,
        audio=tuple(audio),
        chapters=chapters,
        source_tool="ffprobe",
        identified=bool(streams),
        problems=() if audio else ("no audio streams reported",),
    )


# --------------------------------------------------------------------------- #
# Probing entry points
# --------------------------------------------------------------------------- #

def probe_file(path: Path, *, prefer: str = "mediainfo", registry: Any = None) -> MediaFile:
    """Probe one file, preferring MediaInfo and falling back to ffprobe.

    Returns an ``identified=False`` :class:`MediaFile` rather than raising when
    no prober can make sense of the file, so a batch scan can report "3 files
    unidentified" instead of dying or — worse, B-5 — silently skipping them.
    """
    from .adapters import get_registry  # local import: core stays import-light

    reg = registry if registry is not None else get_registry()
    order = ["mediainfo", "ffprobe"] if prefer == "mediainfo" else ["ffprobe", "mediainfo"]

    problems: list[str] = []
    for name in order:
        adapter = reg.get(name)
        if adapter is None or not adapter.detect().found:
            problems.append(f"{name}: not installed")
            continue
        try:
            payload = adapter.probe(path)
        except Exception as exc:  # noqa: BLE001 - a prober failing is data, not a crash
            problems.append(f"{name}: {exc}")
            continue
        media = (
            normalize_mediainfo(payload, path)
            if name == "mediainfo"
            else normalize_ffprobe(payload, path)
        )
        if media.audio:
            return media
        problems.append(f"{name}: reported no audio streams")

    if not problems:
        problems.append("no prober available")
    return MediaFile(
        path=path,
        container=path.suffix.lstrip(".").lower() or "unknown",
        audio=(),
        source_tool="none",
        identified=False,
        problems=tuple(problems),
    )


def probe_many(paths: Iterable[Path], *, prefer: str = "mediainfo") -> Iterator[MediaFile]:
    for path in paths:
        yield probe_file(path, prefer=prefer)


def discover_files(root: Path, *, recursive: bool = False) -> list[Path]:
    """List candidate files.

    No extension allowlist (B-5).  Only obvious non-media noise is filtered,
    and the caller learns about anything unidentified from ``probe_file``.
    """
    if not root.exists():
        raise Refusal(
            RefusalCode.UNIDENTIFIED_SOURCE,
            f"Input path does not exist: {root}",
        )
    if root.is_file():
        return [root]
    iterator = root.rglob("*") if recursive else root.iterdir()
    files = [
        p
        for p in iterator
        if p.is_file()
        and p.suffix.lower() not in SKIP_SUFFIXES
        and not p.name.startswith(".")
    ]
    return sorted(files, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def load_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"prober returned malformed JSON: {exc}") from exc
