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


# ------------------------------------------------------------------ 按需解码

def _fake_camera(monkeypatch, *, grab_seconds=0.01):
    """假相机：grab() 像 V4L2 一样阻塞一个帧间隔，retrieve() 给一张新图。"""
    import time

    cap = Mock()
    cap.get.return_value = 0

    def grab():
        time.sleep(grab_seconds)
        return True

    cap.grab.side_effect = grab
    cap.retrieve.side_effect = lambda: (True, np.zeros((2, 2, 3), dtype=np.uint8))
    monkeypatch.setattr("acepoint.sources.camera.cv2.VideoCapture", lambda *args: cap)
    return cap


def test_on_demand_decodes_only_the_frames_that_are_read(monkeypatch):
    """相机一直在出帧，但没人 read() 就一帧都不解码 —— 省的就是这部分 CPU。"""
    import time

    cap = _fake_camera(monkeypatch)
    source = CameraSource("left", 0, 1920, 1080, on_demand=True)
    try:
        time.sleep(0.15)
        assert cap.grab.call_count >= 5              # 队列一直在被排空
        assert cap.retrieve.call_count == 0          # 但没有解码
        assert "on-demand" in source.description

        first = source.read()
        second = source.read()
        assert (first.index, second.index) == (1, 2)
        assert first.image is not second.image       # 每次 read 都是新解的一帧
        assert cap.retrieve.call_count == 2
    finally:
        source.close()
    cap.release.assert_called_once()


def test_on_demand_does_not_spin_when_grab_never_blocks(monkeypatch):
    """DShow 的 grab() 立刻返回；没人要帧时不能把一个核空转满。"""
    import time

    cap = _fake_camera(monkeypatch, grab_seconds=0)
    source = CameraSource("left", 0, 1920, 1080, on_demand=True)
    try:
        time.sleep(0.2)
        assert cap.grab.call_count < 60              # 约每 10ms 一次，而不是几十万次
        assert source.read() is not None             # 要帧时照样拿得到
    finally:
        source.close()


def test_on_demand_stalled_camera_does_not_hold_up_the_reader(monkeypatch):
    """需求 §5.4：相机卡住时 read() 超时返回 None，之后不再反复干等。"""
    import threading
    import time

    release = threading.Event()
    cap = Mock()
    cap.get.return_value = 0
    cap.grab.side_effect = lambda: release.wait(5) or True      # 卡住，直到放行
    cap.retrieve.side_effect = lambda: (True, np.zeros((2, 2, 3), dtype=np.uint8))
    monkeypatch.setattr("acepoint.sources.camera.cv2.VideoCapture", lambda *args: cap)
    monkeypatch.setattr("acepoint.sources.camera._READ_TIMEOUT", 0.05)
    source = CameraSource("left", 0, 1920, 1080, on_demand=True)
    try:
        started = time.monotonic()
        assert source.read() is None                 # 第一次：等满超时
        assert source.read() is None                 # 之后：立刻放弃
        assert source.read() is None
        assert time.monotonic() - started < 0.5

        release.set()                                # 相机恢复
        deadline = time.monotonic() + 2
        frame = None
        while frame is None and time.monotonic() < deadline:
            frame = source.read()
        assert frame is not None
    finally:
        release.set()
        source.close()


def test_on_demand_reports_a_dead_camera(monkeypatch):
    cap = Mock()
    cap.get.return_value = 0
    cap.grab.return_value = False                    # 拔线后 grab 一直失败
    monkeypatch.setattr("acepoint.sources.camera.cv2.VideoCapture", lambda *args: cap)
    source = CameraSource("left", 0, 1920, 1080, on_demand=True)
    try:
        for _ in range(3):
            assert source.read() is None
        assert source._failures == 3
        cap.retrieve.assert_not_called()
    finally:
        source.close()
