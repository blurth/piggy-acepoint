#!/usr/bin/env python3
"""检查树莓派自己的 SPI 引脚是否完好。只用标准库。

先断电、拔掉屏的 5 根线，用一根杜邦线短接 物理 19 脚(MOSI) 和 21 脚(MISO)，
再通电运行：
    sudo python3 /boot/firmware/acepoint-diag/spi_loopback.py

警告：绝不要把屏的 DOUT 接到 21 脚 —— 那是 5V，会伤树莓派。
"""

import ctypes
import fcntl
import os
import struct
import sys

SPI_IOC_MESSAGE_1 = 0x40206B00          # _IOW('k', 0, struct spi_ioc_transfer[1])，32 字节
PATTERN = bytes([0xAA, 0x55, 0x0F, 0xF0])

try:
    fd = os.open("/dev/spidev0.0", os.O_RDWR)
except OSError as exc:
    sys.exit(f"打不开 /dev/spidev0.0: {exc}（记得加 sudo）")
try:
    tx = ctypes.create_string_buffer(PATTERN, len(PATTERN))
    rx = ctypes.create_string_buffer(len(PATTERN))
    # struct spi_ioc_transfer: tx_buf, rx_buf, len, speed_hz, delay_usecs,
    # bits_per_word, cs_change, tx_nbits, rx_nbits, word_delay_usecs, pad
    xfer = struct.pack("QQIIHBBBBBB", ctypes.addressof(tx), ctypes.addressof(rx),
                       len(PATTERN), 500_000, 0, 8, 0, 0, 0, 0, 0)
    fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, xfer)
finally:
    os.close(fd)

sent, got = list(PATTERN), list(rx.raw)
print(f"发送 {sent}")
print(f"收到 {got}")
if got == sent:
    print("结论：树莓派 SPI 引脚正常，问题在屏或连线")
elif not any(got):
    print("结论：全是 0，没收回来 —— 先确认短接的是 19 和 21 脚；确认无误才怀疑树莓派引脚")
else:
    print("结论：收回来的不一致 —— 短接线接触不良，按紧再试")
