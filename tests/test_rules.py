"""21 分制胜负判定（需求 §5.5）。"""

from __future__ import annotations

import pytest

from acepoint.config import ConfigError, GameConfig
from acepoint.rules import match_points, winner_of

RULES = GameConfig()


@pytest.mark.parametrize("left,right,expected", [
    (0, 0, None),
    (5, 3, None),
    (20, 19, None),
    (21, 19, "left"),        # 先到 21 且领先 2
    (19, 21, "right"),
    (21, 20, None),          # 到了 21 但只领先 1，还得打
    (22, 20, "left"),        # 20:20 之后领先 2 才算
    (24, 23, None),
    (25, 23, "left"),
    (29, 28, None),
    (29, 29, None),
    (30, 29, "left"),        # 封顶：29:29 后先得第 30 分者胜，不要求领先 2
    (29, 30, "right"),
])
def test_winner(left, right, expected):
    assert winner_of(left, right, RULES) == expected


def test_deuce_can_run_long():
    """20:20 之后一直没拉开 2 分就一直打下去。"""
    for point in range(21, 29):
        assert winner_of(point, point, RULES) is None
        assert winner_of(point, point - 1, RULES) is None


def test_disabled_never_reports_a_winner():
    off = GameConfig(enabled=False)
    assert winner_of(30, 0, off) is None


def test_custom_short_game():
    """练习局：先到 11 分领先 2 分。"""
    short = GameConfig(target=11, win_by=2, cap=15)
    assert winner_of(11, 9, short) == "left"
    assert winner_of(11, 10, short) is None
    assert winner_of(15, 14, short) == "left"


@pytest.mark.parametrize("left,right,expected", [
    (0, 0, ()),
    (19, 15, ()),
    (20, 15, ("left",)),      # 再得 1 分就是 21:15
    (15, 20, ("right",)),
    (20, 19, ("left",)),
    (20, 20, ()),             # 21:20 还赢不了，两边都不是赛点
    (21, 20, ("left",)),      # 22:20 就赢了
    (28, 29, ("right",)),
    (29, 29, ("left", "right")),   # 谁先拿第 30 分谁赢，双方同时赛点
    (21, 19, ()),             # 已经赢了，不再报赛点
])
def test_match_points(left, right, expected):
    assert match_points(left, right, RULES) == expected


def test_match_point_is_silent_when_rules_are_off():
    assert match_points(20, 19, GameConfig(enabled=False)) == ()


def test_cap_below_target_is_rejected():
    with pytest.raises(ConfigError, match="cap"):
        GameConfig(target=21, cap=15)


def test_invalid_target_is_rejected():
    with pytest.raises(ConfigError, match="target"):
        GameConfig(target=0)
