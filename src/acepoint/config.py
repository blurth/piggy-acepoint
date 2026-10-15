"""配置加载：TOML 文件 -> dataclass。

需求文档 §7 的可调参数全部落在这里；改参数只改 config*.toml，不改代码。
"""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from acepoint.audio.config import AudioConfig

SIDES = ("left", "right")


class ConfigError(ValueError):
    """配置文件不合法。"""


@dataclass(frozen=True)
class RecognitionConfig:
    model_path: str = "models/gesture_recognizer.task"
    min_confidence: float = 0.6            # 需求 §5.2 置信度阈值
    min_hand_detection_confidence: float = 0.5
    num_hands: int = 2
    # True = 每路一个 GestureRecognizer 实例。**默认值必须是 True**：
    # 需求 §6 原本写的是共用一个省内存，实测双路交替喂帧时 VIDEO 模式的手部
    # 跟踪状态会互相污染（Thumb_Down 检出率掉 77%），§6 v1.1 已修正。
    # 默认值只在找不到配置文件时生效，而那正是最容易被忽略的部署路径。
    per_source_recognizer: bool = True


@dataclass(frozen=True)
class CaptureConfig:
    poll_hz: float = 10.0                  # 总轮询频率,每路分到 poll_hz / 路数
    width: int = 1920
    height: int = 1080
    # USB 摄像头画质控制；None 表示保留驱动默认值。不同型号支持的范围不同。
    brightness: float | None = None
    sharpness: float | None = None
    crop_w_ratio: float = 0.6              # 识别区:占画面宽度的比例
    crop_h_ratio: float = 0.8              # 识别区:占画面高度的比例
    # 识别区中心在画面里的位置（0=左/上,0.5=正中,1=右/下）。默认居中。
    # 需要挪的原因见 engine.roi_rect：球员举手的高度不等于画面中线。
    crop_cx_ratio: float = 0.5
    crop_cy_ratio: float = 0.5

    def __post_init__(self) -> None:
        if self.poll_hz <= 0:
            raise ConfigError("capture.poll_hz 必须 > 0")
        for name in ("crop_w_ratio", "crop_h_ratio"):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ConfigError(f"capture.{name} 必须在 (0, 1] 区间, 当前 {value}")
        for name in ("crop_cx_ratio", "crop_cy_ratio"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ConfigError(f"capture.{name} 必须在 [0, 1] 区间, 当前 {value}")

    @property
    def crop(self) -> tuple[float, float, float, float]:
        """喂给 roi_rect / crop_roi 的四个参数，四个总是一起用。"""
        return (self.crop_w_ratio, self.crop_h_ratio,
                self.crop_cx_ratio, self.crop_cy_ratio)


@dataclass(frozen=True)
class ScoringConfig:
    hold_seconds: float = 0.5              # 加分 / 撤销手势保持时长
    reset_hold_seconds: float = 3.0        # Open_Palm 清零保持时长
    cooldown_seconds: float = 5.0          # 计分后该半场冷却
    # 保持期间允许手势短暂丢检多久而不重新计时。5fps 采样下识别难免闪断，
    # 不留容忍窗口的话掉一帧就归零 —— 实测成功率 45% vs 91%。
    hold_grace_seconds: float = 0.4
    # 必须真正识别到多少次才算数（需求 §5.2「连续多次采样中保持」）。
    # 只有容忍窗口没有这条的话，手势闪现一次就能凑够时长触发 —— 实测误触率 98%。
    hold_min_samples: int = 2

    def __post_init__(self) -> None:
        if self.hold_min_samples < 1:
            raise ConfigError("scoring.hold_min_samples 必须 >= 1")
        if self.hold_grace_seconds < 0:
            raise ConfigError("scoring.hold_grace_seconds 不能为负")


@dataclass(frozen=True)
class GameConfig:
    """21 分制赛制（需求 §5.5）。"""

    enabled: bool = True
    target: int = 21                       # 先到多少分
    win_by: int = 2                        # 且至少领先多少分
    cap: int = 30                          # 29:29 后先到 30 者直接获胜

    def __post_init__(self) -> None:
        if self.target < 1:
            raise ConfigError("game.target 必须 >= 1")
        if self.win_by < 1:
            raise ConfigError("game.win_by 必须 >= 1")
        if self.cap < self.target:
            raise ConfigError(f"game.cap({self.cap}) 不能小于 game.target({self.target})")


@dataclass(frozen=True)
class SourceConfig:
    side: str
    kind: str = "camera"                   # "camera" | "video"
    device: int = 0                        # kind=camera: V4L2/DShow 设备号
    path: str = ""                         # kind=video: 视频文件路径
    loop: bool = True                      # kind=video: 播完循环
    realtime: bool = True                  # kind=video: 按视频真实 fps 对齐挂钟时间
    start_offset: float = 0.0              # kind=video: 起播偏移(秒)
    mirror: bool = False                   # 画面左右镜像
    # 这一路单独的识别区，不填就用 [capture] 里的。两路的分辨率和取景可以完全
    # 不同（开发期一路实拍横屏一路手持竖屏；实机两台相机也各看各的半场），
    # 强行共用一套比例会把其中一路裁废。
    crop_w_ratio: float | None = None
    crop_h_ratio: float | None = None
    crop_cx_ratio: float | None = None
    crop_cy_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ConfigError(f"source.side 只能是 {SIDES}, 当前 {self.side!r}")
        if self.kind not in ("camera", "video"):
            raise ConfigError(f"source.kind 只能是 camera/video, 当前 {self.kind!r}")
        if self.kind == "video" and not self.path:
            raise ConfigError(f"[{self.side}] kind=video 必须提供 path")
        for name in ("crop_w_ratio", "crop_h_ratio"):
            value = getattr(self, name)
            if value is not None and not 0 < value <= 1:
                raise ConfigError(f"[{self.side}] {name} 必须在 (0, 1] 区间, 当前 {value}")
        for name in ("crop_cx_ratio", "crop_cy_ratio"):
            value = getattr(self, name)
            if value is not None and not 0 <= value <= 1:
                raise ConfigError(f"[{self.side}] {name} 必须在 [0, 1] 区间, 当前 {value}")

    def crop_for(self, capture: CaptureConfig) -> tuple[float, float, float, float]:
        """这一路实际用的识别区：逐项回落到 [capture] 的默认值。"""
        default = capture.crop
        return tuple(  # type: ignore[return-value]
            default[i] if own is None else own
            for i, own in enumerate((self.crop_w_ratio, self.crop_h_ratio,
                                     self.crop_cx_ratio, self.crop_cy_ratio)))


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    preview: bool = True                   # 网页里显示摄像头画面(Pi 上建议关掉)
    preview_width: int = 480
    preview_quality: int = 60              # JPEG 质量 1~100
    allow_manual: bool = True              # 网页上的 +1/-1/清零 按钮


@dataclass(frozen=True)
class LedConfig:
    brightness: int = 60                   # 0~255
    cascaded: int = 4                      # 8x8 模块数量：左右各两块，每边两位数字
    block_orientation: int = -90
    rotate: int = 0
    spi_port: int = 0
    spi_device: int = 0
    # luma 默认 8MHz。MAX7219 接 5V 时 V_IH = 3.5V，而 Pi 的 SPI 只有 3.3V，
    # 本来就低于规格，靠的是余量；十几厘米无屏蔽杜邦线会把余量吃掉。
    # 首次上电花屏/乱码就先把这个降到 1MHz。
    bus_speed_hz: int = 8_000_000
    reverse_blocks: bool = False           # 左右装反了就打开（接线方向决定）
    show_match_point: bool = True          # 赛点时在该侧底部画一条下划线

    def __post_init__(self) -> None:
        if self.cascaded < 2 or self.cascaded % 2:
            raise ConfigError(
                f"display.led.cascaded 必须是 >=2 的偶数（左右各一半），当前 {self.cascaded}")

    @property
    def half_width(self) -> int:
        """每个半场分到的像素宽度。4 块 8x8 -> 每边 16px -> 两位数字。"""
        return self.cascaded * 8 // 2

    @property
    def digits_per_side(self) -> int:
        return self.half_width // 8


@dataclass(frozen=True)
class DisplayConfig:
    backends: tuple[str, ...] = ("web",)   # web | console | led
    web: WebConfig = field(default_factory=WebConfig)
    led: LedConfig = field(default_factory=LedConfig)

    def __post_init__(self) -> None:
        known = {"web", "console", "led"}
        unknown = set(self.backends) - known
        if unknown:
            raise ConfigError(f"未知的 display.backends: {sorted(unknown)}, 可选 {sorted(known)}")
        if not self.backends:
            raise ConfigError("display.backends 不能为空")


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    game: GameConfig = field(default_factory=GameConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    sources: tuple[SourceConfig, ...] = ()
    root: Path = Path(".")

    def resolve(self, relative: str) -> Path:
        """把配置里的相对路径按配置文件所在目录解析。"""
        path = Path(relative)
        return path if path.is_absolute() else (self.root / path)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] 必须是一个表(table)")
    return value


def _build(cls, data: dict[str, Any], where: str, **extra):
    fields = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - fields - set(extra)
    if unknown:
        raise ConfigError(f"[{where}] 存在未知配置项: {sorted(unknown)}")
    try:
        return cls(**{k: v for k, v in data.items() if k in fields}, **extra)
    except TypeError as exc:                     # 缺必填项 / 类型不对
        raise ConfigError(f"[{where}] {exc}") from exc


def load_config(path: str | Path | None = None) -> Config:
    """读取 TOML 配置；path 为 None 时返回全默认配置。"""
    if path is None:
        return Config(sources=(SourceConfig(side="left", device=0),
                               SourceConfig(side="right", device=2)))

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    display_raw = _section(data, "display")
    web_raw = _section(display_raw, "web")
    led_raw = _section(display_raw, "led")
    backends = display_raw.get("backends", ["web"])
    if isinstance(backends, str):
        backends = [backends]

    display = DisplayConfig(
        backends=tuple(backends),
        web=_build(WebConfig, web_raw, "display.web"),
        led=_build(LedConfig, led_raw, "display.led"),
    )

    sources_raw = data.get("sources", [])
    if not isinstance(sources_raw, list):
        raise ConfigError("[[sources]] 必须是数组表(array of tables)")
    sources = tuple(_build(SourceConfig, s, "sources") for s in sources_raw)
    seen = [s.side for s in sources]
    if len(seen) != len(set(seen)):
        raise ConfigError(f"同一半场配置了多个视频源: {seen}")

    return Config(
        audio=_build_audio(_section(data, "audio")),
        recognition=_build(RecognitionConfig, _section(data, "recognition"), "recognition"),
        capture=_build(CaptureConfig, _section(data, "capture"), "capture"),
        scoring=_build(ScoringConfig, _section(data, "scoring"), "scoring"),
        game=_build(GameConfig, _section(data, "game"), "game"),
        display=display,
        sources=sources,
        root=path.parent.resolve(),
    )


def apply_overrides(
    config: Config,
    *,
    backends: list[str] | None = None,
    port: int | None = None,
    videos: dict[str, str] | None = None,
    no_preview: bool = False,
) -> Config:
    """命令行参数覆盖配置文件。"""
    display = config.display
    if backends:
        display = replace(display, backends=tuple(backends))
    if port is not None:
        display = replace(display, web=replace(display.web, port=port))
    if no_preview:
        display = replace(display, web=replace(display.web, preview=False))

    sources = config.sources
    if videos:
        by_side = {s.side: s for s in sources}
        for side, path in videos.items():
            if side not in SIDES:
                raise ConfigError(f"--video 的半场只能是 {SIDES}, 当前 {side!r}")
            base = by_side.get(side, SourceConfig(side=side))
            by_side[side] = replace(base, kind="video", path=path)
        sources = tuple(by_side[s] for s in SIDES if s in by_side)

    return replace(config, display=display, sources=sources)


def _build_audio(data):
    try:
        return _build(AudioConfig, data, "audio")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
