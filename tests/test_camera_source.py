from unittest.mock import Mock

import numpy as np

from acepoint.sources.camera import CameraSource


def test_capture_keeps_latest_frame_and_read_consumes_it(monkeypatch):
    cap = Mock()
    cap.get.return_value = 0
    thread = Mock()
    monkeypatch.setattr("acepoint.sources.camera.cv2.VideoCapture", lambda *args: cap)
    monkeypatch.setattr("acepoint.sources.camera.threading.Thread", lambda **kwargs: thread)
    source = CameraSource("left", 0, 1920, 1080)
    first = np.zeros((2, 2, 3), dtype=np.uint8)
    latest = np.ones((2, 2, 3), dtype=np.uint8)
    calls = iter([(True, first), (True, latest)])

    def capture():
        result = next(calls)
        if result[1] is latest:
            source._stop.set()
        return result

    cap.read.side_effect = capture
    source._capture()
    frame = source.read()
    assert frame.index == 2
    assert frame.image is latest
    assert source.read() is None
    cap.release.assert_called_once()
