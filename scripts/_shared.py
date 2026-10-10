"""分析脚本共用的取帧与识别工具。

这些脚本刻意复用 `acepoint` 自己的代码（crop_roi / GestureFSM / 配置），
而不是另写一份 —— 否则量出来的数字和实际跑的东西对不上，等于白量。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import cv2

from acepoint.config import Config, RecognitionConfig, load_config
from acepoint.engine import crop_roi
from acepoint.fsm import GESTURE_ACTIONS

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config.toml"
SCORING_GESTURES = frozenset(GESTURE_ACTIONS)


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("videos", nargs="*", type=Path,
                        help="要分析的视频；不给就用配置里的视频源")
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"配置文件（默认 {DEFAULT_CONFIG.name}）")
    return parser


def resolve(args: argparse.Namespace) -> tuple[Config, list[Path]]:
    """加载配置，并定出要分析哪些视频。"""
    config = load_config(args.config)
    videos = list(args.videos)
    if not videos:
        videos = [config.resolve(s.path) for s in config.sources if s.kind == "video"]
    missing = [v for v in videos if not v.exists()]
    if missing:
        raise SystemExit(f"视频不存在: {', '.join(str(m) for m in missing)}")
    if not videos:
        raise SystemExit("没有可分析的视频：配置里没有 kind=video 的源，也没在命令行给")
    return config, videos


# --------------------------------------------------------------------- 取帧

@dataclass
class Clip:
    path: Path
    fps: float
    frames: int
    width: int
    height: int

    @property
    def duration(self) -> float:
        return self.frames / self.fps if self.fps else 0.0

    def stride_for(self, target_fps: float) -> int:
        return max(1, round(self.fps / target_fps))

    def __str__(self) -> str:
        return (f"{self.path.name} {self.width}x{self.height} "
                f"@{self.fps:.1f}fps {self.duration:.1f}s")


def open_clip(path: Path) -> Clip:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit(f"打不开视频: {path}")
    clip = Clip(path=path,
                fps=cap.get(cv2.CAP_PROP_FPS) or 30.0,
                frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
                width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    cap.release()
    return clip


def iter_frames(clip: Clip, stride: int):
    """按抽帧间隔产出 (秒, BGR 画面)。"""
    cap = cv2.VideoCapture(str(clip.path))
    index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % stride == 0:
                yield index / clip.fps, frame
            index += 1
    finally:
        cap.release()


def crop_for(config: Config, path: Path) -> tuple[float, float, float, float]:
    """这段素材该用哪个识别区。

    配置里配过这个文件就用那一路自己的（两路取景可以完全不同），
    否则回落到 [capture]。脚本量出来的必须和实际跑的一致。
    """
    # 两边都化成绝对路径再比：命令行给的往往是相对路径，配置里解析出来的是绝对的
    target = path.resolve()
    for source in config.sources:
        if source.kind == "video" and config.resolve(source.path).resolve() == target:
            return source.crop_for(config.capture)
    return config.capture.crop


# ------------------------------------------------------------------- 识别

def make_recognizer(config: RecognitionConfig, model_path: Path, *, num_hands: int | None = None):
    """新建一个独立的 GestureRecognizer（VIDEO 模式）。"""
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    if not model_path.exists():
        raise SystemExit(f"缺少手势模型 {model_path}，先跑 python scripts/fetch_model.py")
    return vision.GestureRecognizer.create_from_options(
        vision.GestureRecognizerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=num_hands or config.num_hands,
            min_hand_detection_confidence=config.min_hand_detection_confidence,
        ))


def best_gesture(result, min_confidence: float) -> tuple[str | None, float]:
    """取所有手里置信度最高的有效手势；没过阈值返回 (None, 分数)。"""
    name, score = None, 0.0
    for hand in result.gestures:
        for candidate in hand:
            if candidate.category_name in ("", "None"):
                continue
            if candidate.score > score:
                name, score = candidate.category_name, candidate.score
    return (name, score) if name and score >= min_confidence else (None, score)


def recognize_clip(clip: Clip, config: Config, *, target_fps: float,
                   crop: tuple[float, ...] | None = None,
                   num_hands: int | None = None,
                   recognizer=None) -> list[tuple[float, str | None]]:
    """按指定采样率跑完整段视频，返回 [(秒, 手势)]。

    crop 不给就用配置里的识别区（宽比, 高比[, 中心x比, 中心y比]）。
    只给两项时中心沿用配置里的，这样扫比例的脚本不会顺手把中心也改回正中。
    传自己的 recognizer 可以复用同一个实例。
    """
    import mediapipe as mp

    own = recognizer is None
    if own:
        recognizer = make_recognizer(
            config.recognition, config.resolve(config.recognition.model_path),
            num_hands=num_hands)

    rect = tuple(crop) if crop else crop_for(config, clip.path)
    if len(rect) == 2:
        rect += (config.capture.crop_cx_ratio, config.capture.crop_cy_ratio)
    stride = clip.stride_for(target_fps)
    step_ms = int(1000 * stride / clip.fps)

    out, timestamp = [], 0
    try:
        for seconds, frame in iter_frames(clip, stride):
            roi = crop_roi(frame, *rect)
            image = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
            timestamp += step_ms
            name, _ = best_gesture(recognizer.recognize_for_video(image, timestamp),
                                   config.recognition.min_confidence)
            out.append((seconds, name))
    finally:
        if own:
            recognizer.close()
    return out


# --------------------------------------------------------------- 手势分段

@dataclass
class Episode:
    """一次"有意做出的手势"。"""

    gesture: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_episodes(samples, *, gap: float = 0.5, min_duration: float = 0.3) -> list[Episode]:
    """把连续检出合并成一次手势。

    gap          同一手势中断多久以内仍算同一次
    min_duration 短于这个时长的判为手在变换姿势途中的瞬时误识，不是有意手势
    """
    episodes: list[Episode] = []
    current: Episode | None = None
    for seconds, gesture in samples:
        if gesture not in SCORING_GESTURES:
            continue
        if current and gesture == current.gesture and seconds - current.end <= gap:
            current.end = seconds
        else:
            if current:
                episodes.append(current)
            current = Episode(gesture=gesture, start=seconds, end=seconds)
    if current:
        episodes.append(current)
    return [e for e in episodes if e.duration >= min_duration]


def quantiles(values: list[float]) -> tuple[float, float, float]:
    """返回 (最小, 中位, 最大)。"""
    ordered = sorted(values)
    return ordered[0], ordered[len(ordered) // 2], ordered[-1]
