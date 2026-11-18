"""网页"录像"区域：看占用、一键清空（正在录的那次保留，别的东西一概不碰）。"""

from __future__ import annotations

from pathlib import Path

from acepoint import recorder
from acepoint.config import WebConfig
from acepoint.displays.web import WebDisplay


def _session(root: Path, name: str, size: int) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "left_000.mkv").write_bytes(b"x" * size)
    (path / "events.csv").write_bytes(b"t\n")
    return path


def test_summary_counts_sessions_and_bytes(tmp_path):
    _session(tmp_path, "20261118_140000", 100)
    current = _session(tmp_path, "20261118_150000", 50)
    summary = recorder.summarize(tmp_path, current)
    assert summary["sessions"] == 2
    assert summary["bytes"] == 100 + 50 + 2 * 2
    assert summary["free_bytes"] > 0
    assert summary["current"] == "20261118_150000"


def test_clear_keeps_the_running_session_and_anything_else(tmp_path):
    old = _session(tmp_path, "20261118_140000", 100)
    current = _session(tmp_path, "20261118_150000", 50)
    (tmp_path / "notes").mkdir()                    # 不是会话目录：绝不能删
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")

    result = recorder.clear(tmp_path, current)

    assert result == {"removed": 1, "freed_bytes": 102}
    assert not old.exists()
    assert current.exists() and (tmp_path / "notes").exists() and (tmp_path / "keep.txt").exists()


def test_missing_recordings_folder_is_just_empty(tmp_path):
    summary = recorder.summarize(tmp_path / "recordings", None)
    assert summary["sessions"] == 0 and summary["bytes"] == 0


class _Engine:
    def __init__(self, root: Path) -> None:
        self.root, self.cleared = root, 0

    def recordings_summary(self):
        return recorder.summarize(self.root, None)

    def clear_recordings(self):
        self.cleared += 1
        return recorder.clear(self.root, None)


def test_web_section_only_when_enabled(tmp_path):
    engine = _Engine(tmp_path)
    off = WebDisplay(WebConfig(), recordings=engine).app.test_client()
    assert 'id="rec-clear"' not in off.get("/").get_data(as_text=True)
    assert off.get("/api/recordings").status_code == 403
    assert off.post("/api/recordings/clear", json={"confirm": True}).status_code == 403
    on = WebDisplay(WebConfig(allow_clear_recordings=True), recordings=engine).app.test_client()
    assert 'id="rec-clear"' in on.get("/").get_data(as_text=True)


def test_web_clear_needs_confirmation_and_reports_the_result(tmp_path):
    _session(tmp_path, "20261118_140000", 1000)
    engine = _Engine(tmp_path)
    client = WebDisplay(WebConfig(allow_clear_recordings=True), recordings=engine).app.test_client()

    assert client.get("/api/recordings").get_json()["sessions"] == 1
    assert client.post("/api/recordings/clear", json={}).status_code == 400
    assert engine.cleared == 0

    body = client.post("/api/recordings/clear", json={"confirm": True}).get_json()
    assert body["removed"] == 1 and body["freed_bytes"] == 1002
    assert body["sessions"] == 0
