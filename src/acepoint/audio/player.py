"""Validated WAV packs and optional pygame output."""
import json
import wave
from pathlib import Path

from acepoint.audio.policy import PRIORITIES


class Pack:
    def __init__(self, root: str | Path, *, strict: bool = True) -> None:
        self.root = Path(root).resolve()
        raw = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("audio manifest must be an array")
        self.clips, self.warnings = [], []
        seen = set()
        for clip in raw:
            try:
                self._validate(clip, seen)
            except (ValueError, KeyError, TypeError, OSError, EOFError, wave.Error) as exc:
                if strict:
                    raise ValueError(f"invalid audio clip: {exc}") from exc
                self.warnings.append(str(exc))
                continue
            self.clips.append(clip)
        for event in PRIORITIES:
            for style in ("friends", "clean", "effects"):
                if not any(c["event"] == event and c["style"] == style for c in self.clips):
                    message = f"missing audio: {style}/{event}"
                    if strict:
                        raise ValueError(message)
                    self.warnings.append(message)
        if not self.clips:
            raise ValueError("no playable audio clips")

    def _validate(self, clip: dict, seen: set) -> None:
        if clip["event"] not in PRIORITIES or clip["style"] not in ("friends", "clean", "effects"):
            raise ValueError("invalid clip event/style")
        if not all(isinstance(clip[k], str) for k in ("id", "file", "text")):
            raise ValueError("clip id/file/text must be strings")
        path = (self.root / clip["file"]).resolve()
        if not path.is_relative_to(self.root) or clip["id"] in seen:
            raise ValueError("invalid clip path or duplicate ID")
        seen.add(clip["id"])
        with wave.open(str(path), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 44100:
                raise ValueError(f"invalid WAV format: {path}")
            if not 0 < duration <= (4 if clip["event"].startswith("win_") else 2.5):
                raise ValueError(f"invalid WAV duration: {path}: {duration:.2f}s")
            if len(wav.readframes(wav.getnframes())) != wav.getnframes() * 2:
                raise ValueError(f"truncated WAV: {path}")
        clip["path"], clip["duration"] = path, duration

    def choose(self, event: str, style: str, rng, previous: str | None) -> dict | None:
        clips = [c for c in self.clips if c["event"] == event and c["style"] == style]
        if not clips:
            clips = [c for c in self.clips if c["event"] == event and c["style"] == "effects"]
        choices = [c for c in clips if c["id"] != previous] or clips
        return rng.choice(choices) if choices else None


class NullPlayer:
    def __init__(self, config):
        self.played = []

    def play(self, clip, volume):
        self.played.append((clip["id"], volume))

    def prepare(self, clips):
        pass

    def fadeout(self, milliseconds):
        pass

    def stop(self):
        pass

    def volume(self, volume):
        pass

    def close(self):
        pass


class PygamePlayer:
    def __init__(self, config):
        import pygame.mixer as mixer
        self.mixer = mixer
        mixer.init(frequency=44100, size=-16, channels=1,
                   devicename=config.device_name or None)
        self.channel = mixer.Channel(0)
        self.sounds = {}

    def prepare(self, clips):
        # Decode before ready, outside the scoring lock. Bound the PCM footprint.
        if sum(c["path"].stat().st_size for c in clips) > 16 * 1024 * 1024:
            raise ValueError("audio pack exceeds 16 MiB cache budget")
        self.sounds = {c["id"]: self.mixer.Sound(str(c["path"])) for c in clips}

    def fadeout(self, milliseconds):
        self.channel.fadeout(milliseconds)

    def play(self, clip, volume):
        sound = self.sounds.get(clip["id"])
        if sound is None:
            sound = self.mixer.Sound(str(clip["path"]))
        self.channel.play(sound)
        self.channel.set_volume(volume)

    def stop(self):
        self.channel.stop()

    def volume(self, volume):
        self.channel.set_volume(volume)

    def close(self):
        self.stop()
        self.mixer.quit()
