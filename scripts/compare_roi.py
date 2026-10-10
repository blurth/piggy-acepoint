"""对比不同识别区裁剪比例：裁窄了还认不认得出手势。

    python scripts/compare_roi.py
    python scripts/compare_roi.py videos/B.mp4
    python scripts/compare_roi.py videos/B.mp4 --ratios 1.0x1.0 0.7x0.6 0.6x0.8
    python scripts/compare_roi.py videos/A.mp4 --ratios 0.40x0.47 0.40x0.27@0.47x0.34

以整帧（1.0x1.0）为基准，看各比例还剩多少计分手势帧。

比例后面可以跟 @中心x比x中心y比 把识别区挪离画面正中，不写就沿用配置里的中心。
**先扫中心再扫大小**：实拍 A.mp4 上同样是长边 768，居中只认出 3/13 次手势，
把中心挪到球员举手的高度后认出 9/13 —— 中心比大小重要得多（需求 §7）。

注意长宽比：手持竖屏素材和网柱上的横屏实拍取景完全不同，
在竖屏素材上得出的最佳比例**不能直接搬到实机**，换素材要重跑。
"""

from __future__ import annotations

from collections import Counter

from _shared import (SCORING_GESTURES, build_parser, crop_for, make_recognizer,
                     open_clip, recognize_clip, resolve)

from acepoint.engine import roi_rect

DEFAULT_RATIOS = ((1.0, 1.0), (0.8, 0.8), (0.7, 0.6), (0.6, 0.8), (0.6, 0.6))
SAMPLE_FPS = 5.0


def parse_ratios(values: list[str] | None, default_center: tuple[float, float]):
    """解析 宽x高 或 宽x高@中心x比x中心y比，统一返回四元组。"""
    if not values:
        return tuple(r + default_center for r in DEFAULT_RATIOS)
    ratios = []
    for item in values:
        size, _, center = item.partition("@")
        try:
            w, _, h = size.partition("x")
            if center:
                cx, _, cy = center.partition("x")
                spot = (float(cx), float(cy))
            else:
                spot = default_center
            ratios.append((float(w), float(h)) + spot)
        except ValueError:
            raise SystemExit("--ratios 格式应为 宽x高 或 宽x高@中心x比x中心y比，"
                             f"例如 0.6x0.8 / 0.4x0.27@0.47x0.34（当前 {item!r}）")
    return tuple(ratios)


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("--ratios", nargs="+", metavar="WxH",
                        help="要对比的识别区比例，默认扫一组常用值")
    args = parser.parse_args()
    config, videos = resolve(args)

    for path in videos:
        # 基准是这一路自己在用的识别区（[[sources]] 可以覆盖 [capture]），
        # 不写 @ 时也沿用它的中心，免得扫大小的时候顺手把中心挪回正中
        current = crop_for(config, path)
        ratios = parse_ratios(args.ratios, (current[2], current[3]))
        clip = open_clip(path)
        print(f"\n===== {clip} =====")
        print(f"{'识别区':<22} {'裁后尺寸':>13} {'长边':>6} {'计分手势帧':>11} {'保留率':>8}   明细")

        baseline = None
        for rect in ratios:
            # 每个比例用独立实例，免得 VIDEO 模式的跟踪状态互相干扰
            recognizer = make_recognizer(
                config.recognition, config.resolve(config.recognition.model_path))
            try:
                samples = recognize_clip(clip, config, target_fps=SAMPLE_FPS,
                                         crop=rect, recognizer=recognizer)
            finally:
                recognizer.close()

            counts = Counter(g for _, g in samples if g in SCORING_GESTURES)
            total = sum(counts.values())
            if baseline is None:
                baseline = total or 1
            _, _, width, height = roi_rect((clip.height, clip.width), *rect)
            detail = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            mark = "  <== 当前配置" if rect == current else ""
            # 长边决定手被缩成多少像素 —— MediaPipe 统一把识别区缩到 192x192
            print(f"{rect[0]:.2f}x{rect[1]:.2f} @({rect[2]:.2f},{rect[3]:.2f})"
                  f"  {width:>5}x{height:<6} {max(width, height):>6} "
                  f"{total:>11} {total / baseline * 100:>7.0f}%   {detail}{mark}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
