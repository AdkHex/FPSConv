from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class FpsProfile:
    key: str
    label: str
    from_fps: str
    to_fps: str
    tempo_ratio: Fraction

    @property
    def atempo_value(self) -> str:
        ratio = self.tempo_ratio
        return f"{ratio.numerator}/{ratio.denominator}"


FPS_PROFILES: dict[str, FpsProfile] = {
    "none": FpsProfile("none", "No retime (convert only)", "-", "-", Fraction(1, 1)),
    "23.976_to_25": FpsProfile("23.976_to_25", "23.976 -> 25", "23.976", "25", Fraction(25025, 24000)),
    # 23.976 is treated as 24000/1001 exactly; 24 / (24000/1001) = 1001/1000
    "23.976_to_24": FpsProfile("23.976_to_24", "23.976 -> 24", "23.976", "24", Fraction(1001, 1000)),
    "25_to_23.976": FpsProfile("25_to_23.976", "25 -> 23.976", "25", "23.976", Fraction(24000, 25025)),
    "24_to_23.976": FpsProfile("24_to_23.976", "24 -> 23.976", "24", "23.976", Fraction(1000, 1001)),
    "25_to_24": FpsProfile("25_to_24", "25 -> 24", "25", "24", Fraction(24, 25)),
    "24_to_25": FpsProfile("24_to_25", "24 -> 25", "24", "25", Fraction(25, 24)),
}


def list_profile_options() -> list[tuple[str, str]]:
    return [(profile.label, key) for key, profile in FPS_PROFILES.items()]


def get_profile(key: str) -> FpsProfile:
    return FPS_PROFILES.get(key, FPS_PROFILES["none"])
