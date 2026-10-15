"""Validated audio configuration and runtime controls."""
from dataclasses import dataclass, fields, replace
import math


@dataclass(frozen=True)
class AudioConfig:
    enabled: bool = False
    backend: str = "pygame"
    pack_root: str = "audio"
    style: str = "friends"
    mode: str = "balanced"
    muted: bool = False
    volume: float = 0.6
    device_name: str = ""
    allow_web_control: bool = True
    streak_thresholds: tuple = (3, 5, 7)
    shutdown_min_streak: int = 3
    comeback_deficit: int = 5
    dominant_margin: int = 10
    icebreaker_min_opponent: int = 5
    banter_points_min: int = 3
    banter_points_max: int = 5
    min_voice_interval_seconds: float = 6.0
    trigger_delay_seconds: float = 0.4
    pending_ttl_seconds: float = 2.0
    win_ttl_seconds: float = 5.0
    repeat_guard_seconds: float = 30.0
    preempt_fade_ms: int = 80
    point_sound: bool = True
    history_limit: int = 512

    def __post_init__(self):
        for name in ("enabled", "muted", "allow_web_control", "point_sound"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"audio.{name} must be boolean")
        for name, choices in {"backend": ("pygame", "null"), "style": ("friends", "clean"),
                              "mode": ("balanced", "key_only", "effects_only")}.items():
            if getattr(self, name) not in choices:
                raise ValueError(f"invalid audio.{name}")
        for name in ("pack_root", "device_name"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"audio.{name} must be text")
        for f in fields(self):
            value = getattr(self, f.name)
            if f.type is int:
                if type(value) is not int or value <= 0:
                    raise ValueError(f"audio.{f.name} must be a positive integer")
            if f.type is float:
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"audio.{f.name} must be finite and nonnegative")
        if self.volume > 1 or self.banter_points_min > self.banter_points_max:
            raise ValueError("invalid audio volume or banter range")
        thresholds = self.streak_thresholds
        if not isinstance(thresholds, (list, tuple)) or not thresholds or any(
                type(v) is not int or v not in (3, 5, 7) for v in thresholds):
            raise ValueError("audio.streak_thresholds must contain supported values 3, 5, 7")
        if list(thresholds) != sorted(set(thresholds)):
            raise ValueError("audio.streak_thresholds must be strictly increasing")
        if min(self.pending_ttl_seconds, self.win_ttl_seconds) <= self.trigger_delay_seconds:
            raise ValueError("audio TTL must exceed trigger delay")


def settings(config, body):
    if not isinstance(body, dict) or not body or set(body) - {"muted", "volume", "style", "mode"}:
        raise ValueError("invalid audio settings")
    return replace(config, **body)
