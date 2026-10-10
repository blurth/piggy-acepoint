"""显示后端抽象。

同一份比分可以同时推给多个后端：开发阶段是网页，装到球场上是 LED 点阵，
终端打印随时可以叠加上去做日志。
"""

from __future__ import annotations

import abc
import logging

from acepoint.state import Telemetry

log = logging.getLogger(__name__)


class Display(abc.ABC):
    """比分输出目标。"""

    #: 是否需要主循环额外编码预览图。这是**动态**的：网页后端只在真的有人
    #: 看画面时才返回 True，没人看就省掉每帧的 resize + 画框 + JPEG 编码。
    wants_preview: bool = False

    def start(self) -> None:
        """启动后台线程/初始化硬件。"""

    @abc.abstractmethod
    def update(self, telemetry: Telemetry) -> None:
        """主循环每轮调用一次，必须快速返回，不能阻塞识别。"""

    def push_preview(self, side: str, jpeg: bytes) -> None:
        """收一帧预览图（JPEG 字节）。只有 wants_preview 为真时才会被调用。"""

    def close(self) -> None:
        """释放资源。"""


class CompositeDisplay(Display):
    """把多个后端串起来，任何一个抛异常都不能影响其它后端和主循环。"""

    def __init__(self, displays: list[Display]) -> None:
        self.displays = displays

    @property
    def wants_preview(self) -> bool:            # noqa: D102 - 见基类
        # 这是唯一在 _guard 之外被调用的后端属性，所以自己兜异常
        for display in self.displays:
            try:
                if display.wants_preview:
                    return True
            except Exception:                    # noqa: BLE001
                log.exception("display wants_preview failed")
        return False

    def start(self) -> None:
        active = []
        for display in self.displays:
            try:
                display.start()
            except Exception:
                log.exception("%s start failed; backend disabled", type(display).__name__)
                self._guard(display.close, "close")
            else:
                active.append(display)
        self.displays = active
        if not active:
            raise RuntimeError("没有显示后端成功启动，请检查启动日志")

    def update(self, telemetry: Telemetry) -> None:
        for display in self.displays:
            self._guard(display.update, "update", telemetry)

    def push_preview(self, side: str, jpeg: bytes) -> None:
        for display in self.displays:
            try:
                wanted = display.wants_preview
            except Exception:                    # noqa: BLE001
                log.exception("display wants_preview failed")
                continue
            if wanted:
                self._guard(display.push_preview, "push_preview", side, jpeg)

    def close(self) -> None:
        for display in self.displays:
            self._guard(display.close, "close")

    @staticmethod
    def _guard(func, what: str, *args) -> None:
        try:
            func(*args)
        except Exception:                     # noqa: BLE001 - 显示故障不能拖垮计分
            log.exception("display %s failed", what)
