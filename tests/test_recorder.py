"""边计分边录像：时间轴对齐挂钟、分段、磁盘下限、日志。"""

from __future__ import annotations

import csv
import logging
from collections import namedtuple

import cv2
import numpy as np
import pytest

from acepoint import recorder as recorder_module
from acepoint.config import ConfigError, RecordConfig
from acepoint.recorder import SessionRecorder


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch):
    """测试走 OpenCV 的 AVI 分支，不依赖机器上有没有 ffmpeg。"""
    monkeypatch.setattr(recorder_module.shutil, "which", lambda name: None)
    # 测试一口气塞完所有帧；实机上每 0.2 秒才来一帧，队列不会满
    monkeypatch.setattr(recorder_module, "_QUEUE_FRAMES", 1000)


def _frame(value: int) -> np.ndarray:
    return np.full((48, 64, 3), value, dtype=np.uint8)


def _frames(path) -> int:
    capture = cv2.VideoCapture(str(path))
    try:
        return int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        capture.release()


def _make(tmp_path, **overrides) -> SessionRecorder:
    config = RecordConfig(enabled=True, **overrides)
    return SessionRecorder(config, tmp_path, ["left"], fps=5.0, clock=lambda: 0.0)


def test_slow_sampling_is_padded_to_wall_clock(tmp_path):
    """采样只有 2.5fps 时，5fps 的录像要补帧：4 秒就该是 4 秒，不能放成 2 秒。"""
    rec = _make(tmp_path)
    for i in range(10):                         # 0, 0.4, ... 3.6 秒
        rec.frame("left", _frame(i * 20), 100.0 + i * 0.4)
    rec.close()
    (video,) = rec.dir.glob("left_000.*")
    assert _frames(video) == 19                 # 序号 0..18 = 3.6 秒 + 首帧


def test_fast_sampling_drops_frames_ahead_of_the_clock(tmp_path):
    rec = _make(tmp_path)
    for i in range(20):                         # 10fps 喂进 5fps 的录像，0~1.9 秒
        rec.frame("left", _frame(i), 100.0 + i * 0.1)
    rec.close()
    (video,) = rec.dir.glob("left_000.*")
    assert _frames(video) == 11                 # 序号 0..10：同一序号只写一次，不会多出时长


def test_segments_rotate_and_report_position(tmp_path):
    rec = _make(tmp_path, segment_minutes=1 / 60)   # 1 秒一段
    positions = [rec.frame("left", _frame(i), 100.0 + i * 0.2) for i in range(8)]
    rec.close()
    assert positions[0] == ("left_000", 0.0)
    assert positions[4] == ("left_000", 0.8)
    assert positions[5] == ("left_001", 0.0)    # 满 5 帧(1 秒)换段，段内从 0 计
    assert sorted(p.stem for p in rec.dir.glob("left_*")) == ["left_000", "left_001"]


def test_low_disk_stops_video_but_keeps_the_log(tmp_path, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(recorder_module.shutil, "disk_usage",
                        lambda path: usage(10**10, 10**10, 10**8))   # 0.1 GB
    rec = _make(tmp_path, min_free_gb=2.0)
    assert rec.frame("left", _frame(0), 100.0) == ("", 0.0)
    rec.event(3.5, side="left", action="increment", score="1:0")
    rec.close()
    assert not list(rec.dir.glob("left_*"))
    with (rec.dir / "events.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["action"] == "increment" and rows[0]["score"] == "1:0"
    assert rows[0]["t"] == "3.500"


def test_video_off_still_writes_events_and_log(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="acepoint")
    rec = _make(tmp_path, video=False)
    assert rec.frame("left", _frame(0), 0.0) == ("", 0.0)
    rec.event(1.0, side="left", detected="Victory", confidence=0.91)
    rec.close()
    assert (rec.dir / "events.csv").read_text(encoding="utf-8").count("\n") == 2
    assert "recording session" in (rec.dir / "acepoint.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [{"jpeg_quality": 0}, {"segment_minutes": 0},
                                 {"min_free_gb": -1}])
def test_record_config_validation(bad):
    with pytest.raises(ConfigError):
        RecordConfig(**bad)
