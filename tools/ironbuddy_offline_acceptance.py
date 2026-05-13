#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IronBuddy offline acceptance smoke.

This is a local, non-destructive helper for demo prep.  It checks the API
surfaces that the operator console asks the user to verify, while keeping
Feishu/OpenClaw sends in dry-run mode unless the caller explicitly changes the
target service itself.
"""

from __future__ import absolute_import

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BASE_URL = os.environ.get("IRONBUDDY_BASE_URL", "http://127.0.0.1:5000")


def _request_json(base_url, path, method="GET", payload=None, timeout=5):
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Cache-Control": "no-cache"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"raw": raw[:500]}
        return {
            "ok": True,
            "path": path,
            "status": "ok",
            "elapsed_ms": int((time.time() - started) * 1000),
            "body": body,
        }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "path": path,
            "status": "http_%s" % exc.code,
            "error": raw[:500],
        }
    except Exception as exc:
        return {
            "ok": False,
            "path": path,
            "status": "error",
            "error": str(exc)[:500],
        }


def _run_openclaw_dry_run():
    script = os.path.join(ROOT, "scripts", "opencloud_reminder_daemon.py")
    if not os.path.exists(script):
        return {"ok": False, "error": "opencloud_reminder_daemon.py missing"}
    try:
        proc = subprocess.run(
            [sys.executable, script, "--once", "--mode", "weekly", "--dry-run"],
            cwd=ROOT,
            capture_output=True,
            timeout=25,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        body = {}
        try:
            body = json.loads(stdout)
        except Exception:
            body = {"stdout_tail": stdout[-1000:]}
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "body": body,
            "stderr_tail": stderr[-500:],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def run_acceptance(base_url):
    checks = [
        _request_json(base_url, "/api/coach/capabilities"),
        _request_json(
            base_url,
            "/api/coach/rag_query",
            method="POST",
            payload={"query": "膝盖不舒服怎么办", "limit": 3, "dry_run": True},
        ),
        _request_json(
            base_url,
            "/api/feishu/card_push",
            method="POST",
            payload={
                "type": "summary",
                "text": "IronBuddy offline acceptance dry-run",
                "dry_run": True,
            },
        ),
        _request_json(base_url, "/api/opencloud/status"),
        _request_json(base_url, "/api/openclaw/status"),
    ]
    daemon = _run_openclaw_dry_run()
    ok = all(item.get("ok") for item in checks) and bool(daemon.get("ok"))
    return {
        "ok": ok,
        "base_url": base_url,
        "checks": checks,
        "openclaw_dry_run": daemon,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="IronBuddy offline acceptance smoke")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    result = run_acceptance(args.base_url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
