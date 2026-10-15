"""Bounded, cancellable commentary; only the worker touches the audio device."""
import logging
import random
import threading
import time

from acepoint.audio.config import settings
from acepoint.audio.player import Pack, NullPlayer, PygamePlayer
from acepoint.audio.policy import Candidate, History, ONCE
from acepoint.state import Action

log = logging.getLogger(__name__)


class AudioService:
    def __init__(self, config, game, root, *, clock=time.monotonic, rng=None,
                 player_factory=None, threaded=True):
        self.config, self.game = config, game
        self.root = root
        self.clock, self.rng = clock, rng or random.Random()
        self.history = History()
        self.pending = None
        self.current = None
        self.played = {}
        self.once = set()
        self.last_clip = {}
        self.last_voice = float("-inf")
        self.point_count = 0
        self.banter_after = self.rng.randint(config.banter_points_min, config.banter_points_max)
        self.last_version = -1
        self.last_test = float("-inf")
        self.last_text = ""
        self.error = ""
        self.status = "starting" if config.enabled else "disabled"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._stop_requested = False
        self._thread = None
        self.player = None
        self._volume = None
        self.pack = None
        self._factory = player_factory or (NullPlayer if config.backend == "null" else PygamePlayer)
        if config.enabled and threaded:
            self._thread = threading.Thread(target=self._run, name="audio", daemon=True)
            self._thread.start()

    def initialize(self):
        try:
            self.pack = Pack(self.root, strict=False)
            self.player = self._factory(self.config)
            self.player.prepare(self.pack.clips)
            self.status = "ready"
            if self.pack.warnings:
                self.error = f"素材不完整：{len(self.pack.warnings)}项；缺失语音回退音效"
                log.warning(self.error)
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc):
        self.error, self.status = str(exc), "error"
        self.pending = None
        self.current = None
        if self.player:
            try:
                self.player.stop()
            except Exception:
                pass
        log.warning("Audio unavailable: %s", exc)

    def snapshot(self):
        with self._lock:
            return {"enabled": self.config.enabled, "status": self.status, "error": self.error,
                    "muted": self.config.muted, "volume": self.config.volume,
                    "style": self.config.style, "mode": self.config.mode,
                    "allow_control": self.config.allow_web_control,
                    "last_text": self.last_text}

    def configure(self, body):
        with self._lock:
            new = settings(self.config, body)
            self.config = new
            self.pending = None
            if new.muted or new.volume == 0:
                self._stop_requested = True
            self._wake.set()

    def _cancel(self):
        self.pending = None
        self._stop_requested = True
        self._wake.set()

    @staticmethod
    def _once_key(candidate):
        side = "both" if candidate.name in ("deuce", "decider") else candidate.side
        return (candidate.name, side)

    def set_score(self, snapshot):
        """Direct score edits establish a baseline, never invent past rallies."""
        with self._lock:
            self._cancel()
            self.history.baseline(snapshot)
            self.last_version = snapshot.version
            self.point_count = 0

    def score(self, side, action, before, after):
        with self._lock:
            if after.version <= self.last_version:
                return
            self.last_version = after.version
            candidates = self.history.update(side, action, before, after, self.config, self.game)
            changed = (before.left, before.right) != (after.left, after.right)
            if action is Action.RESET:
                self._cancel()
                self.played.clear()
                self.once.clear()
                self.point_count = 0
                self.last_voice = float("-inf")
                self.last_text = ""
                return
            if not changed:
                return
            self.pending = None
            if action is not Action.INCREMENT:
                self._cancel()
                self.point_count = 0
                return
            if self.current and self.current[2]:
                self._cancel()  # Real scoring always interrupts a preview.
            self.point_count += 1
            now = self.clock()
            self.played = {k: t for k, t in self.played.items()
                           if now - t < self.config.repeat_guard_seconds}
            if not candidates:
                name = "banter" if self.config.mode == "balanced" and self.point_count >= self.banter_after else "point"
                candidates = [Candidate(name, side, after.left, after.right,
                                        self.history.game, after.version)]
            candidate = candidates[0]  # Never replace suppressed milestones with lower jokes.
            if (candidate.name in ONCE and self._once_key(candidate) in self.once) or candidate.key in self.played:
                return
            if candidate.name == "point" and (not self.config.point_sound or self.config.mode != "balanced"):
                return
            if candidate.name == "banter" and self.config.mode != "balanced":
                return
            if self.config.muted or self.config.volume == 0 or not self.config.enabled:
                return
            if candidate.priority < 80 and now - self.last_voice < self.config.min_voice_interval_seconds:
                return
            self.pending = (candidate, now, False)
            self._wake.set()

    def preview(self, snapshot):
        with self._lock:
            now = self.clock()
            if snapshot.left or snapshot.right or now - self.last_test < 5:
                return False
            if not self.config.enabled or self.config.muted or self.config.volume == 0:
                return False
            self.last_test = now
            self.pending = (Candidate("streak_3", "left", 0, 0, self.history.game, -1), now, True)
            self._wake.set()
            return True

    def pump(self):
        """One worker iteration; fake clocks can drive this synchronously in tests."""
        with self._lock:
            if self.status != "ready":
                return
            try:
                now = self.clock()
                if self._stop_requested:
                    self.player.stop()
                    self.current = None
                    self._stop_requested = False
                if self._volume != self.config.volume:
                    self.player.volume(self.config.volume)
                    self._volume = self.config.volume
                if self.current and now >= self.current[1]:
                    self.current = None
                if not self.pending:
                    return
                candidate, created, preview = self.pending
                ttl = self.config.win_ttl_seconds if candidate.priority == 100 else self.config.pending_ttl_seconds
                if now - created > ttl or candidate.game != self.history.game:
                    self.pending = None
                    return
                if now - created < self.config.trigger_delay_seconds:
                    return
                if self.current:
                    if candidate.priority >= 80 and candidate.priority >= self.current[0]:
                        self.player.fadeout(self.config.preempt_fade_ms)
                        self.current = (101, now + self.config.preempt_fade_ms / 1000, False)
                        return
                    else:
                        return
                style = "effects" if self.config.mode == "effects_only" or candidate.name == "point" else self.config.style
                clip = self.pack.choose(candidate.name, style, self.rng, self.last_clip.get((candidate.name, style)))
                if clip is None:
                    self.pending = None
                    return
                self.player.play(clip, self.config.volume)
                self.current = (candidate.priority, now + clip["duration"], preview)
                self.pending = None
                self.last_clip[(candidate.name, style)] = clip["id"]
                self.last_text = clip["text"]
                if not preview:
                    self.played[candidate.key] = now
                    if candidate.name in ONCE:
                        self.once.add(self._once_key(candidate))
                    if candidate.name != "point":
                        self.last_voice = now
                        self.point_count = 0
                        self.banter_after = self.rng.randint(self.config.banter_points_min, self.config.banter_points_max)
            except Exception as exc:
                self._fail(exc)

    def _run(self):
        self.initialize()
        try:
            while not self._quit.is_set():
                self.pump()
                self._wake.wait(0.02)
                self._wake.clear()
        finally:
            if self.player:
                try:
                    self.player.close()
                except Exception:
                    log.exception("Closing audio failed")

    def close(self):
        self._quit.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        elif self.player:
            self.player.close()
