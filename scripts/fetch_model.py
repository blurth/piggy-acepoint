"""下载 MediaPipe 手势识别模型到 models/gesture_recognizer.task。

    python scripts/fetch_model.py [目标路径]

模型约 8MB，只需下载一次。Windows 没有 wget，所以用标准库下。
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/latest/gesture_recognizer.task"
)
DEFAULT = Path(__file__).resolve().parent.parent / "models" / "gesture_recognizer.task"


def _progress(block: int, block_size: int, total: int) -> None:
    if total <= 0:
        return
    done = min(block * block_size, total)
    bar = "#" * int(30 * done / total)
    sys.stdout.write(f"\r  [{bar:<30}] {done / 1e6:5.1f} / {total / 1e6:.1f} MB")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    target = Path(argv[0]).resolve() if argv else DEFAULT
    if target.exists():
        print(f"已存在，跳过: {target} ({target.stat().st_size / 1e6:.1f} MB)")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    print(f"下载 {URL}\n  -> {target}")
    try:
        urllib.request.urlretrieve(URL, tmp, _progress)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"\n下载失败: {exc}\n可手动下载后放到 {target}", file=sys.stderr)
        return 1
    tmp.replace(target)
    print(f"\n完成: {target.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
