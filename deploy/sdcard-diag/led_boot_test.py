#!/usr/bin/env python3
"""AcePoint 开机诊断：MAX7219 底层自检 + 环境信息，日志写回 SD 卡启动分区。

只用标准库（不依赖 spidev / luma / 项目虚拟环境），项目没装好也能跑。
由 acepoint-diag.service 每次开机以 root 运行。拔卡到 Windows 上看：
    <SD 卡>/acepoint-diag/logs/boot-NNNN.txt

开机时屏上应依次出现（各 6 秒）：
    CE0 (24 脚) 全亮 -> CE0 棋盘格 -> 熄 2 秒 -> CE1 (26 脚) 全亮 -> CE1 棋盘格
哪一轮亮了就说明 CS 接在哪个脚；两轮都不亮就是供电 / DIN 端 / 电平问题。
"""

from __future__ import annotations

import fcntl
import glob
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
KEEP_LOGS = 20

CASCADED = 4
SPEED_HZ = 500_000          # 故意放慢，排除信号质量干扰
STEP_SECONDS = 6
LATE_DELAY = 120            # 开机后多久再抓一次 acepoint 服务日志

# linux/spi/spidev.h：_IOW('k', nr, size)
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

REG_DIGIT0 = 0x01
REG_DECODE = 0x09
REG_INTENSITY = 0x0A
REG_SCAN_LIMIT = 0x0B
REG_SHUTDOWN = 0x0C
REG_DISPLAY_TEST = 0x0F

INFO_COMMANDS = [
    ("型号", "cat /proc/device-tree/model; echo"),
    ("内核", "uname -a"),
    ("运行时间", "uptime"),
    ("欠压/降频 (0x0 为正常; 末位1=当前欠压, 0x5xxxx=开机后发生过)", "vcgencmd get_throttled"),
    ("config.txt 里的 SPI", "grep -n -i spi /boot/firmware/config.txt"),
    ("SPI 设备节点", "ls -l /dev/spidev*"),
    ("SPI 内核模块", "lsmod | grep -i spi"),
    ("GPIO7-11 引脚功能 (应为 SPI0_*)", "pinctrl get 7-11 2>/dev/null || raspi-gpio get 7-11"),
    ("dmesg 中的 SPI", "dmesg | grep -i -E 'spi|max7219' | tail -20"),
    ("家目录", "ls -d /home/*/"),
    ("项目与虚拟环境", "ls -d /home/*/AcePoint /home/*/scorer 2>&1"),
    ("虚拟环境依赖",
     "for p in /home/*/scorer/bin/python; do echo \"$p\"; "
     "\"$p\" -c 'import spidev, luma.led_matrix; print(\"spidev + luma OK\")' 2>&1 | tail -1; done"),
    ("spi 组成员", "getent group spi"),
    ("acepoint 服务是否启用/运行", "systemctl is-enabled acepoint; systemctl is-active acepoint"),
    ("acepoint 服务的用户和路径",
     "grep -E '^(User|WorkingDirectory|ExecStart)=' /etc/systemd/system/acepoint.service"),
    ("上次开机的 acepoint 日志", "journalctl -b -1 -u acepoint --no-pager -n 40"),
    ("USB 设备", "lsusb"),
    ("摄像头物理路径", "ls -l /dev/v4l/by-path/"),
]

LATE_COMMANDS = [
    ("acepoint 服务状态", "systemctl status acepoint --no-pager -n 0"),
    ("本次开机 acepoint 日志", "journalctl -b -u acepoint --no-pager -n 80"),
]


class Log:
    """每行立刻 fsync —— 这台设备是直接拔电关机的，缓冲里的日志会丢。"""

    def __init__(self, path: str, mode: str = "a") -> None:
        self.path = path
        self._f = open(path, mode, encoding="utf-8")

    def __call__(self, text: str = "") -> None:
        stamp = f"[+{time.monotonic():7.1f}s] "
        for line in (text.splitlines() or [""]):
            self._f.write(stamp + line + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()
        os.sync()


def run_cmd(log: Log, title: str, command: str) -> None:
    log(f"--- {title}")
    log(f"$ {command}")
    try:
        out = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=20)
        log((out.stdout + out.stderr).rstrip() or "(无输出)")
    except Exception as exc:                                    # noqa: BLE001
        log(f"执行失败: {exc!r}")


def wait_for(path: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return os.path.exists(path)


def led_test(log: Log, path: str, label: str) -> None:
    log(f"=== 点阵屏自检 {path} ({label})")
    if not wait_for(path, 2):
        log(f"{path} 不存在，跳过")
        return
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        log(f"打开失败: {exc!r}")
        return
    try:
        fcntl.ioctl(fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPEED_HZ))

        def write_all(register: int, value: int) -> None:
            # 级联时一帧里每块各 16 bit，CS 拉高时一起锁存；一次 write() 就是一帧
            os.write(fd, bytes([register, value] * CASCADED))

        log(f"开始：全亮 {STEP_SECONDS} 秒（display-test，不依赖其他寄存器）")
        write_all(REG_DISPLAY_TEST, 1)
        time.sleep(STEP_SECONDS)
        write_all(REG_DISPLAY_TEST, 0)

        log(f"开始：棋盘格 {STEP_SECONDS} 秒（正常显示模式）")
        write_all(REG_DECODE, 0)
        write_all(REG_SCAN_LIMIT, 7)
        write_all(REG_INTENSITY, 4)
        write_all(REG_SHUTDOWN, 1)
        for row in range(8):
            write_all(REG_DIGIT0 + row, 0x55 if row % 2 else 0xAA)
        time.sleep(STEP_SECONDS)

        for row in range(8):
            write_all(REG_DIGIT0 + row, 0)
        write_all(REG_SHUTDOWN, 0)
        log("结束：SPI 写入全部成功（写入成功不代表屏亮了，亮没亮只能目视）")
    except Exception as exc:                                    # noqa: BLE001
        log(f"自检出错: {exc!r}")
    finally:
        os.close(fd)


def next_log_path() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(LOG_DIR, "boot-*.txt")))
    numbers = [int(os.path.basename(p)[5:9]) for p in existing
               if os.path.basename(p)[5:9].isdigit()]
    for old in existing[:max(0, len(existing) - KEEP_LOGS + 1)]:
        try:
            os.remove(old)
        except OSError:
            pass
    return os.path.join(LOG_DIR, f"boot-{(max(numbers) + 1) if numbers else 1:04d}.txt")


def main_boot() -> int:
    log = Log(next_log_path(), "w")
    try:
        log("AcePoint 开机诊断")
        log(f"系统时间 {time.strftime('%Y-%m-%d %H:%M:%S')}（树莓派无 RTC，没联网时这个时间不准）")
        if not wait_for("/dev/spidev0.0", 30):
            log("等了 30 秒 /dev/spidev0.0 仍未出现 —— SPI 未启用或驱动没加载")
        led_test(log, "/dev/spidev0.0", "CS 接 CE0 = 物理 24 脚")
        time.sleep(2)
        led_test(log, "/dev/spidev0.1", "CS 接 CE1 = 物理 26 脚")
        for title, command in INFO_COMMANDS:
            run_cmd(log, title, command)
        try:
            subprocess.run(["systemd-run", f"--on-active={LATE_DELAY}",
                            f"--unit=acepoint-diag-late-{os.getpid()}",
                            sys.executable, os.path.abspath(__file__), "late", log.path],
                           capture_output=True, timeout=10)
            log(f"已安排 {LATE_DELAY} 秒后追加 acepoint 服务日志")
        except Exception as exc:                                # noqa: BLE001
            log(f"安排追加日志失败: {exc!r}")
        log("诊断完成")
    finally:
        log.close()
    return 0


def main_late(path: str) -> int:
    log = Log(path, "a")
    try:
        log("")
        log(f"===== 开机 {LATE_DELAY} 秒后追加 =====")
        for title, command in LATE_COMMANDS:
            run_cmd(log, title, command)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "late":
        raise SystemExit(main_late(sys.argv[2]))
    raise SystemExit(main_boot())
