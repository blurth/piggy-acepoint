"""MAX7219 点阵屏（仅树莓派，需要 raspi-config 里开 SPI）。

硬件是 4 块 8x8 级联，**左右各两块，每边两位数字**：

    +----+----+  +----+----+
    | 2  | 1  |  | 1  | 9  |      左半场 21，右半场 19
    +----+----+  +----+----+
     <-- 16px -->  <-- 16px -->

所以不能把 "21:19" 当一串文字去画 —— 没有冒号的位置，两边各自右对齐两位。
字体用 CP437_FONT（8x8 定宽），不能套 proportional()，否则字宽会缩、对不上格子。

渲染放在独立线程：闪烁要 sleep，绝不能卡住识别主循环。
"""

from __future__ import annotations

import logging
import threading
import time

from acepoint.config import LedConfig
from acepoint.displays.base import Display
from acepoint.state import Snapshot, Telemetry

log = logging.getLogger(__name__)

_FLASH_TIMES = 2          # 计分生效闪烁 2 次（需求 §5.3）
_FLASH_INTERVAL = 0.15
_WIN_BLINK = 0.6          # 分出胜负后，获胜方的两位数字持续闪烁（塞不下 "WIN" 文字）
_TICK = 0.25              # 渲染线程的心跳，决定闪烁节奏的精度


class LedDisplay(Display):
    wants_preview = False

    def __init__(self, config: LedConfig) -> None:
        self.config = config
        self._latest: Snapshot | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device = None
        self._drawn_flash = -1

    # ------------------------------------------------------------ 生命周期

    def open_device(self):
        """打开 SPI 并初始化点阵屏。失败时保证不漏 SPI 句柄。

        单独抽出来是因为 `acepoint --check` / `--led-test` 要在不起渲染线程的
        情况下真正碰一次硬件 —— 只 import luma 是检不出 SPI 没启用、排线没插、
        杜邦线接错这三种最常见的故障的。
        """
        from luma.core.interface.serial import noop, spi
        from luma.led_matrix.device import max7219

        serial = spi(port=self.config.spi_port, device=self.config.spi_device,
                     bus_speed_hz=self.config.bus_speed_hz, gpio=noop())
        try:
            device = max7219(
                serial,
                cascaded=self.config.cascaded,
                block_orientation=self.config.block_orientation,
                rotate=self.config.rotate,
            )
            device.contrast(self.config.brightness)
        except Exception:
            # max7219 构造失败时 serial 已经占着 SPI 了，不放手的话下次打不开
            try:
                serial.cleanup()
            except Exception:                 # noqa: BLE001
                pass
            raise
        return device

    def start(self) -> None:
        self._device = self.open_device()
        # 同步点亮，确保初始化结束前四个数字已经显示。
        self._draw(Snapshot())
        self._thread = threading.Thread(target=self._run, name="led", daemon=True)
        self._thread.start()
        log.info("LED matrix ready (%d blocks, %d digits per side, brightness %d)",
                 self.config.cascaded, self.config.digits_per_side, self.config.brightness)

    def update(self, telemetry: Telemetry) -> None:
        with self._lock:
            self._latest = telemetry.snapshot
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._device is not None:
            try:
                self._device.cleanup()
            except Exception:                 # noqa: BLE001
                pass

    # ---------------------------------------------------------------- 自检

    def self_test(self, dwell: float = 2.0) -> None:
        """点屏测试图案，用来标定 block_orientation / rotate / reverse_blocks。

        不起渲染线程，跑完自己收拾干净。顺序：
          1. 全亮      —— 有哪一块不亮就是那块坏了或没供上电
          2. 88 88     —— 所有段都点一遍，缺笔画一眼看得出
          3. 逐块点亮  —— 看点亮顺序，判断级联方向和左右是否装反
          4. 12 34     —— 数字朝向对不对、左右半场分得对不对
        """
        from luma.core.render import canvas

        self._device = self.open_device()
        width = self.config.cascaded * 8
        try:
            print(f"点阵屏 {width}x8，每边 {self.config.digits_per_side} 位数字")

            print("  [1/4] 全亮 —— 检查有没有整块不亮的")
            with canvas(self._device) as draw:
                draw.rectangle((0, 0, width - 1, 7), fill="white")
            time.sleep(dwell)

            digits = self.config.digits_per_side
            print("  [2/4] 全 8 —— 检查笔画有没有缺")
            self._draw(Snapshot(left=int("8" * digits), right=int("8" * digits)))
            time.sleep(dwell)

            print("  [3/4] 逐块点亮 —— 记下点亮顺序，判断级联方向")
            for block in range(self.config.cascaded):
                print(f"        第 {block + 1} 块（从接线端数）")
                with canvas(self._device) as draw:
                    draw.rectangle((block * 8, 0, block * 8 + 7, 7), fill="white")
                time.sleep(dwell)

            print("  [4/4] 左 12 / 右 34 —— 数字朝向和左右分区")
            self._draw(Snapshot(left=12, right=34))
            time.sleep(dwell * 2)

            print("\n对照结果调整 config.pi.toml [display.led]：")
            print("  数字倒置/侧躺  -> block_orientation（0 / 90 / -90 / 180）或 rotate")
            print("  左右两半反了   -> reverse_blocks = true")
            print("  显示花屏乱码   -> 调低 bus_speed_hz（先试 1000000）")
        finally:
            try:
                self._device.cleanup()
            except Exception:                 # noqa: BLE001
                pass
            self._device = None

    # -------------------------------------------------------------- 渲染线程

    def _run(self) -> None:
        drawn: tuple | None = None
        blink_at = 0.0
        winner_lit = True

        while not self._stop.is_set():
            self._wake.wait(timeout=_TICK)
            self._wake.clear()
            with self._lock:
                snapshot = self._latest
            if snapshot is None:
                continue

            # 获胜方的数字要一直闪，所以不能只靠 version 变化决定重画
            if snapshot.winner:
                now = time.monotonic()
                if now >= blink_at:
                    winner_lit = not winner_lit
                    blink_at = now + _WIN_BLINK
                dark = None if winner_lit else snapshot.winner
            else:
                winner_lit, blink_at, dark = True, 0.0, None

            state = (snapshot.version, snapshot.left, snapshot.right,
                     snapshot.match_points, dark)
            if drawn is not None and snapshot.flash_seq != self._drawn_flash:
                try:
                    self._flash(snapshot)
                except Exception:
                    log.exception("LED render failed; retrying on next update")
                    drawn = None
                    continue
            elif state != drawn:
                try:
                    self._draw(snapshot, dark=dark)
                except Exception:
                    log.exception("LED render failed; retrying on next update")
                    drawn = None
                    continue
            drawn, self._drawn_flash = state, snapshot.flash_seq

    def _flash(self, snapshot: Snapshot) -> None:
        for _ in range(_FLASH_TIMES):
            self._draw(snapshot, dark="both")
            time.sleep(_FLASH_INTERVAL)
            self._draw(snapshot)
            time.sleep(_FLASH_INTERVAL)

    def _draw(self, snapshot: Snapshot, *, dark: str | None = None) -> None:
        """把左右比分画到各自的一半上。dark 指定哪一侧熄灭（闪烁用）。"""
        from luma.core.legacy import text
        from luma.core.legacy.font import CP437_FONT
        from luma.core.render import canvas

        half = self.config.half_width
        digits = self.config.digits_per_side
        left_x, right_x = (half, 0) if self.config.reverse_blocks else (0, half)

        with canvas(self._device) as draw:
            for side, x, value in (("left", left_x, snapshot.left),
                                   ("right", right_x, snapshot.right)):
                if dark in (side, "both"):
                    continue
                # 定宽字体 + 右对齐：个位永远在同一格，看着不跳
                text(draw, (x, 0), f"{value:0{digits}d}"[-digits:],
                     fill="white", font=CP437_FONT)
                if self.config.show_match_point and side in snapshot.match_points:
                    # 塞不下文字，用底部一条下划线标赛点；静态不闪 —— 赛点时球还在打
                    draw.line((x, 7, x + half - 1, 7), fill="white")
