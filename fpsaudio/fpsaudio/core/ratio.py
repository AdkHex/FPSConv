"""Exact-rational frame-rate maths.

Seeded from ``Fps Converter Batch Mode/FPS Converter/core/profiles.py:21-30`` —
the one artefact in either legacy program that was already correct — and then
extended with the 29.97 / 30 / 47.952 / 48 / 50 / 59.94 / 60 families, arbitrary
source/destination pairs, and direct-ratio entry.

The whole module is :class:`fractions.Fraction` end to end.  No float ever
enters a computation that affects a sample count; the only floats produced are
for display, and they are produced at the very last moment by explicit
``float()`` calls in ``*_approx`` helpers.

Terminology
-----------
``speed``           dst_fps / src_fps.  The factor by which playback speeds up.
``duration_scale``  1 / speed.  The factor by which wall-clock duration scales.

A speed change is a **resample**, not a time-stretch: pitch moves with speed.
Both legacy programs used ``atempo`` / ``sox tempo`` / ``rubberband --tempo``,
which are pitch-preserving stretchers, i.e. the wrong operation (A-2, B-2).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Iterator

from .contracts import Refusal, RefusalCode, fraction_to_str

__all__ = [
    "CANONICAL_FPS",
    "FPS_ALIASES",
    "LEGACY_PRESET_KEYS",
    "RetimeRatio",
    "STANDARD_SAMPLE_RATES",
    "drift_seconds",
    "format_fps",
    "iter_presets",
    "get_preset",
    "parse_fps",
    "parse_ratio",
    "preset_key",
    "presets",
    "redeclared_rate",
    "resolve_ratio",
    "round_half_up",
    "scaled_samples",
    "scaled_time",
]


# --------------------------------------------------------------------------- #
# Frame rates
# --------------------------------------------------------------------------- #

#: The broadcast "pull-down" rates are exactly N000/1001.  Writing 23.976 as a
#: decimal literal (2997/125) would be wrong by 1 part in 8 million — about
#: 1.6 ms over a two-hour feature, which already fails the 1 ms gate.
FPS_ALIASES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "24000/1001": Fraction(24000, 1001),
    "24": Fraction(24),
    "25": Fraction(25),
    "29.97": Fraction(30000, 1001),
    "30000/1001": Fraction(30000, 1001),
    "30": Fraction(30),
    "47.952": Fraction(48000, 1001),
    "48000/1001": Fraction(48000, 1001),
    "48": Fraction(48),
    "50": Fraction(50),
    "59.94": Fraction(60000, 1001),
    "60000/1001": Fraction(60000, 1001),
    "60": Fraction(60),
    # Common spellings people actually type.
    "ntsc": Fraction(30000, 1001),
    "ntsc-film": Fraction(24000, 1001),
    "film": Fraction(24),
    "pal": Fraction(25),
}

#: Canonical rates that get a generated preset, in display order.
CANONICAL_FPS: tuple[str, ...] = (
    "23.976",
    "24",
    "25",
    "29.97",
    "30",
    "47.952",
    "48",
    "50",
    "59.94",
    "60",
)

#: The six presets both legacy programs shipped, in their original order.  Kept
#: as a named group so the TUI can offer "the classic six" first.
LEGACY_PRESET_KEYS: tuple[str, ...] = (
    "23.976_to_25",
    "23.976_to_24",
    "25_to_23.976",
    "24_to_23.976",
    "25_to_24",
    "24_to_25",
)

STANDARD_SAMPLE_RATES: tuple[int, ...] = (
    32000, 44100, 48000, 88200, 96000, 176400, 192000,
)


def parse_fps(text: str | Fraction | int | float) -> Fraction:
    """Parse a frame rate into an exact Fraction.

    Accepts the broadcast aliases (``23.976`` → 24000/1001), explicit rationals
    (``24000/1001``), and plain integers.  A bare float is rejected: if the
    caller has already lost exactness there is nothing this function can
    recover, and silently accepting it is how the legacy ratios went wrong.
    """
    if isinstance(text, Fraction):
        return text
    if isinstance(text, int):
        return Fraction(text)
    if isinstance(text, float):
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Frame rate {text!r} was given as a float, which has already lost "
            f"exactness. Pass a string such as '23.976' or '24000/1001'.",
        )
    key = str(text).strip().lower()
    if key in FPS_ALIASES:
        return FPS_ALIASES[key]
    try:
        value = Fraction(key)
    except (ValueError, ZeroDivisionError) as exc:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Cannot parse {text!r} as a frame rate.",
            remedies=[
                "Use a preset name such as 23.976, 24, 25, 29.97, 30, 50, 59.94.",
                "Or give an exact rational such as 24000/1001.",
            ],
        ) from exc
    if value <= 0:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Frame rate must be positive; got {text!r}.",
        )
    return value


def parse_ratio(text: str | Fraction) -> Fraction:
    """Parse a direct speed ratio such as ``25025/24000`` or ``1.0427``."""
    if isinstance(text, Fraction):
        value = text
    else:
        raw = str(text).strip()
        try:
            value = Fraction(raw)
        except (ValueError, ZeroDivisionError) as exc:
            raise Refusal(
                RefusalCode.UNSUPPORTED_OPERATION,
                f"Cannot parse {text!r} as a ratio.",
                remedies=["Give an exact rational such as 25025/24000."],
            ) from exc
    if value <= 0:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            f"Speed ratio must be positive; got {text!r}.",
        )
    return value


def format_fps(value: Fraction) -> str:
    """Render a frame rate the way a human wrote it, without losing exactness."""
    for label in CANONICAL_FPS:
        if FPS_ALIASES[label] == value:
            return label
    if value.denominator == 1:
        return str(value.numerator)
    return fraction_to_str(value)


# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #

def round_half_up(value: Fraction) -> int:
    """Round a Fraction to the nearest integer, halves away from zero.

    Python's built-in :func:`round` uses banker's rounding, which would make a
    sample count depend on the parity of the value below it.  Sample counts are
    a contract with the container, so the rule is stated explicitly here and
    asserted in the property tests.
    """
    if value >= 0:
        return (2 * value.numerator + value.denominator) // (2 * value.denominator)
    return -((2 * (-value).numerator + value.denominator) // (2 * value.denominator))


# --------------------------------------------------------------------------- #
# The ratio object
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class RetimeRatio:
    """An exact speed change from ``src_fps`` to ``dst_fps``.

    ``speed`` is always ``dst_fps / src_fps`` unless an explicit override was
    supplied, in which case the override *is* the speed and the frame rates are
    carried only for labelling.
    """

    src_fps: Fraction
    dst_fps: Fraction
    speed: Fraction

    # -- construction ------------------------------------------------------ #

    @classmethod
    def between(
        cls,
        src: str | Fraction | int,
        dst: str | Fraction | int,
        *,
        override: Fraction | None = None,
    ) -> "RetimeRatio":
        src_fps = parse_fps(src)
        dst_fps = parse_fps(dst)
        speed = override if override is not None else dst_fps / src_fps
        return cls(src_fps=src_fps, dst_fps=dst_fps, speed=speed)

    @classmethod
    def identity(cls) -> "RetimeRatio":
        return cls(src_fps=Fraction(1), dst_fps=Fraction(1), speed=Fraction(1))

    # -- identity / labelling ---------------------------------------------- #

    @property
    def is_identity(self) -> bool:
        return self.speed == 1

    @property
    def key(self) -> str:
        if self.is_identity:
            return "none"
        return preset_key(self.src_fps, self.dst_fps)

    @property
    def label(self) -> str:
        if self.is_identity:
            return "No retime (convert only)"
        return f"{format_fps(self.src_fps)} -> {format_fps(self.dst_fps)}"

    @property
    def speed_str(self) -> str:
        return fraction_to_str(self.speed)

    @property
    def duration_scale(self) -> Fraction:
        """Factor by which wall-clock duration changes.  Exactly ``1/speed``."""
        return 1 / self.speed

    def speed_approx(self) -> float:
        """Display-only float.  Never feed this back into a computation."""
        return float(self.speed)

    def percent_approx(self) -> float:
        return float((self.speed - 1) * 100)

    # -- the actual maths -------------------------------------------------- #

    def output_samples(
        self,
        input_samples: int,
        *,
        src_rate: int,
        dst_rate: int | None = None,
    ) -> int:
        """Exact output sample count.

        The physical duration after the speed change is
        ``input_samples / (src_rate * speed)`` seconds; at ``dst_rate`` samples
        per second that is ``input_samples * dst_rate / (src_rate * speed)``
        samples, rounded half up.

        With ``dst_rate == src_rate`` this reduces to ``round(N / speed)`` —
        the acceptance gate stated in Part 7 of the plan.
        """
        if input_samples < 0:
            raise ValueError("input_samples must be non-negative")
        if src_rate <= 0:
            raise ValueError("src_rate must be positive")
        rate_out = src_rate if dst_rate is None else dst_rate
        if rate_out <= 0:
            raise ValueError("dst_rate must be positive")
        exact = Fraction(input_samples * rate_out, 1) / (Fraction(src_rate) * self.speed)
        return round_half_up(exact)

    def output_seconds(self, input_seconds: Fraction | int) -> Fraction:
        return Fraction(input_seconds) / self.speed

    def scale_timestamp(self, value: Fraction | int) -> Fraction:
        """Rescale any time-domain metadata (delay, chapter, object timestamp).

        §3.6 and §3.5: delays, MKV ``CodecDelay``, chapter marks and Atmos
        object timestamps all move by the same exact factor as the audio.
        Neither legacy program touched any of them (B-19).
        """
        return Fraction(value) * self.duration_scale

    def redeclared_rate(self, src_rate: int) -> Fraction:
        """Sample rate that realises the speed change with no resampling."""
        return Fraction(src_rate) * self.speed

    def redeclare_is_exact(self, src_rate: int) -> bool:
        return self.redeclared_rate(src_rate).denominator == 1

    def to_dict(self) -> dict[str, str]:
        return {
            "src_fps": fraction_to_str(self.src_fps),
            "dst_fps": fraction_to_str(self.dst_fps),
            "speed": fraction_to_str(self.speed),
            "key": self.key,
            "label": self.label,
        }


# --------------------------------------------------------------------------- #
# Module-level helpers mirroring the RetimeRatio methods
# --------------------------------------------------------------------------- #

def scaled_samples(
    input_samples: int, speed: Fraction, *, src_rate: int, dst_rate: int | None = None
) -> int:
    return RetimeRatio(Fraction(1), Fraction(1), speed).output_samples(
        input_samples, src_rate=src_rate, dst_rate=dst_rate
    )


def scaled_time(value: Fraction | int, speed: Fraction) -> Fraction:
    return Fraction(value) / speed


def redeclared_rate(src_rate: int, speed: Fraction) -> Fraction:
    return Fraction(src_rate) * speed


def drift_seconds(duration_s: Fraction | int, wrong: Fraction, right: Fraction) -> Fraction:
    """Accumulated sync error from using ``wrong`` where ``right`` was correct.

    Used by ``docs/AUDIT.md`` and by ``fpsaudio explain`` to quantify A-1.
    """
    return Fraction(duration_s) / wrong - Fraction(duration_s) / right


# --------------------------------------------------------------------------- #
# Preset table
# --------------------------------------------------------------------------- #

def preset_key(src: Fraction, dst: Fraction) -> str:
    return f"{format_fps(src)}_to_{format_fps(dst)}"


def _build_presets() -> dict[str, RetimeRatio]:
    table: dict[str, RetimeRatio] = {
        "none": RetimeRatio.identity(),
    }
    for src_label in CANONICAL_FPS:
        for dst_label in CANONICAL_FPS:
            if src_label == dst_label:
                continue
            ratio = RetimeRatio.between(src_label, dst_label)
            table[ratio.key] = ratio
    return table


_PRESETS: dict[str, RetimeRatio] = _build_presets()


def presets() -> dict[str, RetimeRatio]:
    """All generated presets, keyed ``<src>_to_<dst>``."""
    return dict(_PRESETS)


def iter_presets(*, legacy_first: bool = True) -> Iterator[tuple[str, RetimeRatio]]:
    seen: set[str] = set()
    if legacy_first:
        for key in LEGACY_PRESET_KEYS:
            seen.add(key)
            yield key, _PRESETS[key]
    for key, ratio in _PRESETS.items():
        if key in seen:
            continue
        yield key, ratio


def get_preset(key: str) -> RetimeRatio:
    """Look up a preset.

    Unlike the legacy ``get_profile``, an unknown key is an error rather than a
    silent fallback to "no retime" — a typo must not quietly produce a job that
    does nothing.
    """
    normalised = key.strip()
    if normalised in _PRESETS:
        return _PRESETS[normalised]
    raise Refusal(
        RefusalCode.UNSUPPORTED_OPERATION,
        f"Unknown retime preset {key!r}.",
        remedies=[
            "Run `fpsaudio presets` to list every available preset.",
            "Or give explicit rates: --from 23.976 --to 25.",
        ],
    )


def resolve_ratio(
    *,
    preset: str | None = None,
    src_fps: str | Fraction | None = None,
    dst_fps: str | Fraction | None = None,
    ratio: str | Fraction | None = None,
) -> RetimeRatio:
    """The single entry point the CLI and TUI both use.

    Precedence: explicit ``ratio`` > explicit ``src``/``dst`` > ``preset``.
    """
    if ratio is not None:
        speed = parse_ratio(ratio)
        if src_fps is not None and dst_fps is not None:
            return RetimeRatio.between(src_fps, dst_fps, override=speed)
        return RetimeRatio(src_fps=Fraction(1), dst_fps=speed, speed=speed)
    if src_fps is not None and dst_fps is not None:
        return RetimeRatio.between(src_fps, dst_fps)
    if src_fps is not None or dst_fps is not None:
        raise Refusal(
            RefusalCode.UNSUPPORTED_OPERATION,
            "Give both --from and --to, or neither.",
            remedies=["Example: --from 23.976 --to 25"],
        )
    if preset:
        return get_preset(preset)
    return RetimeRatio.identity()


def nearest_standard_rate(rate: Fraction | int) -> int:
    """Closest standard sample rate, used when suggesting a resample target."""
    target = Fraction(rate)
    return min(STANDARD_SAMPLE_RATES, key=lambda r: abs(Fraction(r) - target))


def exact_redeclare_targets(src_rate: int) -> Iterable[tuple[str, int]]:
    """Presets for which ``src_rate`` admits a bit-exact redeclaration."""
    for key, ratio in _PRESETS.items():
        if ratio.is_identity:
            continue
        new_rate = ratio.redeclared_rate(src_rate)
        if new_rate.denominator == 1:
            yield key, int(new_rate)
