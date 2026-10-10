"""显示后端：网页 / 终端 / LED 点阵。"""

from __future__ import annotations

import logging
from typing import Callable

from acepoint.config import Config
from acepoint.displays.base import CompositeDisplay, Display
from acepoint.displays.console import ConsoleDisplay
from acepoint.state import Action

log = logging.getLogger(__name__)

__all__ = ["Display", "CompositeDisplay", "ConsoleDisplay", "build_display"]


def build_display(
    config: Config,
    *,
    on_action: Callable[[str, Action], None] | None = None,
) -> CompositeDisplay:
    """按 display.backends 组装显示后端。LED 装不上时降级为终端输出。"""
    displays: list[Display] = []
    for name in config.display.backends:
        if name == "console":
            displays.append(ConsoleDisplay())
        elif name == "web":
            from acepoint.displays.web import WebDisplay

            displays.append(WebDisplay(config.display.web, on_action=on_action))
        elif name == "led":
            try:
                from acepoint.displays.led import LedDisplay
            except ImportError as exc:
                log.error("LED backend unavailable (%s); falling back to console", exc)
                displays.append(ConsoleDisplay())
            else:
                displays.append(LedDisplay(config.display.led))
    if not displays:
        displays.append(ConsoleDisplay())
    return CompositeDisplay(displays)
