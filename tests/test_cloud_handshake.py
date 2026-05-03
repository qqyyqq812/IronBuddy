"""Tests for /api/cloud_handshake_status endpoint and helper.

Pure stdlib + source-text checks (streamer_app.py imports cv2/flask/etc.
that don't load in stripped CI env).
"""
import json
import os
import sys
import importlib.util


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMER = os.path.join(PROJECT_ROOT, "streamer_app.py")
INDEX_HTML = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _load_helper(name):
    """Load a single function from streamer_app.py into an isolated module
    that doesn't trigger the heavy module-level imports.
    """
    src = _read(STREAMER)
    # Extract the helper function source (must exist, see test below)
    marker = "def " + name + "("
    start = src.find(marker)
    assert start != -1, name + " not found in streamer_app.py"
    # Grab the function definition until the next top-level def/class/route
    end = len(src)
    for off, line in enumerate(src[start:].splitlines(keepends=True)):
        if off == 0:
            continue
        if line and (line.startswith("def ") or line.startswith("class ")
                     or line.startswith("@app.route(")):
            end = start + sum(len(l) for l in src[start:].splitlines(keepends=True)[:off])
            break
    snippet = "import os\nimport json\n" + src[start:end]
    mod = type(sys)("_handshake_helper")
    exec(compile(snippet, "<handshake_helper>", "exec"), mod.__dict__)
    return getattr(mod, name)


# ---------- Source-text checks ----------

def test_endpoint_route_present():
    src = _read(STREAMER)
    assert "@app.route('/api/cloud_handshake_status')" in src


def test_helper_function_present():
    src = _read(STREAMER)
    assert "def _read_cloud_handshake_status(" in src


def test_endpoint_uses_env_var_for_path():
    """Override path via IRONBUDDY_CLOUD_STATUS_PATH (used by tests)."""
    src = _read(STREAMER)
    assert "IRONBUDDY_CLOUD_STATUS_PATH" in src


def test_default_status_path_is_shm():
    src = _read(STREAMER)
    assert "/dev/shm/cloud_rtmpose_status.json" in src


# ---------- Helper logic checks ----------

def test_helper_returns_payload(tmp_path):
    helper = _load_helper("_read_cloud_handshake_status")
    p = tmp_path / "cloud_rtmpose_status.json"
    p.write_text(json.dumps({
        "phase": "ready",
        "ts": 1.0,
        "detail": "first frame ok",
        "backend": "cloud",
    }))
    data = helper(str(p))
    assert data["ok"] is True
    assert data["phase"] == "ready"
    assert data["backend"] == "cloud"


def test_helper_missing_file_returns_unknown(tmp_path):
    helper = _load_helper("_read_cloud_handshake_status")
    data = helper(str(tmp_path / "nope.json"))
    assert data["ok"] is True
    assert data["phase"] == "unknown"


def test_helper_failed_phase(tmp_path):
    helper = _load_helper("_read_cloud_handshake_status")
    p = tmp_path / "cloud_rtmpose_status.json"
    p.write_text(json.dumps({
        "phase": "failed",
        "ts": 2.0,
        "detail": "3 consecutive errors",
        "backend": "cloud",
    }))
    data = helper(str(p))
    assert data["phase"] == "failed"
    assert data["detail"] == "3 consecutive errors"


def test_helper_handles_corrupt_json(tmp_path):
    helper = _load_helper("_read_cloud_handshake_status")
    p = tmp_path / "cloud_rtmpose_status.json"
    p.write_text("{not json")
    data = helper(str(p))
    assert data["ok"] is False
