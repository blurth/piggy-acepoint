"""采样相位：手势落在采样点之间的哪个位置，会不会漏判或误判。

主循环每路只有约 5fps，一次手势只落到 3~4 个采样点上，而状态机比较的是
**首末采样点的跨度**，不是手势的真实时长 —— 跨度最多比真实时长短一个采样周期。
所以要扫遍相位，确认发货配置在最坏相位下依然成立。

这里用的是真正发货的 config.toml，不是测试专用参数：这些断言守的就是实际部署值。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from acepoint.config import load_config
from acepoint.fsm import GestureFSM

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(REPO_ROOT / "config.toml")
SCORING = replace(CONFIG.scoring, cooldown_seconds=0.0)   # 冷却与相位无关，摘掉
PERIOD = 1.0 / (CONFIG.capture.poll_hz / 2)               # 每路采样周期
PHASES = 40                                               # 一个采样周期内扫多少个相位


def fires(duration: float, phase: float, scoring=SCORING) -> bool:
    """手势从 phase 起持续 duration 秒，按采样周期喂给状态机，看触不触发。"""
    fsm = GestureFSM("left", scoring)
    now = 0.0
    while now < phase + duration + 4 * PERIOD + scoring.hold_grace_seconds:
        present = phase <= now < phase + duration
        if fsm.feed("Victory" if present else None, now) is not None:
            return True
        now += PERIOD
    return False


def trigger_rate(duration: float, scoring=SCORING) -> float:
    hits = sum(fires(duration, i * PERIOD / PHASES, scoring) for i in range(PHASES))
    return hits / PHASES


@pytest.mark.parametrize("duration", [0.05, 0.1, 0.15, 0.2])
def test_a_flicker_never_scores_at_any_phase(duration):
    """短于一个采样周期的闪现只会被看到一次，任何相位都不该计分。

    这是 hold_min_samples 存在的唯一理由：只有 hold_grace 的话，容忍窗口会让
    计时一直跑到 hold_seconds，实测这类闪现的误触率高达 98%。
    """
    assert trigger_rate(duration) == 0.0


@pytest.mark.parametrize("duration", [0.4, 0.5, 0.6, 0.8, 1.0, 1.6])
def test_a_real_gesture_scores_at_every_phase(duration):
    """真人自然手势实测 0.50~1.60 秒，这个区间必须相位无关地全中。"""
    assert trigger_rate(duration) == 1.0


def test_the_shipped_config_separates_flickers_from_gestures():
    """闪现 0%、真实手势 100%，中间没有含糊地带才算参数配得住。"""
    assert trigger_rate(0.2) == 0.0
    assert trigger_rate(0.4) == 1.0


def test_grace_is_what_makes_short_gestures_survive_sampling():
    """去掉容忍窗口，短手势就会因为采样截断而在部分相位上漏判。

    注意 `hold_grace_seconds = 0` 并不等于"零容忍"：手势刚消失那一个采样点，
    距丢失时刻正好 0 秒，仍在窗口内（`now - lost_at <= grace`）。
    所以 grace=0 实际还留着一个采样点的余量，只是比 0.4 少两个。
    """
    without = replace(SCORING, hold_grace_seconds=0.0)
    assert trigger_rate(0.4, without) < 1.0
    assert trigger_rate(0.4) == 1.0


def test_measured_span_is_shorter_than_the_real_duration():
    """状态机看到的跨度最多比真实时长短一个采样周期 —— 参数要按跨度来配。"""
    duration, worst = 0.6, None
    for index in range(PHASES):
        phase = index * PERIOD / PHASES
        seen = [t for t in (i * PERIOD for i in range(20))
                if phase <= t < phase + duration]
        span = seen[-1] - seen[0] if len(seen) > 1 else 0.0
        worst = span if worst is None else min(worst, span)
    assert worst == pytest.approx(duration - PERIOD, abs=1e-6)
    assert worst < CONFIG.scoring.hold_seconds     # 正因为够不着，才需要容忍窗口
