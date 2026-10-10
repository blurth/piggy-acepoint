"""对比识别器的两个关键取舍，看省下的开销值不值那点识别质量。

    python scripts/compare_recognizer.py
    python scripts/compare_recognizer.py videos/B.mp4

1. num_hands 1 vs 2
   降到 1 推理明显变快，但会丢边缘检出。状态机要求手势**连续**保持，
   掉一帧计时就归零，所以丢检的代价远大于丢检率本身。

2. per_source_recognizer true vs false
   共用一个实例省内存，但双路交替喂帧时 VIDEO 模式的手部跟踪状态会互相污染。
   这里用"原片 + 镜像"模拟两个不同半场来复现这个场景。
"""

from __future__ import annotations

import statistics
import time
from collections import Counter

import cv2
import numpy as np

from _shared import (best_gesture, build_parser, iter_frames, make_recognizer,
                     open_clip, recognize_clip, resolve)

from acepoint.engine import crop_roi

SAMPLE_FPS = 5.0


def agreement(a, b) -> float:
    return sum(1 for (_, x), (_, y) in zip(a, b) if x == y) / max(1, len(a)) * 100


def histogram(samples) -> Counter:
    return Counter(g for _, g in samples if g)


def show_counts(title_a, count_a, title_b, count_b) -> None:
    print(f"  {'手势':<14} {title_a:>12} {title_b:>12}")
    for name in sorted(set(count_a) | set(count_b)):
        print(f"  {name:<14} {count_a.get(name, 0):>12} {count_b.get(name, 0):>12}")


def compare_num_hands(clip, config) -> None:
    print("\n--- num_hands 1 vs 2 ---")
    results = {}
    for hands in (2, 1):
        recognizer = make_recognizer(config.recognition,
                                     config.resolve(config.recognition.model_path),
                                     num_hands=hands)
        started = time.perf_counter()
        try:
            results[hands] = recognize_clip(clip, config, target_fps=SAMPLE_FPS,
                                            recognizer=recognizer)
        finally:
            recognizer.close()
        elapsed = (time.perf_counter() - started) / max(1, len(results[hands])) * 1000
        print(f"  num_hands={hands}: 每帧约 {elapsed:.1f}ms")

    print(f"  计分结论一致: {agreement(results[1], results[2]):.1f}%")
    show_counts("num_hands=1", histogram(results[1]), "num_hands=2", histogram(results[2]))


def compare_per_source(clip, config) -> None:
    """左路喂原片、右路喂镜像，模拟两个不同的半场交替送帧。"""
    print("\n--- per_source_recognizer true vs false ---")
    import mediapipe as mp

    stride = clip.stride_for(SAMPLE_FPS)
    frames = []
    for _, frame in iter_frames(clip, stride):
        roi = crop_roi(frame, *config.capture.crop)
        rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
        frames.append(("left", np.ascontiguousarray(rgb)))
        frames.append(("right", np.ascontiguousarray(cv2.flip(rgb, 1))))

    model = config.resolve(config.recognition.model_path)
    min_conf = config.recognition.min_confidence

    per_side = {s: make_recognizer(config.recognition, model) for s in ("left", "right")}
    stamps = {"left": 0, "right": 0}
    out_per = []
    try:
        for side, rgb in frames:
            stamps[side] += 200
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            out_per.append((0.0, best_gesture(
                per_side[side].recognize_for_video(image, stamps[side]), min_conf)[0]))
    finally:
        for r in per_side.values():
            r.close()

    shared = make_recognizer(config.recognition, model)
    stamp = 0
    out_shared = []
    try:
        for _, rgb in frames:
            stamp += 100                      # 两路共享一条时间轴
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            out_shared.append((0.0, best_gesture(
                shared.recognize_for_video(image, stamp), min_conf)[0]))
    finally:
        shared.close()

    print(f"  {len(frames)} 帧（左右交替，右路为镜像）")
    print(f"  计分结论一致: {agreement(out_per, out_shared):.1f}%")
    show_counts("每路一个", histogram(out_per), "共用一个", histogram(out_shared))
    print("  共用实例若明显丢检，说明跟踪状态被污染了，per_source_recognizer 应设 true")


def main() -> int:
    args = build_parser(__doc__).parse_args()
    config, videos = resolve(args)
    print(f"当前配置: num_hands={config.recognition.num_hands}  "
          f"per_source_recognizer={config.recognition.per_source_recognizer}")

    for path in videos:
        clip = open_clip(path)
        print(f"\n===== {clip} =====")
        compare_num_hands(clip, config)
        compare_per_source(clip, config)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
