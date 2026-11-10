"""绕过 luma，直接用 spidev 驱动 MAX7219 —— 用来区分"硬件不亮"和"软件没画"。

MAX7219 上电默认处于 shutdown，全黑是正常的，不代表坏了。这里第一步写的是
display-test 寄存器(0x0F)：它强制点亮所有 LED，不依赖 shutdown/扫描/亮度/译码
任何其他寄存器。所以：

  * 第 1 步全亮都没有  -> 硬件问题：供电、接线(DIN 端 / CS 脚)、电平、模块本身
  * 第 1 步亮、第 2 步不亮 -> 普通显示寄存器没写进去，多半是信号质量，降 --speed
  * 两步都亮、acepoint 不亮 -> 软件/配置问题(spi_device、display.backends、服务没跑)

用法(树莓派上，先停掉服务，避免两个进程抢 SPI)：
    sudo systemctl stop acepoint
    python scripts/led_raw_test.py
    python scripts/led_raw_test.py --device 1          # CS 接到了 CE1(物理 26 脚)时
    python scripts/led_raw_test.py --speed 100000      # 怀疑信号质量时
"""

from __future__ import annotations

import argparse
import time

REG_DIGIT0 = 0x01
REG_DECODE = 0x09
REG_INTENSITY = 0x0A
REG_SCAN_LIMIT = 0x0B
REG_SHUTDOWN = 0x0C
REG_DISPLAY_TEST = 0x0F


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bus", type=int, default=0, help="SPI 总线，默认 0")
    parser.add_argument("--device", type=int, default=0,
                        help="片选：0 = CE0(物理 24 脚，默认)，1 = CE1(物理 26 脚)")
    parser.add_argument("--cascaded", type=int, default=4, help="级联块数，默认 4")
    parser.add_argument("--speed", type=int, default=1_000_000, help="SPI 时钟 Hz，默认 1MHz")
    parser.add_argument("--seconds", type=float, default=5.0, help="每步保持秒数")
    args = parser.parse_args()

    try:
        import spidev
    except ImportError:
        print("缺 spidev：pip install spidev")
        return 1

    spi = spidev.SpiDev()
    try:
        spi.open(args.bus, args.device)
    except FileNotFoundError:
        print(f"/dev/spidev{args.bus}.{args.device} 不存在 —— SPI 没开："
              "sudo raspi-config -> Interface Options -> SPI -> Enable，然后重启")
        return 1
    spi.max_speed_hz = args.speed
    spi.mode = 0

    def write_all(register: int, value: int) -> None:
        # 级联时一帧里每块各发 16 bit（地址 + 数据），CS 拉高时一起锁存
        spi.xfer2([register, value] * args.cascaded)

    try:
        print(f"spidev{args.bus}.{args.device} @ {args.speed} Hz, {args.cascaded} 块")

        print(f"[1/2] display-test 全亮 {args.seconds:.0f} 秒 —— 应该每块 64 个灯全亮")
        write_all(REG_DISPLAY_TEST, 1)
        time.sleep(args.seconds)
        write_all(REG_DISPLAY_TEST, 0)

        print(f"[2/2] 正常模式棋盘格 {args.seconds:.0f} 秒")
        write_all(REG_DECODE, 0)
        write_all(REG_SCAN_LIMIT, 7)
        write_all(REG_INTENSITY, 4)
        write_all(REG_SHUTDOWN, 1)
        for row in range(8):
            write_all(REG_DIGIT0 + row, 0x55 if row % 2 else 0xAA)
        time.sleep(args.seconds)

        for row in range(8):
            write_all(REG_DIGIT0 + row, 0)
        write_all(REG_SHUTDOWN, 0)
        print("完成。注意：SPI 写入不会因为没接屏而报错，亮没亮只能看。")
    finally:
        spi.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
