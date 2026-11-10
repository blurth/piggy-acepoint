#!/usr/bin/env python3
"""让 MAX7219 一直保持全亮，边看边查接线。只用标准库。

    sudo python3 /boot/firmware/acepoint-diag/led_hold.py        # CS 接 24 脚
    sudo python3 /boot/firmware/acepoint-diag/led_hold.py 1      # CS 接 26 脚

每秒重发一次 display-test（全亮）指令：运行中重插、按紧杜邦线，哪根接对了
1 秒内就会亮。Ctrl+C 停止并熄屏。
"""

import os
import struct
import sys
import time
import fcntl

SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
CASCADED = 4

device = sys.argv[1] if len(sys.argv) > 1 else "0"
path = f"/dev/spidev0.{device}"
pin = {"0": "24", "1": "26"}.get(device, "?")

try:
    fd = os.open(path, os.O_RDWR)
except OSError as exc:
    sys.exit(f"打不开 {path}: {exc}（记得加 sudo）")
fcntl.ioctl(fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", 500_000))

print(f"{path}（CS = 物理 {pin} 脚）全亮测试中，每秒重发一次，Ctrl+C 停止")
n = 0
try:
    while True:
        os.write(fd, bytes([0x0F, 1] * CASCADED))
        n += 1
        print(f"\r已发送 {n} 次", end="", flush=True)
        time.sleep(1)
except KeyboardInterrupt:
    os.write(fd, bytes([0x0F, 0] * CASCADED))
    print("\n已停止并熄屏")
finally:
    os.close(fd)
