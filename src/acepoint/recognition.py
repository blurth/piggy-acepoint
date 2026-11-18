"""MediaPipe 手势识别封装。

只用官方预置手势（Victory / Thumb_Down / Open_Palm），不需要训练。

两个坑：
  * VIDEO 模式要求同一个 recognizer 实例的时间戳严格递增。双路共用一个实例时，
    两路的帧共享同一条时间轴 —— 这里统一在 _next_timestamp() 里保证单调。
  * 共用实例还会让 VIDEO 模式的手部跟踪状态在左右画面之间串味，实测 Thumb_Down
    检出率掉 77%。所以 per_source_recognizer 默认且应当保持 True（每路一个实例），
    代价约 70MB。需求 §6 原本建议共用省内存，v1.1 已按实测数据修正。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import cv2

from acepoint.config import RecognitionConfig
from acepoint.fsm import GESTURE_ACTIONS

log = logging.getLogger(__name__)

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/latest/gesture_recognizer.task"
)


@dataclass(frozen=True)
class Observation:
    """一帧的识别结果。

    gesture   过了置信度阈值、可以参与计分的手势；None 表示本帧不计分。
    raw       原始最高分手势（可能没过阈值），只用于界面调试显示。
    """

    gesture: str | None = None
    confidence: float = 0.0
    hands: int = 0
    raw: str | None = None


class ModelMissingError(FileNotFoundError):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"手势模型不存在: {path}\n"
            f"运行 `python scripts/fetch_model.py` 下载，或手动从下面地址下载后放到该路径:\n  {MODEL_URL}"
        )


def threaded_base_options(base_options_cls, model_path: Path, threads: int):
    """模型选项；threads > 1 时让推理用多个线程。返回 (选项, 实际生效的线程数)。

    MediaPipe 在 Linux 上给 XNNPACK 的默认线程数是 1，Python 的 BaseOptions 也没有
    暴露这个设置。0.10.x 的选项最终会转成 protobuf，这里在转出来的 proto 上把
    acceleration 换成带 num_threads 的 XNNPACK 委托。Pi 4 实测（0.10.18，画面里
    有一只手）：1 线程 297ms，2 线程 185ms，识别结果和置信度逐位相同。

    1.x 改成了 C 绑定，没有 to_pb2 —— 那就按默认线程数跑，不报错：
    这个设置只是性能旋钮，不该让识别起不来。
    """
    plain = base_options_cls(model_asset_path=str(model_path))
    if threads <= 1:
        return plain, 1
    try:
        if not hasattr(base_options_cls, "to_pb2"):
            raise ImportError("BaseOptions has no to_pb2")
        from mediapipe.calculators.tensor import inference_calculator_pb2
        from mediapipe.tasks.cc.core.proto import acceleration_pb2
    except ImportError as exc:
        log.warning("recognition.inference_threads=%d 在这个 mediapipe 版本上不生效（%s），"
                    "按默认线程数运行", threads, exc)
        return plain, 1

    xnnpack = inference_calculator_pb2.InferenceCalculatorOptions.Delegate.Xnnpack

    class ThreadedBaseOptions(base_options_cls):
        def to_pb2(self):
            options = super().to_pb2()
            options.acceleration.CopyFrom(
                acceleration_pb2.Acceleration(xnnpack=xnnpack(num_threads=threads)))
            return options

    return ThreadedBaseOptions(model_asset_path=str(model_path)), threads


class GestureReader:
    """把 BGR 画面变成手势名。"""

    _SHARED = "__shared__"
    # 哪些手势是计分指令（用来判两只手是否互相矛盾）；默认就是状态机那张表
    actions: Mapping[str, object] = GESTURE_ACTIONS

    def __init__(self, config: RecognitionConfig, sides: list[str], model_path: Path,
                 actions: Mapping[str, object] | None = None) -> None:
        if not model_path.exists():
            raise ModelMissingError(model_path)

        # 延迟导入：跑单元测试/只看配置时不必装 mediapipe
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        self._mp = mp
        self.config = config
        self.per_source = config.per_source_recognizer
        if actions is not None:
            self.actions = dict(actions)

        base_options, self.inference_threads = threaded_base_options(
            mp_python.BaseOptions, model_path, config.inference_threads)

        def make():
            return vision.GestureRecognizer.create_from_options(
                vision.GestureRecognizerOptions(
                    base_options=base_options,
                    running_mode=vision.RunningMode.VIDEO,
                    num_hands=config.num_hands,
                    min_hand_detection_confidence=config.min_hand_detection_confidence,
                )
            )

        if self.per_source:
            self._recognizers = {side: make() for side in sides}
        else:
            self._recognizers = {self._SHARED: make()}
        self._last_ts = dict.fromkeys(self._recognizers, 0)
        log.info(
            "gesture recognizer ready (%s instance%s, %s inference thread%s each, "
            "min_confidence=%.2f)",
            len(self._recognizers),
            "" if len(self._recognizers) == 1 else "s",
            self.inference_threads,
            "" if self.inference_threads == 1 else "s",
            config.min_confidence,
        )

    def _key(self, side: str) -> str:
        return side if self.per_source else self._SHARED

    def _next_timestamp(self, key: str) -> int:
        """严格递增的毫秒时间戳；同一毫秒内的两帧也要错开，否则 MediaPipe 会报错。"""
        ts = max(int(time.monotonic() * 1000), self._last_ts[key] + 1)
        self._last_ts[key] = ts
        return ts

    def observe(self, side: str, image_bgr) -> Observation:
        key = self._key(side)
        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        mp_image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._recognizers[key].recognize_for_video(mp_image, self._next_timestamp(key))

        best_name, best_score = None, 0.0
        for hand in result.gestures:                     # 每只手一组候选
            for candidate in hand:
                if candidate.category_name in ("", "None"):
                    continue
                if candidate.score > best_score:
                    best_name, best_score = candidate.category_name, candidate.score

        hands = len(result.gestures)
        # 多只手给出不同计分指令时拒绝本帧，避免最高分在两只手之间跳动。
        # 同一动作的不同手势（比耶和食指都是加分）不算矛盾
        actions = {
            self.actions[candidate.category_name]
            for hand in result.gestures
            for candidate in hand
            if candidate.category_name in self.actions
            and candidate.score >= self.config.min_confidence
        }
        if len(actions) > 1:
            return Observation(confidence=best_score, hands=hands, raw="Ambiguous")
        if best_name is None:
            return Observation(hands=hands)
        if best_score < self.config.min_confidence:      # 置信度不够只做展示，不计分
            return Observation(confidence=best_score, hands=hands, raw=best_name)
        return Observation(gesture=best_name, confidence=best_score, hands=hands, raw=best_name)

    def close(self) -> None:
        for recognizer in self._recognizers.values():
            try:
                recognizer.close()
            except Exception:                            # noqa: BLE001 - 关闭失败不影响退出
                pass
