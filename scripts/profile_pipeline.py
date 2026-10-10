"""剖析主循环的耗时与内存，找瓶颈。

    python scripts/profile_pipeline.py
    python scripts/profile_pipeline.py --rounds 100 --resolution

上 Pi 之后重点看 recognize 那一行和 RSS —— 开发机上它约占主循环 78%，
Pi 4 单核慢 4~6 倍，这里就是决定 poll_hz 能不能维持的地方。

已知结论（开发机，720x1280 输入）：
  * 输入分辨率对推理耗时几乎无影响，MediaPipe 内部固定缩放到 192x192 做手掌检测。
    所以"裁剪而非缩放"的识别区方案不额外花 CPU，想靠降分辨率提速是死路。
  * 内存在前十几帧由 TFLite 一次性分配推理内存后就稳住，不是泄漏。
"""

from __future__ import annotations

import gc
import statistics
import time

import cv2

from _shared import build_parser, open_clip, resolve

from acepoint.engine import crop_roi, roi_rect
from acepoint.recognition import GestureReader
from acepoint.sources import build_sources

try:
    import psutil
    _PROC = psutil.Process()
except ImportError:                     # pragma: no cover - 只是少一列数字
    _PROC = None


def rss_mb() -> float:
    return _PROC.memory_info().rss / 1e6 if _PROC else 0.0


def report(timings: dict[str, list[float]]) -> None:
    budget = statistics.mean(timings["total"]) if timings["total"] else 0.0
    print(f"\n{'阶段':<16} {'均值':>9} {'p50':>9} {'p95':>9} {'最大':>9}   占比")
    for name, samples in timings.items():
        if not samples:
            continue
        ms = sorted(v * 1000 for v in samples)
        share = "" if name == "total" else f"{statistics.mean(ms) / (budget * 1000) * 100:5.1f}%"
        print(f"{name:<16} {statistics.mean(ms):8.2f}ms {ms[len(ms)//2]:8.2f}ms "
              f"{ms[int(len(ms) * .95)]:8.2f}ms {ms[-1]:8.2f}ms   {share}")


def resolution_curve(reader: GestureReader, side: str, frame, config) -> None:
    print("\n--- 输入分辨率 vs 推理耗时 ---")
    height, width = frame.shape[:2]
    for scale in (1.0, 0.75, 0.5, 0.35):
        size = (int(width * scale), int(height * scale))
        image = cv2.resize(frame, size) if scale < 1.0 else frame
        samples = []
        for _ in range(20):
            started = time.perf_counter()
            reader.observe(side, image)
            samples.append((time.perf_counter() - started) * 1000)
        print(f"  {size[0]:>5}x{size[1]:<6} ({size[0] * size[1] / 1e6:.2f}MP)  "
              f"均值 {statistics.mean(samples):6.2f}ms")
    print("  几乎持平就说明降分辨率省不到 CPU")


def main() -> int:
    parser = build_parser(__doc__)
    parser.add_argument("--rounds", type=int, default=60, help="跑多少轮（每轮遍历所有源）")
    parser.add_argument("--resolution", action="store_true", help="附带跑分辨率-耗时曲线")
    args = parser.parse_args()
    config, _ = resolve(args)

    sources = build_sources(config)
    if not sources:
        raise SystemExit("没有可用的视频源")
    reader = GestureReader(config.recognition, list(sources),
                           config.resolve(config.recognition.model_path))

    capture, web = config.capture, config.display.web
    print(f"per_source_recognizer={config.recognition.per_source_recognizer}  "
          f"num_hands={config.recognition.num_hands}  "
          f"识别区={capture.crop_w_ratio:.2f}x{capture.crop_h_ratio:.2f} "
          f"@({capture.crop_cx_ratio:.2f},{capture.crop_cy_ratio:.2f})")
    for side, source in sources.items():
        print(f"  [{side}] {source.description}")
    print(f"初始化后 RSS {rss_mb():.0f} MB")

    keys = ("read", "crop", "recognize", "preview_resize", "preview_encode", "total")
    timings: dict[str, list[float]] = {k: [] for k in keys}
    interval = 1.0 / config.capture.poll_hz
    marks = {int(args.rounds * f) for f in (0.02, 0.05, 0.1, 0.25, 0.5, 1.0)}
    last_frame = None

    for round_index in range(1, args.rounds + 1):
        for side, source in sources.items():
            start = time.perf_counter()

            mark = time.perf_counter()
            frame = source.read()
            timings["read"].append(time.perf_counter() - mark)
            if frame is None:
                continue
            last_frame = frame.image

            mark = time.perf_counter()
            roi = crop_roi(frame.image, *capture.crop)
            timings["crop"].append(time.perf_counter() - mark)

            mark = time.perf_counter()
            reader.observe(side, roi)
            timings["recognize"].append(time.perf_counter() - mark)

            mark = time.perf_counter()
            height, width = frame.image.shape[:2]
            scale = min(1.0, web.preview_width / width)
            small = (cv2.resize(frame.image, (int(width * scale), int(height * scale)))
                     if scale < 1.0 else frame.image.copy())
            x, y, rw, rh = roi_rect(frame.image.shape, *capture.crop)
            cv2.rectangle(small, (int(x * scale), int(y * scale)),
                          (int((x + rw) * scale), int((y + rh) * scale)), (0, 200, 255), 2)
            timings["preview_resize"].append(time.perf_counter() - mark)

            mark = time.perf_counter()
            cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), web.preview_quality])
            timings["preview_encode"].append(time.perf_counter() - mark)

            timings["total"].append(time.perf_counter() - start)
            time.sleep(max(0.0, interval - (time.perf_counter() - start)))

        if round_index in marks:
            print(f"  {round_index:>4} 轮   RSS {rss_mb():6.0f} MB")

    report(timings)
    gc.collect()
    print(f"\nRSS 收尾 {rss_mb():.0f} MB（gc 之后）—— 前期上涨是 TFLite 一次性分配，"
          f"稳住不再涨就不是泄漏")

    if args.resolution and last_frame is not None:
        resolution_curve(reader, next(iter(sources)), last_frame, config)

    for source in sources.values():
        source.close()
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
