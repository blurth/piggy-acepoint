"""标定 hold_seconds / hold_grace_seconds：真人到底把手势保持了多久。

    python scripts/analyze_gestures.py                     # 用配置里的视频源
    python scripts/analyze_gestures.py videos/B.mp4        # 指定素材
    python scripts/analyze_gestures.py videos/B.mp4 --sweep

做三件事：
  1. 按 30fps 逐帧识别，量出每次手势的**实际保持时长**（真值）
  2. 按主循环真实的采样率重放 GestureFSM，算当前配置的**成功率**
  3. --sweep 扫 hold x grace 网格，告诉你参数该往哪调

为什么要分这两个采样率：主循环每路只有约 5fps，一次 0.7 秒的手势只落到 3~4 个
采样点，而状态机要求"连续保持"。真值告诉你人做了多久，重放告诉你系统抓不抓得住。

换素材（尤其是阶段2 换成网柱横屏实拍）后要重跑一遍，参数是跟素材走的。
"""

from __future__ import annotations

from dataclasses import replace

from _shared import (SCORING_GESTURES, build_parser, detect_episodes, open_clip,
                     quantiles, recognize_clip, resolve)

from acepoint.fsm import GestureFSM

TRUTH_FPS = 30.0          # 真值用逐帧
HOLDS = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0)
GRACES = (0.0, 0.2, 0.4, 0.6)


def replay(samples, scoring, episodes, *, cooldown: float) -> int:
    """用真实状态机重放采样序列，返回命中了几次有意手势。"""
    fsm = GestureFSM("left", replace(scoring, cooldown_seconds=cooldown))
    hit = set()
    for seconds, gesture in samples:
        if fsm.feed(gesture, seconds) is None:
            continue
        for index, episode in enumerate(episodes):
            # 触发点落在这次手势期间（或刚结束的 2 秒内）就算命中它
            if episode.start <= seconds <= episode.end + 2.0:
                hit.add(index)
                break
    return len(hit)


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("--sweep", action="store_true", help="扫 hold x grace 参数网格")
    parser.add_argument("--poll-fps", type=float, default=None,
                        help="每路采样率，默认按配置的 poll_hz / 视频源数量算")
    args = parser.parse_args()
    config, videos = resolve(args)

    poll_fps = args.poll_fps or config.capture.poll_hz / max(1, len(config.sources))
    print(f"配置: hold={config.scoring.hold_seconds}s  "
          f"grace={config.scoring.hold_grace_seconds}s  "
          f"cooldown={config.scoring.cooldown_seconds}s  "
          f"min_confidence={config.recognition.min_confidence}  "
          f"识别区={config.capture.crop_w_ratio:.2f}x{config.capture.crop_h_ratio:.2f} "
          f"@({config.capture.crop_cx_ratio:.2f},{config.capture.crop_cy_ratio:.2f})")
    print(f"每路采样率: {poll_fps:.1f} fps\n")

    for path in videos:
        clip = open_clip(path)
        print(f"===== {clip} =====")

        truth_samples = recognize_clip(clip, config, target_fps=TRUTH_FPS)
        episodes = detect_episodes(truth_samples)
        if not episodes:
            print("  没识别到任何有意手势，跳过\n")
            continue

        print(f"\n  {'#':>3} {'手势':<14} {'起点':>8} {'时长':>8}")
        for index, episode in enumerate(episodes, 1):
            print(f"  {index:>3} {episode.gesture:<14} "
                  f"{episode.start:7.1f}s {episode.duration:7.2f}s")

        low, mid, high = quantiles([e.duration for e in episodes])
        print(f"\n  有意手势 {len(episodes)} 次，时长 {low:.2f}~{high:.2f}s，中位 {mid:.2f}s")
        noise = len(detect_episodes(truth_samples, min_duration=0.0)) - len(episodes)
        if noise:
            print(f"  （另有 {noise} 次 <0.3s 的瞬时误识，判为手变换姿势途中的噪声，已剔除）")

        play = recognize_clip(clip, config, target_fps=poll_fps)
        runs = max((sum(1 for _ in group) for group in _runs(play)), default=0)
        print(f"  {poll_fps:.0f}fps 采样下最长连续检出 {runs} 个点 "
              f"({runs / poll_fps:.1f}s)")

        for cooldown, label in ((0.0, "不含冷却"), (config.scoring.cooldown_seconds, "含冷却")):
            hit = replay(play, config.scoring, episodes, cooldown=cooldown)
            print(f"  当前配置成功率（{label}）: {hit}/{len(episodes)} = "
                  f"{hit / len(episodes) * 100:.0f}%")
        gaps = sum(1 for a, b in zip(episodes, episodes[1:])
                   if b.start - a.end < config.scoring.cooldown_seconds)
        if gaps:
            print(f"  注意：有 {gaps} 次手势间隔短于 {config.scoring.cooldown_seconds}s 冷却，"
                  f"被挡掉了。真实打球一个回合远超这个间隔，别按「含冷却」那行调参")

        if args.sweep:
            print(f"\n  {'hold':>6} " + " ".join(f"grace={g:.1f}s" for g in GRACES))
            for hold in HOLDS:
                cells = []
                for grace in GRACES:
                    scoring = replace(config.scoring, hold_seconds=hold,
                                      hold_grace_seconds=grace)
                    hit = replay(play, scoring, episodes, cooldown=0.0)
                    cells.append(f"{hit / len(episodes) * 100:9.0f}%")
                mark = "  <-- 当前" if abs(hold - config.scoring.hold_seconds) < 1e-9 else ""
                print(f"  {hold:>5.1f}s " + " ".join(cells) + mark)
            print("  （扫描不含冷却，单独看 hold/grace 的影响）")
        print()
    return 0


def _runs(samples):
    """把采样序列切成"同一手势的连续段"。"""
    group, previous = [], object()
    for _, gesture in samples:
        if gesture != previous:
            if group and previous in SCORING_GESTURES:
                yield group
            group, previous = [], gesture
        group.append(gesture)
    if group and previous in SCORING_GESTURES:
        yield group


if __name__ == "__main__":
    raise SystemExit(main())
