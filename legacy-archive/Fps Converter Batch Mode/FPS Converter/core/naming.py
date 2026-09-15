from __future__ import annotations

from pathlib import Path


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    base = path.stem
    suffix = path.suffix
    parent = path.parent
    i = 1
    while True:
        candidate = parent / f"{base}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def build_output_path(
    *,
    output_dir: Path,
    source_name: str,
    stream_index: int,
    profile_slug: str,
    extension: str,
    overwrite_policy: str,
) -> tuple[Path, str]:
    ensure_output_dir(output_dir)
    ext = extension.lstrip(".")
    safe_profile = profile_slug.replace(".", "_")
    stem = f"{source_name}__a{stream_index}"
    if safe_profile != "none":
        stem += f"__{safe_profile}"
    target = output_dir / f"{stem}.{ext}"

    if target.exists():
        if overwrite_policy == "skip":
            return target, "skip"
        if overwrite_policy == "rename":
            return unique_path(target), "write"
    return target, "write"

