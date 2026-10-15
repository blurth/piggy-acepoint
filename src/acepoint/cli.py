"""命令行入口。

    acepoint                                  用 config.toml 跑（默认：录制视频 + 网页记分牌）
    acepoint -c config.pi.toml                树莓派实机：双摄像头 + LED 点阵
    acepoint --video left=videos/A.mp4 --video right=videos/B.mp4
    acepoint --display console --duration 20  无界面冒烟测试
    acepoint --check                          只检查环境（模型/视频源/依赖），不跑主循环
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from acepoint import __version__
from acepoint.config import Config, ConfigError, apply_overrides, load_config
from acepoint.state import Snapshot

DEFAULT_CONFIG = "config.toml"


def _parse_video(values: list[str] | None) -> dict[str, str]:
    videos: dict[str, str] = {}
    for item in values or []:
        side, sep, path = item.partition("=")
        if not sep or not path:
            raise ConfigError(f"--video 格式应为 side=path，例如 left=videos/A.mp4（当前 {item!r}）")
        videos[side.strip()] = path.strip()
    return videos


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acepoint", description="羽毛球手势计分系统")
    parser.add_argument("-c", "--config", default=None,
                        help=f"配置文件路径（默认 ./{DEFAULT_CONFIG}，不存在则用内置默认值）")
    parser.add_argument("--display", default=None,
                        help="显示后端，逗号分隔：web,console,led")
    parser.add_argument("--port", type=int, default=None, help="网页记分牌端口")
    parser.add_argument("--video", action="append", metavar="SIDE=PATH",
                        help="用录制视频替代摄像头，可重复：--video left=videos/A.mp4")
    parser.add_argument("--no-preview", action="store_true", help="网页不显示画面预览")
    parser.add_argument("--duration", type=float, default=None,
                        help="跑满这么多秒后自动退出（冒烟测试用）")
    parser.add_argument("--check", action="store_true", help="只做环境自检（真读帧、真开 SPI）")
    parser.add_argument("--audio-test", action="store_true", help="离线语音试听，不加载摄像头或模型")
    parser.add_argument("--audio-check", action="store_true", help="检查音频包和输出设备，不发声")
    parser.add_argument("--led-test", action="store_true",
                        help="点阵屏测试图案，用来标定 block_orientation / reverse_blocks")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 DEBUG 日志")
    parser.add_argument("--version", action="version", version=f"acepoint {__version__}")
    return parser


def resolve_config(args: argparse.Namespace) -> Config:
    path = args.config
    if path is None and Path(DEFAULT_CONFIG).exists():
        path = DEFAULT_CONFIG
    config = load_config(path)
    return apply_overrides(
        config,
        backends=[b.strip() for b in args.display.split(",") if b.strip()] if args.display else None,
        port=args.port,
        videos=_parse_video(args.video),
        no_preview=args.no_preview,
    )


def _check_source(spec, source, config: Config, frames: int) -> bool:
    """真读若干帧再下结论。

    只判 isOpened() 是不够的：V4L2 的 open() 只做 QUERYCAP/S_FMT/REQBUFS，
    真正的 STREAMON 发生在**第一次 read**。双 USB 摄像头最经典的故障
    —— 第二个相机等时带宽预留失败（ENOSPC）—— 只在那时才暴露。
    """
    label = f"视频源 {spec.side}"
    interval = len(config.sources) / config.capture.poll_hz
    started, ok_frames, shape = time.monotonic(), 0, None
    for _ in range(frames):
        frame = source.read()
        if frame is not None:
            ok_frames += 1
            shape = frame.image.shape
        time.sleep(interval)
    elapsed = time.monotonic() - started

    if ok_frames == 0:
        print(f"[FAIL] {label}: 打得开但读不到画面（{source.description}）")
        if spec.kind == "camera":
            print("       双摄常见原因：USB 带宽不足。把两个摄像头分插 USB2 和 USB3 口，"
                  "或降低 capture.width/height")
        return False

    height, width = shape[:2]
    detail = f"{ok_frames}/{frames} 帧，{width}x{height}"
    healthy = True

    if spec.kind == "camera":
        want = (config.capture.height, config.capture.width)
        if shape[:2] != want:
            print(f"[WARN] {label}: 实际分辨率 {width}x{height} != 配置的 "
                  f"{config.capture.width}x{config.capture.height}（摄像头不支持，已回退）")
            healthy = False
        fourcc = getattr(source, "fourcc", "")
        if fourcc != "MJPG":
            print(f"[FAIL] {label}: 编码是 {fourcc or '未知'} 而不是 MJPG "
                  f"—— 1080p 会把 USB 带宽吃满，帧率掉到个位数")
            return False
        detail += f" MJPG {ok_frames / elapsed:.1f}fps"

    print(f"[{'ok' if healthy else 'WARN'}]   {label}: {detail}")
    return healthy


def check(config: Config, *, frames: int = 10) -> int:
    """自检：模型、视频源（真读帧）、LED（真开 SPI）。"""
    from acepoint.sources import build_sources

    ok = True
    model = config.resolve(config.recognition.model_path)
    if model.exists():
        print(f"[ok]   模型  {model} ({model.stat().st_size / 1e6:.1f} MB)")
    else:
        print(f"[FAIL] 模型  {model} 不存在 —— 运行 python scripts/fetch_model.py")
        ok = False

    sources = build_sources(config)
    try:
        for spec in config.sources:
            source = sources.get(spec.side)
            if source is None:
                print(f"[FAIL] 视频源 {spec.side}: 打不开（kind={spec.kind}）")
                ok = False
            elif not _check_source(spec, source, config, frames):
                ok = False
    finally:
        for source in sources.values():
            source.close()
    if len(sources) < len(config.sources):
        print(f"       注意：配置了 {len(config.sources)} 路，只起来 {len(sources)} 路。"
              f"启动时少一路会改变每路采样率，防误触参数会失准")
    if not sources:
        ok = False

    if "led" in config.display.backends:
        ok = _check_led(config) and ok

    if config.audio.enabled:
        ok = (check_audio(config) == 0) and ok

    print("\n自检" + ("通过" if ok else "未通过"))
    return 0 if ok else 1


def _check_led(config: Config) -> bool:
    """真开一次 SPI 并点屏 —— 只 import luma 检不出 SPI 没启用/排线没插/接错线。"""
    try:
        from acepoint.displays.led import LedDisplay
        import luma.led_matrix  # noqa: F401
    except ImportError as exc:
        print(f"[FAIL] LED   依赖缺失（{exc}）—— pip install -e \".[pi]\"")
        return False

    display = LedDisplay(config.display.led)
    try:
        device = display.open_device()
    except Exception as exc:                    # noqa: BLE001
        print(f"[FAIL] LED   打不开（{type(exc).__name__}: {exc}）")
        print("       依次检查：raspi-config 里 SPI 是否已启用、5 根杜邦线是否按 §4 接、"
              "排线有没有插紧")
        return False

    try:
        display._device = device
        display._draw(Snapshot())               # 画一次 0 0，确认真的能点亮
        led = config.display.led
        print(f"[ok]   LED   SPI 写入成功，{led.cascaded} 块 8x8，每边 {led.digits_per_side} 位，"
              f"SPI {led.bus_speed_hz / 1e6:.1f}MHz —— 请目视确认屏上显示 00 00")
        print("       此检查无法读取模块反馈，写入成功不代表接线或供电正常")
        time.sleep(2.0)
        print("       字符方向或左右不对就跑 acepoint --led-test -c config.pi.toml")
        return True
    finally:
        display._device = None
        try:
            device.cleanup()
        except Exception:                       # noqa: BLE001
            pass


def check_audio(config, *, preview=False):
    """Independent pack/device check; preview is explicit and camera-free."""
    from acepoint.audio.player import Pack, NullPlayer, PygamePlayer
    import random
    player = None
    try:
        pack = Pack(config.resolve(config.audio.pack_root))
        player = (NullPlayer if config.audio.backend == "null" else PygamePlayer)(config.audio)
        player.prepare(pack.clips)
        print(f"[ok] audio: {len(pack.clips)} clips, backend={config.audio.backend}")
        if preview:
            if config.audio.muted or config.audio.volume == 0:
                print("Audio muted; change audio.muted/volume before preview")
                return 0
            for event in ("streak_3", "shutdown", "decider", "win_normal"):
                style = "effects" if config.audio.mode == "effects_only" else config.audio.style
                clip = pack.choose(event, style, random.Random(), None)
                print(clip["text"] or event)
                player.play(clip, config.audio.volume)
                time.sleep(clip["duration"] + 0.2)
        return 0
    except Exception as exc:
        print(f"[FAIL] audio: {exc}; install with pip install -e \".[audio]\"")
        return 1
    finally:
        if player is not None:
            player.close()


def main(argv: list[str] | None = None) -> int:
    # Redirected Windows consoles may use an encoding without Chinese glyphs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = resolve_config(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.audio_test or args.audio_check:
        return check_audio(config, preview=args.audio_test)

    if args.led_test:
        try:
            from acepoint.displays.led import LedDisplay
        except ImportError as exc:
            print(f"LED 依赖缺失（{exc}）—— pip install -e \".[pi]\"", file=sys.stderr)
            return 1
        try:
            LedDisplay(config.display.led).self_test()
        except Exception as exc:                # noqa: BLE001
            print(f"点屏失败（{type(exc).__name__}: {exc}）", file=sys.stderr)
            return 1
        return 0

    if args.check:
        return check(config)

    from acepoint.engine import NoSourceError, ScoringEngine
    from acepoint.recognition import ModelMissingError

    try:
        engine = ScoringEngine(config)
    except (NoSourceError, ModelMissingError) as exc:
        print(exc, file=sys.stderr)
        return 1

    engine.run(max_seconds=args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
