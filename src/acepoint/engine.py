"""主循环：轮询取帧 -> 裁识别区 -> 手势识别 -> 状态机 -> 比分 -> 显示。

单线程跑识别，显示后端各自开线程。左右半场轮流占用时间片，
每个时间片长 1/poll_hz 秒，所以两路各得 poll_hz/2 的帧率（需求 §6）。
"""

from __future__ import annotations

import logging
import queue
import time

import cv2
import numpy as np

from acepoint.config import Config
from acepoint.audio.service import AudioService
from acepoint.displays import build_display
from acepoint.fsm import GestureFSM
from acepoint.recognition import GestureReader, Observation
from acepoint.sources import FrameSource, build_sources
from acepoint.state import Action, ScoreBoard, SideStatus, Telemetry

log = logging.getLogger(__name__)


class NoSourceError(RuntimeError):
    """一路视频源都开不起来。"""


def roi_rect(shape: tuple[int, ...], w_ratio: float, h_ratio: float,
             cx_ratio: float = 0.5, cy_ratio: float = 0.5) -> tuple[int, int, int, int]:
    """识别区的 (x, y, w, h)。cx/cy_ratio 是识别区中心在画面里的相对位置。

    默认 0.5/0.5 就是居中。之所以要能挪，是因为球员举手的位置并不在画面正中：
    实拍 A.mp4 里 13 次真手势的 y 全落在 289~438（画面中心是 540），
    居中裁只能把识别区拉得又高又大，手占识别区长边的比例被稀释到 2.9%，
    13 次只认出 1 次；同样长边 768 但把中心挪到手上，认出 9 次（见 §7）。
    """
    h, w = shape[:2]
    cw, ch = max(1, int(w * w_ratio)), max(1, int(h * h_ratio))
    # 夹回画面内：宁可偏离设定的中心，也不能裁出界
    x = max(0, min(w - cw, round(w * cx_ratio - cw / 2)))
    y = max(0, min(h - ch, round(h * cy_ratio - ch / 2)))
    return x, y, cw, ch


def crop_roi(frame: np.ndarray, w_ratio: float, h_ratio: float,
             cx_ratio: float = 0.5, cy_ratio: float = 0.5) -> np.ndarray:
    """裁出识别区。注意是"裁"不是"缩" —— 保留 1080p 的像素密度，
    网前的手才有足够分辨率被识别到（需求 §5.2）。"""
    x, y, w, h = roi_rect(frame.shape, w_ratio, h_ratio, cx_ratio, cy_ratio)
    return frame[y:y + h, x:x + w]


class ScoringEngine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.board = ScoreBoard(config.game)
        self._manual: queue.Queue[tuple[str, Action]] = queue.Queue()

        self.sources: dict[str, FrameSource] = build_sources(config)
        if not self.sources:
            raise NoSourceError("没有可用的视频源，退出")

        self.fsms = {side: GestureFSM(side, config.scoring) for side in self.sources}
        # 每路各自的识别区（没单独配就是 [capture] 的）
        self.crops = {s.side: s.crop_for(config.capture) for s in config.sources}
        self.audio = None
        self._audio_controls = queue.Queue(maxsize=32)
        self.reader = None
        self.display = None
        try:
            self.audio = AudioService(config.audio, config.game, config.resolve(config.audio.pack_root))
            self.display = build_display(config, on_action=self._enqueue_manual,
                                         on_audio=self._enqueue_audio, audio_config=config.audio)
            self.display.start()
            self.reader = GestureReader(
                config.recognition,
                list(self.sources),
                config.resolve(config.recognition.model_path),
            )
        except Exception:
            # 别把摄像头句柄和 TFLite 解释器漏在这 —— 显示后端构造失败同样要清干净
            self.close()
            raise

        self._fps: dict[str, float] = dict.fromkeys(self.sources, 0.0)
        self._recognize_ms: dict[str, float] = dict.fromkeys(self.sources, 0.0)
        self._last_read: dict[str, float] = {}
        self._last_obs: dict[str, Observation] = {s: Observation() for s in self.sources}
        self._last_frame: dict[str, object] = {}
        self._stop = False

    # ------------------------------------------------------------------ 运行

    def run(self, max_seconds: float | None = None) -> None:
        self.display.update(self._telemetry())

        # 每路采样率必须锁死在标定值上（poll_hz / 配置的路数），不能随实际打开
        # 几路而变。启动时少起来一路的话，若按打开数分片，活着那路会跑成两倍
        # 采样率 —— 防误触参数全部按 5fps/路 标定，翻倍后 0.2 秒的误识 100% 会计分。
        configured = max(1, len(self.config.sources))
        live = max(1, len(self.sources))
        per_side_fps = self.config.capture.poll_hz / configured
        interval = 1.0 / (per_side_fps * live)
        started = time.monotonic()
        rois = "; ".join(
            f"{side} {w * 100:.0f}%x{h * 100:.0f}%@({cx * 100:.0f}%,{cy * 100:.0f}%)"
            for side in self.sources
            for w, h, cx, cy in (self._crop(side),)
        )
        log.info(
            "ready — sides=%s, %.1f fps per side (configured %d source%s), ROI %s",
            list(self.sources), per_side_fps, configured,
            "" if configured == 1 else "s", rois,
        )
        if live < configured:
            log.warning("只起来 %d/%d 路视频源；采样率仍按 %.1f fps/路 保持不变",
                        live, configured, per_side_fps)

        try:
            while not self._stop:
                for side, source in self.sources.items():
                    slice_start = time.monotonic()
                    self._drain_audio()
                    self._drain_manual()
                    self._tick(side, source, slice_start)
                    self.display.update(self._telemetry())

                    if max_seconds is not None and time.monotonic() - started >= max_seconds:
                        self._stop = True
                        break

                    spent = time.monotonic() - slice_start
                    if spent < interval:
                        time.sleep(interval - spent)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self.close()

    def stop(self) -> None:
        self._stop = True

    def close(self) -> None:
        """每一步各自兜异常：一个关不掉不能连累后面的都关不掉。"""
        if getattr(self, "audio", None) is not None:
            self._safely(self.audio.close, "关闭语音")
        for side, source in self.sources.items():
            self._safely(source.close, f"关闭视频源 {side}")
        if self.reader is not None:
            self._safely(self.reader.close, "关闭识别器")
        if self.display is not None:
            self._safely(self.display.close, "关闭显示后端")

    @staticmethod
    def _safely(func, what: str) -> None:
        try:
            func()
        except Exception:                        # noqa: BLE001
            log.exception("%s 失败", what)

    # -------------------------------------------------------------- 单路处理

    def _tick(self, side: str, source: FrameSource, now: float) -> None:
        """跑完一个半场的一拍。

        整体包一层异常保护：单路故障不能拖垮另一路（需求 §5.4）。取帧路径本来
        就靠返回 None 来表达失败，但**识别路径**才是第三方代码最脆弱的地方 ——
        MediaPipe 时间戳异常、OpenCV 拔线时抛 cv2.error，都会一路掀翻主循环。
        """
        try:
            self._tick_side(side, source, now)
        except Exception:                        # noqa: BLE001 - 这一路的问题不能连坐
            log.exception("[%s] 这一拍出错，跳过；该半场继续", side)
            source.note_failure()
            try:
                self.fsms[side].feed(None, time.monotonic())
            except Exception:                    # noqa: BLE001
                log.exception("[%s] 状态机复位也失败了", side)

    def _tick_side(self, side: str, source: FrameSource, now: float) -> None:
        frame = source.read()
        if frame is None:
            # 单路故障不影响另一路（需求 §5.4）：这一拍跳过即可
            self.fsms[side].feed(None, time.monotonic())
            return

        previous = self._last_read.get(side)
        if previous is not None:
            instant = 1.0 / max(now - previous, 1e-6)
            self._fps[side] = instant if self._fps[side] == 0 else self._fps[side] * 0.8 + instant * 0.2
        self._last_read[side] = now

        repeated = frame is self._last_frame.get(side)
        if repeated:
            # 视频源在两次轮询之间还没走到下一帧，会把同一个 Frame 再给一次。
            # 同一张图重跑一遍推理是纯浪费（这是整个循环里最贵的一步），
            # 直接沿用上次结果，但时间照常推进，手势保持时长不受影响。
            observation = self._last_obs[side]
        else:
            roi = crop_roi(frame.image, *self._crop(side))
            started = time.perf_counter()
            observation = self.reader.observe(side, roi)
            elapsed = (time.perf_counter() - started) * 1000
            previous = self._recognize_ms[side]
            self._recognize_ms[side] = elapsed if previous == 0 else previous * 0.8 + elapsed * 0.2
            self._last_obs[side] = observation
            self._last_frame[side] = frame

        # 单调时钟：Pi 没有 RTC，挂钟会在 timesyncd 同步时跳变 ——
        # 向前跳会让所有冷却瞬间失效，向后跳会把半场冻死
        action = self.fsms[side].feed(observation.gesture, time.monotonic(),
                                      fresh=not repeated)
        if action is not None:
            before = self.board.snapshot().version
            snapshot = self._apply_score(side, action)
            if snapshot.version == before:
                log.info("%s %s ignored — %s already won this game",
                         side, action.value, snapshot.winner)
            elif snapshot.winner:
                log.info("%s %s -> %s  ** %s WINS **",
                         side, action.value, snapshot.text, snapshot.winner.upper())
            else:
                log.info("%s %s -> %s", side, action.value, snapshot.text)

        if self.display.wants_preview:
            self._publish_preview(side, frame.image, observation)

    # ------------------------------------------------------------------ 预览

    def _crop(self, side: str) -> tuple[float, float, float, float]:
        """该半场的识别区；`--video` 之类临时加的源没配就用全局的。"""
        return self.crops.get(side, self.config.capture.crop)

    def _publish_preview(self, side: str, frame: np.ndarray, observation: Observation) -> None:
        web = self.config.display.web
        h, w = frame.shape[:2]
        scale = min(1.0, web.preview_width / w)
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1.0 else frame.copy()

        x, y, rw, rh = roi_rect(frame.shape, *self._crop(side))
        p1 = (int(x * scale), int(y * scale))
        p2 = (int((x + rw) * scale), int((y + rh) * scale))
        armed = observation.gesture is not None
        cv2.rectangle(small, p1, p2, (0, 200, 255) if armed else (90, 90, 90), 2)

        label = f"{observation.raw} {observation.confidence:.2f}" if observation.raw else "-"
        cv2.putText(small, label, (p1[0] + 6, max(18, p1[1] - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 220, 120) if armed else (160, 160, 160), 2, cv2.LINE_AA)

        ok, buffer = cv2.imencode(
            ".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), web.preview_quality]
        )
        if ok:
            self.display.push_preview(side, buffer.tobytes())

    # -------------------------------------------------------------- 手动控制

    def _apply_score(self, side, action, *, source="gesture"):
        before, after = self.board.apply_with_change(side, action, source=source)
        audio = getattr(self, "audio", None)
        if audio is not None:
            try:
                audio.score(side, action, before, after)
            except Exception:
                log.exception("Audio event failed; scoring remains active")
        return after

    def set_score(self, left, right):
        """Use this on the engine thread for score edits with audio enabled."""
        snapshot = self.board.set_score(left, right)
        if self.audio:
            self.audio.set_score(snapshot)
        return snapshot

    def _enqueue_audio(self, body):
        try:
            self._audio_controls.put_nowait(body)
        except queue.Full:
            return False
        return True

    def _drain_audio(self):
        for _ in range(32):
            try:
                body = self._audio_controls.get_nowait()
            except queue.Empty:
                break
            if body == {"test": True}:
                self.audio.preview(self.board.snapshot())
            else:
                self.audio.configure(body)

    def _enqueue_manual(self, side: str, action: Action) -> None:
        """网页线程调用；只入队，真正生效在主循环里，避免跨线程改状态机。"""
        self._manual.put((side, action))

    def _drain_manual(self) -> None:
        while True:
            try:
                side, action = self._manual.get_nowait()
            except queue.Empty:
                return
            snapshot = self._apply_score(side, action, source="manual")
            if side in self.fsms:
                self.fsms[side].force_cooldown(time.monotonic())
            log.info("manual %s %s -> %s", side, action.value, snapshot.text)

    # -------------------------------------------------------------- 状态汇总

    def _telemetry(self) -> Telemetry:
        now = time.monotonic()
        sides: dict[str, SideStatus] = {}
        for side, source in self.sources.items():
            try:
                sides[side] = self._side_status(side, source, now)
            except Exception:                    # noqa: BLE001 - 遥测坏了也不能停计分
                log.exception("[%s] 遥测采集失败", side)
                sides[side] = SideStatus(side=side, online=False, detail="telemetry failed")
        return Telemetry(snapshot=self.board.snapshot(), sides=sides,
                         audio=self.audio.snapshot() if getattr(self, "audio", None) else {})

    def _side_status(self, side: str, source: FrameSource, now: float) -> SideStatus:
        observation = self._last_obs[side]
        status = self.fsms[side].status(now, confidence=observation.confidence)
        # 冷却期间状态机会清空手势，但识别器照常在输出。界面要能分清
        # "没识别到" 和 "识别到了但正在冷却"，否则看着像识别失灵。
        status.detected = observation.gesture
        status.online = source.online
        status.fps = self._fps[side]
        status.recognize_ms = self._recognize_ms[side]
        status.position = source.position       # 录制视频的播放进度，摄像头恒为 0
        status.duration = source.duration
        status.loops = source.loops
        status.detail = source.description
        if observation.gesture is None and observation.raw:
            reason = "conflicting hand commands" if observation.raw == "Ambiguous" else f"low confidence: {observation.raw}"
            status.detail = f"{source.description} | {reason}"
        return status
