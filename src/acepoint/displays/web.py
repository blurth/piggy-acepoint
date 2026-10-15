"""网页记分牌 —— 前期替代 LED 点阵屏，同时充当调试面板。

    /                 记分牌页面（大比分 + 双半场识别状态 + 画面预览）
    /api/state        当前状态 JSON
    /api/events       SSE 推送，页面靠它实时刷新
    /api/action       手动 +1 / -1 / 清零（调试用）
    /preview/<side>   MJPEG 画面流

Flask 跑在后台线程，主循环只往内存里丢最新状态，不做任何网络 IO。
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
from typing import Callable

from flask import Flask, Response, jsonify, render_template, request

from acepoint.config import WebConfig
from acepoint.audio.config import AudioConfig, settings
from acepoint.displays.base import Display
from acepoint.state import Action, Snapshot, Telemetry

log = logging.getLogger(__name__)

_ACTIONS = {a.value: a for a in Action}
_BOUNDARY = b"--acepoint"


def _port_conflict(host: str, port: int) -> str | None:
    """试着占一下端口；返回错误说明，None 表示可用。

    注意不要设 SO_REUSEADDR —— Windows 上它会让 bind 在端口已被占用时照样成功，
    探测就失去意义了。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            return str(exc)
    return None


class _Broker:
    """SSE 广播：每个浏览器连接一个队列，慢的连接直接丢帧，不阻塞主循环。"""

    def __init__(self, maxsize: int = 8) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def idle(self) -> bool:
        """没人订阅时主循环连 JSON 都不用序列化。"""
        with self._lock:
            return not self._subscribers

    def publish(self, payload: str) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


class _PreviewStore:
    """按半场保存最新一帧 JPEG，MJPEG 流等在条件变量上，不轮询。

    同时记着有几个人在看：没人看的时候主循环就不用编码预览图了。
    """

    def __init__(self) -> None:
        self._frames: dict[str, tuple[int, bytes]] = {}
        self._cond = threading.Condition()
        self._seq = 0
        self._viewers = 0

    @property
    def watched(self) -> bool:
        with self._cond:
            return self._viewers > 0

    def open_stream(self) -> None:
        with self._cond:
            self._viewers += 1

    def close_stream(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._frames.clear()          # 别攥着最后几帧的内存不放
            self._cond.notify_all()

    def put(self, side: str, jpeg: bytes) -> None:
        with self._cond:
            self._seq += 1
            self._frames[side] = (self._seq, jpeg)
            self._cond.notify_all()

    def wait(self, side: str, last_seq: int, timeout: float = 2.0) -> tuple[int, bytes] | None:
        with self._cond:
            current = self._frames.get(side)
            if current is None or current[0] <= last_seq:
                self._cond.wait(timeout)
                current = self._frames.get(side)
            if current is None or current[0] <= last_seq:
                return None
            return current


class WebDisplay(Display):
    def __init__(
        self,
        config: WebConfig,
        *,
        on_action: Callable[[str, Action], None] | None = None,
        on_audio=None,
        audio_config=None,
    ) -> None:
        self._audio_config = audio_config or AudioConfig()
        self._on_audio = on_audio
        self.config = config
        self._on_action = on_action
        self._broker = _Broker()
        self._previews = _PreviewStore()
        self._latest = Telemetry(snapshot=Snapshot())
        self._latest_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.app = self._build_app()

    @property
    def wants_preview(self) -> bool:
        """只有真的有人在看 MJPEG 流时才要预览图 —— 没人看就别浪费 CPU 编码。"""
        return self.config.preview and self._previews.watched

    def _state(self) -> dict:
        with self._latest_lock:
            return self._latest.to_dict()

    # ------------------------------------------------------------- Display

    def start(self) -> None:
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        conflict = _port_conflict(self.config.host, self.config.port)
        if conflict:
            # 端口被占时 werkzeug 会在子线程里 sys.exit()，静默死掉，
            # 结果就是"程序在跑但网页打不开" —— 提前探一次，把话说清楚
            log.error("端口 %d 不可用（%s）—— 换一个: acepoint --port 8081",
                      self.config.port, conflict)
            return

        self._thread = threading.Thread(target=self._serve, name="web", daemon=True)
        self._thread.start()
        host = "localhost" if self.config.host in ("0.0.0.0", "127.0.0.1") else self.config.host
        log.info("scoreboard: http://%s:%d", host, self.config.port)

    def update(self, telemetry: Telemetry) -> None:
        with self._latest_lock:
            self._latest = telemetry
        # to_dict() 要做一次 asdict 深拷贝、json.dumps 再走一遍，
        # 没人订阅 SSE 时这两步纯属白烧 CPU（10Hz × 全天候）
        if not self._broker.idle:
            self._broker.publish(json.dumps(telemetry.to_dict()))

    def push_preview(self, side: str, jpeg: bytes) -> None:
        self._previews.put(side, jpeg)

    def close(self) -> None:
        # Flask 开发服务器跑在 daemon 线程里，进程退出即回收
        pass

    # ----------------------------------------------------------- Flask

    def _serve(self) -> None:
        try:
            self.app.run(
                host=self.config.host,
                port=self.config.port,
                threaded=True,
                debug=False,
                use_reloader=False,
            )
        except BaseException as exc:          # noqa: BLE001 - werkzeug 绑定失败会 SystemExit
            log.error("网页服务停止（%s:%d）: %r", self.config.host, self.config.port, exc)

    def _build_app(self) -> Flask:
        app = Flask(__name__, template_folder="templates")
        app.config["TEMPLATES_AUTO_RELOAD"] = False

        @app.get("/")
        def index():
            return render_template(
                "dashboard.html",
                initial=json.dumps(self._state()),
                preview=self.config.preview,
                allow_manual=self.config.allow_manual,
            )

        @app.get("/api/state")
        def api_state():
            return jsonify(self._state())

        @app.get("/api/events")
        def api_events():
            def stream():
                q = self._broker.subscribe()
                try:
                    yield f"data: {json.dumps(self._state())}\n\n"
                    while True:
                        try:
                            yield f"data: {q.get(timeout=15)}\n\n"
                        except queue.Empty:
                            yield ": keepalive\n\n"
                finally:
                    self._broker.unsubscribe(q)

            return Response(
                stream(),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.post("/api/action")
        def api_action():
            if not self.config.allow_manual:
                return jsonify(error="manual control disabled"), 403
            body = request.get_json(silent=True) or {}
            side, name = body.get("side"), body.get("action")
            action = _ACTIONS.get(name)
            if side not in ("left", "right") or action is None:
                return jsonify(error="invalid side/action"), 400
            if self._on_action is not None:
                self._on_action(side, action)
            return jsonify(ok=True)

        @app.get("/preview/<side>.mjpg")
        def preview(side: str):
            if not self.config.preview:
                return jsonify(error="preview disabled"), 404

            def stream():
                # 计数进出要配对，否则主循环会一直以为有人在看，白编码到天亮
                self._previews.open_stream()
                try:
                    last = 0
                    while True:
                        item = self._previews.wait(side, last)
                        if item is None:
                            continue
                        last, jpeg = item
                        yield (
                            _BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
                        )
                finally:
                    self._previews.close_stream()

            return Response(
                stream(),
                mimetype="multipart/x-mixed-replace; boundary=acepoint",
                headers={"Cache-Control": "no-cache"},
            )

        @app.post("/api/audio/settings")
        def api_audio_settings():
            if not self._audio_config.allow_web_control or self._on_audio is None:
                return jsonify(error="audio control disabled"), 403
            body = request.get_json(silent=True)
            try:
                settings(self._audio_config, body)
            except (ValueError, TypeError):
                return jsonify(error="invalid audio settings"), 400
            if not self._on_audio(body):
                return jsonify(error="control queue full"), 429
            return jsonify(ok=True), 202

        @app.post("/api/audio/test")
        def api_audio_test():
            if not self._audio_config.allow_web_control or self._on_audio is None:
                return jsonify(error="audio control disabled"), 403
            state = self._state()
            if state["score"]["left"] or state["score"]["right"]:
                return jsonify(error="preview requires 0:0"), 409
            if not self._on_audio({"test": True}):
                return jsonify(error="control queue full"), 429
            return jsonify(ok=True), 202

        @app.get("/healthz")
        def healthz():
            return jsonify(ok=True)

        return app
