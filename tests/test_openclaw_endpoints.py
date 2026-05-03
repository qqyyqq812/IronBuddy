"""Tests for OpenClaw V7.37 backend endpoints.

streamer_app.py imports flask/cv2 — use source-text checks (project pattern).
"""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMER = os.path.join(PROJECT_ROOT, "streamer_app.py")
DAEMON = os.path.join(PROJECT_ROOT, "scripts", "opencloud_reminder_daemon.py")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_status_endpoint_exists():
    src = _read(STREAMER)
    assert "@app.route('/api/openclaw/status'" in src


def test_once_endpoint_exists():
    src = _read(STREAMER)
    assert "@app.route('/api/openclaw/once'" in src


def test_history_endpoint_exists():
    src = _read(STREAMER)
    assert "@app.route('/api/openclaw/history'" in src


def test_status_includes_next_push():
    """Status payload should expose next push prediction (mode + ts)."""
    src = _read(STREAMER)
    assert "next_push_ts" in src
    assert "next_push_mode" in src


def test_status_includes_schedule_env():
    """Status should reflect weekly_hour / morning_hour / weekly_dow env."""
    src = _read(STREAMER)
    assert "weekly_hour" in src
    assert "morning_hour" in src


def test_history_path_from_daemon():
    """Daemon writes history jsonl alongside status file."""
    src = _read(DAEMON)
    assert "opencloud_reminder_history.jsonl" in src


def test_history_endpoint_reads_history_file():
    src = _read(STREAMER)
    assert "opencloud_reminder_history.jsonl" in src


def test_once_endpoint_accepts_send_param():
    """POST /api/openclaw/once {dry_run|send|mode}."""
    src = _read(STREAMER)
    assert "openclaw_once" in src or "openclaw_run_once" in src
    # body must read mode + dry_run/send
    idx_once = src.find("@app.route('/api/openclaw/once'")
    assert idx_once != -1
    fn_body = src[idx_once:idx_once + 2000]
    assert "mode" in fn_body
    assert ("dry_run" in fn_body) or ("send" in fn_body)


def test_settings_card_present_in_html():
    html = os.path.join(PROJECT_ROOT, "templates", "index.html")
    src = _read(html)
    # Stage 1 will add a card with id openclawCard
    assert 'id="openclawCard"' in src
    assert "loadOpenclawStatus" in src
