"""USB 摄像头。

两个要点：
  * 必须走 MJPEG，否则 1080p 会退到 YUYV，USB 带宽吃满、帧率掉到个位数（需求 §2）。
  * BUFFERSIZE=1，read() 永远拿最新画面而不是排队的旧帧 —— 否则手势会"延迟生效"。
"""

from __future__ import annotations

import sys
import logging
import threading

import cv2

from acepoint.sources.base import Frame, FrameSource

log = logging.getLogger(__name__)


def _backend() -> int:
    """Pi/Linux 用 V4L2；Windows 上开发调试用 DShow。"""
    if sys.platform.startswith("linux"):
        return cv2.CAP_V4L2
    if sys.platform == "win32":
        return cv2.CAP_DSHOW
    return cv2.CAP_ANY


def _fourcc(code: str) -> int:
    """OpenCV 5 去掉了模块级的 VideoWriter_fourcc，两种写法都兜住。"""
    factory = getattr(cv2, "VideoWriter_fourcc", None) or cv2.VideoWriter.fourcc
    return factory(*code)


class CameraSource(FrameSource):
    def __init__(
        self,
        side: str,
        device: int,
        width: int,
        height: int,
        *,
        mirror: bool = False,
        brightness: float | None = None,
        sharpness: float | None = None,
    ) -> None:
        super().__init__(side)
        self.device = device
        self.mirror = mirror
        self._index = 0

        cap = cv2.VideoCapture(device, _backend())
        if not cap.isOpened():
            cap.release()
            raise OSError(f"camera #{device} could not be opened")

        # 先设分辨率再设 FOURCC。部分摄像头（尤其 Windows + DirectShow）
        # 必须先固定分辨率，MJPG 设置才会生效；否则驱动会回退到 YUY2。
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FOURCC, _fourcc("MJPG"))
        if not cap.set(cv2.CAP_PROP_BUFFERSIZE, 1):
            log.debug("camera #%s ignores buffer size; background capture keeps latest frame", device)
        if brightness is not None:
            if not cap.set(cv2.CAP_PROP_BRIGHTNESS, brightness):
                log.warning("camera #%s rejected brightness=%s", device, brightness)
        if sharpness is not None:
            if not cap.set(cv2.CAP_PROP_SHARPNESS, sharpness):
                log.warning("camera #%s rejected sharpness=%s", device, sharpness)
        self._cap = cap

        self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.fourcc = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip()
        if (self.actual_width, self.actual_height) != (width, height):
            log.warning("camera #%s resolution fell back to %sx%s (requested %sx%s)",
                        device, self.actual_width, self.actual_height, width, height)
        if self.fourcc != "MJPG":
            log.warning("camera #%s format is %s instead of MJPG; check USB bandwidth",
                        device, self.fourcc or "unknown")
        self.brightness = cap.get(cv2.CAP_PROP_BRIGHTNESS)
        self.sharpness = cap.get(cv2.CAP_PROP_SHARPNESS)
        self._latest: Frame | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._capture, name=f"camera-{side}", daemon=True)
        self._thread.start()

    def _capture(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    ok, image = self._cap.read()
                    if not ok or image is None:
                        with self._lock:
                            self._latest = None
                        self._stop.wait(0.05)
                        continue
                    if self.mirror:
                        image = cv2.flip(image, 1)
                    self._index += 1
                    with self._lock:
                        self._latest = Frame(image=image, index=self._index)
                except Exception:
                    log.exception("camera #%s capture failed", self.device)
                    with self._lock:
                        self._latest = None
                    self._stop.wait(0.1)
        finally:
            # 句柄只由采集线程释放，避免与正在阻塞的 read 并发操作。
            self._cap.release()

    def read(self) -> Frame | None:
        with self._lock:
            frame = self._latest
            self._latest = None
        self._mark(frame is not None)
        return frame

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._thread.is_alive():
            log.warning("camera #%s read is still blocked; capture thread will release on return", self.device)

    @property
    def description(self) -> str:
        return (
            f"camera #{self.device} {self.actual_width}x{self.actual_height} "
            f"[{self.fourcc or 'unknown'}] brightness={self.brightness:g} "
            f"sharpness={self.sharpness:g}"
        )
