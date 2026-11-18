"""网页关机按钮，以及点阵屏显示本机 IP（手机热点下找网页地址）。"""

from __future__ import annotations

import pytest

from acepoint import power
from acepoint.config import LedConfig, WebConfig
from acepoint.displays import led as led_module
from acepoint.displays.led import LedDisplay
from acepoint.displays.led_captions import ip_pages, render_page
from acepoint.displays.web import WebDisplay


@pytest.fixture
def requests():
    return []


def _web(requests, **config) -> WebDisplay:
    return WebDisplay(WebConfig(**config), on_poweroff=lambda: requests.append("off"))


def test_button_only_appears_when_enabled(requests):
    assert 'id="poweroff"' not in _web(requests).app.test_client().get("/").get_data(as_text=True)
    page = _web(requests, allow_poweroff=True).app.test_client().get("/").get_data(as_text=True)
    assert 'id="poweroff"' in page


def test_disabled_endpoint_refuses(requests):
    client = _web(requests).app.test_client()
    assert client.post("/api/system/poweroff", json={"confirm": True}).status_code == 403
    assert requests == []


def test_poweroff_needs_explicit_confirmation(requests, monkeypatch):
    monkeypatch.setattr(power, "can_poweroff", lambda: True)
    client = _web(requests, allow_poweroff=True).app.test_client()
    assert client.post("/api/system/poweroff", json={}).status_code == 400
    assert requests == []


def test_missing_sudo_permission_is_explained(requests, monkeypatch):
    monkeypatch.setattr(power, "can_poweroff", lambda: False)
    response = _web(requests, allow_poweroff=True).app.test_client().post(
        "/api/system/poweroff", json={"confirm": True})
    assert response.status_code == 403
    assert "sudoers" in response.get_json()["hint"]
    assert requests == []


def test_confirmed_poweroff_reaches_the_engine(requests, monkeypatch):
    monkeypatch.setattr(power, "can_poweroff", lambda: True)
    response = _web(requests, allow_poweroff=True).app.test_client().post(
        "/api/system/poweroff", json={"confirm": True})
    assert response.status_code == 202
    assert requests == ["off"]


def test_engine_finishes_up_before_powering_off(monkeypatch):
    """关机在主循环收尾（录像、日志、点阵屏都关好）之后才发出。"""
    from acepoint.engine import ScoringEngine

    order = []
    engine = ScoringEngine.__new__(ScoringEngine)
    engine._poweroff = __import__("threading").Event()
    engine._stop = False
    engine.close = lambda: order.append("close")
    monkeypatch.setattr(power, "poweroff", lambda: order.append("poweroff"))

    class _Display:
        def update(self, telemetry):
            pass

    engine.display = _Display()
    engine._workers = None
    engine.sources = {"left": object()}
    engine._tick = lambda side, source, now: None
    engine.config = type("C", (), {"sources": (), "capture": type("K", (), {"poll_hz": 100})()})()
    engine._telemetry = lambda: None
    engine._drain_audio = engine._drain_manual = lambda: None
    engine._crop = lambda side: (1, 1, 0.5, 0.5)
    engine.request_poweroff()
    engine.run(max_seconds=5)
    assert order == ["close", "poweroff"]


def test_ip_is_shown_one_group_per_page():
    assert ip_pages("192.168.43.17") == ("IP", "192.", "168.", "43.", "17")
    for page in ip_pages("172.20.10.248") + ip_pages("10.0.0.1"):
        assert len(render_page(page, 0)) == 32       # 每页都放得下


def test_led_shows_the_ip_when_it_first_appears_and_when_it_changes(monkeypatch):
    address = ["192.168.43.17"]
    monkeypatch.setattr(led_module, "local_ip", lambda: address[0])
    display = LedDisplay(LedConfig())

    display._check_ip(0.0)
    assert display._caption.pages == ip_pages("192.168.43.17")

    display._caption = None
    display._check_ip(10.0)                          # 没变：不再打扰
    assert display._caption is None

    address[0] = "172.20.10.3"                       # 换了网络
    display._check_ip(20.0)
    assert display._caption.pages == ip_pages("172.20.10.3")


def test_ip_display_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(led_module, "local_ip", lambda: "192.168.43.17")
    display = LedDisplay(LedConfig(show_ip=False))
    display._check_ip(0.0)
    assert display._caption is None
