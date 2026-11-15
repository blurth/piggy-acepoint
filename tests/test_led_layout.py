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
    calls = {"text": [], "line": [], "point": []}

    def fake_text(draw, xy, content, *, fill=None, font=None):
        calls["text"].append((xy, content))

    class FakeCanvas:
        def __init__(self, device):
            self.draw = MagicMock()
            self.draw.line.side_effect = lambda xy, fill=None: calls["line"].append(xy)
            self.draw.point.side_effect = lambda xy, fill=None: calls["point"].append(xy)

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


# ----------------------------------------------------------------- 状态条
#
# 最底下一行：实线从本方外侧边缘变长 = 正在识别；虚线向本方边缘缩短 = 冷却。
# 只有一方有状态时占满 32px，两方都有就各用自己的 16px。

def _telemetry(**sides):
    from acepoint.state import SideStatus, Telemetry

    return Telemetry(snapshot=Snapshot(),
                     sides={side: SideStatus(side=side, **fields) for side, fields in sides.items()})


def _pixels(config: LedConfig, *, age: float = 0.0, **sides):
    """遥测到达 age 秒之后，状态条该亮成什么样。"""
    display = LedDisplay(config)
    bars = display._bars_from(_telemetry(**sides), 100.0)
    return display._bar_pixels(bars, 100.0 + age)


HOLDING = {"gesture": "Victory", "required": 0.5}
COOLING = {"cooldown_total": 5.0}


@pytest.mark.parametrize("progress, lit", [(0.0, 3), (0.25, 8), (0.5, 16), (1.0, 32), (1.7, 32)])
def test_a_lone_hold_bar_spans_the_whole_bottom_row(progress, lit):
    """进度为 0（刚认出手势）时也要亮一小段：这条线首先是告诉球员机器看见他了。"""
    assert _pixels(LedConfig(cascaded=4), left={**HOLDING, "progress": progress}) \
        == (("left", "hold", lit),)


@pytest.mark.parametrize("remaining, lit", [(5.0, 32), (2.5, 16), (0.16, 1), (0.05, 0)])
def test_a_lone_cooldown_bar_drains_across_the_whole_row(remaining, lit):
    pixels = _pixels(LedConfig(cascaded=4), right={**COOLING, "cooldown_remaining": remaining})
    assert pixels == ((("right", "cooldown", lit),) if lit else ())


def test_two_active_sides_each_keep_to_their_own_half():
    """左边在冷却、右边在比手势：谁也不遮谁，各退回自己的 16px。"""
    pixels = _pixels(LedConfig(cascaded=4),
                     left={**COOLING, "cooldown_remaining": 2.5},
                     right={**HOLDING, "progress": 0.5})
    assert pixels == (("left", "cooldown", 8), ("right", "hold", 8))


def test_hold_wins_over_cooldown_on_the_same_side():
    pixels = _pixels(LedConfig(cascaded=4),
                     left={**HOLDING, "progress": 0.5, **COOLING, "cooldown_remaining": 3.0})
    assert pixels == (("left", "hold", 16),)


def test_idle_sides_have_no_bar():
    assert _pixels(LedConfig(cascaded=4), left={"detected": "Victory"}, right={}) == ()


def test_bars_keep_moving_between_telemetry_updates():
    """遥测一秒只来四五次；渲染线程按上一次的速度往后推，条才是连续动的。"""
    config = LedConfig(cascaded=4)
    hold = {"left": {**HOLDING, "progress": 0.2}}
    assert _pixels(config, **hold) == (("left", "hold", 6),)
    assert _pixels(config, age=0.2, **hold) == (("left", "hold", 19),)       # 0.2 + 0.2/0.5
    assert _pixels(config, age=0.9, **hold) == (("left", "hold", 32),)       # 到头就停

    cool = {"right": {**COOLING, "cooldown_remaining": 5.0}}
    assert _pixels(config, age=0.5, **cool) == (("right", "cooldown", 29),)  # 4.5 / 5


def test_bars_stop_extrapolating_when_telemetry_stalls():
    """主循环卡住时冷却条不能自己一路缩到 0，那等于谎报"可以再比了"。"""
    pixels = _pixels(LedConfig(cascaded=4), age=30.0,
                     right={**COOLING, "cooldown_remaining": 5.0})
    assert pixels == (("right", "cooldown", 26),)                            # 只往前推 1 秒


def test_each_bar_can_be_switched_off():
    sides = {"left": {**HOLDING, "progress": 0.5},
             "right": {**COOLING, "cooldown_remaining": 2.5}}
    assert _pixels(LedConfig(cascaded=4, show_hold_progress=False), **sides) \
        == (("right", "cooldown", 16),)
    assert _pixels(LedConfig(cascaded=4, show_cooldown=False), **sides) \
        == (("left", "hold", 16),)


def test_hold_bar_is_a_solid_line_growing_from_that_sides_edge(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(left=3, right=5), bars=(("left", "hold", 20),))
    assert fake_luma["line"] == [(0, 7, 19, 7)]
    assert fake_luma["point"] == []


def test_right_sides_hold_bar_grows_leftwards_from_the_right_edge(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(), bars=(("right", "hold", 20),))
    assert fake_luma["line"] == [(12, 7, 31, 7)]


def test_cooldown_bar_is_dotted_and_anchored_to_that_sides_edge(fake_luma):
    """单色屏上靠图案区分：冷却隔一个点亮一个。固定在边缘的那个点不动，缩短时不跳。"""
    draw_once(LedConfig(cascaded=4), Snapshot(), bars=(("left", "cooldown", 7),))
    assert fake_luma["point"] == [[(0, 7), (2, 7), (4, 7), (6, 7)]]
    assert fake_luma["line"] == []


def test_right_sides_cooldown_bar_is_anchored_to_the_right_edge(fake_luma):
    draw_once(LedConfig(cascaded=4), Snapshot(), bars=(("right", "cooldown", 5),))
    assert fake_luma["point"] == [[(31, 7), (29, 7), (27, 7)]]


def test_bars_follow_reversed_blocks(fake_luma):
    """左右装反时左半场画在屏的右半，它的条也跟着从右边缘长出来。"""
    draw_once(LedConfig(cascaded=4, reverse_blocks=True), Snapshot(),
              bars=(("left", "hold", 8),))
    assert fake_luma["line"] == [(24, 7, 31, 7)]


def test_bars_take_the_row_over_from_the_match_point_underline(fake_luma):
    """赛点的整条实线会和识别条混在一起，有状态条时整行让给它。"""
    draw_once(LedConfig(cascaded=4), Snapshot(left=20, right=19, match_points=("left",)),
              bars=(("right", "cooldown", 6),))
    assert fake_luma["line"] == []
    assert fake_luma["point"] == [[(31, 7), (29, 7), (27, 7)]]


def test_bars_survive_a_blinking_winner(fake_luma):
    """获胜方数字熄灭的那半拍，撤销/清零手势的进度条照样要画。"""
    draw_once(LedConfig(cascaded=4), Snapshot(left=21, right=19, winner="left"),
              dark="left", bars=(("left", "hold", 10),))
    assert fake_luma["text"] == [((16, 0), "19")]
    assert fake_luma["line"] == [(0, 7, 9, 7)]


def test_update_hands_the_bars_to_the_render_thread():
    display = LedDisplay(LedConfig(cascaded=4))
    display.update(_telemetry(left={**HOLDING, "progress": 0.5}))
    assert [(bar.side, bar.kind, bar.fraction) for bar in display._bars] == [("left", "hold", 0.5)]


@pytest.fixture
def running_display():
    """起真的渲染线程，但把 _draw 换成记录调用，不碰硬件。"""
    import threading

    display = LedDisplay(LedConfig(cascaded=4))
    display._device = object()
    drawn = []
    display._draw = lambda snapshot, *, dark=None, bars=(): drawn.append((dark, bars))
    display._thread = threading.Thread(target=display._run, daemon=True)
    display._thread.start()
    yield display, drawn
    display._stop.set()
    display._wake.set()
    display._thread.join(timeout=2.0)


def test_render_thread_animates_the_bar_without_new_telemetry(running_display):
    """只推一次遥测，冷却条也要自己一格一格缩下去。"""
    import time

    display, drawn = running_display
    display.update(_telemetry(right={**COOLING, "cooldown_remaining": 5.0}))
    time.sleep(0.7)                                    # 5 秒缩 32 格，约 0.16 秒一格

    lengths = [bars[0][2] for _, bars in drawn if bars]
    assert lengths[0] == 32
    assert len(lengths) >= 3
    assert lengths == sorted(lengths, reverse=True)    # 只减不增
    assert len(set(lengths)) == len(lengths)           # 没动就不重画（不白写 SPI）


def test_cooldown_bar_appears_right_after_the_score_flash(running_display, monkeypatch):
    import time

    from acepoint.state import SideStatus, Telemetry

    monkeypatch.setattr("acepoint.displays.led._FLASH_INTERVAL", 0.01)
    display, drawn = running_display
    display.update(Telemetry(snapshot=Snapshot(), sides={}))
    time.sleep(0.1)
    drawn.clear()

    scored = Snapshot(left=1, version=1, flash_seq=1)
    display.update(Telemetry(snapshot=scored, sides={
        "left": SideStatus(side="left", cooldown_remaining=5.0, cooldown_total=5.0)}))
    time.sleep(0.3)

    flashes = [dark for dark, bars in drawn[:4]]
    assert flashes == ["both", None, "both", None]     # 闪两次，闪的时候不画条
    assert all(not bars for _, bars in drawn[:4])
    assert drawn[4][1][0][:2] == ("left", "cooldown")  # 闪完紧接着就是冷却条
