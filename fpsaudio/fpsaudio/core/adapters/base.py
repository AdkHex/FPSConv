"""The adapter contract: detect / version / capabilities / build_argv / parse_progress.

One module per external binary.  Adapters are the *only* place in the codebase
that knows a tool's command-line syntax, and they learn that syntax from the
binary itself:

* :meth:`BinaryAdapter.detect` runs the version and help commands once and
  caches the result.  It catches ``FileNotFoundError`` — the exact defect that
  made the legacy ``check_dependencies()`` incapable of ever reporting a
  missing dependency, and that crashed the GUI on startup (B-14).
* :attr:`DetectResult.capabilities` holds the flags actually observed in
  ``--help``.  Adapters consult it instead of trying a flag and retrying on
  failure, which is the B-10 defect that turned real errors into silent retries.

Nothing here invents a flag.  If a capability cannot be confirmed, the adapter
says so and the planner refuses rather than hoping.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..contracts import (
    Command,
    DetectResult,
    Refusal,
    RefusalCode,
    ToolCapabilities,
)

__all__ = [
    "BinaryAdapter",
    "LineSink",
    "RunOutcome",
    "extract_flags",
]

LineSink = Callable[[str], None]

#: How long a version/help probe may take before we call the tool broken.
DETECT_TIMEOUT_S = 20.0

_FLAG_RE = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)")


def extract_flags(text: str) -> frozenset[str]:
    """Every option-looking token in a help dump.

    Deliberately generous: a false positive only means we believe a tool
    supports something it advertises, while a false negative would make us
    refuse work the tool can do.  Semantic checks live in ``features``.
    """
    return frozenset(_FLAG_RE.findall(text or ""))


@dataclass(slots=True)
class RunOutcome:
    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def message(self) -> str:
        return (
            self.error
            or self.stderr.strip()
            or self.stdout.strip()
            or f"exited with code {self.returncode}"
        )


@dataclass
class BinaryAdapter:
    """Base class for every external-binary adapter."""

    #: Stable adapter id used in Command.adapter, doctor output and manifests.
    name: str = "unnamed"
    #: Executable name looked up on PATH, or an absolute path.
    binary: str = ""
    #: Alternative executable names to try, in order.
    aliases: tuple[str, ...] = ()
    version_argv: tuple[str, ...] = ("--version",)
    help_argv: tuple[str, ...] = ("--help",)
    #: Some tools (ffmpeg, flac) print help/version on stderr.
    #: Some (fdkaac, rubberband) exit non-zero from --help; that is not an error.
    version_ok_codes: tuple[int, ...] = (0, 1, 2, 64, 255)
    required_for: tuple[str, ...] = ()
    #: Optional override from config, e.g. tools.ffmpeg = "C:/ffmpeg/bin/ffmpeg.exe"
    override_path: str | None = None

    _cached: DetectResult | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # -- discovery --------------------------------------------------------- #

    def candidate_names(self) -> tuple[str, ...]:
        if self.override_path:
            return (self.override_path,)
        return (self.binary, *self.aliases)

    def which(self) -> str | None:
        for candidate in self.candidate_names():
            if not candidate:
                continue
            if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
                if Path(candidate).exists():
                    return candidate
                continue
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def detect(self, *, refresh: bool = False) -> DetectResult:
        with self._lock:
            if self._cached is not None and not refresh:
                return self._cached
            self._cached = self._detect_uncached()
            return self._cached

    def _detect_uncached(self) -> DetectResult:
        path = self.which()
        if path is None:
            return DetectResult(
                name=self.name,
                found=False,
                error=f"'{self.binary}' not found on PATH",
                required_for=self.required_for,
            )

        version_out = self._probe(path, self.version_argv)
        help_out = self._probe(path, self.help_argv) if self.help_argv else RunOutcome(True, 0)

        if version_out.error and not version_out.stdout and not version_out.stderr:
            return DetectResult(
                name=self.name,
                found=False,
                path=path,
                error=version_out.error,
                required_for=self.required_for,
            )

        version_text = f"{version_out.stdout}\n{version_out.stderr}"
        help_text = f"{help_out.stdout}\n{help_out.stderr}"
        return DetectResult(
            name=self.name,
            found=True,
            path=path,
            version=self.parse_version(version_text),
            capabilities=self.parse_capabilities(help_text, version_text),
            required_for=self.required_for,
        )

    def _probe(self, path: str, argv: Sequence[str]) -> RunOutcome:
        try:
            proc = subprocess.run(  # noqa: S603
                [path, *argv],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=DETECT_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            # The bug B-14 documents: subprocess raises here, it does not return
            # a non-zero code, so a returncode check can never see it.
            return RunOutcome(False, 127, error=f"{path} disappeared: {exc}")
        except PermissionError as exc:
            return RunOutcome(False, 126, error=f"{path} is not executable: {exc}")
        except OSError as exc:
            return RunOutcome(False, 126, error=f"{path} could not be run: {exc}")
        except subprocess.TimeoutExpired:
            return RunOutcome(
                False, 124, error=f"{path} did not respond within {DETECT_TIMEOUT_S:g}s"
            )
        ok = proc.returncode in self.version_ok_codes
        return RunOutcome(ok, proc.returncode, proc.stdout or "", proc.stderr or "")

    # -- overridable parsing ----------------------------------------------- #

    def parse_version(self, text: str) -> str | None:
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)*(?:[-+][\w.]+)?)\b", text or "")
        return match.group(1) if match else (text.strip().splitlines() or [None])[0]

    def parse_capabilities(self, help_text: str, version_text: str) -> ToolCapabilities:
        return ToolCapabilities(flags=extract_flags(help_text))

    # -- guards ------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        return self.detect().found

    def resolved_path(self) -> str:
        detected = self.detect()
        if not detected.found or not detected.path:
            self.refuse_missing()
        return detected.path

    def refuse_missing(self) -> None:
        raise Refusal(
            RefusalCode.TOOL_MISSING,
            f"{self.name} is required for this operation but was not found.",
            remedies=[
                f"Install {self.name} and make sure '{self.binary}' is on PATH.",
                "Run `fpsaudio doctor` for install commands for this machine.",
                f"Or point at it explicitly in config: tools.{self.name} = \"C:/path/to/{self.binary}.exe\"",
            ],
            detail={"adapter": self.name, "binary": self.binary},
        )

    def require_flag(self, flag: str, *, purpose: str) -> None:
        """Assert a flag exists before we build a command around it.

        This is the structural replacement for the legacy retry-on-failure
        loops: a missing capability is reported as a refusal naming the tool
        and the purpose, not as a second command whose error message hides the
        first one's.
        """
        caps = self.detect().capabilities
        if caps.has_flag(flag):
            return
        raise Refusal(
            RefusalCode.TOOL_MISSING,
            f"{self.name} does not advertise '{flag}', needed for {purpose}.",
            remedies=[
                f"Upgrade {self.name}; run `{self.binary} {' '.join(self.help_argv)}` to see its options.",
                "Run `fpsaudio doctor --verbose` to see the flags this build detected.",
            ],
            detail={"adapter": self.name, "flag": flag, "purpose": purpose},
        )

    # -- command construction ---------------------------------------------- #

    def command(
        self,
        argv: Sequence[str],
        *,
        purpose: str = "",
        weight: float = 1.0,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdout_is_data: bool = False,
    ) -> Command:
        return Command(
            adapter=self.name,
            argv=(self.resolved_path(), *(str(a) for a in argv)),
            purpose=purpose,
            weight=weight,
            cwd=cwd,
            env=dict(env or {}),
            stdout_is_data=stdout_is_data,
        )

    # -- execution --------------------------------------------------------- #

    def run(self, command: Command, *, timeout: float | None = None) -> RunOutcome:
        env = os.environ.copy()
        env.update(command.env)
        try:
            proc = subprocess.run(  # noqa: S603
                list(command.argv),
                capture_output=True,
                text=True,
                errors="replace",
                cwd=str(command.cwd) if command.cwd else None,
                env=env,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            return RunOutcome(False, 127, error=str(exc))
        except OSError as exc:
            return RunOutcome(False, 126, error=str(exc))
        except subprocess.TimeoutExpired as exc:
            return RunOutcome(False, 124, error=f"timed out after {exc.timeout}s")
        return RunOutcome(
            proc.returncode == 0, proc.returncode, proc.stdout or "", proc.stderr or ""
        )

    def run_streaming(
        self,
        command: Command,
        *,
        on_stdout: LineSink | None = None,
        on_stderr: LineSink | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> RunOutcome:
        """Run a command, streaming stdout and stderr on separate threads.

        The streams are kept separate on purpose.  The legacy tool merged them
        (``stderr=subprocess.STDOUT``) and then read one character at a time,
        so real error text was interleaved into the progress display and could
        not be recovered afterwards (A-9).
        """
        env = os.environ.copy()
        env.update(command.env)
        try:
            proc = subprocess.Popen(  # noqa: S603
                list(command.argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
                cwd=str(command.cwd) if command.cwd else None,
                env=env,
            )
        except FileNotFoundError as exc:
            return RunOutcome(False, 127, error=str(exc))
        except OSError as exc:
            return RunOutcome(False, 126, error=str(exc))

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def pump(stream: Any, sink: LineSink | None, buffer: list[str], keep: int) -> None:
            try:
                for raw in stream:
                    line = raw.rstrip("\r\n")
                    if sink is not None:
                        sink(line)
                    buffer.append(line)
                    if len(buffer) > keep:
                        del buffer[: len(buffer) - keep]
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass

        threads = [
            threading.Thread(
                target=pump, args=(proc.stdout, on_stdout, stdout_lines, 200), daemon=True
            ),
            threading.Thread(
                target=pump, args=(proc.stderr, on_stderr, stderr_lines, 400), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()

        cancelled = False
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel() and not cancelled:
                    cancelled = True
                    proc.terminate()

        for thread in threads:
            thread.join(timeout=5.0)

        if cancelled:
            return RunOutcome(False, proc.returncode, error="cancelled by user")
        return RunOutcome(
            proc.returncode == 0,
            proc.returncode,
            "\n".join(stdout_lines),
            "\n".join(stderr_lines),
        )

    # -- progress ---------------------------------------------------------- #

    def parse_progress(self, line: str, state: dict[str, Any]) -> float | None:
        """Return 0-100 for a line of tool output, or ``None`` if it says nothing.

        ``state`` is a per-run scratch dict owned by the caller.
        """
        return None

    def to_dict(self) -> dict[str, Any]:
        return self.detect().to_dict()


def scale_progress(percent: float, start: float, end: float) -> float:
    """Map 0-100 into a sub-range so a multi-step pipeline stays monotonic.

    Ported verbatim in logic from
    ``Fps Converter Batch Mode/FPS Converter/core/ffmpeg_runner.py:422-426``.
    """
    if end <= start:
        return max(0.0, min(100.0, end))
    clamped = max(0.0, min(100.0, percent))
    return start + ((end - start) * (clamped / 100.0))


def merged_flags(*results: DetectResult) -> frozenset[str]:
    flags: set[str] = set()
    for result in results:
        flags |= set(result.capabilities.flags)
    return frozenset(flags)


def summarise(results: Iterable[DetectResult]) -> str:
    lines = []
    for result in results:
        mark = "OK " if result.found else "-- "
        version = f" {result.version}" if result.version else ""
        lines.append(f"{mark}{result.name}{version}")
    return "\n".join(lines)
