"""Behavior tests for the OpenClaw local/offboard reminder daemon."""

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DAEMON_PATH = ROOT / "scripts" / "opencloud_reminder_daemon.py"


def _load_daemon():
    spec = importlib.util.spec_from_file_location("opencloud_reminder_daemon", str(DAEMON_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_board_url_uses_current_board_by_default(monkeypatch):
    daemon = _load_daemon()
    monkeypatch.delenv("IRONBUDDY_BOARD_URL", raising=False)
    monkeypatch.delenv("IRONBUDDY_BOARD_IP", raising=False)

    assert daemon.resolve_board_url() == "http://10.29.10.224:5000"
    assert daemon.resolve_board_url(board_ip="10.29.10.224") == "http://10.29.10.224:5000"


def test_resolve_board_url_precedence_and_normalization(monkeypatch):
    daemon = _load_daemon()
    monkeypatch.setenv("IRONBUDDY_BOARD_URL", "10.1.2.3:7000")

    assert daemon.resolve_board_url() == "http://10.1.2.3:7000"
    assert daemon.resolve_board_url("http://runtime-board:9000/path") == "http://runtime-board:9000"


def test_load_insights_caches_board_data_and_reuses_on_failure(tmp_path, monkeypatch):
    daemon = _load_daemon()
    monkeypatch.setattr(daemon, "INSIGHTS_PATH", str(tmp_path / "insights.json"))

    def fetch_ok(board_url, timeout=4):
        return {
            "ok": True,
            "weekly_training": {"sessions": 2, "good": 18, "failed": 1},
        }, ""

    monkeypatch.setattr(daemon, "_fetch_insights", fetch_ok)
    insights, status = daemon.load_insights("http://10.29.10.224:5000", True)
    assert status["source"] == "board"
    assert insights["_insights_source"] == "board"
    assert json.loads(Path(daemon.INSIGHTS_PATH).read_text(encoding="utf-8"))["ok"] is True

    def fetch_fail(board_url, timeout=4):
        return {}, "offline"

    monkeypatch.setattr(daemon, "_fetch_insights", fetch_fail)
    cached, cached_status = daemon.load_insights("http://10.29.10.224:5000", True)
    assert cached_status["source"] == "cached"
    assert cached_status["error"] == "offline"
    assert cached["_insights_source"] == "cached"


def test_run_once_status_exposes_local_offboard_runtime(tmp_path, monkeypatch):
    daemon = _load_daemon()
    monkeypatch.setattr(daemon, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(daemon, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(daemon, "HISTORY_PATH", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(
        daemon,
        "load_snapshot",
        lambda board_url: ({"exercise": "bicep_curl", "good": 9, "_snapshot_source": "board"}, True, ""),
    )
    monkeypatch.setattr(
        daemon,
        "load_insights",
        lambda board_url, online: (
            {"ok": True, "weekly_training": {"sessions": 3, "good": 27, "failed": 2}},
            {"ok": True, "source": "cached", "error": "", "cached_at": 123.0},
        ),
    )
    monkeypatch.setattr(daemon, "push_card", lambda mode, snapshot, text, dry_run=True: {"ok": True})

    status = daemon.run_once("weekly", "http://10.29.10.224:5000", dry_run=True)

    assert status["presentation_name"] == "OpenClaw 后台提醒"
    assert status["primary_runtime"] == "local_offboard"
    assert status["runtime_location"] == "local/offboard"
    assert status["runtime"]["offboard_runtime"] is True
    assert status["runtime"]["board_url"] == "http://10.29.10.224:5000"
    assert status["insights_source"] == "cached"
    assert "本周训练统计" in status["last_push_text"]
