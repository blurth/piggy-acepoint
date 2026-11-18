"""边计分边录像 + 计分日志（`[record]`），给事后调试和重新标定用。

每次启动建一个会话目录 `<dir>/<YYYYmmdd_HHMMSS>/`：

    left_000.mkv, left_001.mkv ...   该路识别用到的帧，整帧不裁，按分段切
                                     （没有 ffmpeg 时是 .avi；events.csv 的 segment 列不带扩展名）
    right_000.mkv ...
    events.csv                       每路每拍一行：认到什么、状态机在干什么、比分
    acepoint.log                     程序日志（实机上 journald 不持久，重启就没）

为什么录在计分程序里面而不是另开一个录像进程：摄像头被计分程序独占，
外部根本打不开。顺带的好处是录下的就是识别器当时看到的那一帧。

三条硬约束：

- **不能拖慢计分**。编码在每路自己的后台线程里做，Linux 上降到 nice 10；
  队列满了直接丢帧（记数），主线程永远不等。
- **时间轴对齐挂钟**。录像按固定帧率（每路采样率）写，帧的序号由采样时刻
  算出来，采样慢了就重复上一帧补齐 —— 这样 VideoFileSource 回放和
  scripts/ 下的分析工具量出来的"保持了几秒"才是真的。events.csv 的
  `video_s` 列就是该帧在当前分段里的秒数。
- **热拔电只坏最后一段**。实机没有关机键：容器用 Matroska（边写边可读，
  截断了前面照样能放），每段 `segment_minutes` 分钟。

有 ffmpeg 就把 JPEG 直接灌进 ffmpeg 封装（不重编码，几乎不占 CPU）；
没有（Windows 开发机）就退回 OpenCV 的 MJPG AVI。
"""

from __future__ import annotations

import csv
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from acepoint.config import RecordConfig

log = logging.getLogger(__name__)

EVENT_FIELDS = (
    "wall_time", "t", "side", "segment", "video_s", "fps", "recognize_ms",
    "hands", "raw", "confidence", "detected", "gesture", "held", "required",
    "cooldown", "action", "score",
)
_QUEUE_FRAMES = 6            # 每路最多排队几帧；1080p BGR 一帧 6MB
_FSYNC_SECONDS = 10.0        # events.csv 至少这么久落一次盘（热拔电最多丢这么多）


@dataclass
class _Job:
    index: int                       # 该帧在当前分段里的序号
    image: np.ndarray | None = None
    path: Path | None = None         # 非 None 表示从这一帧起换新分段


class _Sink:
    """一个分段文件。ffmpeg 封装 JPEG，或者 OpenCV 自己编。"""

    def __init__(self, path: Path, fps: float, size: tuple[int, int], quality: int) -> None:
        self.path = path
        self.quality = quality
        self._proc: subprocess.Popen | None = None
        self._writer: cv2.VideoWriter | None = None
        self._last: bytes | np.ndarray | None = None
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "image2pipe", "-framerate", f"{fps:g}", "-c:v", "mjpeg", "-i", "-",
                   "-c:v", "copy", "-f", "matroska", str(path)]
            if os.name != "nt" and shutil.which("nice"):
                cmd = ["nice", "-n", "10", *cmd]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.path = path.with_suffix(".avi")
            self._writer = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*"MJPG"),
                                           fps, size)
            if not self._writer.isOpened():
                raise OSError(f"cannot open {self.path} for writing")

    def write(self, image: np.ndarray) -> None:
        if self._proc is not None:
            ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
            if not ok:
                return
            self._last = buffer.tobytes()
        else:
            self._last = image
        self.repeat()

    def repeat(self) -> None:
        """把上一帧再写一次（采样慢于录像帧率时补齐时间轴）。"""
        if self._last is None:
            return
        if self._proc is not None:
            self._proc.stdin.write(self._last)
        else:
            self._writer.write(self._last)

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            finally:
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if self._writer is not None:
            self._writer.release()


class _SideWriter:
    """一路的后台编码线程。主线程只往队列里放，满了就丢。"""

    def __init__(self, side: str, fps: float, quality: int) -> None:
        self.side = side
        self.fps = fps
        self.quality = quality
        self.dropped = 0
        self.failed = False
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=_QUEUE_FRAMES)
        self._thread = threading.Thread(target=self._run, name=f"record-{side}", daemon=True)
        self._thread.start()

    def put(self, job: _Job) -> None:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        # 结束标记必须进得去，哪怕要等编码线程腾位置
        self._queue.put(None)
        self._thread.join(timeout=15)

    def _run(self) -> None:
        if os.name != "nt":
            try:   # 只降这一个线程，识别线程不受影响
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
            except (AttributeError, OSError):
                pass
        sink: _Sink | None = None
        written = 0
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    break
                if self.failed:
                    continue
                try:
                    if job.path is not None:
                        if sink is not None:
                            sink.close()
                        h, w = job.image.shape[:2]
                        sink = _Sink(job.path, self.fps, (w, h), self.quality)
                        written = 0
                        log.info("[%s] recording to %s", self.side, sink.path)
                    if sink is None:
                        continue
                    # 序号落后于采样时刻：重复上一帧补到位；超前（采样快于帧率）就丢
                    while written < job.index:
                        sink.repeat()
                        written += 1
                    if written == job.index:
                        sink.write(job.image)
                        written += 1
                except (OSError, ValueError, cv2.error):
                    log.exception("[%s] 录像写入失败，本路停止录像（计分不受影响）", self.side)
                    self.failed = True
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:                # noqa: BLE001
                    log.exception("[%s] 关闭录像文件失败", self.side)


class SessionRecorder:
    """一次运行的录像 + 日志。所有公开方法只在主线程调用，且绝不抛异常。"""

    def __init__(self, config: RecordConfig, root: Path, sides: list[str], fps: float,
                 *, clock=time.monotonic) -> None:
        self.config = config
        self.fps = fps
        self._clock = clock
        self.dir = root / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self._segment_frames = max(1, round(config.segment_minutes * 60 * fps))
        # 每路：当前分段号、分段起点（单调时钟）
        self._segments: dict[str, tuple[int, float] | None] = dict.fromkeys(sides)
        self._writers: dict[str, _SideWriter] = {}
        self._video_on = config.video
        if self._video_on:
            self._writers = {s: _SideWriter(s, fps, config.jpeg_quality) for s in sides}

        self._events_file = (self.dir / "events.csv").open("w", newline="", encoding="utf-8")
        self._events = csv.writer(self._events_file)
        self._events.writerow(EVENT_FIELDS)
        self._t0 = self._last_sync = clock()

        self._log_handler = logging.FileHandler(self.dir / "acepoint.log", encoding="utf-8")
        self._log_handler.setLevel(logging.INFO)
        self._log_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logging.getLogger("acepoint").addHandler(self._log_handler)
        log.info("recording session %s (video %s, %.1f fps per side, %g min segments)",
                 self.dir, "on" if self._video_on else "off", fps, config.segment_minutes)
        self._check_disk()

    # ---------------------------------------------------------------- 录像

    def frame(self, side: str, image: np.ndarray, now: float) -> tuple[str, float]:
        """登记一帧，返回 (分段文件名, 在分段里的秒数) 供 events.csv 对照。"""
        if not self._video_on or side not in self._writers:
            return "", 0.0
        current = self._segments.get(side)
        path = None
        if current is None:
            number, start = 0, now
            path = self._segment_path(side, number)
        else:
            number, start = current
            if round((now - start) * self.fps) >= self._segment_frames:
                if not self._check_disk():
                    return "", 0.0
                number, start = number + 1, now
                path = self._segment_path(side, number)
        self._segments[side] = (number, start)
        index = round((now - start) * self.fps)
        self._writers[side].put(_Job(index=index, image=image, path=path))
        return f"{side}_{number:03d}", index / self.fps

    def _segment_path(self, side: str, number: int) -> Path:
        return self.dir / f"{side}_{number:03d}.mkv"

    def _check_disk(self) -> bool:
        """剩余空间不够就关录像（日志照记）。"""
        if not self._video_on:
            return False
        try:
            free_gb = shutil.disk_usage(self.dir).free / 1e9
        except OSError:
            return True
        if free_gb < self.config.min_free_gb:
            log.warning("磁盘只剩 %.1f GB（下限 %.1f GB），停止录像；计分日志照记",
                        free_gb, self.config.min_free_gb)
            self._video_on = False
            return False
        return True

    # ---------------------------------------------------------------- 日志

    def event(self, now: float, **row) -> None:
        """记一行。now 是单调时钟，落到 t 列（相对本次会话开始的秒数）。"""
        row["t"] = now - self._t0
        row.setdefault("wall_time", datetime.now().strftime("%H:%M:%S.%f")[:-3])
        self._events.writerow([_cell(row.get(k)) for k in EVENT_FIELDS])
        self._events_file.flush()
        now = self._clock()
        if now - self._last_sync >= _FSYNC_SECONDS:
            self._last_sync = now
            try:
                os.fsync(self._events_file.fileno())
            except OSError:
                pass

    # ---------------------------------------------------------------- 收尾

    def close(self) -> None:
        for writer in self._writers.values():
            try:
                writer.close()
                if writer.dropped:
                    log.warning("[%s] 录像丢了 %d 帧（编码跟不上）", writer.side, writer.dropped)
            except Exception:                    # noqa: BLE001
                log.exception("[%s] 关闭录像线程失败", writer.side)
        try:
            self._events_file.close()
        except OSError:
            pass
        logging.getLogger("acepoint").removeHandler(self._log_handler)
        self._log_handler.close()


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


# ---------------------------------------------------------------- 管理（网页上的"录像"区域）

_SESSION_NAME = re.compile(r"^\d{8}_\d{6}$")


def _sessions(root: Path) -> list[Path]:
    """root 下的会话目录。只认 <8位日期>_<6位时间> 这种名字 —— 清空时绝不碰别的东西。"""
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and _SESSION_NAME.match(p.name))
    except OSError:
        return []


def _size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def summarize(root: Path, current: Path | None) -> dict:
    """录像占了多少、盘还剩多少。current 是正在录的那次（清空时保留）。"""
    sessions = _sessions(root)
    try:
        free = shutil.disk_usage(root if root.exists() else root.parent).free
    except OSError:
        free = 0
    return {
        "sessions": len(sessions),
        "bytes": sum(_size(s) for s in sessions),
        "free_bytes": free,
        "current": current.name if current is not None else None,
    }


def clear(root: Path, current: Path | None) -> dict:
    """删掉除正在录的那次以外的所有会话目录。返回删了几个、腾出多少字节。"""
    removed, freed = 0, 0
    keep = current.resolve() if current is not None else None
    for session in _sessions(root):
        if keep is not None and session.resolve() == keep:
            continue
        size = _size(session)
        try:
            shutil.rmtree(session)
        except OSError:
            log.exception("删除录像 %s 失败", session)
            continue
        removed, freed = removed + 1, freed + size
    log.warning("cleared %d recording session(s), %.1f MB freed", removed, freed / 1e6)
    return {"removed": removed, "freed_bytes": freed}
