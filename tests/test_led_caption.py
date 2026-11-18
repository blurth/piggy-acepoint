"""点阵屏氛围字幕：语音模块说台词时整屏滚动 5 秒，再切回比分。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from acepoint.config import ConfigError, LedConfig
from acepoint.displays import led as led_module
from acepoint.displays.led import LedDisplay, caption_columns, caption_offset
from acepoint.state import SideStatus, Snapshot, Telemetry

REPO = Path(__file__).resolve().parent.parent


def test_every_line_in_the_voice_pack_can_be_drawn():
    """字模表由语音包台词生成；台词改了没重跑 build_led_glyphs.py 就会缺字。"""
    table = led_module._glyph_table()
    manifest = json.loads((REPO / "audio" / "manifest.json").read_text(encoding="utf-8"))
    missing = {ch for clip in manifest for ch in clip["text"] if ch not in table}
    assert not missing, f"字模表缺字，重跑 scripts/build_led_glyphs.py：{''.join(sorted(missing))}"


def test_glyphs_fit_in_eight_rows_and_chinese_is_eight_wide():
    columns = caption_columns("三连击！")
    assert len(columns) == 32                       # 4 个全角字 x 8px，正好一屏
    assert all(0 <= c < 256 for c in columns)
    assert any(columns)


def test_short_caption_is_centred_and_still():
    assert caption_offset(16, 0.0, 5.0, 32) == -8
    assert caption_offset(16, 4.9, 5.0, 32) == -8


def test_long_caption_holds_scrolls_and_holds():
    width = 72                                      # 9 个字
    assert caption_offset(width, 0.0, 5.0, 32) == 0
    assert caption_offset(width, 0.7, 5.0, 32) == 0             # 开头停住
    middle = caption_offset(width, 2.5, 5.0, 32)
    assert 0 < middle < width - 32
    assert caption_offset(width, 4.3, 5.0, 32) == width - 32    # 结尾停在最后一屏
    offsets = [caption_offset(width, t / 10, 5.0, 32) for t in range(51)]
    assert offsets == sorted(offsets)                           # 只往前滚


def test_caption_seconds_must_be_positive():
    with pytest.raises(ConfigError):
        LedConfig(caption_seconds=0)


def _telemetry(seq, text="三连击！手感来了！", *, version=1, holding=False):
    sides = {}
    if holding:
        sides["left"] = SideStatus(side="left", gesture="Victory", progress=0.2, required=0.2)
    return Telemetry(snapshot=Snapshot(left=3, version=version), sides=sides,
                     audio={"caption": text, "caption_seq": seq})


def test_first_telemetry_only_records_the_sequence():
    """开机时语音模块里留着的旧台词不该翻出来再放一遍。"""
    display = LedDisplay(LedConfig())
    display.update(_telemetry(4))
    assert display._caption is None
    display.update(_telemetry(5))
    assert display._caption is not None


def test_score_change_cuts_the_caption_short():
    display = LedDisplay(LedConfig())
    display.update(_telemetry(0))
    display.update(_telemetry(1, version=1))
    assert display._caption is not None
    display.update(_telemetry(1, version=2))
    assert display._caption is None


def test_a_gesture_in_progress_cuts_the_caption_short():
    display = LedDisplay(LedConfig())
    display.update(_telemetry(0))
    display.update(_telemetry(1))
    display.update(_telemetry(1, holding=True))
    assert display._caption is None


def test_captions_can_be_switched_off():
    display = LedDisplay(LedConfig(show_captions=False))
    display.update(_telemetry(0))
    display.update(_telemetry(1))
    assert display._caption is None


def test_render_thread_scrolls_then_returns_to_the_score(monkeypatch):
    monkeypatch.setattr(led_module, "_CAPTION_HOLD", 0.1)
    display = LedDisplay(LedConfig(caption_seconds=0.6, show_ip=False))
    display._device = object()
    frames, scores = [], []
    display._draw_caption = lambda columns, offset: frames.append(offset)
    display._draw = lambda snapshot, *, dark=None, bars=(): scores.append(snapshot.left)
    display._thread = threading.Thread(target=display._run, daemon=True)
    display._thread.start()
    try:
        display.update(_telemetry(0))
        time.sleep(0.1)
        scores.clear()
        display.update(_telemetry(1))
        time.sleep(0.9)
    finally:
        display._stop.set()
        display._wake.set()
        display._thread.join(timeout=2)
    assert frames and frames == sorted(frames)
    assert frames[-1] == len(caption_columns("三连击！手感来了！")) - 32
    assert scores == [3]                            # 字幕放完切回比分，且只重画一次


def test_reversed_blocks_put_the_caption_back_in_reading_order(monkeypatch):
    import sys
    import types
    from unittest.mock import MagicMock

    points = []

    class FakeCanvas:
        def __init__(self, device):
            self.draw = MagicMock()
            self.draw.point.side_effect = lambda xy, fill=None: points.extend(xy)

        def __enter__(self):
            return self.draw

        def __exit__(self, *exc):
            return False

    monkeypatch.setitem(sys.modules, "luma.core.render", types.SimpleNamespace(canvas=FakeCanvas))
    display = LedDisplay(LedConfig(reverse_blocks=True))
    display._device = object()
    display._draw_caption((0b1,) + (0,) * 31, 0)    # 只有整句第 0 列第 0 行亮
    assert points == [(16, 0)]                      # 画布右半边 = 屏幕左半边


def test_audio_service_counts_each_spoken_line(tmp_path):
    from acepoint.audio.config import AudioConfig
    from acepoint.audio.service import AudioService
    from acepoint.config import GameConfig
    from acepoint.state import Action

    clock = [0.0]
    service = AudioService(AudioConfig(enabled=True, backend="null"), GameConfig(),
                           REPO / "audio", clock=lambda: clock[0], threaded=False)
    service.initialize()
    assert service.snapshot()["caption_seq"] == 0
    before = Snapshot()
    for point in range(1, 4):                      # 左边三连 -> streak_3
        after = Snapshot(left=point, version=point)
        service.score("left", Action.INCREMENT, before, after)
        clock[0] += 1.0
        service.pump()
        before = after
    snap = service.snapshot()
    assert snap["caption_seq"] >= 1
    assert snap["caption"]
    assert snap["caption_event"] in ("streak_3", "banter", "icebreaker")


# ------------------------------------------------------------ 图标画面

from acepoint.displays.led_captions import (  # noqa: E402
    PAGE_SECONDS, SCENES, pick_scene, render_page, scene_frame)


def test_every_moment_with_a_spoken_line_has_a_picture():
    """有台词的事件都要有图标画面，否则点阵屏又退回糊掉的中文。"""
    from acepoint.audio.policy import PRIORITIES

    assert set(PRIORITIES) - {"point"} <= set(SCENES)


def test_every_page_fits_the_screen_in_both_frames():
    for options in SCENES.values():
        for pages in options:
            for page in pages:
                for frame in (0, 1):
                    columns = render_page(page, frame)
                    assert len(columns) == 32 and any(columns), page
                    assert all(c < 128 for c in columns), page   # 第 8 行留空


def test_pages_rotate_and_icons_animate():
    pages = ("{heart}NICE", "{heart}{heart}{heart}")
    assert scene_frame(pages, 0.0) == render_page(pages[0], 0)
    assert scene_frame(pages, 0.35) == render_page(pages[0], 1)      # 爱心跳一下
    assert scene_frame(pages, PAGE_SECONDS + 0.01) == render_page(pages[1], 0)
    assert scene_frame(pages, 2 * PAGE_SECONDS + 0.01) == render_page(pages[0], 0)


def test_banter_does_not_repeat_the_same_picture_twice_in_a_row():
    import random

    rng, previous = random.Random(1), None
    for _ in range(20):
        index, _pages = pick_scene("banter", rng, previous)
        assert index != previous
        previous = index


def test_icons_style_shows_the_scene_and_text_style_the_chinese_line():
    def caption_for(style):
        display = LedDisplay(LedConfig(caption_style=style))
        for seq in (0, 1):
            display.update(Telemetry(snapshot=Snapshot(version=1), sides={}, audio={
                "caption": "三连击！手感来了！", "caption_event": "streak_3",
                "caption_seq": seq}))
        return display._caption

    assert caption_for("icons").pages in SCENES["streak_3"]
    text = caption_for("text")
    assert not text.pages and text.columns == caption_columns("三连击！手感来了！")


def test_unknown_moment_falls_back_to_the_chinese_line():
    display = LedDisplay(LedConfig())
    for seq in (0, 1):
        display.update(Telemetry(snapshot=Snapshot(version=1), sides={}, audio={
            "caption": "好球", "caption_event": "something_new", "caption_seq": seq}))
    assert display._caption.columns == caption_columns("好球")


def test_caption_style_is_validated():
    with pytest.raises(ConfigError):
        LedConfig(caption_style="emoji")
