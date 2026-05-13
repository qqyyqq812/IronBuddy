"""Source contract for cloud RTMPose tunnel auto-recovery."""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMER = os.path.join(PROJECT_ROOT, "streamer_app.py")


def _read_streamer():
    with open(STREAMER, "r", encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    marker = "def " + name + "("
    start = src.find(marker)
    assert start >= 0, name + " not found"
    end = src.find("\n@app.route", start + 1)
    if end < 0:
        end = src.find("\ndef ", start + 1)
    if end < 0:
        end = len(src)
    return src[start:end]


def test_cloud_tunnel_helpers_exist_and_report_failure_segments():
    src = _read_streamer()
    assert "def _probe_cloud_health(" in src
    assert "def _ensure_cloud_tunnel(" in src
    assert "tunnel_down" in src
    assert "cloud_health_failed" in src
    assert "vision_worker_failed" in src


def test_switch_to_cloud_ensures_tunnel_before_reporting_ready():
    src = _read_streamer()
    body = _function_body(src, "api_switch_vision")
    ensure_idx = body.find("_ensure_cloud_tunnel(")
    status_idx = body.find("_write_cloud_switch_status(")
    assert ensure_idx >= 0
    assert status_idx >= 0
    assert ensure_idx < status_idx
    assert '"cloud"' in body
    assert "return Response(json.dumps({" in body


def test_admin_start_all_best_effort_ensures_tunnel():
    body = _function_body(_read_streamer(), "admin_start")
    assert "_ensure_cloud_tunnel(blocking=False)" in body
    assert "cloud_tunnel" in body


def test_cloud_gpu_connect_endpoint_parses_ssh_and_masks_password():
    src = _read_streamer()
    assert "def _parse_cloud_ssh_command" in src
    assert "shlex.split" in src
    assert "@app.route('/api/admin/cloud_gpu/connect'" in src
    body = _function_body(src, "admin_cloud_gpu_connect")
    worker = _function_body(src, "_cloud_gpu_reconnect_worker")
    assert "CLOUD_SSH_HOST" in body
    assert "CLOUD_SSH_PORT" in body
    assert "CLOUD_SSH_USER" in body
    assert "CLOUD_SSH_PASSWORD" in body
    assert "_ensure_cloud_tunnel(blocking=True)" in worker
    assert "_ensure_rag_tunnel(blocking=True)" in worker
    assert "cloud_gpu_bootstrap.py" in worker
    assert "threading.Thread(target=_cloud_gpu_reconnect_worker)" in body
    assert '"reconnect_started": True' in body
    assert "password_configured" in src
    assert "_cloud_gpu_public_config(cfg)" in body
    assert '_pick_config(cfg, "CLOUD_SSH_PASSWORD")' in body
    assert '"CLOUD_SSH_PASSWORD": password' not in body


def test_rag_tunnel_has_python_fallback():
    path = os.path.join(PROJECT_ROOT, "scripts", "rag_tunnel.sh")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "rag_tunnel.py" in src
    assert "python3 -c \"import pexpect\"" in src


def test_cloud_gpu_bootstrap_starts_cloned_services_without_printing_password():
    path = os.path.join(PROJECT_ROOT, "scripts", "cloud_gpu_bootstrap.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    shell_path = os.path.join(PROJECT_ROOT, "scripts", "cloud_gpu_bootstrap.sh")
    with open(shell_path, "r", encoding="utf-8") as f:
        shell_src = f.read()
    assert "/root/ironbuddy_cloud" in src
    assert "/root/ironbuddy_rag" in src
    assert "rtmpose_http_server.py" in src
    assert "start_qdrant.sh" in src
    assert "start_embedding.sh" in src
    assert "PASSWORD_REDACTED" in src
    assert "cloud_gpu_bootstrap.sh" in src
    assert "expect -f" in shell_src
    assert "PASSWORD_REDACTED" in shell_src
    assert "/root/ironbuddy_cloud" in shell_src
    assert "/root/ironbuddy_rag" in shell_src
