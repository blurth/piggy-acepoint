"""放大看某个时间点：这个手势到底识别到没有，没计分是为什么。

    python scripts/explain_moment.py videos/B.mp4 14
    python scripts/explain_moment.py videos/B.mp4 14 --window 6

"某个手势没反应" 通常是四种原因之一，这个脚本能一眼分清：

  1. 根本没识别到       —— 逐帧那段显示 (无)
  2. 识别到了但置信度不够 —— 过阈值帧数远少于总帧数，考虑降 min_confidence
  3. 保持时长不够        —— 重放里"保持"始终没到要求值
  4. 冷却期挡掉了        —— 重放里显示"冷却剩 N 秒"（最常见，尤其在连续摆拍的素材上）

输出分两段：30fps 逐帧看识别质量，5fps 重放看状态机为什么不动。
重放必须**从视频开头跑起**，否则冷却状态对不上。
"""

from __future__ import annotations

import cv2
import mediapipe as mp

from _shared import best_gesture, build_parser, iter_frames, make_recognizer, open_clip

from acepoint.config import load_config
from acepoint.engine import crop_roi
from acepoint.fsm import GestureFSM

PLAY_FPS = 5.0


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("at", type=float, help="要检查的时间点（秒）")
    parser.add_argument("--window", type=float, default=5.0,
                        help="前后各看多少秒（默认 5）")
    args = parser.parse_args()
    if not args.videos:
        raise SystemExit("要指定视频，例如：python scripts/explain_moment.py videos/B.mp4 14")

    config = load_config(args.config)
    clip = open_clip(args.videos[0])
    model = config.resolve(config.recognition.model_path)
    min_conf = config.recognition.min_confidence
    crop = config.capture.crop
    start, end = max(0.0, args.at - args.window), args.at + args.window

    def gesture_at(recognizer, frame, timestamp, threshold):
        roi = crop_roi(frame, *crop)
        image = mp.Image(image_format=mp.ImageFormat.SRGB,
                         data=cv2.cvtColor(roi, cv2.COLOR_BGR2RGB))
        return best_gesture(recognizer.recognize_for_video(image, timestamp), threshold)

    # ---------------- 1. 逐帧看识别质量 ----------------
    print(f"=== {clip} ===")
    print(f"--- {start:.1f}~{end:.1f}s 逐帧识别（置信度阈值 {min_conf}）---")
    recognizer = make_recognizer(config.recognition, model)
    step = int(1000 / clip.fps)
    runs: list[dict] = []
    try:
        timestamp = 0
        for seconds, frame in iter_frames(clip, 1):
            timestamp += step
            if seconds > end:
                break
            if seconds < start:
                continue
            raw, score = gesture_at(recognizer, frame, timestamp, 0.0)
            if runs and runs[-1]["raw"] == raw:
                runs[-1]["end"] = seconds
                runs[-1]["scores"].append(score)
                runs[-1]["passed"] += score >= min_conf
            else:
                runs.append({"raw": raw, "start": seconds, "end": seconds,
                             "scores": [score], "passed": int(score >= min_conf)})
    finally:
        recognizer.close()

    print(f"{'区间':>16} {'手势':<14} {'帧数':>5} {'置信度 最低/中位/最高':>25} {'过阈值':>7}")
    for run in runs:
        scores = sorted(run["scores"])
        name = run["raw"] or "(无)"
        print(f"{run['start']:6.2f}~{run['end']:6.2f}s {name:<14} {len(scores):>5} "
              f"{scores[0]:8.2f} /{scores[len(scores)//2]:6.2f} /{scores[-1]:6.2f}   "
              f"{run['passed']:>7}")

    # ---------------- 2. 状态机重放 ----------------
    print(f"\n--- {PLAY_FPS:.0f}fps 采样重放（从头跑起，含 "
          f"{config.scoring.cooldown_seconds}s 冷却）---")
    recognizer = make_recognizer(config.recognition, model)
    fsm = GestureFSM("left", config.scoring)
    stride = clip.stride_for(PLAY_FPS)
    step = int(1000 * stride / clip.fps)
    try:
        timestamp = 0
        for seconds, frame in iter_frames(clip, stride):
            if seconds > end:
                break
            timestamp += step
            gesture, _ = gesture_at(recognizer, frame, timestamp, min_conf)
            action = fsm.feed(gesture, seconds)
            if seconds < start and not action:
                continue
            status = fsm.status(seconds)
            cool = (f"冷却剩 {status.cooldown_remaining:4.1f}s"
                    if status.cooldown_remaining > 0 else "就绪      ")
            held = (f"保持 {status.held:.1f}/{status.required:.1f}s"
                    if status.required else "")
            fired = f"   ==> {action.value.upper()}" if action else ""
            print(f"  {seconds:6.2f}s  {str(gesture):<14} {cool}  {held}{fired}")
    finally:
        recognizer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
