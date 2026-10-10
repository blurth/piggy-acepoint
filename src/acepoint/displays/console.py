"""终端显示：比分变化时打印一行。没接屏幕时的最小可用输出。"""

from __future__ import annotations

import logging

from acepoint.displays.base import Display
from acepoint.state import Telemetry

log = logging.getLogger("acepoint.score")


class ConsoleDisplay(Display):
    def __init__(self) -> None:
        self._version = -1

    def update(self, telemetry: Telemetry) -> None:
        snapshot = telemetry.snapshot
        if snapshot.version == self._version:
            return
        self._version = snapshot.version
        detail = f"  <- {snapshot.last_event}" if snapshot.last_event else ""
        if snapshot.winner:
            detail += f"   ** {snapshot.winner.upper()} WINS **  (张开手掌 3 秒开下一局)"
        elif snapshot.match_points:
            detail += f"   [赛点: {'/'.join(snapshot.match_points)}]"
        log.info("%s%s", snapshot.marked_text, detail)
