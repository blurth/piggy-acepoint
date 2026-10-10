"""录制视频文件作为视频源 —— 前期没有摄像头/没到球馆时的替身。

关键是 **按挂钟时间对齐**：主循环每路只跑约 5fps，而视频通常是 30fps。
如果傻读下一帧，视频会被放慢 6 倍，"保持 1 秒"在视频里就变成了 6 秒，
计分时序完全失真。所以这里按 elapsed * fps 算出应该显示第几帧，
中间的帧用 grab() 跳过（跨度大就直接 seek）。
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2

from acepoint.sources.base import Frame, FrameSource

# 目标帧与当前帧相差超过这个数量就直接 seek，而不是一帧帧 grab
_SEEK_THRESHOLD = 30
_DEFAULT_FPS = 30.0


class VideoFileSource(FrameSource):
    def __init__(
        self,
        side: str,
        path: str | Path,
        *,
        loop: bool = True,
        realtime: bool = True,
        start_offset: float = 0.0,
        mirror: bool = False,
    ) -> None:
        super().__init__(side)
        self.path = Path(path)
        if not self.path.exists():
            raise OSError(f"video file not found: {self.path}")

        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise OSError(f"video file could not be decoded: {self.path}")
        self._cap = cap

        fps = cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if fps and fps > 1e-3 else _DEFAULT_FPS
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.total_frames = total if total > 0 else 0
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self.loop = loop
        self.realtime = realtime
        self.start_offset = start_offset
        self.mirror = mirror

        self._t0: float | None = None
        self._pos = -1                      # 已解码到的帧号
        self._last: Frame | None = None
        self._finished = False
        self._loops = 0                     # 已经循环播放了几遍

    # ---------------------------------------------------------------- 读帧

    def read(self) -> Frame | None:
        if self._finished:
            return None
        frame = self._read_realtime() if self.realtime else self._read_sequential()
        self._mark(frame is not None)
        return frame

    def _read_sequential(self) -> Frame | None:
        """不做时间对齐，逐帧读 —— 离线批量分析时用。"""
        ok, image = self._cap.read()
        if not ok:
            if not self.loop:
                self._finished = True
                return None
            self._rewind(0)
            self._loops += 1
            ok, image = self._cap.read()
            if not ok:
                self._finished = True
                return None
        self._pos += 1
        return self._wrap(image)

    def _read_realtime(self) -> Frame | None:
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now

        target = int((now - self._t0 + self.start_offset) * self.fps)

        if self.total_frames:
            if target >= self.total_frames:
                if not self.loop:
                    self._finished = True
                    return self._last
                # 循环播放：把时间轴原点往前推整段视频的时长，避免 t0 漂移
                loops, target = divmod(target, self.total_frames)
                self._t0 += loops * self.total_frames / self.fps
                self._loops += loops

        if target == self._pos and self._last is not None:
            return self._last                # 还没到下一帧，复用上一帧

        if target < self._pos or target - self._pos > _SEEK_THRESHOLD:
            self._rewind(target)
        else:
            for _ in range(target - self._pos - 1):
                if not self._cap.grab():
                    break

        ok, image = self._cap.read()
        if not ok:
            # 有些容器的帧数元数据不准，读到尾就当作一轮结束
            if not self.loop:
                self._finished = True
                return self._last
            self._rewind(0)
            self._t0 = now
            self._loops += 1
            ok, image = self._cap.read()
            if not ok:
                self._finished = True
                return self._last
            target = 0

        self._pos = target
        return self._wrap(image)

    # ------------------------------------------------------------- 内部工具

    def _rewind(self, index: int) -> None:
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, index))
        self._pos = max(0, index) - 1

    def _wrap(self, image) -> Frame:
        if self.mirror:
            image = cv2.flip(image, 1)
        self._last = Frame(image=image, index=self._pos)
        return self._last

    # ---------------------------------------------------------------- 元信息

    @property
    def online(self) -> bool:
        return not self._finished and super().online

    @property
    def duration(self) -> float:
        return self.total_frames / self.fps if self.total_frames else 0.0

    @property
    def position(self) -> float:
        return max(0, self._pos) / self.fps if self.fps else 0.0

    @property
    def loops(self) -> int:
        return self._loops

    def close(self) -> None:
        self._cap.release()

    @property
    def description(self) -> str:
        length = f"{self.duration:.1f}s" if self.duration else "unknown length"
        mode = "realtime" if self.realtime else "sequential"
        return (
            f"video {self.path.name} {self.width}x{self.height} "
            f"@{self.fps:.1f}fps {length} ({mode}{', loop' if self.loop else ''})"
        )
