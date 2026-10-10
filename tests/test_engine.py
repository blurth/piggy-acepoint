"""端到端冒烟：视频源 -> 识别 -> 状态机 -> 比分 -> 显示，整条链路跑通。

需要 models/gesture_recognizer.task（python scripts/fetch_model.py），没有就跳过。
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
pytest.importorskip("mediapipe")

from acepoint.config import (  # noqa: E402
    CaptureConfig,
    Config,
    DisplayConfig,
    RecognitionConfig,
    SourceConfig,
)
from acepoint.displays.base import Display  # noqa: E402
from acepoint.engine import ScoringEngine, crop_roi, roi_rect  # noqa: E402
from acepoint.state import Action, Telemetry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = REPO_ROOT / "models" / "gesture_recognizer.task"


def test_roi_rect_is_centred_by_default():
    assert roi_rect((1080, 1920, 3), 0.6, 0.8) == (384, 108, 1152, 864)


def test_roi_rect_follows_the_centre_ratios():
    """识别区能挪离画面正中 —— 球员举手的高度不等于画面中线。"""
    # 中心落在 (1920*0.47, 1080*0.34) = (902.4, 367.2)，减去半个识别区
    assert roi_rect((1080, 1920, 3), 0.40, 0.27, 0.47, 0.34) == (518, 222, 768, 291)


def test_roi_rect_clamps_to_the_frame():
    """中心贴边时宁可偏离设定的中心，也不能裁出界。"""
    assert roi_rect((1080, 1920, 3), 0.5, 0.5, 0.0, 1.0) == (0, 540, 960, 540)


def test_crop_keeps_pixel_density():
    """识别区是"裁"出来的，不是缩放 —— 手上的像素数不能变少。"""
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    roi = crop_roi(frame, 0.6, 0.8)
    assert roi.shape == (864, 1152, 3)


def test_offset_crop_keeps_the_requested_size():
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    assert crop_roi(frame, 0.40, 0.27, 0.47, 0.34).shape == (291, 768, 3)


def test_full_ratio_keeps_the_whole_frame():
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    assert crop_roi(frame, 1.0, 1.0).shape == frame.shape


class RecordingDisplay(Display):
    wants_preview = True

    def __init__(self) -> None:
        self.updates: list[Telemetry] = []
        self.previews: list[str] = []

    def update(self, telemetry: Telemetry) -> None:
        self.updates.append(telemetry)

    def push_preview(self, side: str, jpeg: bytes) -> None:
        assert jpeg.startswith(b"\xff\xd8")          # JPEG 魔数
        self.previews.append(side)


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, str]:
    """两段无手势的合成视频，只用来验证链路。"""
    out = tmp_path_factory.mktemp("clips")
    paths = {}
    for side, shade in (("left", 60), ("right", 120)):
        path = out / f"{side}.avi"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"MJPG"), 30, (320, 240))
        if not writer.isOpened():
            pytest.skip("当前 OpenCV 没有可用的视频编码器")
        for _ in range(60):
            writer.write(np.full((240, 320, 3), shade, dtype=np.uint8))
        writer.release()
        paths[side] = str(path)
    return paths


@pytest.fixture
def config(clips) -> Config:
    if not MODEL.exists():
        pytest.skip(f"缺少手势模型 {MODEL}，先跑 python scripts/fetch_model.py")
    return Config(
        recognition=RecognitionConfig(model_path=str(MODEL), per_source_recognizer=True),
        capture=CaptureConfig(poll_hz=20, crop_w_ratio=1.0, crop_h_ratio=1.0),
        display=DisplayConfig(backends=("console",)),
        sources=tuple(
            SourceConfig(side=side, kind="video", path=path) for side, path in clips.items()
        ),
        root=REPO_ROOT,
    )


def test_engine_runs_both_sides(config):
    engine = ScoringEngine(config)
    recorder = RecordingDisplay()
    engine.display.displays.append(recorder)   # wants_preview 由后端聚合，无需手动打开

    engine.run(max_seconds=1.0)

    assert len(recorder.updates) >= 4
    last = recorder.updates[-1]
    assert set(last.sides) == {"left", "right"}
    assert all(status.online for status in last.sides.values())
    assert {"left", "right"} <= set(recorder.previews)
    # 合成画面里没有真人手势，不该凭空加分
    assert (last.snapshot.left, last.snapshot.right) == (0, 0)


def test_manual_action_from_the_web_ui_reaches_the_board(config):
    engine = ScoringEngine(config)
    try:
        engine._enqueue_manual("left", Action.INCREMENT)
        engine._enqueue_manual("left", Action.INCREMENT)
        engine._drain_manual()
        assert engine.board.snapshot().text == "2:0"
        # 手动改分后本半场进冷却，避免和手势打架。状态机走单调时钟，
        # 不能拿 Snapshot.updated_at（挂钟）去问它。
        assert engine.fsms["left"].status(time.monotonic()).cooldown_remaining > 0
    finally:
        engine.close()


def test_telemetry_separates_detection_from_the_fsm_state(config):
    """冷却期间状态机会清空手势，但界面要能看出识别器还在正常输出。

    否则冷却时网页显示「识别手势 —」，看着像识别失灵，实际只是不计分。
    """
    from acepoint.recognition import Observation

    engine = ScoringEngine(config)
    try:
        engine._last_obs["left"] = Observation(gesture="Victory", confidence=0.9)
        engine.fsms["left"].force_cooldown(time.monotonic())

        status = engine._telemetry().sides["left"]
        assert status.gesture is None          # 状态机不累计
        assert status.detected == "Victory"    # 但确实识别到了
        assert status.cooldown_remaining > 0
    finally:
        engine.close()


def test_repeated_frame_is_not_re_recognised(config):
    """视频源没走到下一帧时会把同一个 Frame 再给一次，不该重跑最贵的那步推理。"""
    engine = ScoringEngine(config)
    try:
        source = engine.sources["left"]
        calls = []
        original = engine.reader.observe
        engine.reader.observe = lambda side, roi: (calls.append(side), original(side, roi))[1]

        engine._tick("left", source, 0.0)
        assert len(calls) == 1

        frame = engine._last_frame["left"]
        source.read = lambda: frame              # 模拟"还是上一帧"
        engine._tick("left", source, 0.1)
        engine._tick("left", source, 0.2)
        assert len(calls) == 1                   # 依然只推理过一次
    finally:
        engine.close()


def test_a_raising_source_does_not_kill_the_other_half_court(config):
    """需求 §5.4：单路故障不能拖垮另一路。取帧返回 None 早就处理了，
    但识别路径抛异常（MediaPipe / OpenCV 拔线）才是真正会掀翻主循环的那条。"""
    engine = ScoringEngine(config)
    recorder = RecordingDisplay()
    engine.display.displays.append(recorder)

    def explode():
        raise RuntimeError("camera yanked")

    engine.sources["left"].read = explode
    engine.run(max_seconds=1.0)                   # 不该抛出来

    healthy = [t.sides["right"] for t in recorder.updates if "right" in t.sides]
    assert healthy, "健康的半场应该照常产出遥测"
    assert healthy[-1].online
    assert healthy[-1].fps > 0                    # 它一直在正常取帧识别


def test_a_raising_display_does_not_kill_the_loop(config):
    engine = ScoringEngine(config)
    try:
        class Exploding(Display):
            wants_preview = True

            def update(self, telemetry):
                raise RuntimeError("display on fire")

            def push_preview(self, side, jpeg):
                raise RuntimeError("preview on fire")

        engine.display.displays.append(Exploding())
        engine.run(max_seconds=0.6)
        assert engine.board.snapshot().version >= 0
    finally:
        engine.close()


def test_per_side_rate_survives_a_source_missing_at_boot(config):
    """启动时少一路，活着那路的采样率不能翻倍。

    翻倍会让所有防误触参数失准 —— 实测 0.2 秒的误识触发率从 0% 变成 100%。
    """
    broken = replace(config.sources[0], path="does-not-exist.mp4")
    engine = ScoringEngine(replace(config, sources=(broken, config.sources[1])))
    recorder = RecordingDisplay()
    engine.display.displays.append(recorder)
    try:
        assert len(engine.sources) == 1          # 确实只起来一路
        engine.run(max_seconds=1.5)
        survivor = [t.sides["right"] for t in recorder.updates if "right" in t.sides]
        expected = config.capture.poll_hz / len(config.sources)
        assert survivor[-1].fps == pytest.approx(expected, rel=0.25)
    finally:
        engine.close()


def test_close_survives_a_source_that_refuses_to_close(config):
    """一个关不掉，不能连累后面的都关不掉。"""
    engine = ScoringEngine(config)

    def explode():
        raise RuntimeError("stuck")

    engine.sources["left"].close = explode
    closed = []
    engine.sources["right"].close = lambda: closed.append("right")
    engine.close()
    assert closed == ["right"]


def test_missing_video_disables_only_that_side(config):
    broken = replace(config.sources[0], path="does-not-exist.mp4")
    engine = ScoringEngine(replace(config, sources=(broken, config.sources[1])))
    try:
        assert list(engine.sources) == [config.sources[1].side]
    finally:
        engine.close()
