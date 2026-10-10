"""手势状态机：持续确认 + 冷却（需求 §5.2）。

纯逻辑，不碰摄像头也不碰比分板 —— 喂进手势名和当前时间，吐出 Action 或 None。
每个半场一个实例，互不影响。
"""

from __future__ import annotations

from acepoint.config import ScoringConfig
from acepoint.state import Action, SideStatus

# MediaPipe 预置手势 -> 动作（需求 §5.1，无需训练模型）
# 加分给了两个手势，球员比哪个都行，不用记
GESTURE_ACTIONS: dict[str, Action] = {
    "Victory": Action.INCREMENT,
    "Pointing_Up": Action.INCREMENT,
    "Thumb_Down": Action.DECREMENT,
    "Open_Palm": Action.RESET,
}


class GestureFSM:
    """单个半场的手势状态机。

    规则：
      1. 手势必须连续保持同一个不变，累计达到时长才触发；中途换手势重新计时。
      2. 触发后进入冷却，冷却期内本半场忽略一切手势。
    """

    def __init__(self, side: str, config: ScoringConfig) -> None:
        self.side = side
        self.config = config
        self._current: str | None = None
        self._since: float = 0.0
        self._seen = 0                      # 本次保持中真正识别到的次数（不含容忍窗口）
        self._cooldown_until: float = 0.0
        # 刚刚丢掉的手势 (手势名, 起始时间, 已检出次数) 和丢掉的时刻。
        # grace 窗口内它回来了就接着原来的计时，而不是从头再数。
        self._pending: tuple[str, float, int] | None = None
        self._lost_at: float = 0.0

    def required_hold(self, gesture: str | None) -> float:
        """该手势需要保持多久。清零手势门槛更高，防误触。"""
        if gesture is None:
            return 0.0
        if GESTURE_ACTIONS.get(gesture) is Action.RESET:
            return self.config.reset_hold_seconds
        if gesture in GESTURE_ACTIONS:
            return self.config.hold_seconds
        return 0.0

    def feed(self, gesture: str | None, now: float, *, fresh: bool = True) -> Action | None:
        """喂入本帧识别到的手势（未识别到传 None），触发时返回动作。

        fresh=False 表示"这一帧没有新证据"——引擎复用了上一帧的识别结果
        （视频源还没走到下一帧）。时间照常推进，但**不计入检出次数**，
        否则同一次识别被重放几遍就能凑够 hold_min_samples，等于把这道防线拆了。
        """
        if now < self._cooldown_until:
            self._reset(now)
            return None

        if gesture is not None:
            if gesture != self._current:
                if (self._pending and self._pending[0] == gesture
                        and self._within_grace(now)):
                    # 闪断后又回来了，接上原计时和已检出次数
                    _, self._since, self._seen = self._pending
                else:
                    self._since, self._seen = now, 0
                self._current = gesture
            self._pending = None
            if fresh:                           # 只有真正识别到才计数
                self._seen += 1
            effective = gesture
        else:
            # 手势丢了：不立刻清零，先挂起
            if self._current is not None:
                self._pending = (self._current, self._since, self._seen)
                self._lost_at = now
                self._current = None
            if not (self._pending and self._within_grace(now)):
                self._pending = None
                return None
            # grace 窗口内仍按"手势还在"计时判定 —— 否则手势恰好在最后一个
            # 采样点掉了就永远触发不了，实测成功率会从 91% 掉到 55%
            effective = self._pending[0]

        action = GESTURE_ACTIONS.get(effective)
        if action is None:
            return None

        # 光有时长不够：容忍窗口能补偿采样截断，但不能把"只闪现一次"凑成一次保持
        if self._seen < self.config.hold_min_samples:
            return None

        if now - self._since >= self.required_hold(effective):
            # 触发后立刻进冷却并清空手势，避免同一次抬手连续触发两次
            self._cooldown_until = now + self.config.cooldown_seconds
            self._reset(now)
            return action
        return None

    def _within_grace(self, now: float) -> bool:
        return now - self._lost_at <= self.config.hold_grace_seconds

    def _reset(self, now: float) -> None:
        self._current = None
        self._since = now
        self._seen = 0
        self._pending = None

    def status(self, now: float, *, confidence: float = 0.0) -> SideStatus:
        """给界面看的实时状态。

        grace 窗口内虽然当前帧没识别到，但计时还在走，界面上也照实显示 ——
        否则进度条会在每次闪断时掉回 0 再弹回来，看着像坏了。
        """
        gesture = self._current
        if gesture is None and self._pending and self._within_grace(now):
            gesture = self._pending[0]

        cooldown = max(0.0, self._cooldown_until - now)
        required = self.required_hold(gesture)
        held = (now - self._since) if gesture else 0.0
        progress = min(1.0, held / required) if required > 0 else 0.0
        return SideStatus(
            side=self.side,
            gesture=gesture,
            confidence=confidence,
            held=held,
            required=required,
            progress=progress,
            cooldown_remaining=cooldown,
            cooldown_total=self.config.cooldown_seconds,
        )

    def force_cooldown(self, now: float) -> None:
        """网页手动改分后也走一次冷却，避免手动和手势打架。"""
        self._cooldown_until = now + self.config.cooldown_seconds
        self._reset(now)
