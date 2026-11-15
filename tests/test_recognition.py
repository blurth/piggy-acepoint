from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from acepoint.config import RecognitionConfig
from acepoint.recognition import GestureReader


def observe_hands(monkeypatch, names):
    reader = GestureReader.__new__(GestureReader)
    reader.config = RecognitionConfig()
    reader.per_source = True
    reader._last_ts = {"left": 0}
    reader._mp = SimpleNamespace(Image=lambda **kwargs: object(),
                                 ImageFormat=SimpleNamespace(SRGB=1))
    recognizer = Mock()
    recognizer.recognize_for_video.return_value = SimpleNamespace(
        gestures=[[SimpleNamespace(category_name=name, score=0.9)] for name in names])
    reader._recognizers = {"left": recognizer}
    return reader.observe("left", np.zeros((2, 2, 3), dtype=np.uint8))


def test_conflicting_hands_do_not_score(monkeypatch):
    observation = observe_hands(monkeypatch, ["Victory", "Open_Palm"])
    assert observation.gesture is None
    assert observation.raw == "Ambiguous"


def test_two_increment_gestures_are_compatible(monkeypatch):
    observation = observe_hands(monkeypatch, ["Victory", "Pointing_Up"])
    assert observation.gesture == "Victory"


# ------------------------------------------------------------ 推理线程数

class _PlainOptions:
    """mediapipe 1.x 那种 BaseOptions：没有 to_pb2。"""

    def __init__(self, model_asset_path=None):
        self.model_asset_path = model_asset_path


class _ProtoOptions(_PlainOptions):
    """mediapipe 0.10.x 那种：选项最终转成 protobuf。"""

    def to_pb2(self):
        import types

        copied = []
        return types.SimpleNamespace(
            copied=copied,
            acceleration=types.SimpleNamespace(CopyFrom=copied.append))


@pytest.fixture
def fake_protos(monkeypatch):
    """假装装的是 0.10.x：提供 threaded_base_options 要导入的两个 proto 模块。"""
    import sys
    import types

    class Xnnpack:
        def __init__(self, num_threads=None):
            self.num_threads = num_threads

    class Acceleration:
        def __init__(self, xnnpack=None):
            self.xnnpack = xnnpack

    inference = types.ModuleType("inference_calculator_pb2")
    inference.InferenceCalculatorOptions = types.SimpleNamespace(
        Delegate=types.SimpleNamespace(Xnnpack=Xnnpack))
    acceleration = types.ModuleType("acceleration_pb2")
    acceleration.Acceleration = Acceleration
    tensor = types.ModuleType("tensor")
    tensor.inference_calculator_pb2 = inference
    proto = types.ModuleType("proto")
    proto.acceleration_pb2 = acceleration
    for name, module in {
        "mediapipe.calculators.tensor": tensor,
        "mediapipe.calculators.tensor.inference_calculator_pb2": inference,
        "mediapipe.tasks.cc.core.proto": proto,
        "mediapipe.tasks.cc.core.proto.acceleration_pb2": acceleration,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_one_inference_thread_leaves_the_options_alone(tmp_path):
    from acepoint.recognition import threaded_base_options

    options, threads = threaded_base_options(_ProtoOptions, tmp_path / "m.task", 1)
    assert type(options) is _ProtoOptions
    assert threads == 1


def test_inference_threads_are_injected_into_the_proto(fake_protos, tmp_path):
    """0.10.x：在转出来的 proto 上换成带 num_threads 的 XNNPACK 委托。"""
    from acepoint.recognition import threaded_base_options

    options, threads = threaded_base_options(_ProtoOptions, tmp_path / "m.task", 2)
    assert threads == 2
    assert options.model_asset_path == str(tmp_path / "m.task")
    proto = options.to_pb2()
    assert [a.xnnpack.num_threads for a in proto.copied] == [2]


def test_inference_threads_fall_back_when_mediapipe_does_not_expose_them(tmp_path, caplog):
    """1.x 没有 to_pb2：这只是性能旋钮，不生效也不能让识别起不来。"""
    from acepoint.recognition import threaded_base_options

    with caplog.at_level("WARNING"):
        options, threads = threaded_base_options(_PlainOptions, tmp_path / "m.task", 2)
    assert type(options) is _PlainOptions
    assert threads == 1
    assert "inference_threads" in caplog.text


def test_inference_threads_must_be_positive():
    from acepoint.config import ConfigError, RecognitionConfig

    with pytest.raises(ConfigError, match="inference_threads"):
        RecognitionConfig(inference_threads=0)
