"""生成两段占位视频（videos/A.mp4 / videos/B.mp4），用来在真实素材到位前跑通链路。

    python scripts/make_sample_videos.py

画面里只有色块和帧号，**不含真人手势**，所以不会加分 ——
它验证的是"取帧 → 识别 → 网页刷新"这条链路通不通，不验证识别准确率。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "videos"
WIDTH, HEIGHT, FPS, SECONDS = 1280, 720, 30, 10


def _writer(path: Path):
    for codec in ("mp4v", "MJPG"):
        target = path if codec == "mp4v" else path.with_suffix(".avi")
        writer = cv2.VideoWriter(str(target), cv2.VideoWriter.fourcc(*codec), FPS, (WIDTH, HEIGHT))
        if writer.isOpened():
            return writer, target
        writer.release()
    return None, path


def make(path: Path, label: str, color: tuple[int, int, int]) -> Path | None:
    writer, target = _writer(path)
    if writer is None:
        print(f"无法创建视频文件: {path}", file=sys.stderr)
        return None

    total = FPS * SECONDS
    for i in range(total):
        frame = np.full((HEIGHT, WIDTH, 3), 24, dtype=np.uint8)
        x = int((WIDTH - 200) * (0.5 + 0.5 * np.sin(2 * np.pi * i / total)))
        cv2.rectangle(frame, (x, 260), (x + 200, 460), color, -1)
        cv2.putText(frame, f"{label}  frame {i:04d}/{total}", (40, 80),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (235, 235, 235), 2, cv2.LINE_AA)
        writer.write(frame)
    writer.release()
    print(f"生成 {target} ({SECONDS}s @ {FPS}fps {WIDTH}x{HEIGHT})")
    return target


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ok = make(OUT_DIR / "A.mp4", "LEFT / A", (40, 140, 230))
    ok = make(OUT_DIR / "B.mp4", "RIGHT / B", (90, 200, 90)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
