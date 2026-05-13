#!/bin/bash
# Restart only the IronBuddy web/control surface.
set -eu

ROOT="${IRONBUDDY_ROOT:-/home/toybrick/streamer_v3}"
cd "$ROOT"

python3 - <<'PY'
import os
import signal
import time

root = os.path.realpath(os.environ.get("IRONBUDDY_ROOT", "/home/toybrick/streamer_v3"))
pids = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        raw = open("/proc/%s/cmdline" % entry, "rb").read()
        argv = [x.decode("utf-8", "ignore") for x in raw.split(b"\0") if x]
        cwd = os.path.realpath(os.readlink("/proc/%s/cwd" % entry))
    except Exception:
        continue
    if cwd == root and any(os.path.basename(arg) == "streamer_app.py" for arg in argv):
        pids.append(int(entry))

for pid in pids:
    try:
        os.kill(pid, signal.SIGTERM)
        print("stopped streamer pid=%s" % pid)
    except OSError:
        pass

deadline = time.time() + 2.0
while time.time() < deadline:
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            alive.append(pid)
        except OSError:
            pass
    if not alive:
        break
    time.sleep(0.2)

for pid in pids:
    try:
        os.kill(pid, 0)
    except OSError:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
        print("force-stopped streamer pid=%s" % pid)
    except OSError:
        pass
PY

nohup python3 -u streamer_app.py > streamer.log 2>&1 < /dev/null &
echo "started streamer pid=$!"
