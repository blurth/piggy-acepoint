"""视频源抽象：真实摄像头和录制视频对上层是同一个接口。"""

from __future__ import annotations

import abc
from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    """一帧原始画面（BGR，未裁剪）。"""

    image: np.ndarray
    index: int = 0


class FrameSource(abc.ABC):
    """半场画面来源。"""

    def __init__(self, side: str) -> None:
        self.side = side
        self._failures = 0

    @abc.abstractmethod
    def read(self) -> Frame | None:
        """取最新一帧；取不到返回 None（不抛异常，单路故障不能拖垮另一路）。"""

    @abc.abstractmethod
    def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """给日志和网页显示的一句话说明。"""

    @property
    def online(self) -> bool:
        """连续取帧失败过多则认为该路掉线（需求 §5.4）。"""
        return self._failures < 30

    # -------------------------------------------------- 播放进度（仅录制视频有）

    @property
    def duration(self) -> float:
        """素材总时长（秒）。实时摄像头没有这个概念，返回 0。"""
        return 0.0

    @property
    def position(self) -> float:
        """当前播放位置（秒）。"""
        return 0.0

    @property
    def loops(self) -> int:
        """已经循环播放了几遍。"""
        return 0

    def _mark(self, ok: bool) -> None:
        self._failures = 0 if ok else self._failures + 1

    def note_failure(self) -> None:
        """引擎在这一路上捕获到异常时调用，让 online 状态如实反映故障。"""
        self._mark(False)
