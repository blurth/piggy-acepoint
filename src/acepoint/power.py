"""从网页关机（display.web.allow_poweroff）。

设备没有关机键、靠充电宝供电，又开着只读文件系统以外的维护状态时，拔电前
正常关机能保住 SD 卡和最后一段录像。计分程序以普通用户运行，关机要借
sudo，而且只能是这一条一字不差的命令 —— 需要在设备上一次性授权：

    echo 'moray ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff' \\
        | sudo tee /etc/sudoers.d/acepoint-poweroff
    sudo chmod 440 /etc/sudoers.d/acepoint-poweroff && sudo visudo -c

没授权时网页按钮会直接告诉你缺这一步，而不是点了没反应。
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

POWEROFF = ("/usr/bin/systemctl", "poweroff")
SUDOERS_HINT = ("在树莓派上授权一次：echo 'moray ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff'"
                " | sudo tee /etc/sudoers.d/acepoint-poweroff")


def can_poweroff() -> bool:
    """sudo 是否允许免密执行关机（不会真的关）。"""
    if os.name == "nt":
        return False
    try:
        result = subprocess.run(["sudo", "-n", "-l", *POWEROFF], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def poweroff() -> None:
    """发起关机，不等它完成（systemd 会来停掉本进程）。"""
    log.warning("powering off on request from the web scoreboard")
    subprocess.Popen(["sudo", "-n", *POWEROFF])
