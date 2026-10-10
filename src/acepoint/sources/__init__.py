"""视频源：摄像头 / 录制视频。"""

from __future__ import annotations

import logging

from acepoint.config import CaptureConfig, Config, SourceConfig
from acepoint.sources.base import Frame, FrameSource
from acepoint.sources.camera import CameraSource
from acepoint.sources.video import VideoFileSource

log = logging.getLogger(__name__)

__all__ = ["Frame", "FrameSource", "CameraSource", "VideoFileSource", "build_sources"]


def build_source(spec: SourceConfig, capture: CaptureConfig, config: Config) -> FrameSource | None:
    """按配置造一个视频源；打不开返回 None，调用方负责跳过该半场。"""
    try:
        if spec.kind == "video":
            return VideoFileSource(
                side=spec.side,
                path=config.resolve(spec.path),
                loop=spec.loop,
                realtime=spec.realtime,
                start_offset=spec.start_offset,
                mirror=spec.mirror,
            )
        return CameraSource(
            side=spec.side,
            device=spec.device,
            width=capture.width,
            height=capture.height,
            mirror=spec.mirror,
            brightness=capture.brightness,
            sharpness=capture.sharpness,
        )
    except OSError as exc:
        log.warning("[%s] source unavailable, this half-court is disabled: %s", spec.side, exc)
        return None


def build_sources(config: Config) -> dict[str, FrameSource]:
    """造出所有可用视频源。一路失败不影响另一路。"""
    sources: dict[str, FrameSource] = {}
    for spec in config.sources:
        source = build_source(spec, config.capture, config)
        if source is not None:
            sources[spec.side] = source
            log.info("[%s] %s", spec.side, source.description)
    return sources
