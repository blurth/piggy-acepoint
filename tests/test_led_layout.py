"""点阵屏布局：4 块 8x8，左右各两块、每边两位数字。

luma 是 Pi 专属依赖，开发机上装不了，所以这里只测不依赖硬件的那部分：
几何计算，以及 _draw() 往画布上落的笔画（用假的 canvas 记录调用）。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from acepoint.config import ConfigError, LedConfig
from acepoint.displays.led import LedDisplay
from acepoint.state import Snapshot


def test_four_blocks_split_into_two_digits_per_side():
    config = LedConfig(cascaded=4)
    assert config.half_width == 16          # 每边 16px
    assert config.digits_per_side == 2      # 两位数字


def test_eight_blocks_would_give_three_digits():
    config = LedConfig(cascaded=8)
    assert (config.half_width, config.digits_per_side) == (32, 4)


@pytest.mark.parametrize("cascaded", [1, 3, 5])
def test_odd_block_count_is_rejected(cascaded):
    """左右要对半分，奇数块没法分。"""
    with pytest.raises(ConfigError, match="cascaded"):
        LedConfig(cascaded=cascaded)


@pytest.fixture
def fake_luma(monkeypatch):
    """把 luma 的 text/canvas/font 换成假的，记录画了什么。"""
    calls = {"text": [], "line": []}

    def fake_text(draw, xy, content, *, fill=None, font=None):
        calls["text"].append((xy, content))

    class FakeCanvas:
        def __init__(self, device):
            self.draw = MagicMock()
            self.draw.line.side_effect = lambda xy, fill=None: calls["line"].append(xy)

        def __enter__(self):
            return self.draw

        def __exit__(self, *exc):
            return False

    for name, module in {
        "luma.core.legacy": types.SimpleNamespace(text=fake_text),
        "luma.core.legacy.font": types.SimpleNamespace(CP437_FONT=object()),
        "luma.core.render": types.SimpleNamespace(canvas=FakeCanvas),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def draw_once(config: LedConfig, snapshot: Snapshot, **kwargs) -> None:
    display = LedDisplay(config)
    display._device = object()
    display._draw(snapshot, **kwargs)


def test_scores_land_on_their_own_half(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(left=21, right=9))
    assert fake_luma["text"] == [((0, 0), "21"), ((16, 0), "09")]


def test_single_digit_is_zero_padded(fake_luma):
    """个位永远在同一格，比分跳动时看着不晃。"""
    draw_once(LedConfig(cascaded=4), Snapshot(left=5, right=0))
    assert fake_luma["text"] == [((0, 0), "05"), ((16, 0), "00")]


def test_reverse_blocks_swaps_the_halves(fake_luma):
    draw_once(LedConfig(cascaded=4, reverse_blocks=True), Snapshot(left=21, right=9))
    assert fake_luma["text"] == [((16, 0), "21"), ((0, 0), "09")]


def test_match_point_draws_an_underline_under_that_side(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(left=20, right=19, match_points=("left",)))
    assert fake_luma["line"] == [(0, 7, 15, 7)]


def test_both_sides_at_match_point_get_underlines(fake_luma):
    draw_once(LedConfig(cascaded=4),
              Snapshot(left=29, right=29, match_points=("left", "right")))
    assert fake_luma["line"] == [(0, 7, 15, 7), (16, 7, 31, 7)]


def test_match_point_marker_can_be_switched_off(fake_luma):
    draw_once(LedConfig(cascaded=4, show_match_point=False),
              Snapshot(left=20, right=19, match_points=("left",)))
    assert fake_luma["line"] == []


def test_dark_side_is_not_drawn(fake_luma):
    """获胜方闪烁：熄灭那一侧时，另一侧要保持常亮。"""
    draw_once(LedConfig(cascaded=4), Snapshot(left=21, right=19, winner="left"),
              dark="left")
    assert fake_luma["text"] == [((16, 0), "19")]


def test_flash_blanks_both_sides(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(left=21, right=19), dark="both")
    assert fake_luma["text"] == []


def test_initial_score_shows_four_zeros(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot())
    assert fake_luma["text"] == [((0, 0), "00"), ((16, 0), "00")]


def test_start_draws_zeros_before_starting_thread(fake_luma, monkeypatch):
    display = LedDisplay(LedConfig())
    monkeypatch.setattr(display, "open_device", lambda: MagicMock())
    thread = MagicMock()
    monkeypatch.setattr("acepoint.displays.led.threading.Thread", lambda **kwargs: thread)
    def assert_initialized():
        assert fake_luma["text"] == [((0, 0), "00"), ((16, 0), "00")]

    thread.start.side_effect = assert_initialized
    display.start()
    assert fake_luma["text"] == [((0, 0), "00"), ((16, 0), "00")]
    thread.start.assert_called_once()
    display.close()
