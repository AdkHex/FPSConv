"""Token-template output naming.

Generalised from ``Fps Converter Batch Mode/FPS Converter/core/naming.py:24-46``,
whose ``build_output_path`` / ``unique_path`` / overwrite-policy triad was
sound in shape.  Two defects are fixed here:

* **The ``mkdir`` side effect is gone.**  The legacy ``build_output_path`` called
  ``ensure_output_dir`` from what reads as a pure naming function, so merely
  *planning* a job created directories — which made ``--dry-run`` impossible to
  implement honestly (B-13).  Naming is now pure; :func:`claim` is the one
  function that touches the filesystem, and it says so.

* **The ``unique_path`` TOCTOU race is gone.**  The legacy loop asked "does
  ``_1`` exist?" and then returned it, so two workers in the thread pool could
  both observe ``_1`` as free and both claim it (B-13).  :func:`claim` uses an
  atomic ``O_CREAT | O_EXCL`` open, so exactly one worker can win.
"""

from __future__ import annotations

import errno
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .contracts import AudioStream, Refusal, RefusalCode

__all__ = [
    "DEFAULT_TEMPLATE",
    "NameResult",
    "TOKENS",
    "build_name",
    "claim",
    "render_template",
    "resolve_output",
    "sanitize",
]

DEFAULT_TEMPLATE = "{stem}__a{index}__{profile}"

#: Every token the template understands, with a one-line description for
#: ``fpsaudio presets --tokens`` and the TUI's template editor.
TOKENS: dict[str, str] = {
    "stem": "source filename without extension",
    "index": "audio stream index within the source",
    "profile": "retime preset key, e.g. 23_976_to_25",
    "srcfps": "source frame rate, e.g. 23.976",
    "dstfps": "target frame rate, e.g. 25",
    "codec": "output codec id, e.g. flac",
    "srccodec": "source codec id, e.g. truehd",
    "lang": "stream language tag, or 'und'",
    "channels": "channel count, e.g. 8",
    "layout": "channel layout, e.g. 7.1",
    "rate": "output sample rate in Hz",
    "depth": "output bit depth, or 'float'",
    "title": "stream title, sanitised, or empty",
    "parent": "name of the source's parent directory",
    "method": "retime method: redeclare, resample or stretch",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows refuses these names in any directory, with or without an extension.
_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize(text: str, *, replacement: str = "_") -> str:
    """Make a string safe as a Windows path component.

    The runtime target is Windows only, so this enforces Windows' rules even
    when running on the dev machine: reserved device names, no trailing dot or
    space, no reserved characters.
    """
    cleaned = _ILLEGAL.sub(replacement, str(text)).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        return "untitled"
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:180]


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        raise KeyError(key)


def render_template(template: str, values: Mapping[str, object]) -> str:
    """Render a token template, refusing unknown tokens rather than guessing."""
    try:
        return string.Formatter().vformat(template, (), _SafeDict(values))
    except KeyError as exc:
        unknown = str(exc).strip("'")
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Output template uses unknown token {{{unknown}}}.",
            remedies=[
                "Available tokens: " + ", ".join(f"{{{t}}}" for t in sorted(TOKENS)),
                "Run `fpsaudio presets --tokens` for what each one means.",
            ],
        ) from exc
    except (IndexError, ValueError) as exc:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Output template {template!r} is malformed: {exc}",
        ) from exc


def build_name(
    *,
    template: str,
    source: Path,
    stream: AudioStream,
    profile_key: str,
    codec: str,
    method: str,
    sample_rate: int | None = None,
    bit_depth: int | None = None,
    extra: Mapping[str, object] | None = None,
) -> str:
    """Render the output stem.  Pure: touches nothing on disk."""
    values: dict[str, object] = {
        "stem": sanitize(source.stem),
        "index": stream.stream_index,
        "profile": profile_key.replace(".", "_"),
        "srcfps": "",
        "dstfps": "",
        "codec": codec,
        "srccodec": stream.codec,
        "lang": stream.language or "und",
        "channels": stream.channels or 0,
        "layout": sanitize(stream.channel_layout or "") or "unknown",
        "rate": sample_rate or stream.sample_rate or 0,
        "depth": bit_depth if bit_depth else "float",
        "title": sanitize(stream.title) if stream.title else "",
        "parent": sanitize(source.parent.name) if source.parent.name else "",
        "method": method,
    }
    values.update(extra or {})
    return sanitize(render_template(template, values))


@dataclass(frozen=True, slots=True)
class NameResult:
    path: Path
    action: str  # write | skip | fail
    reason: str = ""

    @property
    def should_write(self) -> bool:
        return self.action == "write"


def resolve_output(
    *,
    directory: Path,
    stem: str,
    extension: str,
    overwrite: str = "skip",
) -> NameResult:
    """Decide the output path under an overwrite policy.  Still pure.

    ``rename`` returns the first free candidate as a *suggestion*; the actual
    guarantee of uniqueness comes from :func:`claim`, which is atomic.
    """
    # Validate the policy before looking at the filesystem, so a typo fails the
    # same way whether or not the output happens to exist yet.
    if overwrite not in _OVERWRITE_POLICIES:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Unknown overwrite policy {overwrite!r}.",
            remedies=["Use one of: " + ", ".join(sorted(_OVERWRITE_POLICIES)) + "."],
        )

    ext = extension.lstrip(".")
    target = directory / f"{stem}.{ext}"

    if not target.exists():
        return NameResult(target, "write")

    if overwrite == "overwrite":
        return NameResult(target, "write", "existing file will be replaced")
    if overwrite == "skip":
        return NameResult(target, "skip", "output already exists")
    if overwrite == "fail":
        return NameResult(target, "fail", "output already exists and policy is 'fail'")
    return NameResult(_next_free(target), "write", "renamed to avoid an existing file")


#: Every accepted value of ``--overwrite``.
_OVERWRITE_POLICIES = frozenset({"skip", "overwrite", "rename", "fail"})


def _next_free(path: Path) -> Path:
    parent, stem, suffix = path.parent, path.stem, path.suffix
    index = 1
    while True:
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def claim(path: Path, *, overwrite: str = "skip") -> Path:
    """Atomically reserve an output path.  **This one touches the filesystem.**

    Creates the parent directory and the file itself with ``O_CREAT | O_EXCL``,
    so two workers racing for the same name cannot both win — the loser gets
    ``EEXIST`` and moves to the next candidate.  The legacy check-then-use
    pattern had no such guarantee (B-13).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if overwrite == "overwrite":
        return path

    candidate = path
    index = 0
    while True:
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if overwrite != "rename":
                raise Refusal(
                    RefusalCode.UNSUPPORTED_OPERATION,
                    f"Output already exists: {candidate}",
                    remedies=[
                        "Use --overwrite to replace it, or --rename to write alongside it.",
                    ],
                )
            index += 1
            candidate = path.parent / f"{path.stem}_{index}{path.suffix}"
            continue
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                index += 1
                candidate = path.parent / f"{path.stem}_{index}{path.suffix}"
                continue
            raise Refusal(
                RefusalCode.UNSUPPORTED_OPERATION,
                f"Cannot create {candidate}: {exc}",
                remedies=["Check the output directory exists and is writable."],
            ) from exc
        os.close(handle)
        return candidate
