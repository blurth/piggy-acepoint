"""网页记分牌：路由、手动控制、端口探测。用 Flask test_client，不起真服务器。"""

from __future__ import annotations

import json
import socket

import pytest

pytest.importorskip("flask")

from acepoint.config import WebConfig  # noqa: E402
from acepoint.displays.web import WebDisplay, _port_conflict  # noqa: E402
from acepoint.state import Action, ScoreBoard, SideStatus, Telemetry  # noqa: E402


def telemetry() -> Telemetry:
    board = ScoreBoard()
    board.apply("left", Action.INCREMENT)
    return Telemetry(
        snapshot=board.snapshot(),
        sides={"left": SideStatus(side="left", fps=5.0, gesture="Victory", progress=0.5)},
    )


@pytest.fixture
def display() -> WebDisplay:
    actions: list[tuple[str, Action]] = []
    web = WebDisplay(WebConfig(preview=True), on_action=lambda s, a: actions.append((s, a)))
    web.actions = actions          # type: ignore[attr-defined]  测试里方便断言
    web.update(telemetry())
    return web


def test_index_renders(display):
    response = display.app.test_client().get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "ACEPOINT" in body
    assert "const PREVIEW = true" in body       # 画面预览由前端按这个开关渲染
    assert '"text": "1:0"' in body              # 首屏直接带上当前比分，不用等 SSE


def test_index_without_preview():
    web = WebDisplay(WebConfig(preview=False))
    assert "const PREVIEW = false" in web.app.test_client().get("/").get_data(as_text=True)


def test_state_endpoint(display):
    data = display.app.test_client().get("/api/state").get_json()
    assert data["score"]["text"] == "1:0"
    assert data["sides"]["left"]["fps"] == 5.0
    assert data["sides"]["left"]["gesture"] == "Victory"


def test_manual_action_is_forwarded(display):
    response = display.app.test_client().post(
        "/api/action", json={"side": "right", "action": "increment"}
    )
    assert response.status_code == 200
    assert display.actions == [("right", Action.INCREMENT)]


@pytest.mark.parametrize("body", [
    {"side": "middle", "action": "increment"},
    {"side": "left", "action": "explode"},
    {},
])
def test_invalid_action_is_rejected(display, body):
    assert display.app.test_client().post("/api/action", json=body).status_code == 400
    assert display.actions == []


def test_manual_control_can_be_disabled():
    web = WebDisplay(WebConfig(allow_manual=False))
    response = web.app.test_client().post("/api/action", json={"side": "left", "action": "reset"})
    assert response.status_code == 403


def test_preview_endpoint_is_absent_when_disabled():
    web = WebDisplay(WebConfig(preview=False))
    assert web.app.test_client().get("/preview/left.mjpg").status_code == 404


def test_no_preview_encoding_until_someone_watches(display):
    """没人开着画面时，主循环不该浪费 CPU 去 resize + 编 JPEG。"""
    assert display.wants_preview is False

    display._previews.open_stream()
    assert display.wants_preview is True

    display._previews.close_stream()
    assert display.wants_preview is False


def test_broker_is_idle_without_subscribers(display):
    """没人订阅 SSE 时连 json.dumps 都可以省掉。"""
    assert display._broker.idle is True
    q = display._broker.subscribe()
    assert display._broker.idle is False
    display._broker.unsubscribe(q)
    assert display._broker.idle is True


def test_update_skips_serialisation_when_nobody_listens(display, monkeypatch):
    calls = []
    monkeypatch.setattr(display._broker, "publish", lambda payload: calls.append(payload))

    display.update(telemetry())
    assert calls == []                          # 没订阅者 -> 一次都不发

    display._broker.subscribe()
    display.update(telemetry())
    assert len(calls) == 1

    # 但 /api/state 任何时候都要能读到最新状态
    assert display.app.test_client().get("/api/state").get_json()["score"]["text"] == "1:0"


def test_events_stream_opens_with_the_current_state(display):
    response = display.app.test_client().get("/api/events", buffered=False)
    assert response.headers["Content-Type"].startswith("text/event-stream")
    first = next(response.response).decode()
    assert first.startswith("data: ")
    assert json.loads(first[6:])["score"]["text"] == "1:0"
    response.close()


def test_port_conflict_detection():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        assert _port_conflict("127.0.0.1", port) is not None

    # 端口释放后应该可用了
    assert _port_conflict("127.0.0.1", port) is None


def test_start_does_not_raise_when_the_port_is_taken(caplog):
    """端口被占时要给出清楚的日志，而不是静默不起服务。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        web = WebDisplay(WebConfig(port=port))
        web.start()
    assert any("不可用" in record.getMessage() for record in caplog.records)
    assert web._thread is None
