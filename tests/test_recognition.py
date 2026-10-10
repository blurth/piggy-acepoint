from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from acepoint.config import RecognitionConfig
from acepoint.recognition import GestureReader


def observe_hands(monkeypatch, names):
    reader = GestureReader.__new__(GestureReader)
    reader.config = RecognitionConfig()
    reader.per_source = True
    reader._last_ts = {"left": 0}
    reader._mp = SimpleNamespace(Image=lambda **kwargs: object(),
                                 ImageFormat=SimpleNamespace(SRGB=1))
    recognizer = Mock()
    recognizer.recognize_for_video.return_value = SimpleNamespace(
        gestures=[[SimpleNamespace(category_name=name, score=0.9)] for name in names])
    reader._recognizers = {"left": recognizer}
    return reader.observe("left", np.zeros((2, 2, 3), dtype=np.uint8))


def test_conflicting_hands_do_not_score(monkeypatch):
    observation = observe_hands(monkeypatch, ["Victory", "Open_Palm"])
    assert observation.gesture is None
    assert observation.raw == "Ambiguous"


def test_two_increment_gestures_are_compatible(monkeypatch):
    observation = observe_hands(monkeypatch, ["Victory", "Pointing_Up"])
    assert observation.gesture == "Victory"
