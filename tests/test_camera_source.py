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


def test_resolve_device_keeps_numbers():
    from acepoint.sources.camera import resolve_device

    assert resolve_device(2) == 2
    assert resolve_device("2") == 2


def test_resolve_device_follows_by_id_symlink(monkeypatch):
    from acepoint.sources.camera import resolve_device

    by_id = "/dev/v4l/by-id/usb-openaicam_openaicam_2505121128-video-index0"
    monkeypatch.setattr("acepoint.sources.camera.os.path.realpath",
                        lambda p: "/dev/video1" if p == by_id else p)
    assert resolve_device(by_id) == 1


def test_missing_device_path_disables_only_that_source(tmp_path):
    import pytest
    from acepoint.sources.camera import resolve_device

    # 拔掉或序列号写错：抛 OSError，build_sources 会只停用这一半场（需求 §5.4）
    with pytest.raises(OSError, match="not found"):
        resolve_device(str(tmp_path / "usb-gone-video-index0"))


def test_camera_opens_the_resolved_index(monkeypatch):
    opened = []
    cap = Mock()
    cap.get.return_value = 0
    monkeypatch.setattr("acepoint.sources.camera.os.path.realpath", lambda p: "/dev/video3")
    monkeypatch.setattr("acepoint.sources.camera.cv2.VideoCapture",
                        lambda target, backend: opened.append(target) or cap)
    monkeypatch.setattr("acepoint.sources.camera.threading.Thread", lambda **kwargs: Mock())
    source = CameraSource("left", "/dev/v4l/by-id/usb-cam_SN1-video-index0", 1920, 1080)
    assert opened == [3]
    assert source.name == "#3 (usb-cam_SN1-video-index0)"
    assert "#3 (usb-cam_SN1-video-index0)" in source.description
