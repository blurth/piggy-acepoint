"""从 8px 中文点阵字体生成点阵屏用的字模表 src/acepoint/displays/led_glyphs.json。

    python scripts/build_led_glyphs.py <fusion-pixel-8px-monospaced-zh_hans.bdf>

字体：Fusion Pixel Font 8px（https://github.com/TakWolf/fusion-pixel-font，
SIL OFL 1.1），下载 release 里的 fusion-pixel-font-8px-monospaced-bdf-*.zip。
字体本体有 2 万多字、二十几 MB，不进仓库 —— 只取语音包台词（audio/manifest.json）
和可打印 ASCII 用到的字，几十 KB。台词改了就重跑一遍。

为什么不在运行时用 PIL 渲染 TTF：8px 的字一个像素都不能错，BDF 是逐点的位图，
直接读最准；树莓派上也就不用多装字体文件。

输出格式：{"height": 8, "glyphs": {"字": [宽度, [第0列, 第1列, ...]]}}，
每列一个整数，第 r 位 = 第 r 行（0 在最上面）亮。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "src" / "acepoint" / "displays" / "led_glyphs.json"
HEIGHT = 8


def parse_bdf(path: Path) -> tuple[int, dict[int, tuple[int, list[int]]]]:
    """返回 (ascent, {码位: (宽度, 每列位图)})。"""
    ascent = None
    glyphs: dict[int, tuple[int, list[int]]] = {}
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    for line in lines:
        if line.startswith("FONT_ASCENT"):
            ascent = int(line.split()[1])
        elif line.startswith("STARTCHAR"):
            code = advance = None
            bbx = (0, 0, 0, 0)
            for line in lines:
                key, *rest = line.split()
                if key == "ENCODING":
                    code = int(rest[0])
                elif key == "DWIDTH":
                    advance = int(rest[0])
                elif key == "BBX":
                    bbx = tuple(int(v) for v in rest)
                elif key == "BITMAP":
                    break
            w, h, xoff, yoff = bbx
            rows = [int(next(lines), 16) for _ in range(h)]
            next(lines)                                   # ENDCHAR
            nbits = ((w + 7) // 8) * 8
            columns = [0] * max(advance or 0, w + xoff, 0)
            top = ascent - (yoff + h)                     # 字形最上一行落在第几行
            for r, bits in enumerate(rows):
                y = top + r
                if not 0 <= y < HEIGHT:
                    continue
                for c in range(w):
                    if bits >> (nbits - 1 - c) & 1:
                        x = xoff + c
                        if 0 <= x < len(columns):
                            columns[x] |= 1 << y
            glyphs[code] = (advance or w, columns)
    if ascent is None:
        raise SystemExit("不是 BDF 字体：没有 FONT_ASCENT")
    return ascent, glyphs


def wanted_chars() -> set[str]:
    manifest = json.loads((REPO / "audio" / "manifest.json").read_text(encoding="utf-8"))
    chars = {chr(c) for c in range(0x20, 0x7F)}
    for clip in manifest:
        chars.update(clip.get("text", ""))
    return chars


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bdf", type=Path)
    args = parser.parse_args()

    _, glyphs = parse_bdf(args.bdf)
    table, missing = {}, []
    for ch in sorted(wanted_chars()):
        glyph = glyphs.get(ord(ch))
        if glyph is None:
            missing.append(ch)
            continue
        table[ch] = [glyph[0], glyph[1]]
    if missing:
        print(f"字体里没有这些字（显示时跳过）：{''.join(missing)!r}", file=sys.stderr)

    OUT.write_text(json.dumps({
        "source": "Fusion Pixel Font 8px monospaced zh_hans (SIL OFL 1.1, "
                  "https://github.com/TakWolf/fusion-pixel-font), see LED_FONT_LICENSE.txt",
        "height": HEIGHT,
        "glyphs": table,
    }, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(table)} 个字 -> {OUT.relative_to(REPO)} ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
