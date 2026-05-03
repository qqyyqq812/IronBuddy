"""Tests for cloud_rtmpose_client.py handshake state writer.

cloud_rtmpose_client.py imports cv2/numpy/requests so we extract the helper
function via text-load (same pattern as test_cloud_handshake.py).
"""
import json
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(PROJECT_ROOT, "hardware_engine", "ai_sensory",
                      "cloud_rtmpose_client.py")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def _load_helper(name):
    src = _read(CLIENT)
    marker = "def " + name + "("
    start = src.find(marker)
    assert start != -1, name + " not found"
    end = len(src)
    lines = src[start:].splitlines(keepends=True)
    for off, line in enumerate(lines):
        if off == 0:
            continue
        if line and (line.startswith("def ") or line.startswith("class ")):
            end = start + sum(len(l) for l in lines[:off])
            break
    snippet = "import os\nimport json\nimport time\n" + src[start:end]
    mod = type(sys)("_status_writer")
    mod.__dict__["SHM_CLOUD_STATUS"] = "/tmp/_test_cloud_status.json"
    exec(compile(snippet, "<status_writer>", "exec"), mod.__dict__)
    sys.modules["_status_writer"] = mod
    return getattr(mod, name)


# ---------- Source-text checks ----------

def test_helper_function_present():
    src = _read(CLIENT)
    assert "def _write_cloud_status(" in src


def test_shm_path_constant_present():
    src = _read(CLIENT)
    assert "SHM_CLOUD_STATUS" in src
    assert "/dev/shm/cloud_rtmpose_status.json" in src


def test_writes_connecting_on_cloud_switch():
    """When user switches to cloud, we write phase=connecting."""
    src = _read(CLIENT)
    # one of the call sites must include connecting + cloud
    assert '_write_cloud_status("connecting"' in src or \
           "_write_cloud_status('connecting'" in src


def test_writes_ready_on_first_frame():
    src = _read(CLIENT)
    assert '_write_cloud_status("ready"' in src or \
           "_write_cloud_status('ready'" in src


def test_writes_failed_on_consecutive_errors():
    """Allow either inline or multi-line call format."""
    src = _read(CLIENT)
    assert '_write_cloud_status("failed"' in src or \
           "_write_cloud_status('failed'" in src or \
           ('"failed"' in src and "_consecutive_cloud_fail" in src)


# ---------- Helper logic checks ----------

def test_writer_atomic(tmp_path, monkeypatch):
    helper = _load_helper("_write_cloud_status")
    target = tmp_path / "cloud_rtmpose_status.json"
    monkeypatch.setattr(sys.modules[helper.__module__], "SHM_CLOUD_STATUS",
                        str(target), raising=False)
    helper("ready", "first frame ok", "cloud")
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["phase"] == "ready"
    assert data["detail"] == "first frame ok"
    assert data["backend"] == "cloud"
    assert "ts" in data and isinstance(data["ts"], float)


def test_writer_default_backend(tmp_path):
    helper = _load_helper("_write_cloud_status")
    mod = sys.modules[helper.__module__]
    target = tmp_path / "cloud_rtmpose_status.json"
    mod.SHM_CLOUD_STATUS = str(target)
    helper("connecting", "switching")
    data = json.loads(target.read_text())
    assert data["backend"] == "cloud"  # default


def test_writer_swallows_exceptions(tmp_path):
    """Writer must never raise (called from inference hot-path)."""
    helper = _load_helper("_write_cloud_status")
    mod = sys.modules[helper.__module__]
    mod.SHM_CLOUD_STATUS = "/nonexistent/dir/cannot/write/here.json"
    helper("ready")  # must not raise
