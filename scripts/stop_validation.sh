#!/usr/bin/env bash
# IronBuddy training-service stop.
#
# Stops only the four training processes. The web/control surface stays alive
# so the UI can start a clean recording take again.

set -euo pipefail

BOARD_IP="${IRONBUDDY_BOARD_IP:-10.29.10.224}"
BOARD_USER="${IRONBUDDY_BOARD_USER:-toybrick}"
BOARD_KEY="${IRONBUDDY_BOARD_KEY:-$HOME/.ssh/id_rsa_toybrick}"

SSH_OPTS=(
  -i "$BOARD_KEY"
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=8
)

echo "[1/2] stopping training services on ${BOARD_USER}@${BOARD_IP}..."
ssh "${SSH_OPTS[@]}" "${BOARD_USER}@${BOARD_IP}" 'python3 - <<'"'"'PY'"'"'
import os
import signal
import time

SIGNATURES = (
    "cloud_rtmpose_client.py",
    "main_claw_loop.py",
    "udp_emg_server.py",
    "voice_daemon.py",
)


def pids_for(sig):
    out = []
    base = os.path.basename(sig)
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/%s/cmdline" % entry, "rb").read()
        except Exception:
            continue
        argv = [x.decode("utf-8", "ignore") for x in raw.split(b"\0") if x]
        if any(os.path.basename(arg) == base for arg in argv):
            out.append(int(entry))
    return out


targets = []
for sig in SIGNATURES:
    targets.extend(pids_for(sig))

for pid in sorted(set(targets)):
    try:
        os.kill(pid, signal.SIGTERM)
        print("term pid=%s" % pid)
    except OSError:
        pass

deadline = time.time() + 2.0
while time.time() < deadline:
    if not any(pids_for(sig) for sig in SIGNATURES):
        break
    time.sleep(0.2)

for sig in SIGNATURES:
    for pid in pids_for(sig):
        try:
            os.kill(pid, signal.SIGKILL)
            print("kill pid=%s sig=%s" % (pid, sig))
        except OSError:
            pass

for name in ("arecord", "aplay"):
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            raw = open("/proc/%s/comm" % entry, "r").read().strip()
        except Exception:
            continue
        if raw == name:
            try:
                os.kill(int(entry), signal.SIGKILL)
            except OSError:
                pass

print("training services stopped; web control surface preserved")
PY'

echo "[2/2] closing optional local cloud tunnel..."
if [ -f /tmp/ironbuddy_tunnel.pid ]; then
  kill "$(cat /tmp/ironbuddy_tunnel.pid)" 2>/dev/null || true
  rm -f /tmp/ironbuddy_tunnel.pid
fi

echo "IronBuddy training services stopped. Web: http://${BOARD_IP}:5000/"
