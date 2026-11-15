"""MAX7219 点阵屏（仅树莓派，需要 raspi-config 里开 SPI）。

硬件是 4 块 8x8 级联，**左右各两块，每边两位数字**：

    +----+----+  +----+----+
    | 2  | 1  |  | 1  | 9  |      左半场 21，右半场 19
    +----+----+  +----+----+
     <-- 16px -->  <-- 16px -->

所以不能把 "21:19" 当一串文字去画 —— 没有冒号的位置，两边各自右对齐两位。
字体用 CP437_FONT（8x8 定宽），不能套 proportional()，否则字宽会缩、对不上格子。

数字只用到第 0~6 行，最底下一行（第 7 行）是状态条，屏是单色的，靠图案和方向区分：

    实线，从本方外侧边缘向对面变长   正在识别手势，涨满即计分
    虚线，向本方外侧边缘缩短         计分后的冷却，缩完才能再比手势
    本方半边的整条实线（静态）       赛点

只有一方有状态时状态条占满整个底边（32px）；两方同时有就各用自己那半边。

渲染放在独立线程：闪烁要 sleep，绝不能卡住识别主循环。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from acepoint.config import LedConfig
from acepoint.displays.base import Display
from acepoint.state import Snapshot, Telemetry

log = logging.getLogger(__name__)

_FLASH_TIMES = 2          # 计分生效闪烁 2 次（需求 §5.3）
_FLASH_INTERVAL = 0.15
_WIN_BLINK = 0.6          # 分出胜负后，获胜方的两位数字持续闪烁（塞不下 "WIN" 文字）
_TICK = 0.25              # 渲染线程的心跳，决定闪烁节奏的精度
_ANIMATION_TICK = 0.05    # 状态条在动的时候改用这个心跳：遥测一秒只来四五次，自己补帧才顺
_HOLD_MIN_FRACTION = 0.1  # 手势刚被认出、进度还是 0 时进度条的最短长度（占可用宽度）
_MAX_EXTRAPOLATION = 1.0  # 遥测断了的话最多按上一次的速度往前推这么久，免得条自己跑飞

BarPixels = tuple[tuple[str, str, int], ...]     # (半场, "hold" | "cooldown", 点亮的像素数)


@dataclass(frozen=True)
class _Bar:
    """某一方的状态条在一次遥测到达时的样子；渲染线程按 rate 往后推算。"""

    side: str
    kind: str              # "hold" 正在识别 | "cooldown" 冷却中
    fraction: float        # 遥测到达那一刻的比例，0~1
    rate: float            # 每秒变化多少：识别为正（变长），冷却为负（缩短）
    at: float              # 遥测到达的时刻（monotonic）


class LedDisplay(Display):
    wants_preview = False

    def __init__(self, config: LedConfig) -> None:
        self.config = config
        self._latest: Snapshot | None = None
        self._bars: tuple[_Bar, ...] = ()
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
        bars = self._bars_from(telemetry, time.monotonic())
        with self._lock:
            self._latest = telemetry.snapshot
            self._bars = bars
        self._wake.set()

    def _bars_from(self, telemetry: Telemetry, now: float) -> tuple[_Bar, ...]:
        """从遥测里取出各方的状态条：正在识别优先，其次是冷却。

        冷却期间状态机不累计手势（gesture 恒为 None），所以两者不会同时出现在同一方。
        """
        bars = []
        for side, status in sorted(telemetry.sides.items()):
            if self.config.show_hold_progress and status.gesture and status.required > 0:
                bars.append(_Bar(side, "hold", min(1.0, max(0.0, status.progress)),
                                 1.0 / status.required, now))
            elif (self.config.show_cooldown and status.cooldown_remaining > 0
                  and status.cooldown_total > 0):
                remaining = min(1.0, status.cooldown_remaining / status.cooldown_total)
                bars.append(_Bar(side, "cooldown", remaining,
                                 -1.0 / status.cooldown_total, now))
        return tuple(bars)

    def _bar_pixels(self, bars: tuple[_Bar, ...], now: float) -> BarPixels:
        """此刻各状态条该亮几个像素。

        遥测一秒只来四五次，直接照着画条是一跳一跳的；这里按上一次遥测的速度
        往后推到 now，渲染线程每 `_ANIMATION_TICK` 调一次，条就是连续动的。
        只有一方有状态时用整个底边，两方都有就各用自己那半边。
        进度为 0（手势刚被认出）时识别条也至少亮一小段 —— 它首先是"看见你了"的反馈。
        """
        width = self.config.half_width * (2 if len(bars) == 1 else 1)
        pixels = []
        for bar in bars:
            age = min(max(0.0, now - bar.at), _MAX_EXTRAPOLATION)
            fraction = min(1.0, max(0.0, bar.fraction + bar.rate * age))
            lit = round(fraction * width)
            if bar.kind == "hold":
                lit = max(lit, round(width * _HOLD_MIN_FRACTION))
            if lit > 0:
                pixels.append((bar.side, bar.kind, lit))
        return tuple(pixels)

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
        animating = False

        while not self._stop.is_set():
            self._wake.wait(timeout=_ANIMATION_TICK if animating else _TICK)
            self._wake.clear()
            with self._lock:
                snapshot = self._latest
                bars = self._bars
            if snapshot is None:
                continue
            # 像素数没变就不会重画，所以补帧只在条真的动了一格时才写 SPI
            pixels = self._bar_pixels(bars, time.monotonic())
            animating = bool(pixels)

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
                     snapshot.match_points, dark, pixels)
            if drawn is not None and snapshot.flash_seq != self._drawn_flash:
                try:
                    self._flash(snapshot)
                except Exception:
                    log.exception("LED render failed; retrying on next update")
                    drawn = None
                    continue
                # 闪烁的最后一帧不带状态条：记成"没画条"，下一圈就会把冷却条补上
                state = state[:-1] + ((),)
            elif state != drawn:
                try:
                    self._draw(snapshot, dark=dark, bars=pixels)
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

    def _draw(self, snapshot: Snapshot, *, dark: str | None = None,
              bars: BarPixels = ()) -> None:
        """把左右比分画到各自的一半上，再画最底下一行的状态条。

        dark  指定哪一侧的数字熄灭（闪烁用）。
        bars  `_bar_pixels` 的结果：哪一方、哪种状态条、亮几个像素。
        """
        from luma.core.legacy import text
        from luma.core.legacy.font import CP437_FONT
        from luma.core.render import canvas

        half = self.config.half_width
        digits = self.config.digits_per_side
        left_x, right_x = (half, 0) if self.config.reverse_blocks else (0, half)
        origin = {"left": left_x, "right": right_x}
        last = half * 2 - 1

        with canvas(self._device) as draw:
            for side, x, value in (("left", left_x, snapshot.left),
                                   ("right", right_x, snapshot.right)):
                if dark in (side, "both"):
                    continue
                # 定宽字体 + 右对齐：个位永远在同一格，看着不跳
                text(draw, (x, 0), f"{value:0{digits}d}"[-digits:],
                     fill="white", font=CP437_FONT)
                # 状态条和赛点下划线共用最底下一行。有状态条时整行都让给它：
                # 赛点的整条实线会和识别条混在一起，看不出条在涨。
                if (not bars and self.config.show_match_point
                        and side in snapshot.match_points):
                    # 塞不下文字，用底部一条下划线标赛点；静态不闪 —— 赛点时球还在打
                    draw.line((x, 7, x + half - 1, 7), fill="white")

            for side, kind, lit in bars:
                # 条固定在本方的外侧边缘：画在屏左半的那一方从最左往右长，另一方从最右往左长
                from_left = origin[side] == 0
                if kind == "hold":
                    span = (0, 7, lit - 1, 7) if from_left else (last - lit + 1, 7, last, 7)
                    draw.line(span, fill="white")
                else:
                    # 冷却用虚线（隔一个点亮一个），单色屏上和识别的实线一眼能分开。
                    # 从固定在边缘的那个点起隔点亮，条缩短时剩下的点不会跟着跳。
                    step = 1 if from_left else -1
                    start = 0 if from_left else last
                    draw.point([(start + step * offset, 7) for offset in range(0, lit, 2)],
                               fill="white")
