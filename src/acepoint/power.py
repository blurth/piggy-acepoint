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
import time

log = logging.getLogger(__name__)

POWEROFF = ("/usr/bin/systemctl", "poweroff")
SUDOERS_HINT = ("在树莓派上授权一次：echo 'moray ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff'"
                " | sudo tee /etc/sudoers.d/acepoint-poweroff")


def can_poweroff() -> bool:
    """sudo 是否允许**免密**执行关机（不会真的关）。

    不能用 `sudo -n -l <命令>` 的退出码判断：树莓派的用户通常有
    `(ALL : ALL) ALL`（要密码），它照样返回 0，结果网页说能关、真关时 sudo 要密码失败。
    所以读 `sudo -n -l` 的清单，只认 NOPASSWD 下面一字不差的这条（或 NOPASSWD: ALL）。
    """
    if os.name == "nt":
        return False
    try:
        result = subprocess.run(["sudo", "-n", "-l"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and nopasswd_allows(result.stdout, " ".join(POWEROFF))


def nopasswd_allows(listing: str, command: str) -> bool:
    for line in listing.splitlines():
        if "NOPASSWD:" not in line:
            continue
        allowed = [c.strip() for c in line.split("NOPASSWD:", 1)[1].split(",")]
        if command in allowed or "ALL" in allowed:
            return True
    return False


def poweroff(*, linger: float = 60.0) -> bool:
    """发起关机，成功后原地等系统来停掉本进程。

    不能发完就退出：acepoint.service 是 Restart=always、KillMode 默认 control-group，
    主进程一退出，systemd 会把同一个 cgroup 里还没跑完的 sudo/systemctl 一起杀掉，
    5 秒后再把服务拉起来 —— 网页上看就是"点了关机没反应"（2026-11 实机）。
    所以同步等 systemctl 把关机任务交给 systemd（它很快就返回），然后不退出，
    让关机流程按顺序来停我们。
    """
    log.warning("powering off on request from the web scoreboard")
    try:
        result = subprocess.run(["sudo", "-n", *POWEROFF], capture_output=True, text=True,
                                timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        log.exception("关机命令执行失败")
        return False
    if result.returncode != 0:
        log.error("关机命令失败（%s）：%s", result.returncode, result.stderr.strip())
        return False
    time.sleep(linger)
    return True
