"""Deploy Cloud_RTMPose_Receiver.py to a remote GPU instance via scp/ssh.

Reads SSH credentials from environment (or .api_config.json fallback).
NEVER hardcode the password in source — that previously leaked into the
public git history and the secret was rotated. This file now refuses to
run if CLOUD_SSH_PASSWORD is missing.
"""
from __future__ import absolute_import, print_function

import json
import os
import sys

try:
    import pexpect  # type: ignore
except ImportError:
    print("[deploy_to_cloud] pexpect missing: pip install pexpect", file=sys.stderr)
    sys.exit(2)


def _load_api_config():
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".api_config.json",
    )
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _resolve(env_key, cfg_key, default=""):
    val = os.environ.get(env_key, "")
    if val:
        return val
    cfg = _load_api_config()
    return cfg.get(cfg_key, default) or default


PASSWORD = _resolve("CLOUD_SSH_PASSWORD", "CLOUD_SSH_PASSWORD")
HOST = _resolve("CLOUD_SSH_HOST", "CLOUD_SSH_HOST", "root@connect.westd.seetacloud.com")
PORT = _resolve("CLOUD_SSH_PORT", "CLOUD_SSH_PORT", "42924")

if not PASSWORD:
    print(
        "[deploy_to_cloud] CLOUD_SSH_PASSWORD missing in env and "
        ".api_config.json — refusing to continue.",
        file=sys.stderr,
    )
    sys.exit(1)


def _scp_receiver():
    print("开始上传 Receiver 到云端...")
    child = pexpect.spawn(
        "scp -o StrictHostKeyChecking=no -P {port} "
        "hardware_engine/ai_sensory/Cloud_RTMPose_Receiver.py "
        "{host}:/root/".format(port=PORT, host=HOST)
    )
    idx = child.expect(["assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
    if idx == 0:
        child.sendline(PASSWORD)
        child.expect(pexpect.EOF)
        print("上传完成。")
        return True
    print("上传失败。")
    return False


_SYSTEMD_UNIT = """
cat << 'EOF' > /etc/systemd/system/rtmpose.service
[Unit]
Description=RTMPose UDP Bridge Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=/usr/bin/python3 /root/Cloud_RTMPose_Receiver.py
Restart=on-failure
RestartSec=5s
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=rtmpose-bridge

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable rtmpose
systemctl restart rtmpose
"""


def _install_systemd():
    print("开始配置远端 Systemd 服务...")
    ssh_child = pexpect.spawn(
        "ssh -o StrictHostKeyChecking=no -p {port} {host}".format(port=PORT, host=HOST)
    )
    idx = ssh_child.expect(["assword:", pexpect.EOF, pexpect.TIMEOUT], timeout=15)
    if idx != 0:
        print("远端连接失败。")
        return False
    ssh_child.sendline(PASSWORD)
    ssh_child.expect("#")  # root prompt
    ssh_child.sendline(_SYSTEMD_UNIT)
    ssh_child.expect("#")
    ssh_child.sendline("exit")
    ssh_child.expect(pexpect.EOF)
    print("远端 Systemd 部署并重启成功！")
    return True


if __name__ == "__main__":
    ok = _scp_receiver() and _install_systemd()
    sys.exit(0 if ok else 1)
