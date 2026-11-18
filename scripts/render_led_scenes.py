"""把点阵屏的氛围画面画成效果图（docs/screenshots/led-scenes.png），改图标后看一眼。

    python scripts/render_led_scenes.py [输出路径]

每个场景一行：每一页的两帧（图标动画）依次排开，按真实 8x32 圆点画。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from acepoint.displays.led_captions import SCENES, render_page  # noqa: E402

DOT, GAP = 9, 2
CELL = DOT + GAP
PANEL_W, PANEL_H = 32 * CELL + 6, 8 * CELL + 6
LABEL_W = 150
ON, OFF, BG = (40, 40, 255), (40, 30, 30), (18, 18, 18)


def panel(columns) -> np.ndarray:
    img = np.full((PANEL_H, PANEL_W, 3), BG, np.uint8)
    for x, bits in enumerate(columns):
        for y in range(8):
            c = (3 + x * CELL + DOT // 2, 3 + y * CELL + DOT // 2)
            cv2.circle(img, c, DOT // 2, ON if bits >> y & 1 else OFF, -1, cv2.LINE_AA)
    return img


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "docs" / "screenshots" / "led-scenes.png")
    rows = []
    for event, options in SCENES.items():
        for n, pages in enumerate(options):
            frames = [panel(render_page(p, f)) for p in pages for f in (0, 1)]
            label = np.full((PANEL_H, LABEL_W, 3), (255, 255, 255), np.uint8)
            cv2.putText(label, f"{event}" + (f" #{n + 1}" if len(options) > 1 else ""),
                        (6, PANEL_H // 2 + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1,
                        cv2.LINE_AA)
            spacer = np.full((PANEL_H, 10, 3), (255, 255, 255), np.uint8)
            parts = [label]
            for f in frames:
                parts += [f, spacer]
            rows.append(parts)
    width = max(sum(p.shape[1] for p in r) for r in rows)
    lines = []
    for r in rows:
        line = np.hstack(r)
        pad = np.full((PANEL_H, width - line.shape[1], 3), (255, 255, 255), np.uint8)
        lines += [np.hstack([line, pad]), np.full((8, width, 3), (255, 255, 255), np.uint8)]
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.vstack(lines))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
