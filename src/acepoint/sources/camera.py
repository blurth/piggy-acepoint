"""USB 摄像头。

两个要点：
  * 必须走 MJPEG，否则 1080p 会退到 YUYV，USB 带宽吃满、帧率掉到个位数（需求 §2）。
  * BUFFERSIZE=1，read() 永远拿最新画面而不是排队的旧帧 —— 否则手势会"延迟生效"。
"""

from __future__ import annotations

import os
import re
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


_VIDEO_NODE = re.compile(r"/dev/video(\d+)")


def resolve_device(device: int | str) -> int | str:
    """设备号原样返回；设备路径解析成 V4L2 设备号。

    `/dev/videoN` 的编号跟 USB 枚举顺序走，重启、插拔都可能变，同型号两只时
    左右会**静默对调**（比分加反，不报错）。所以配置里推荐写稳定路径：
      /dev/v4l/by-id/usb-<厂商>_<型号>_<序列号>-video-index0   按序列号，换口也不变
      /dev/v4l/by-path/...-usb-0:1.1:1.0-video-index0          按物理 USB 口
    它们都是指向 /dev/videoN 的符号链接，这里解析成 N 再交给 OpenCV 的按号打开，
    比把符号链接路径直接塞给 OpenCV 更可靠。
    """
    if isinstance(device, int):
        return device
    if device.isdigit():
        return int(device)
    real = os.path.realpath(device)
    match = _VIDEO_NODE.fullmatch(real)
    if match:
        return int(match.group(1))
    if not os.path.exists(device):
        raise OSError(f"camera {device} not found (unplugged, or wrong serial / USB port?)")
    return real


class CameraSource(FrameSource):
    def __init__(
        self,
        side: str,
        device: int | str,
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

        target = resolve_device(device)
        # 日志里同时给出配置的路径和实际编号，排查"哪只是哪只"时一眼对上
        self.name = (f"#{target}" if target == device
                     else f"#{target} ({os.path.basename(str(device))})")

        cap = cv2.VideoCapture(target, _backend())
        if not cap.isOpened():
            cap.release()
            raise OSError(f"camera {self.name} could not be opened")

        # 先设分辨率再设 FOURCC。部分摄像头（尤其 Windows + DirectShow）
        # 必须先固定分辨率，MJPG 设置才会生效；否则驱动会回退到 YUY2。
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FOURCC, _fourcc("MJPG"))
        if not cap.set(cv2.CAP_PROP_BUFFERSIZE, 1):
            log.debug("camera %s ignores buffer size; background capture keeps latest frame", self.name)
        if brightness is not None:
            if not cap.set(cv2.CAP_PROP_BRIGHTNESS, brightness):
                log.warning("camera %s rejected brightness=%s", self.name, brightness)
        if sharpness is not None:
            if not cap.set(cv2.CAP_PROP_SHARPNESS, sharpness):
                log.warning("camera %s rejected sharpness=%s", self.name, sharpness)
        self._cap = cap

        self.actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        self.fourcc = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip()
        if (self.actual_width, self.actual_height) != (width, height):
            log.warning("camera %s resolution fell back to %sx%s (requested %sx%s)",
                        self.name, self.actual_width, self.actual_height, width, height)
        if self.fourcc != "MJPG":
            log.warning("camera %s format is %s instead of MJPG; check USB bandwidth",
                        self.name, self.fourcc or "unknown")
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
                    log.exception("camera %s capture failed", self.name)
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
            log.warning("camera %s read is still blocked; capture thread will release on return", self.name)

    @property
    def description(self) -> str:
        return (
            f"camera {self.name} {self.actual_width}x{self.actual_height} "
            f"[{self.fourcc or 'unknown'}] brightness={self.brightness:g} "
            f"sharpness={self.sharpness:g}"
        )
