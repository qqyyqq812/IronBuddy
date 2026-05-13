#!/usr/bin/env python3
"""Start cloned IronBuddy GPU services through the configured SSH endpoint.

The GPU image is expected to contain:
- /root/ironbuddy_cloud/rtmpose_http_server.py on port 6006
- /root/ironbuddy_rag/start_qdrant.sh on port 6333
- /root/ironbuddy_rag/start_embedding.sh on port 8008

This script never prints the SSH password.
"""
import json
import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", ".api_config.json")


def _load_config():
    with open(CONFIG, "r") as f:
        return json.load(f)


def _remote_command():
    return r"""
set -eu
export PATH=/root/miniconda3/bin:$PATH

if ! curl -fsS -m 3 http://127.0.0.1:6006/health >/dev/null 2>&1; then
  pkill -f '[r]tmpose_http_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_cloud
  nohup python rtmpose_http_server.py >> server.log 2>&1 < /dev/null &
  echo $! > server.pid
fi

if ! curl -fsS -m 3 http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
  pkill -f '[q]drant --http-port 6333|[q]drant_compat_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_rag
  nohup bash start_qdrant.sh > qdrant.log 2>&1 < /dev/null &
  echo $! > qdrant.pid
fi

if ! curl -fsS -m 3 http://127.0.0.1:8008/health >/dev/null 2>&1; then
  pkill -f '[e]mbedding_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_rag
  nohup bash start_embedding.sh > embedding.log 2>&1 < /dev/null &
  echo $! > embedding.pid
fi

sleep 2
printf 'rtmpose='
curl -fsS -m 5 http://127.0.0.1:6006/health >/dev/null 2>&1 && echo ready || echo starting
printf 'qdrant='
curl -fsS -m 5 http://127.0.0.1:6333/healthz >/dev/null 2>&1 && echo ready || echo starting
printf 'embedding='
curl -fsS -m 5 http://127.0.0.1:8008/health >/dev/null 2>&1 && echo ready || echo starting
"""


def main():
    cfg = _load_config()
    host = cfg.get("CLOUD_SSH_HOST", "")
    port = cfg.get("CLOUD_SSH_PORT", 22)
    user = cfg.get("CLOUD_SSH_USER", "root")
    password = cfg.get("CLOUD_SSH_PASSWORD", "")
    if not host or not password:
        print("[cloud_gpu_bootstrap] missing credentials", file=sys.stderr)
        return 1
    try:
        import pexpect
    except ImportError:
        fallback = os.path.join(HERE, "cloud_gpu_bootstrap.sh")
        if os.path.exists(fallback):
            proc = subprocess.run(["bash", fallback], cwd=os.path.dirname(HERE))
            return proc.returncode
        print("[cloud_gpu_bootstrap] pexpect not installed", file=sys.stderr)
        return 1

    cmd = (
        "ssh -p {p} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ControlMaster=no -o ControlPath=none "
        "-o ConnectTimeout=10 {u}@{h} bash -s"
    ).format(p=port, u=user, h=host)
    child = pexpect.spawn("/bin/bash", ["-lc", cmd], encoding="utf-8", timeout=60)
    try:
        while True:
            i = child.expect(["[Pp]assword:", "yes/no", pexpect.EOF, pexpect.TIMEOUT],
                             timeout=12)
            if i == 1:
                child.sendline("yes")
                continue
            if i == 0:
                child.sendline(password)
            elif i == 2:
                output = (child.before or "").replace(password, "PASSWORD_REDACTED")
                print(output.strip())
                return child.exitstatus if child.exitstatus is not None else 2
            # Password auth has completed, or public-key auth left remote bash
            # waiting on stdin. Send the script through stdin instead of
            # embedding it in a shell argument.
            break
        child.send(_remote_command())
        if not _remote_command().endswith("\n"):
            child.send("\n")
        child.sendeof()
        child.expect(pexpect.EOF, timeout=80)
        output = (child.before or "").replace(password, "PASSWORD_REDACTED")
        print(output.strip())
        return child.exitstatus if child.exitstatus is not None else 0
    finally:
        try:
            child.close(force=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
