"""拇指向上 = 给对面半场加分（scoring.thumb_up_scores_opponent，单摄像头过渡用）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from acepoint.config import GameConfig, RecognitionConfig, ScoringConfig
from acepoint.fsm import GestureFSM, gesture_actions
from acepoint.recognition import GestureReader
from acepoint.state import Action, ScoreBoard

ON = ScoringConfig(hold_seconds=0.2, hold_grace_seconds=0.4, hold_min_samples=2,
                   cooldown_seconds=2.0, thumb_up_scores_opponent=True)


def _hold(fsm: GestureFSM, gesture: str, samples: int, period: float = 0.2):
    fired = None
    for i in range(samples):
        fired = fsm.feed(gesture, 10.0 + i * period) or fired
    return fired


def test_thumb_up_is_ignored_unless_enabled():
    fsm = GestureFSM("left", ScoringConfig(hold_seconds=0.2))
    assert "Thumb_Up" not in gesture_actions(fsm.config)
    assert _hold(fsm, "Thumb_Up", 5) is None


def test_thumb_up_fires_increment_opponent_with_the_normal_hold():
    fsm = GestureFSM("left", ON)
    assert fsm.required_hold("Thumb_Up") == ON.hold_seconds
    assert _hold(fsm, "Thumb_Up", 2, period=0.25) is Action.INCREMENT_OPPONENT
    # 和自己加分一样进冷却（冷却在看到手势的这一侧）
    assert fsm.status(10.3).cooldown_remaining > 0


def test_scoreboard_never_accepts_the_unresolved_action():
    board = ScoreBoard(GameConfig())
    with pytest.raises(ValueError):
        board.apply("left", Action.INCREMENT_OPPONENT)


def _observe(names, actions):
    reader = GestureReader.__new__(GestureReader)
    reader.config = RecognitionConfig()
    reader.per_source = True
    reader.actions = actions
    reader._last_ts = {"left": 0}
    reader._mp = SimpleNamespace(Image=lambda **kwargs: object(),
                                 ImageFormat=SimpleNamespace(SRGB=1))
    recognizer = Mock()
    recognizer.recognize_for_video.return_value = SimpleNamespace(
        gestures=[[SimpleNamespace(category_name=name, score=0.9)] for name in names])
    reader._recognizers = {"left": recognizer}
    return reader.observe("left", np.zeros((2, 2, 3), dtype=np.uint8))


def test_own_and_opponent_point_from_two_hands_is_ambiguous():
    observation = _observe(["Victory", "Thumb_Up"], gesture_actions(ON))
    assert observation.gesture is None and observation.raw == "Ambiguous"


def test_thumb_up_next_to_a_command_is_harmless_when_disabled():
    observation = _observe(["Victory", "Thumb_Up"], gesture_actions(ScoringConfig()))
    assert observation.raw != "Ambiguous"
