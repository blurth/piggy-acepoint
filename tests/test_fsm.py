"""手势状态机：防误触规则（需求 §5.2）。时间全部手工喂入，测试不 sleep。"""

from __future__ import annotations

import pytest

from acepoint.config import ScoringConfig
from acepoint.fsm import GestureFSM
from acepoint.state import Action


@pytest.fixture
def fsm() -> GestureFSM:
    """用整数好算的参数，别跟着配置默认值漂。"""
    return GestureFSM("left", ScoringConfig(hold_seconds=1.0,
                                            hold_grace_seconds=0.0,
                                            reset_hold_seconds=3.0,
                                            cooldown_seconds=8.0))


@pytest.fixture
def lenient() -> GestureFSM:
    """带丢检容忍窗口的状态机（实际部署用的形态）。"""
    return GestureFSM("left", ScoringConfig(hold_seconds=1.0,
                                            hold_grace_seconds=0.4,
                                            reset_hold_seconds=3.0,
                                            cooldown_seconds=8.0))


def test_hold_shorter_than_threshold_does_not_score(fsm):
    assert fsm.feed("Victory", 0.0) is None
    assert fsm.feed("Victory", 0.5) is None
    assert fsm.feed("Victory", 0.99) is None


def test_victory_scores_after_hold(fsm):
    fsm.feed("Victory", 0.0)
    assert fsm.feed("Victory", 1.0) is Action.INCREMENT


def test_pointing_up_also_scores(fsm):
    """食指向上和比耶等价，球员比哪个都行。"""
    fsm.feed("Pointing_Up", 0.0)
    assert fsm.feed("Pointing_Up", 1.0) is Action.INCREMENT


def test_switching_between_the_two_scoring_gestures_restarts_the_timer(fsm):
    """两个手势都加分，但它们不是同一个手势，不能接力凑时长。"""
    fsm.feed("Victory", 0.0)
    fsm.feed("Pointing_Up", 0.9)
    assert fsm.feed("Pointing_Up", 1.1) is None
    assert fsm.feed("Pointing_Up", 2.0) is Action.INCREMENT


def test_thumb_down_decrements(fsm):
    fsm.feed("Thumb_Down", 0.0)
    assert fsm.feed("Thumb_Down", 1.2) is Action.DECREMENT


def test_open_palm_needs_the_longer_hold(fsm):
    fsm.feed("Open_Palm", 0.0)
    assert fsm.feed("Open_Palm", 1.5) is None      # 加分手势的时长不够清零
    assert fsm.feed("Open_Palm", 3.0) is Action.RESET


def test_switching_gesture_restarts_the_timer(fsm):
    fsm.feed("Victory", 0.0)
    fsm.feed("Open_Palm", 0.9)                     # 中途换手势
    assert fsm.feed("Victory", 1.1) is None        # 重新计时，只保持了 0.2 秒
    assert fsm.feed("Victory", 2.2) is Action.INCREMENT


def test_losing_the_gesture_restarts_the_timer(fsm):
    fsm.feed("Victory", 0.0)
    fsm.feed(None, 0.8)                            # 手放下了
    fsm.feed("Victory", 0.9)
    assert fsm.feed("Victory", 1.5) is None
    assert fsm.feed("Victory", 1.95) is Action.INCREMENT


def test_cooldown_blocks_everything(fsm):
    fsm.feed("Victory", 0.0)
    assert fsm.feed("Victory", 1.0) is Action.INCREMENT
    for t in (1.1, 3.0, 5.0, 8.9):                 # 冷却 8 秒内一律不响应
        assert fsm.feed("Victory", t) is None


def test_scoring_resumes_after_cooldown(fsm):
    fsm.feed("Victory", 0.0)
    fsm.feed("Victory", 1.0)                       # 触发，冷却到 9.0
    fsm.feed("Victory", 9.1)                       # 冷却刚结束，重新开始计时
    assert fsm.feed("Victory", 10.2) is Action.INCREMENT


def test_one_hold_triggers_only_once(fsm):
    fsm.feed("Victory", 0.0)
    assert fsm.feed("Victory", 1.0) is Action.INCREMENT
    assert fsm.feed("Victory", 1.05) is None       # 手还举着，不能连加


def test_unmapped_gesture_is_ignored(fsm):
    fsm.feed("Closed_Fist", 0.0)
    assert fsm.feed("Closed_Fist", 5.0) is None


def test_status_reports_progress_and_cooldown(fsm):
    fsm.feed("Victory", 0.0)
    status = fsm.status(0.5)
    assert status.gesture == "Victory"
    assert status.progress == pytest.approx(0.5)
    assert status.cooldown_remaining == 0.0

    fsm.feed("Victory", 1.0)
    assert fsm.status(3.0).cooldown_remaining == pytest.approx(6.0)


def test_a_single_flicker_never_scores(lenient):
    """容忍窗口不能把"只闪现一次"凑成一次保持。

    5fps 采样下，手势只在 1 个采样点被识别到（真实时长 <0.2s，多半是手在
    变换姿势途中的误识）。若只看时长不看检出次数，容忍窗口会让计时继续跑，
    到 hold 时长就触发 —— 实测这样单次闪现的误触率高达 98%。
    """
    assert lenient.feed("Victory", 0.0) is None
    for t in (0.2, 0.4, 0.6, 0.8, 1.0, 1.2):
        assert lenient.feed(None, t) is None


def test_replayed_frames_do_not_count_as_detections(lenient):
    """引擎复用上一帧的识别结果时必须传 fresh=False。

    否则同一次识别被重放几遍就凑够了 hold_min_samples —— 单帧误识直接计分，
    正是 hold_min_samples 要挡住的那个 98% 误触模式。
    """
    lenient.feed("Victory", 0.0)                       # 唯一一次真实检出
    for t in (0.2, 0.4):                               # 视频源还没走到下一帧
        assert lenient.feed("Victory", t, fresh=False) is None
    for t in (0.6, 0.8, 1.0, 1.2):
        assert lenient.feed(None, t) is None


def test_replayed_frames_still_advance_the_timer(lenient):
    """但时间要照常走：重放不是"什么都没发生"，冷却和容忍窗口都得推进。"""
    lenient.feed("Victory", 0.0)
    lenient.feed("Victory", 0.4, fresh=False)
    assert lenient.feed("Victory", 1.0) is Action.INCREMENT   # 第二次真实检出补齐


def test_two_samples_plus_grace_do_score(lenient):
    """但真被看到两次就算数 —— 容忍窗口补偿采样截断是正当的。"""
    lenient.feed("Victory", 0.0)
    lenient.feed("Victory", 0.4)              # 第二次真实检出
    lenient.feed(None, 0.6)                   # 手势结束，进入容忍窗口
    lenient.feed(None, 0.8)
    assert lenient.feed(None, 1.0) is Action.INCREMENT


def test_detection_count_resets_with_the_gesture(lenient):
    """换了手势，上一个手势攒的检出次数不能继承。"""
    lenient.feed("Victory", 0.0)
    lenient.feed("Victory", 0.2)
    lenient.feed("Thumb_Down", 0.4)           # 换手势，次数归零
    for t in (0.6, 0.8, 1.0, 1.2, 1.4):       # 只被看到 1 次，不该触发
        assert lenient.feed(None, t) is None


def test_min_samples_can_be_raised(lenient):
    """球馆误触多的话可以调高门槛。"""
    strict = GestureFSM("left", ScoringConfig(hold_seconds=1.0, hold_grace_seconds=0.4,
                                              cooldown_seconds=8.0, hold_min_samples=4))
    for t in (0.0, 0.4, 0.8):                 # 只看到 3 次
        strict.feed("Victory", t)
    assert strict.feed(None, 1.2) is None
    assert strict.feed("Victory", 1.4) is not None   # 第 4 次到了才放行


def test_brief_dropout_does_not_restart_the_timer(lenient):
    """5fps 下识别难免闪断一帧，掉一帧就归零的话真人手势根本达不到时长。"""
    lenient.feed("Victory", 0.0)
    lenient.feed("Victory", 0.4)
    lenient.feed(None, 0.6)                        # 闪断一个采样点
    lenient.feed("Victory", 0.8)                   # 回来了，计时应该接上 0.0
    assert lenient.feed("Victory", 1.0) is Action.INCREMENT


def test_dropout_longer_than_grace_does_restart(lenient):
    """但手真的放下了就得重新计时，容忍窗口不能变成"永远记着"。"""
    lenient.feed("Victory", 0.0)
    lenient.feed(None, 0.4)
    lenient.feed("Victory", 1.0)                   # 丢了 0.6s > grace 0.4s
    assert lenient.feed("Victory", 1.5) is None
    assert lenient.feed("Victory", 2.0) is Action.INCREMENT


def test_grace_only_resumes_the_same_gesture(lenient):
    """闪断后换成另一个手势，不能继承上一个手势攒的时长。"""
    lenient.feed("Victory", 0.0)
    lenient.feed(None, 0.4)
    lenient.feed("Thumb_Down", 0.6)
    assert lenient.feed("Thumb_Down", 1.0) is None
    assert lenient.feed("Thumb_Down", 1.6) is Action.DECREMENT


def test_grace_does_not_survive_cooldown(lenient):
    lenient.feed("Victory", 0.0)
    assert lenient.feed("Victory", 1.0) is Action.INCREMENT
    lenient.feed(None, 1.1)
    assert lenient.feed("Victory", 1.2) is None    # 冷却里一律不响应


def test_status_keeps_the_progress_bar_steady_during_a_dropout(lenient):
    """闪断时进度条不该掉回 0 再弹回来。"""
    lenient.feed("Victory", 0.0)
    lenient.feed(None, 0.4)
    status = lenient.status(0.5)
    assert status.gesture == "Victory"
    assert status.progress == pytest.approx(0.5)

    stale = lenient.status(1.0)                    # 超出 grace 就该清掉
    assert stale.gesture is None


def test_force_cooldown_after_manual_edit(fsm):
    fsm.force_cooldown(0.0)
    fsm.feed("Victory", 1.0)
    assert fsm.feed("Victory", 5.0) is None
    assert fsm.status(0.0).cooldown_remaining == pytest.approx(8.0)
