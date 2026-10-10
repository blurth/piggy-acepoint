"""配置加载与命令行覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from acepoint.config import ConfigError, apply_overrides, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_without_a_file():
    config = load_config(None)
    assert [s.side for s in config.sources] == ["left", "right"]
    assert config.scoring.hold_seconds == 0.5
    assert config.scoring.reset_hold_seconds == 3.0
    assert config.scoring.hold_grace_seconds == 0.4
    assert config.scoring.hold_min_samples == 2
    assert config.scoring.cooldown_seconds == 5.0
    assert config.display.backends == ("web",)


@pytest.mark.parametrize("name", ["config.toml", "config.pi.toml", "config.camera.toml"])
def test_shipped_configs_are_valid(name):
    config = load_config(REPO_ROOT / name)
    assert len(config.sources) == 2
    assert config.recognition.model_path.endswith(".task")


def test_camera_picture_controls_are_loaded(tmp_path):
    path = write(tmp_path, "[capture]\nbrightness = 20\nsharpness = 6\n")
    capture = load_config(path).capture
    assert capture.brightness == 20
    assert capture.sharpness == 6


def test_paths_resolve_against_the_config_directory(tmp_path):
    path = write(tmp_path, '[[sources]]\nside = "left"\nkind = "video"\npath = "videos/A.mp4"\n')
    config = load_config(path)
    assert config.resolve(config.sources[0].path) == tmp_path / "videos" / "A.mp4"


def test_unknown_key_is_rejected(tmp_path):
    path = write(tmp_path, "[scoring]\nhold_secondz = 2.0\n")
    with pytest.raises(ConfigError, match="hold_secondz"):
        load_config(path)


def test_unknown_backend_is_rejected(tmp_path):
    path = write(tmp_path, '[display]\nbackends = ["oled"]\n')
    with pytest.raises(ConfigError, match="oled"):
        load_config(path)


def test_duplicate_side_is_rejected(tmp_path):
    path = write(tmp_path, '[[sources]]\nside = "left"\n\n[[sources]]\nside = "left"\n')
    with pytest.raises(ConfigError, match="同一半场"):
        load_config(path)


def test_video_source_requires_a_path(tmp_path):
    path = write(tmp_path, '[[sources]]\nside = "left"\nkind = "video"\n')
    with pytest.raises(ConfigError, match="path"):
        load_config(path)


def test_bad_crop_ratio_is_rejected(tmp_path):
    path = write(tmp_path, "[capture]\ncrop_w_ratio = 1.5\n")
    with pytest.raises(ConfigError, match="crop_w_ratio"):
        load_config(path)


def test_bad_crop_centre_is_rejected(tmp_path):
    path = write(tmp_path, "[capture]\ncrop_cy_ratio = 1.2\n")
    with pytest.raises(ConfigError, match="crop_cy_ratio"):
        load_config(path)


def test_crop_centre_may_sit_on_the_edge(tmp_path):
    """0 和 1 是合法的（识别区会被夹回画面内），和宽高比例的开区间不同。"""
    path = write(tmp_path, "[capture]\ncrop_cx_ratio = 0.0\ncrop_cy_ratio = 1.0\n")
    assert load_config(path).capture.crop == (0.6, 0.8, 0.0, 1.0)


def test_source_crop_falls_back_per_field(tmp_path):
    """源只覆盖了一项，其余仍走 [capture] —— 两路取景不同才需要各配各的。"""
    path = write(tmp_path, "[capture]\ncrop_w_ratio = 0.4\ncrop_cy_ratio = 0.34\n"
                           '[[sources]]\nside = "left"\ncrop_w_ratio = 1.0\n')
    config = load_config(path)
    assert config.sources[0].crop_for(config.capture) == (1.0, 0.8, 0.5, 0.34)


def test_source_without_overrides_uses_capture(tmp_path):
    path = write(tmp_path, "[capture]\ncrop_w_ratio = 0.4\n"
                           '[[sources]]\nside = "left"\n')
    config = load_config(path)
    assert config.sources[0].crop_for(config.capture) == config.capture.crop


def test_bad_source_crop_is_rejected(tmp_path):
    path = write(tmp_path, '[[sources]]\nside = "left"\ncrop_h_ratio = 0.0\n')
    with pytest.raises(ConfigError, match="crop_h_ratio"):
        load_config(path)


def test_cli_overrides(tmp_path):
    config = load_config(REPO_ROOT / "config.pi.toml")
    updated = apply_overrides(
        config,
        backends=["console"],
        port=9999,
        videos={"left": "videos/A.mp4"},
        no_preview=True,
    )
    assert updated.display.backends == ("console",)
    assert updated.display.web.port == 9999
    assert updated.display.web.preview is False

    by_side = {s.side: s for s in updated.sources}
    assert by_side["left"].kind == "video" and by_side["left"].path == "videos/A.mp4"
    assert by_side["right"].kind == "camera"          # 没覆盖的那路保持原样
    assert config.display.backends == ("led", "console")   # 原配置不被修改
