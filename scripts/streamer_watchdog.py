#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Keep the IronBuddy web/control surface alive.

This script is deliberately independent from ``streamer_app.py``.  If the web
process is killed by an operator action, deploy script, or board reboot, this
watchdog can bring back the Flask control surface so the UI can start the
training services again.

Python 3.7 compatible; stdlib only.
"""

from __future__ import absolute_import

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

try:
    from urllib.request import urlopen
except ImportError:  # pragma: no cover - Python 2 fallback, kept harmless.
    from urllib2 import urlopen


DEFAULT_ROOT = "/home/toybrick/streamer_v3"
DEFAULT_PORT = 5000
DEFAULT_LOCK = "/tmp/ironbuddy_streamer_watchdog.lock"
DEFAULT_STATUS = "/tmp/ironbuddy_streamer_watchdog_status.json"


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _read_cmdline(pid):
    try:
        raw = open("/proc/%s/cmdline" % pid, "rb").read()
    except Exception:
        return []
    return [x.decode("utf-8", "ignore") for x in raw.split(b"\0") if x]


def _read_cwd(pid):
    try:
        return os.path.realpath(os.readlink("/proc/%s/cwd" % pid))
    except Exception:
        return ""


def _streamer_pids(root):
    """Return PIDs for streamer_app.py under the target project root."""
    root = os.path.realpath(root)
    pids = []
    try:
        entries = os.listdir("/proc")
    except Exception:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        argv = _read_cmdline(entry)
        if not argv:
            continue
        has_streamer = any(os.path.basename(arg) == "streamer_app.py" for arg in argv)
        if not has_streamer:
            continue
        cwd = _read_cwd(entry)
        if cwd and os.path.realpath(cwd) != root:
            continue
        pids.append(int(entry))
    return sorted(pids)


def _http_ok(port, timeout):
    url = "http://127.0.0.1:%d/api/fsm_state" % int(port)
    try:
        resp = urlopen(url, timeout=float(timeout))
        try:
            return 200 <= int(resp.getcode()) < 500, ""
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception as exc:
        return False, str(exc)


def _write_status(path, payload):
    payload = dict(payload)
    payload["updated_ts"] = time.time()
    payload["updated_at"] = _now()
    try:
        tmp = path + ".tmp.%s" % os.getpid()
        with open(tmp, "w") as fh:
            json.dump(payload, fh, sort_keys=True)
        os.rename(tmp, path)
    except Exception:
        pass


def _start_streamer(root, python_bin):
    log_path = os.path.join(root, "streamer.log")
    log = open(log_path, "ab", 0)
    proc = subprocess.Popen(
        [python_bin, "-u", "streamer_app.py"],
        cwd=root,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=open(os.devnull, "rb"),
        close_fds=True,
        preexec_fn=os.setsid,
    )
    return proc.pid


def _stop_streamer(root, grace_s):
    pids = _streamer_pids(root)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + float(grace_s)
    while time.time() < deadline:
        if not _streamer_pids(root):
            return pids
        time.sleep(0.2)
    for pid in _streamer_pids(root):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def check_once(root, port, python_bin, timeout=2.0, restart_unhealthy=False, status_path=DEFAULT_STATUS):
    pids = _streamer_pids(root)
    http_ok, http_error = _http_ok(port, timeout) if pids else (False, "no streamer process")
    action = "healthy"
    started_pid = None
    stopped = []
    if not pids:
        started_pid = _start_streamer(root, python_bin)
        action = "started_missing"
        time.sleep(1.0)
        pids = _streamer_pids(root)
        http_ok, http_error = _http_ok(port, timeout)
    elif restart_unhealthy and not http_ok:
        stopped = _stop_streamer(root, grace_s=2.0)
        started_pid = _start_streamer(root, python_bin)
        action = "restarted_unhealthy"
        time.sleep(1.0)
        pids = _streamer_pids(root)
        http_ok, http_error = _http_ok(port, timeout)
    status = {
        "ok": bool(pids),
        "action": action,
        "root": root,
        "port": int(port),
        "pids": pids,
        "started_pid": started_pid,
        "stopped_pids": stopped,
        "http_ok": bool(http_ok),
        "http_error": http_error,
    }
    _write_status(status_path, status)
    return status


def _acquire_lock(lock_path):
    fh = open(lock_path, "w")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main(argv=None):
    parser = argparse.ArgumentParser(description="IronBuddy streamer watchdog")
    parser.add_argument("--root", default=os.environ.get("IRONBUDDY_ROOT", DEFAULT_ROOT))
    parser.add_argument("--port", type=int, default=int(os.environ.get("IRONBUDDY_STREAMER_PORT", DEFAULT_PORT)))
    parser.add_argument("--python", default=os.environ.get("PYTHON", "python3"))
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--max-failures", type=int, default=3)
    parser.add_argument(
        "--restart-unhealthy",
        action="store_true",
        help="restart an existing streamer after repeated HTTP failures; "
             "default is process-missing recovery only",
    )
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lock", default=DEFAULT_LOCK)
    parser.add_argument("--status", default=DEFAULT_STATUS)
    args = parser.parse_args(argv)

    root = os.path.realpath(args.root)
    if not os.path.isdir(root):
        print(json.dumps({"ok": False, "error": "root not found", "root": root}))
        return 2
    if not os.path.exists(os.path.join(root, "streamer_app.py")):
        print(json.dumps({"ok": False, "error": "streamer_app.py not found", "root": root}))
        return 2

    if not args.loop:
        status = check_once(
            root, args.port, args.python, timeout=args.timeout,
            restart_unhealthy=bool(args.once), status_path=args.status)
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        return 0 if status.get("ok") else 1

    try:
        lock_fh = _acquire_lock(args.lock)
    except IOError:
        print(json.dumps({"ok": False, "error": "watchdog already running", "lock": args.lock}))
        return 0

    failures = 0
    print("%s streamer watchdog loop start root=%s port=%s" % (_now(), root, args.port))
    while True:
        status = check_once(
            root, args.port, args.python, timeout=args.timeout,
            restart_unhealthy=(
                bool(args.restart_unhealthy) and
                failures >= max(1, int(args.max_failures))
            ),
            status_path=args.status)
        if status.get("http_ok"):
            failures = 0
        else:
            failures += 1
        status["consecutive_failures"] = failures
        _write_status(args.status, status)
        print("%s %s pids=%s http_ok=%s failures=%s" % (
            _now(), status.get("action"), status.get("pids"),
            status.get("http_ok"), failures))
        sys.stdout.flush()
        time.sleep(max(1.0, float(args.interval)))

    lock_fh.close()  # Unreachable, keeps linters calm.
    return 0


if __name__ == "__main__":
    sys.exit(main())
