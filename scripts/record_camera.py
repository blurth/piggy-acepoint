"""从配置里的视频源录一段素材，给识别区标定用。

    sudo systemctl stop acepoint                 # 摄像头不能被服务占着
    python scripts/record_camera.py -c config.pi.toml left
    python scripts/record_camera.py -c config.pi.toml right --seconds 45
    python scripts/compare_roi.py -c config.pi.toml videos/calib-left.avi \
        --ratios 0.60x0.80 0.40x0.30@0.50x0.35 0.30x0.25@0.50x0.35
    sudo systemctl start acepoint

走的是项目自己的视频源（同样的设备路径、MJPG、分辨率），录下来的就是识别器实际
看到的画面。识别区必须在**真实机位**上标定：设备装在网柱顶、球员站在 2~3 米处，
按平时的习惯比手势。桌面上录的素材取景完全不同，标出来的识别区搬不到球场上。

录的时候每个位置比 3~5 次"耶"，每次保持 1 秒左右、中间把手放下：
近处（约 2 米）、远处（约 3 米）、偏左、偏右各来几次，覆盖球员实际会站的范围。

视频按固定帧率写入，并且**跟挂钟对齐**：某一拍没取到新帧就把上一帧再写一遍。
否则丢帧会让视频时间变短，回放时手势保持时长就量不准了。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

import cv2

from _shared import REPO_ROOT

from acepoint.config import load_config
from acepoint.sources import build_source


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("side", choices=("left", "right"), help="录哪个半场的视频源")
    parser.add_argument("-c", "--config", type=Path, default=REPO_ROOT / "config.toml")
    parser.add_argument("--seconds", type=float, default=30.0, help="录多久（默认 30 秒）")
    parser.add_argument("--fps", type=float, default=10.0,
                        help="写入帧率（默认 10：Pi 4 解码加编码 1080p 能稳住的水平）")
    parser.add_argument("-o", "--output", type=Path, help="输出文件（默认 videos/calib-<side>.avi）")
    args = parser.parse_args()

    config = load_config(args.config)
    spec = next((s for s in config.sources if s.side == args.side), None)
    if spec is None:
        raise SystemExit(f"{args.config.name} 里没有配置 {args.side} 半场的视频源")
    # 录制要尽量多拿帧，所以这里用持续解码，和服务平时用哪种方式无关
    source = build_source(spec, replace(config.capture, decode_on_demand=False), config)
    if source is None:
        raise SystemExit(f"{args.side} 的视频源打不开 —— 服务停了吗？sudo systemctl stop acepoint")

    output = args.output or config.resolve(f"videos/calib-{args.side}.avi")
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    written = repeated = 0
    try:
        print(source.description)
        for remaining in (3, 2, 1):
            print(f"{remaining} 秒后开始录制 …", flush=True)
            time.sleep(1)

        period = 1.0 / args.fps
        total = round(args.seconds * args.fps)
        started = time.monotonic()
        last = None
        for index in range(total):
            delay = started + index * period - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            frame = source.read()
            if frame is not None:
                last = frame.image
            elif last is not None:
                repeated += 1
            if last is None:
                continue                              # 相机还没出第一帧
            if writer is None:
                height, width = last.shape[:2]
                writer = cv2.VideoWriter(str(output), cv2.VideoWriter.fourcc(*"MJPG"),
                                         args.fps, (width, height))
                if not writer.isOpened():
                    raise SystemExit("当前 OpenCV 没有可用的 MJPG 编码器")
            writer.write(last)
            written += 1
            if (index + 1) % round(args.fps) == 0:
                print(f"  已录 {(index + 1) / args.fps:.0f}/{args.seconds:.0f} 秒", flush=True)
    finally:
        source.close()
        if writer is not None:
            writer.release()

    if not written:
        raise SystemExit("一帧都没录到")
    size_mb = output.stat().st_size / 1e6
    print(f"\n写入 {output}（{written} 帧，{written / args.fps:.1f} 秒，{size_mb:.0f} MB；"
          f"其中 {repeated} 帧是没取到新画面时补的上一帧）")
    if repeated > written * 0.3:
        print("补帧超过三成：相机出帧比录制帧率慢，调低 --fps 再录一次更准")
    print("\n下一步：扫识别区（先扫中心，再扫大小）")
    print(f"  python scripts/compare_roi.py -c {args.config} {output} "
          "--ratios 0.60x0.80 0.40x0.30@0.50x0.35 0.30x0.25@0.50x0.35")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
