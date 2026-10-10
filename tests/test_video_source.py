"""录制视频源：重点验证"按挂钟时间对齐"，否则计分时序会失真。"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from acepoint.sources import video as video_module  # noqa: E402
from acepoint.sources.video import VideoFileSource  # noqa: E402

FPS = 30
TOTAL = 60          # 2 秒
SIZE = (160, 120)


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> str:
    """每帧填一个随帧号递增的灰度值，方便断言"读到的是第几帧"。"""
    path = tmp_path_factory.mktemp("clips") / "clip.avi"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), FPS, SIZE)
    if not writer.isOpened():
        pytest.skip("当前 OpenCV 没有可用的视频编码器")
    for i in range(TOTAL):
        writer.write(np.full((SIZE[1], SIZE[0], 3), i * 4, dtype=np.uint8))
    writer.release()
    return str(path)


def decoded_index(frame) -> int:
    """从画面灰度反推帧号（MJPG 有损，取中心区域均值）。"""
    patch = frame.image[40:80, 40:120]
    return int(round(float(patch.mean()) / 4))


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(video_module.time, "monotonic", lambda: now["t"])
    return now


def test_metadata(clip):
    source = VideoFileSource("left", clip)
    try:
        assert source.fps == pytest.approx(FPS, abs=0.5)
        assert source.total_frames == TOTAL
        assert source.duration == pytest.approx(2.0, abs=0.1)
        assert "clip.avi" in source.description
    finally:
        source.close()


def test_realtime_skips_frames_to_track_the_wall_clock(clip, clock):
    """主循环每路只有约 5fps，视频是 30fps —— 必须跳帧，不能放慢播放。"""
    source = VideoFileSource("left", clip, realtime=True, loop=True)
    try:
        assert decoded_index(source.read()) == 0
        clock["t"] += 1.0                       # 过了 1 秒 -> 应该在第 30 帧附近
        assert decoded_index(source.read()) == pytest.approx(30, abs=1)
        clock["t"] += 0.5
        assert decoded_index(source.read()) == pytest.approx(45, abs=1)
    finally:
        source.close()


def test_same_instant_reuses_the_previous_frame(clip, clock):
    source = VideoFileSource("left", clip, realtime=True)
    try:
        first = source.read()
        assert source.read() is first           # 时间没走，不重复解码
    finally:
        source.close()


def test_realtime_loops_seamlessly(clip, clock):
    source = VideoFileSource("left", clip, realtime=True, loop=True)
    try:
        source.read()
        clock["t"] += 2.5                       # 75 帧 -> 绕回第 15 帧
        assert decoded_index(source.read()) == pytest.approx(15, abs=1)
        assert source.online
    finally:
        source.close()


def test_playback_progress_is_reported(clip, clock):
    """网页上的播放进度条靠这几个值。"""
    source = VideoFileSource("left", clip, realtime=True, loop=True)
    try:
        source.read()
        assert source.duration == pytest.approx(2.0, abs=0.1)
        assert source.position == pytest.approx(0.0, abs=0.05)
        assert source.loops == 0

        clock["t"] += 1.0
        source.read()
        assert source.position == pytest.approx(1.0, abs=0.05)
        assert source.loops == 0                # 还没播完一遍
    finally:
        source.close()


def test_loop_counter_advances(clip, clock):
    source = VideoFileSource("left", clip, realtime=True, loop=True)
    try:
        source.read()
        clock["t"] += 2.5                       # 越过一整遍
        source.read()
        assert source.loops == 1
        assert source.position == pytest.approx(0.5, abs=0.05)

        clock["t"] += 2.0                       # 再一遍
        source.read()
        assert source.loops == 2
    finally:
        source.close()


def test_live_sources_report_no_duration():
    """摄像头是实时流，没有进度概念 —— 界面靠 duration==0 隐藏进度条。"""
    from acepoint.sources.base import FrameSource

    class Live(FrameSource):
        def read(self):
            return None

        def close(self):
            pass

        @property
        def description(self):
            return "live"

    live = Live("left")
    assert (live.duration, live.position, live.loops) == (0.0, 0.0, 0)


def test_without_loop_the_source_goes_offline(clip, clock):
    source = VideoFileSource("left", clip, realtime=True, loop=False)
    try:
        source.read()
        clock["t"] += 5.0                       # 远超视频长度
        source.read()
        assert not source.online
        assert source.read() is None
    finally:
        source.close()


def test_sequential_mode_reads_every_frame(clip):
    source = VideoFileSource("left", clip, realtime=False, loop=False)
    try:
        for expected in range(5):
            assert decoded_index(source.read()) == pytest.approx(expected, abs=1)
    finally:
        source.close()


def test_sequential_mode_loops(clip):
    source = VideoFileSource("left", clip, realtime=False, loop=True)
    try:
        for _ in range(TOTAL):
            assert source.read() is not None
        assert source.read() is not None        # 播完自动回到开头
        assert source.online
    finally:
        source.close()


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        VideoFileSource("left", tmp_path / "nope.mp4")


def test_mirror_flips_the_image(clip, clock):
    plain = VideoFileSource("left", clip, realtime=True)
    mirrored = VideoFileSource("left", clip, realtime=True, mirror=True)
    try:
        a, b = plain.read().image, mirrored.read().image
        assert np.array_equal(b, cv2.flip(a, 1))
    finally:
        plain.close()
        mirrored.close()
