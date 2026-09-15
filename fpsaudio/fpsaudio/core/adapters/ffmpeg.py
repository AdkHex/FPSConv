"""ffmpeg / ffprobe adapters.

ffmpeg is used for what it is genuinely best at — demuxing, decoding, and
container work — and never for the retime itself.  The retime happens in
:mod:`fpsaudio.core.dsp` with libsoxr, because a frame-rate change is a
resample and ``atempo`` is a pitch-preserving time-stretch (A-2, B-2).

Everything this adapter builds obeys three rules the legacy code broke:

* ``-map 0:<index> -vn -sn -dn`` on every extract, so no video track is ever
  silently re-encoded (A-4).
* ``-drc_scale 0`` on every Dolby decode, so dynamic range compression is not
  baked in (A-10, B-18).
* Intermediates are RF64 or W64, never plain RIFF WAV, because a 2-hour 7.1
  24-bit 48 kHz track is ~8.3 GB and overflows WAV's 4 GB header (B-8).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import Command, Refusal, RefusalCode, ToolCapabilities
from .base import BinaryAdapter, extract_flags

__all__ = ["FFmpegAdapter", "FFprobeAdapter", "INTERMEDIATE_SUFFIXES"]

#: In preference order.  W64 and RF64 both escape the 4 GB RIFF ceiling.
INTERMEDIATE_SUFFIXES: tuple[str, ...] = (".w64", ".wav")

_ENCODER_RE = re.compile(r"^\s*[A-Z.]{6}\s+(\S+)", re.MULTILINE)


@dataclass
class FFprobeAdapter(BinaryAdapter):
    name: str = "ffprobe"
    binary: str = "ffprobe"
    version_argv: tuple[str, ...] = ("-version",)
    help_argv: tuple[str, ...] = ("-h",)
    required_for: tuple[str, ...] = ("probing when MediaInfo is absent",)

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"ffprobe version (\S+)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def probe_argv(self, path: Path) -> tuple[str, ...]:
        return (
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        )

    def probe(self, path: Path) -> dict[str, Any]:
        outcome = self.run(
            self.command(self.probe_argv(path), purpose=f"probe {path.name}")
        )
        if not outcome.ok:
            raise RuntimeError(outcome.message)
        try:
            return json.loads(outcome.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"ffprobe returned malformed JSON: {exc}") from exc

    def count_samples(self, path: Path, stream_index: int = 0) -> int | None:
        """Exact decoded sample count, for the sample-count acceptance gate."""
        argv = (
            "-v", "error",
            "-select_streams", f"a:{stream_index}",
            "-count_frames", "-count_packets",
            "-show_entries", "stream=nb_read_frames,duration_ts,sample_rate,nb_frames",
            "-print_format", "json",
            str(path),
        )
        outcome = self.run(self.command(argv, purpose=f"count samples in {path.name}"))
        if not outcome.ok:
            return None
        try:
            payload = json.loads(outcome.stdout or "{}")
        except json.JSONDecodeError:
            return None
        streams = payload.get("streams") or []
        if not streams:
            return None
        value = streams[0].get("duration_ts")
        try:
            return int(value) if value not in (None, "N/A") else None
        except (TypeError, ValueError):
            return None


@dataclass
class FFmpegAdapter(BinaryAdapter):
    name: str = "ffmpeg"
    binary: str = "ffmpeg"
    version_argv: tuple[str, ...] = ("-version",)
    help_argv: tuple[str, ...] = ("-h",)
    required_for: tuple[str, ...] = ("demux", "decode", "container work")

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"ffmpeg version (\S+)", text or "")
        return match.group(1) if match else super().parse_version(text)

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        flags = set(extract_flags(help_text))
        features: set[str] = set()
        notes: list[str] = []

        path = self.which()
        if path:
            for kind, prefix in (("-encoders", "encoder:"), ("-decoders", "decoder:")):
                outcome = self._probe(path, ("-hide_banner", kind))
                for match in _ENCODER_RE.finditer(outcome.stdout or ""):
                    features.add(prefix + match.group(1))
            formats = self._probe(path, ("-hide_banner", "-formats"))
            blob = formats.stdout or ""
            for muxer in ("w64", "wav", "matroska", "mp4", "flac", "ogg", "caf"):
                if re.search(rf"\b{muxer}\b", blob):
                    features.add(f"format:{muxer}")
            wav_help = self._probe(path, ("-hide_banner", "-h", "muxer=wav"))
            if "rf64" in (wav_help.stdout or "") + (wav_help.stderr or ""):
                features.add("wav:rf64")
                flags.add("-rf64")

        if "format:w64" not in features and "wav:rf64" not in features:
            notes.append(
                "This ffmpeg advertises neither the W64 muxer nor the WAV rf64 option; "
                "multichannel feature-length intermediates would overflow 4 GB."
            )
        return ToolCapabilities(
            flags=frozenset(flags), features=frozenset(features), notes=tuple(notes)
        )

    # -- capability queries ------------------------------------------------ #

    def has_decoder(self, codec: str) -> bool:
        return self.detect().capabilities.has(f"decoder:{codec}")

    def has_encoder(self, codec: str) -> bool:
        return self.detect().capabilities.has(f"encoder:{codec}")

    def intermediate_suffix(self) -> str:
        """Pick a container for intermediates that cannot overflow (B-8).

        **RF64 is preferred over W64, and the order matters.**  ffmpeg's W64
        muxer writes a WAVE_FORMAT_EXTENSIBLE header for three or more channels
        that libsndfile misparses: it reports the stream as ``PCM_32`` when the
        payload is really ``pcm_f32le``, so every sample is read as a
        reinterpreted float bit pattern.  Measured on ffmpeg 9.0.1 with
        libsndfile 1.2.x: 2-channel W64 reads back correctly, 6- and 8-channel
        W64 do not.  RF64 reads correctly at every channel count and has the
        same freedom from the 4 GB RIFF ceiling.

        :class:`~fpsaudio.core.stages.decode.DecodeStage` additionally asserts
        that what it wrote reads back as float, so even a build that gets this
        wrong in some new way cannot corrupt a job silently.
        """
        caps = self.detect().capabilities
        if caps.has("wav:rf64"):
            return ".wav"
        if caps.has("format:w64"):
            return ".w64"
        raise Refusal(
            RefusalCode.TOOL_MISSING,
            "This ffmpeg build supports neither W64 nor RF64, so it cannot write an "
            "intermediate large enough for a feature-length multichannel track.",
            remedies=[
                "Install a full ffmpeg build (winget install --id Gyan.FFmpeg -e).",
                "Run `fpsaudio doctor --verbose` to see the detected muxers.",
            ],
        )

    # -- argv construction ------------------------------------------------- #

    def _base_argv(self, *, overwrite: bool, progress: bool) -> list[str]:
        argv = ["-hide_banner", "-nostdin", "-loglevel", "error"]
        argv.append("-y" if overwrite else "-n")
        if progress:
            argv += ["-progress", "pipe:1", "-stats_period", "0.25"]
        return argv

    def decoder_options(
        self, codec: str, *, drc_scale: float = 0.0, ignore_dialnorm: bool = True
    ) -> list[str]:
        """Per-decoder options applied *before* ``-i``.

        ``-drc_scale 0`` disables the dynamic-range compression the Dolby
        decoders otherwise apply.  §3.3 calls this the single most common
        quality bug in DD+ conversion; it appears nowhere in either legacy
        program.  ``-target_level 0`` means "no target-level normalisation",
        which is how the dialnorm-driven gain is kept out of the decode.
        """
        if codec not in ("ac3", "eac3", "truehd"):
            return []
        options = ["-drc_scale", _fmt_float(drc_scale)]
        if ignore_dialnorm and codec in ("ac3", "eac3"):
            options += ["-target_level", "0"]
        return options

    def demux_copy_command(
        self,
        source: Path,
        stream_index: int,
        destination: Path,
        *,
        overwrite: bool = True,
    ) -> Command:
        """Bit-exact stream copy — no decode, no re-encode, no video (B-D)."""
        argv = self._base_argv(overwrite=overwrite, progress=True)
        argv += [
            "-i", str(source),
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            "-c:a", "copy",
            str(destination),
        ]
        return self.command(
            argv,
            purpose=f"copy stream {stream_index} out of {source.name} without re-encoding",
        )

    def decode_command(
        self,
        source: Path,
        stream_index: int,
        destination: Path,
        *,
        codec: str,
        sample_format: str = "pcm_f32le",
        drc_scale: float = 0.0,
        ignore_dialnorm: bool = True,
        overwrite: bool = True,
        channel_layout: str | None = None,
    ) -> Command:
        """Decode one stream to a float32 RF64/W64 intermediate.

        32-bit float end to end: the source is quantised exactly once, at the
        final encode.  The legacy HQ engines hardcoded ``pcm_s24le`` and so
        requantised without dither twice per job (B-9).
        """
        argv = self._base_argv(overwrite=overwrite, progress=True)
        argv += self.decoder_options(
            codec, drc_scale=drc_scale, ignore_dialnorm=ignore_dialnorm
        )
        argv += [
            "-i", str(source),
            "-map", f"0:{stream_index}",
            "-vn", "-sn", "-dn",
            "-c:a", sample_format,
        ]
        if channel_layout:
            argv += ["-channel_layout", channel_layout]
        if destination.suffix.lower() == ".wav" and self.detect().capabilities.has("wav:rf64"):
            argv += ["-rf64", "auto"]
        argv.append(str(destination))
        return self.command(
            argv, purpose=f"decode stream {stream_index} of {source.name} to float32"
        )

    def encode_command(
        self,
        source: Path,
        destination: Path,
        *,
        codec: str,
        bitrate: str | None = None,
        quality: str | None = None,
        sample_rate: int | None = None,
        sample_format: str | None = None,
        extra: Sequence[str] = (),
        overwrite: bool = True,
    ) -> Command:
        argv = self._base_argv(overwrite=overwrite, progress=True)
        argv += ["-i", str(source), "-vn", "-sn", "-dn", "-c:a", codec]
        if bitrate:
            argv += ["-b:a", bitrate]
        if quality:
            argv += ["-q:a", quality]
        if sample_rate:
            argv += ["-ar", str(sample_rate)]
        if sample_format:
            argv += ["-sample_fmt", sample_format]
        argv += list(extra)
        argv.append(str(destination))
        return self.command(argv, purpose=f"encode to {codec}")

    def redeclare_command(
        self,
        source: Path,
        destination: Path,
        *,
        new_sample_rate: int,
        input_sample_rate: int,
        overwrite: bool = True,
    ) -> Command:
        """Reinterpret raw PCM at a new sample rate — the bit-exact retime.

        The payload is not touched: ``-f <fmt> -ar <new>`` re-reads the same
        bytes with a different declared rate.  This is §3.2's lossless retime,
        which does not exist in either legacy program (A-12).
        """
        argv = self._base_argv(overwrite=overwrite, progress=True)
        argv += [
            "-ar", str(new_sample_rate),
            "-i", str(source),
            "-c:a", "copy",
            str(destination),
        ]
        return self.command(
            argv,
            purpose=(
                f"redeclare {input_sample_rate} Hz as {new_sample_rate} Hz "
                f"(payload untouched)"
            ),
        )

    def md5_command(self, source: Path, *, stream_index: int = 0) -> Command:
        """Decode to PCM and hash it — the bit-perfect assertion for §6."""
        argv = [
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", str(source),
            "-map", f"0:a:{stream_index}",
            "-vn", "-sn", "-dn",
            "-f", "md5", "-",
        ]
        return self.command(
            argv, purpose=f"PCM MD5 of {source.name}", stdout_is_data=True
        )

    def loudness_command(self, source: Path) -> Command:
        argv = [
            "-hide_banner", "-nostdin",
            "-i", str(source),
            "-map", "0:a:0",
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ]
        return self.command(argv, purpose=f"EBU R128 loudness of {source.name}")

    # -- progress ---------------------------------------------------------- #

    def parse_progress(self, line: str, state: dict[str, Any]) -> float | None:
        """Parse the ``-progress pipe:1`` key/value stream.

        Ported in logic from
        ``Fps Converter Batch Mode/FPS Converter/core/ffmpeg_runner.py:379-442``
        — the one runner detail the legacy build got right — with the
        denominator fixed.  B-20: the legacy code divided by the *source*
        duration even for a retimed encode, so progress was off by up to 4.3%.
        Here the caller sets ``state["duration_s"]`` to the duration of the
        thing actually being written.
        """
        text = line.strip()
        if not text or "=" not in text:
            return None
        key, value = text.split("=", 1)
        state[key] = value
        if key != "progress":
            return None

        duration = state.get("duration_s")
        try:
            duration = float(duration) if duration else 0.0
        except (TypeError, ValueError):
            duration = 0.0
        if duration <= 0:
            return None

        processed: float | None = None
        for field_name, divisor in (("out_time_us", 1_000_000), ("out_time_ms", 1_000_000)):
            raw = state.get(field_name)
            if raw and str(raw).lstrip("-").isdigit():
                processed = int(raw) / divisor
                break
        if processed is None:
            return None
        if value.strip() == "end":
            return 100.0
        return max(0.0, min(100.0, processed / duration * 100.0))


def _fmt_float(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return repr(value)
