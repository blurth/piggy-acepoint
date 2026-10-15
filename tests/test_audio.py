"""Audio regression tests use fake time and no physical output device."""
from dataclasses import replace
from pathlib import Path
import random

import pytest

from acepoint.audio.config import AudioConfig, settings
from acepoint.audio.player import Pack, NullPlayer
from acepoint.audio.policy import History
from acepoint.audio.service import AudioService
from acepoint.config import GameConfig, load_config, ConfigError
from acepoint.state import ScoreBoard, Action

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def rig():
    clock = Clock()
    board = ScoreBoard()
    service = AudioService(AudioConfig(enabled=True, backend="null", min_voice_interval_seconds=0),
                           board.game, ROOT / "audio", clock=clock, rng=random.Random(7), threaded=False)
    service.initialize()
    assert service.status == "ready"
    return board, service, clock


def score(rig, side="left", action=Action.INCREMENT, *, play=True):
    board, service, clock = rig
    before, after = board.apply_with_change(side, action)
    service.score(side, action, before, after)
    event = service.pending[0].name if service.pending else None
    if play:
        clock.now += 0.5
        service.pump()
        clock.now += 4.5
        service.pump()
    return event


def test_bundled_pack_is_complete_and_bounded():
    pack = Pack(ROOT / "audio")
    assert len(pack.clips) == 75
    assert sum(c["path"].stat().st_size for c in pack.clips) < 16 * 1024 * 1024


def test_streaks_and_shutdown(rig):
    names = [score(rig) for _ in range(8)]
    assert [n for n in names if n and n.startswith("streak")] == ["streak_3", "streak_5", "streak_7"]
    assert score(rig, "right") == "icebreaker"
    assert rig[1].history.streak == 1


def test_three_then_opponent_terminates(rig):
    for _ in range(3):
        score(rig)
    assert score(rig, "right") == "shutdown"


def test_side_specific_decrement_replays_history(rig):
    for side in ("left", "left", "left", "right"):
        score(rig, side)
    score(rig, "left", Action.DECREMENT)
    assert rig[1].history.points == ["left", "left", "right"]
    assert rig[1].history.score == {"left": 2, "right": 1}
    assert rig[1].history.deficits["right"] == 2


def test_zero_decrement_and_frozen_score_are_silent(rig):
    assert score(rig, "left", Action.DECREMENT) is None
    b, s, _ = rig
    s.set_score(b.set_score(21, 19))
    assert score(rig) is None
    assert not s.player.played


def test_deuce_matchpoint_and_winner(rig):
    b, s, _ = rig
    s.set_score(b.set_score(20, 19))
    assert score(rig, "right") == "deuce"
    assert score(rig) == "match_point"
    assert b.winner is None
    assert score(rig) == "win_normal"
    assert b.winner == "left"


def test_cap_and_undo_win_dedupe(rig):
    b, s, clock = rig
    s.set_score(b.set_score(28, 29))
    assert score(rig) == "decider"
    assert score(rig) == "win_normal"
    score(rig, "left", Action.DECREMENT)
    assert b.winner is None
    assert score(rig) is None
    score(rig, "left", Action.DECREMENT)
    clock.now += 31
    assert score(rig) == "win_normal"


def test_comeback_events_and_win_classification(rig):
    b, s, _ = rig
    s.set_score(b.set_score(14, 19))
    events = [score(rig) for _ in range(5)]
    assert events[-1] == "comeback_tie"
    # 20:19 is match point, which overrides comeback_lead.
    assert score(rig) == "match_point"
    assert score(rig) == "win_comeback"


def test_comeback_lead_below_matchpoint(rig):
    b, s, _ = rig
    s.set_score(b.set_score(2, 7))
    for _ in range(5):
        score(rig)
    assert score(rig) == "comeback_lead"


def test_dominant_win_overrides_streak(rig):
    b, s, _ = rig
    s.set_score(b.set_score(18, 2))
    score(rig)
    score(rig)
    assert score(rig) == "win_dominant"


def test_reset_and_mute_cancel_pending_and_active(rig):
    b, s, c = rig
    score(rig, play=False)
    s.configure({"muted": True})
    c.now += 1
    s.pump()
    assert not s.player.played
    s.configure({"muted": False})
    s.pump()
    assert not s.player.played
    score(rig)
    score(rig, action=Action.RESET)
    assert not s.history.points and s.current is None and s.pending is None
    assert s.history.game == 1 and not s.played


def test_duplicate_change_and_stale_queue(rig):
    b, s, c = rig
    before, after = b.apply_with_change("left", Action.INCREMENT)
    s.score("left", Action.INCREMENT, before, after)
    s.score("left", Action.INCREMENT, before, after)
    assert s.history.points == ["left"]
    c.now += 3
    s.pump()
    assert not s.player.played and s.pending is None


def test_new_score_replaces_old_pending(rig):
    for _ in range(3):
        score(rig, play=False)
    assert rig[1].pending[0].name == "streak_3"
    rig[2].now += .5
    rig[1].pump()
    assert len(rig[1].player.played) == 1


def test_once_events_not_replayed_after_undo(rig):
    b, s, c = rig
    s.set_score(b.set_score(20, 19))
    score(rig, "right")
    score(rig, "right", Action.DECREMENT)
    c.now += 60
    assert score(rig, "right") is None


def test_custom_game_and_disabled_rules():
    history = History()
    b = ScoreBoard(GameConfig(target=25, cap=35))
    b.set_score(20, 19)
    before, after = b.apply_with_change("right", Action.INCREMENT)
    assert not history.update("right", Action.INCREMENT, before, after, AudioConfig(), b.game)
    b = ScoreBoard(GameConfig(enabled=False))
    b.set_score(29, 29)
    before, after = b.apply_with_change("left", Action.INCREMENT)
    events = History().update("left", Action.INCREMENT, before, after, AudioConfig(), b.game)
    assert not events


def test_history_limit_and_baseline_decrement():
    h = History()
    b = ScoreBoard(GameConfig(enabled=False))
    config = AudioConfig(history_limit=2)
    for _ in range(3):
        before, after = b.apply_with_change("left", Action.INCREMENT)
        h.update("left", Action.INCREMENT, before, after, config, b.game)
    assert h.points == [] and h.score["left"] == 3
    before, after = b.apply_with_change("left", Action.DECREMENT)
    h.update("left", Action.DECREMENT, before, after, config, b.game)
    assert h.score["left"] == 2 and h.streak == 0


def test_preview_rate_limit_no_history_and_scoring_cancels(rig):
    b, s, c = rig
    assert s.preview(b.snapshot())
    assert not s.preview(b.snapshot())
    c.now += .5
    s.pump()
    assert not s.played and not s.history.points
    score(rig, play=False)
    assert not s.preview(b.snapshot())
    s.pump()
    assert s.current is None


def test_preemption_fades_then_plays_winner(rig):
    b, s, c = rig
    s.set_score(b.set_score(19, 18))
    score(rig, play=False)
    c.now += .5
    s.pump()
    assert s.current
    score(rig, play=False)
    c.now += .5
    s.pump()
    assert s.current[0] == 101
    c.now += .1
    s.pump()
    assert s.current[0] == 100


def test_missing_pack_and_player_failure_do_not_escape(rig, tmp_path):
    b, _, c = rig
    s = AudioService(AudioConfig(enabled=True), b.game, tmp_path, threaded=False)
    s.initialize()
    assert s.status == "error"
    before, after = b.apply_with_change("left", Action.INCREMENT)
    s.score("left", Action.INCREMENT, before, after)
    s.pump()
    assert b.snapshot().left == 1
    _, s, c = rig
    def fail(*args):
        raise OSError("unplugged")
    s.player.play = fail
    score(rig)
    assert s.status == "error"


@pytest.mark.parametrize("body", [{"volume": float("nan")}, {"volume": 2}, {"volume": True},
                                  {"muted": "yes"}, {"mode": "loud"}, {"file": "x"}, [], {}])
def test_bad_settings_rejected(body):
    with pytest.raises(ValueError):
        settings(AudioConfig(), body)


def test_config_loader_audio(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[audio]\nenabled=true\nvolume=0.4\n', encoding="utf-8")
    assert load_config(p).audio.volume == .4
    p.write_text('[audio]\nvolume=-1\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_web_controls_validate_permissions_and_queue():
    from acepoint.displays.web import WebDisplay
    from acepoint.config import WebConfig
    from acepoint.state import Telemetry
    received = []
    w = WebDisplay(WebConfig(), on_audio=lambda x: received.append(x) or True,
                   audio_config=AudioConfig(enabled=True))
    client = w.app.test_client()
    assert client.post('/api/audio/settings', json={"muted": True}).status_code == 202
    assert received == [{"muted": True}]
    assert client.post('/api/audio/settings', json={"volume": -1}).status_code == 400
    assert client.post('/api/audio/settings', json=[]).status_code == 400
    b = ScoreBoard()
    b.apply("left", Action.INCREMENT)
    w.update(Telemetry(b.snapshot()))
    assert client.post('/api/audio/test', json={}).status_code == 409
    w._audio_config = replace(w._audio_config, allow_web_control=False)
    assert client.post('/api/audio/settings', json={"muted": True}).status_code == 403
    assert client.post('/api/audio/test', json={}).status_code == 403


def test_partial_pack_falls_back_and_strict_check_fails(tmp_path):
    import json
    import shutil
    shutil.copytree(ROOT / "audio", tmp_path / "pack")
    root = tmp_path / "pack"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest = [c for c in manifest if not (c["event"] == "streak_3" and c["style"] == "friends")]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        Pack(root)
    pack = Pack(root, strict=False)
    assert pack.choose("streak_3", "friends", random.Random(), None)["style"] == "effects"


def test_pack_rejects_outside_paths(tmp_path):
    import json
    (tmp_path / "manifest.json").write_text(json.dumps([dict(
        event="streak_3", style="friends", id="evil", text="x", file="../outside.wav")]))
    with pytest.raises(ValueError, match="path"):
        Pack(tmp_path)


def test_modes_and_voice_gap(rig):
    _, s, c = rig
    s.config = replace(s.config, min_voice_interval_seconds=6, mode="key_only")
    assert score(rig) is None
    assert score(rig) is None
    assert score(rig) == "streak_3"
    # Test a shorter gap than normal rally timing.
    c.now -= 4
    assert score(rig, "right", play=False) is None
    s.configure({"mode": "effects_only"})
    c.now += 7
    score(rig, "right")
    score(rig, "right")
    assert s.player.played[-1][0].startswith("effects_streak_3")


def test_no_consecutive_repeated_clip():
    pack = Pack(ROOT / "audio")
    rng = random.Random(42)
    last = None
    for _ in range(15):
        clip = pack.choose("streak_3", "friends", rng, last)
        assert clip["id"] != last
        last = clip["id"]


def test_engine_manual_and_score_edit_connect_audio(rig):
    pytest.importorskip("mediapipe")
    import queue
    from acepoint.engine import ScoringEngine
    engine = ScoringEngine.__new__(ScoringEngine)
    engine.board, engine.audio, _ = rig
    engine.fsms = {}
    engine._manual = queue.Queue()
    for _ in range(3):
        engine._enqueue_manual("left", Action.INCREMENT)
    engine._drain_manual()
    assert engine.audio.pending[0].name == "streak_3"
    engine.set_score(10, 11)
    assert engine.audio.pending is None
    assert engine.audio.history.points == []
    engine._apply_score("left", Action.INCREMENT)
    assert engine.audio.history.streak == 1


def test_control_queue_is_bounded():
    pytest.importorskip("mediapipe")
    import queue
    from acepoint.engine import ScoringEngine
    engine = ScoringEngine.__new__(ScoringEngine)
    engine._audio_controls = queue.Queue(maxsize=2)
    assert engine._enqueue_audio({"muted": True})
    assert engine._enqueue_audio({"muted": False})
    assert not engine._enqueue_audio({"muted": True})
