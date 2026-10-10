"""比分状态与遥测数据。

ScoreBoard 是唯一的计分真相来源，线程安全；显示后端只读它的 Snapshot。
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum

from acepoint.config import GameConfig
from acepoint.rules import match_points, winner_of


class Action(Enum):
    """手势触发的计分动作。"""

    INCREMENT = "increment"
    DECREMENT = "decrement"
    RESET = "reset"


@dataclass(frozen=True)
class Snapshot:
    """某一时刻的比分。version/flash_seq 用于让显示端判断"要不要重画/要不要闪"。"""

    left: int = 0
    right: int = 0
    version: int = 0
    flash_seq: int = 0
    last_event: str = ""
    updated_at: float = 0.0
    winner: str | None = None                    # "left"/"right"，本局已分出胜负
    match_points: tuple[str, ...] = ()           # 处于赛点的一方，29:29 时两边都是

    @property
    def text(self) -> str:
        """LED 点阵的显示格式(需求 §5.3)。"""
        return f"{self.left}:{self.right}"

    @property
    def marked_text(self) -> str:
        """带赛点标记的比分：左方赛点 `*20:19`，右方 `19:20*`，双方 `*29:29*`。

        给终端日志用。点阵屏是左右各两位数字的布局，标记方式见 `displays/led.py`。
        """
        left_mark = "*" if "left" in self.match_points else ""
        right_mark = "*" if "right" in self.match_points else ""
        return f"{left_mark}{self.text}{right_mark}"

    @property
    def win_text(self) -> str:
        """获胜提示，和比分交替显示在点阵屏上。"""
        return f"{'L' if self.winner == 'left' else 'R'} WIN" if self.winner else ""

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "match_points": list(self.match_points),
            "text": self.text,
            "marked_text": self.marked_text,
            "win_text": self.win_text,
        }


@dataclass
class SideStatus:
    """单个半场的实时状态，只用于界面展示/调试，不参与计分判定。"""

    side: str
    online: bool = True
    gesture: str | None = None       # 状态机正在累计的手势；冷却期间恒为 None
    detected: str | None = None      # 识别器本帧的输出，不受冷却影响
    confidence: float = 0.0
    held: float = 0.0
    required: float = 0.0
    progress: float = 0.0            # 0~1，手势保持进度条
    cooldown_remaining: float = 0.0
    cooldown_total: float = 0.0
    fps: float = 0.0
    recognize_ms: float = 0.0        # 单帧识别耗时（滑动平均），Pi 上调参看这个
    position: float = 0.0            # 录制视频的播放位置（秒），摄像头恒为 0
    duration: float = 0.0            # 录制视频的总时长（秒），摄像头恒为 0
    loops: int = 0                   # 已循环播放几遍
    detail: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("held", "required", "progress", "cooldown_remaining",
                    "cooldown_total", "fps", "confidence", "recognize_ms",
                    "position", "duration"):
            data[key] = round(data[key], 3)
        return data


@dataclass
class Telemetry:
    """推给显示端的一帧完整状态。"""

    snapshot: Snapshot
    sides: dict[str, SideStatus] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": self.snapshot.to_dict(),
            "sides": {k: v.to_dict() for k, v in self.sides.items()},
        }


class ScoreBoard:
    """比分状态机。所有改动都要经过 apply()/set_score()，内部加锁。

    分出胜负后本局**冻结**：
      * 加分被忽略 —— 防止庆祝时挥手误触把比分刷上去
      * 撤销仍然放行 —— 万一最后那一分是误加的，Thumb_Down 能把胜负撤回来
      * 清零放行 —— Open_Palm 开下一局
    """

    def __init__(self, game: GameConfig | None = None, *, min_score: int = 0) -> None:
        self.game = game or GameConfig()
        self._min = min_score
        self._lock = threading.Lock()
        self._score = {"left": 0, "right": 0}
        self._version = 0
        self._flash_seq = 0
        self._last_event = ""
        self._updated_at = time.time()
        self._winner: str | None = None
        self._match_points: tuple[str, ...] = ()

    def apply(self, side: str, action: Action, *, source: str = "gesture") -> Snapshot:
        """执行一个计分动作，返回新的快照。RESET 会同时清零双方(需求 §5.1)。

        本局已结束时加分会被忽略，返回的快照与操作前一致（version 不变）。
        """
        if side not in self._score:
            raise ValueError(f"未知半场: {side!r}")
        with self._lock:
            if action is Action.RESET:
                self._score["left"] = self._score["right"] = 0
                self._last_event = f"reset by {side} ({source})"
            else:
                if self._winner is not None and action is Action.INCREMENT:
                    return self._snapshot_locked()      # 已分胜负，冻结加分
                delta = 1 if action is Action.INCREMENT else -1
                self._score[side] = max(self._min, self._score[side] + delta)
                self._last_event = f"{side} {'+1' if delta > 0 else '-1'} ({source})"
            self._refresh_winner_locked()
            self._version += 1
            self._flash_seq += 1        # 计分生效瞬间闪烁反馈(需求 §5.3)
            self._updated_at = time.time()
            return self._snapshot_locked()

    def set_score(self, left: int, right: int) -> Snapshot:
        with self._lock:
            self._score["left"] = max(self._min, int(left))
            self._score["right"] = max(self._min, int(right))
            self._refresh_winner_locked()
            self._version += 1
            self._last_event = "set"
            self._updated_at = time.time()
            return self._snapshot_locked()

    @property
    def winner(self) -> str | None:
        with self._lock:
            return self._winner

    def _refresh_winner_locked(self) -> None:
        left, right = self._score["left"], self._score["right"]
        self._winner = winner_of(left, right, self.game)
        self._match_points = match_points(left, right, self.game)

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Snapshot:
        return Snapshot(
            left=self._score["left"],
            right=self._score["right"],
            version=self._version,
            flash_seq=self._flash_seq,
            last_event=self._last_event,
            updated_at=self._updated_at,
            winner=self._winner,
            match_points=self._match_points,
        )
