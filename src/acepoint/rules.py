"""羽毛球 21 分制胜负判定。

纯函数，不碰状态也不碰硬件 —— 规则错了单测里就能发现。

完整规则（需求 §5.5）：
  1. 先到 21 分且领先 ≥2 分者胜
  2. 20:20 之后，先领先 2 分者胜（可能打到 24:22、27:25…）
  3. 29:29 之后，先得第 30 分者直接获胜 —— 封顶，不再要求领先 2 分
第 3 条最容易漏：没有封顶的话 30:29 不算赢，理论上会一直打下去。
"""

from __future__ import annotations

from acepoint.config import GameConfig


def winner_of(left: int, right: int, config: GameConfig) -> str | None:
    """返回获胜方 "left"/"right"，还没分出胜负返回 None。"""
    if not config.enabled or left == right:
        return None

    side = "left" if left > right else "right"
    high, low = max(left, right), min(left, right)

    if high >= config.cap:                       # 29:29 之后先到 30 者胜
        return side
    if high >= config.target and high - low >= config.win_by:
        return side
    return None


def match_points(left: int, right: int, config: GameConfig) -> tuple[str, ...]:
    """返回正处于赛点的一方（再得 1 分就赢）。

    两边可能同时是赛点：29:29 时谁先拿第 30 分谁赢。
    20:20 则两边都不是 —— 21:20 还赢不了。
    """
    if not config.enabled or winner_of(left, right, config) is not None:
        return ()
    sides = []
    if winner_of(left + 1, right, config) == "left":
        sides.append("left")
    if winner_of(left, right + 1, config) == "right":
        sides.append("right")
    return tuple(sides)
