"""USB 摄像头。

两个要点：
  * 必须走 MJPEG，否则 1080p 会退到 YUYV，USB 带宽吃满、帧率掉到个位数（需求 §2）。
  * BUFFERSIZE=1，read() 永远拿最新画面而不是排队的旧帧 —— 否则手势会"延迟生效"。

两种采集方式（`capture.decode_on_demand`）：
  * 持续解码（默认）：后台线程把相机出的每一帧都解出来，read() 立刻拿走最新的。
  * 按需解码：read() 时才向后台线程要一帧。相机出 30fps 而每路只用 5fps，
    Pi 4 上两路 1080p MJPEG 全解码要白占一个多核，按需解码把这部分省掉。
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
import threading

import cv2

from acepoint.sources.base import Frame, FrameSource

log = logging.getLogger(__name__)

# 按需解码时用来分辨"队列里压着的旧帧"和"刚出的新帧"：旧帧 grab() 立刻返回，
# 新帧要等相机出下一帧。等待超过这个时长就认为拿到的是新帧。
_FRESH_GRAB_SECONDS = 0.004
# 最多丢多少张旧帧。BUFFERSIZE=1 时只有 1 张；相机不认这个设置时 OpenCV 默认排 4 张。
_MAX_STALE_FRAMES = 6
# grab() 不阻塞的后端（DShow）在没人要帧时每轮歇这么久；有人要帧会立刻被唤醒
_IDLE_POLL_SECONDS = 0.01
# read() 最多等后台线程多久。正常是一个帧间隔加一次解码（几十毫秒）；
# 超过就当这一拍没取到，不能让一只卡住的相机拖住另一半场（需求 §5.4）。
_READ_TIMEOUT = 0.3


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
        on_demand: bool = False,
    ) -> None:
        super().__init__(side)
        self.device = device
        self.mirror = mirror
        self.on_demand = on_demand
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
        # 按需解码的握手：read() 置 _request，采集线程取到帧后置 _ready
        self._request = threading.Event()
        self._ready = threading.Event()
        self._waiting = False            # 上一次请求超时了，采集线程还没回来
        self._thread = threading.Thread(
            target=self._capture_on_demand if on_demand else self._capture,
            name=f"camera-{side}", daemon=True)
        self._thread.start()

    def _capture_on_demand(self) -> None:
        """一直 grab()（只出队、不解码），read() 来要帧时才 retrieve() 解码刚到的那一帧。

        grab() 很便宜，持续调用是为了把驱动队列排空：不排的话队列里压着的是
        几百毫秒前的旧画面。V4L2 的 grab() 会阻塞到相机出下一帧；DShow 的 grab()
        立刻返回、等新帧的是 retrieve()，所以没人要帧时要自己歇一下，别空转。

        取帧放在这个线程里而不是 read() 的调用方线程：拔线时驱动里的 grab()
        会一直阻塞到超时，放在这里才不会把主循环一起卡死。
        """
        stale = 0
        try:
            while not self._stop.is_set():
                image = None
                try:
                    started = time.monotonic()
                    grabbed = self._cap.grab()
                    waited = time.monotonic() - started
                    if grabbed and not self._request.is_set():
                        if waited < _FRESH_GRAB_SECONDS:
                            self._request.wait(_IDLE_POLL_SECONDS)
                        continue
                    if grabbed and waited < _FRESH_GRAB_SECONDS and stale < _MAX_STALE_FRAMES:
                        # 没等就拿到了：多半是队列里积压的旧帧，再取，直到真的等到新帧
                        stale += 1
                        continue
                    stale = 0
                    if grabbed:
                        ok, image = self._cap.retrieve()
                        if not ok:
                            image = None
                        elif self.mirror and image is not None:
                            image = cv2.flip(image, 1)
                except Exception:
                    log.exception("camera %s capture failed", self.name)
                if image is None:
                    self._stop.wait(0.05)          # 掉线/异常：歇一下再试，别狂转
                    if not self._request.is_set():
                        continue
                with self._lock:
                    if image is None:
                        self._latest = None
                    else:
                        self._index += 1
                        self._latest = Frame(image=image, index=self._index)
                self._request.clear()
                self._ready.set()
        finally:
            # 句柄只由采集线程释放，避免与正在阻塞的 grab 并发操作。
            self._cap.release()

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
        if self.on_demand and not self._request_frame():
            self._mark(False)
            return None
        with self._lock:
            frame = self._latest
            self._latest = None
        self._mark(frame is not None)
        return frame

    def _request_frame(self) -> bool:
        """向采集线程要一帧并等它回来；相机卡住时返回 False，且不再反复干等。"""
        if self._waiting:
            if not self._ready.is_set():
                return False             # 上次要的还没回来：相机仍卡着，这一拍直接放弃
            self._waiting = False        # 回来了，但那一帧已经过时，重新要一帧
        self._ready.clear()
        self._request.set()
        if self._ready.wait(_READ_TIMEOUT):
            return True
        self._waiting = True
        return False

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
            f"{' on-demand' if self.on_demand else ''}"
        )
