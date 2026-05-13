#!/usr/bin/env python3
"""IronBuddy Lane B readiness checker.

Offline-first checklist for ESP32 UDP, Sensor Lab, locks, and optional board
probes. It avoids secrets and performs only read-only checks.
"""

from __future__ import print_function

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BOARD_IP = os.environ.get("IRONBUDDY_BOARD_IP", "10.29.10.224")
DEFAULT_SKETCH = Path("/mnt/c/arduino_work/WiFiUDPClient/WiFiUDPClient.ino")
CURRENT = ROOT / "docs" / "test_runs" / "ironbuddy_operator" / "CURRENT.md"
SENSOR_LAB = ROOT / "tools" / "ironbuddy_sensor_lab.py"
RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_sensor_lab"
BOARD_KEY = os.path.expanduser("~/.ssh/id_rsa_toybrick")
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, level, key, message):
        self.rows.append((level, key, message))

    def counts(self):
        return {
            "OK": sum(1 for r in self.rows if r[0] == "OK"),
            "WARN": sum(1 for r in self.rows if r[0] == "WARN"),
            "FAIL": sum(1 for r in self.rows if r[0] == "FAIL"),
            "INFO": sum(1 for r in self.rows if r[0] == "INFO"),
        }

    def print(self):
        for level, key, message in self.rows:
            print("[%s] %s: %s" % (level, key, message))
        c = self.counts()
        print("summary: OK=%d WARN=%d FAIL=%d INFO=%d" % (
            c["OK"], c["WARN"], c["FAIL"], c["INFO"]))
        if c["FAIL"]:
            print("status: NOT READY - fix FAIL items before live acceptance")
            return 1
        if c["WARN"]:
            print("status: READY WITH WARNINGS - acceptable if WARN items match the offline/hotspot state")
            return 0
        print("status: READY")
        return 0


def read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def check_sketch(report, sketch, board_ip):
    if not sketch.exists():
        report.add("WARN", "arduino_sketch", "skipped; sketch not found at %s" % sketch)
        return
    src = read_text(sketch)
    report.add("OK" if "#include <WiFiUdp.h>" in src or "WiFiUDP" in src else "FAIL",
               "wifi_udp_include", "WiFiUDP sketch detected" if "WiFiUDP" in src else "WiFiUDP include missing")
    baud = re.search(r"Serial\.begin\((\d+)\)", src)
    if baud and baud.group(1) == "9600":
        report.add("OK", "serial_baud", "Serial Monitor baud is 9600")
    else:
        report.add("WARN", "serial_baud", "Serial.begin is %r, expected 9600" % (baud.group(1) if baud else None))
    if board_ip in src:
        report.add("OK", "udp_address", "UDP target matches board IP %s" % board_ip)
    else:
        report.add("WARN", "udp_address", "sketch does not visibly target board IP %s" % board_ip)
    for gpio in ("GPIO0", "GPIO2", "GPIO12", "GPIO15", "EN", "RST"):
        if gpio in src:
            report.add("WARN", "boot_pin", "%s may affect boot/download; recheck wiring" % gpio)


def parse_current_lock(report):
    src = read_text(CURRENT)
    if not src:
        report.add("WARN", "board_lock", "CURRENT.md not found")
        return
    m = re.search(r"\|\s*`lock_owner`\s*\|\s*`?([^|` ]+)", src)
    owner = m.group(1).strip() if m else "unknown"
    if owner == "free":
        report.add("OK", "board_lock", "CURRENT.md lock is free")
    else:
        report.add("WARN", "board_lock", "CURRENT.md lock_owner is %r" % owner)


def check_sensor_lab_parse(report):
    try:
        ast.parse(read_text(SENSOR_LAB), filename=str(SENSOR_LAB))
        report.add("OK", "sensor_lab_parse", "tools/ironbuddy_sensor_lab.py parses")
    except Exception as exc:
        report.add("FAIL", "sensor_lab_parse", str(exc))


def latest_run_health(report, board_ip):
    runs = sorted([p for p in RUNS_ROOT.glob("20*") if p.is_dir()])
    if not runs:
        report.add("WARN", "historical_run", "No Sensor Lab run found")
        return
    state_path = runs[-1] / "state.json"
    data = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    run_board = data.get("board_ip")
    if run_board == board_ip:
        report.add("OK", "run_board_ip", "run board_ip matches %s" % board_ip)
    elif run_board:
        report.add("WARN", "run_board_ip", "run board_ip is %s" % run_board)
    else:
        report.add("INFO", "run_board_ip", "latest run has no board_ip snapshot")
    h = data.get("health", {})
    if h.get("real_emg"):
        report.add("OK", "historical_real_emg", "latest run saw real_emg=true")
    else:
        report.add("WARN", "historical_real_emg", "latest run did not end with real_emg=true")
    if h.get("udp_online"):
        report.add("OK", "historical_udp", "latest run saw udp_online=true")
    else:
        report.add("WARN", "historical_udp", "latest run did not end with udp_online=true")
    if h.get("transport_ok") is True:
        report.add("OK", "historical_transport_ok", "latest run saw transport_ok=true")
    elif "transport_ok" in h:
        report.add("WARN", "historical_transport_ok", "latest run transport_ok=false")
    else:
        report.add("INFO", "historical_transport_ok", "latest run predates transport_ok")
    if h.get("valid_for_gru") is True:
        report.add("OK", "historical_valid_for_gru", "latest run gate valid_for_gru=true")
    elif "valid_for_gru" in h:
        report.add("WARN", "historical_valid_for_gru", "latest run gate valid_for_gru=false (%s)" % h.get("signal_mode"))
    else:
        report.add("INFO", "historical_valid_for_gru", "latest run predates valid_for_gru")


def http_json(board_ip, path):
    raw = NO_PROXY_OPENER.open(
        "http://%s:5000%s" % (board_ip, path),
        timeout=4,
    ).read()
    return json.loads(raw.decode("utf-8", "replace"))


def probe_board(report, board_ip):
    for key, path in [
        ("fsm_state", "/api/fsm_state"),
        ("muscle_activation", "/api/muscle_activation"),
        ("inference_mode", "/api/inference_mode"),
    ]:
        try:
            body = http_json(board_ip, path)
            report.add("OK", key, "HTTP ok (%s)" % (body.get("exercise") or body.get("mode") or "json"))
        except Exception as exc:
            report.add("WARN", key, "HTTP probe failed: %s" % exc)
    cmd = [
        "ssh", "-i", BOARD_KEY,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=4",
        "toybrick@%s" % board_ip,
        "pgrep -af '[s]treamer_app|[m]ain_claw_loop|[u]dp_emg_server|[v]oice_daemon|[c]loud_rtmpose_client' || true; "
        "test -e /dev/shm/emg_heartbeat && echo heartbeat || true; "
        "test -e /dev/shm/emg_raw_waveform.json && echo raw_waveform || true; "
        "test -e /dev/shm/emg_debug_snapshot.json && echo emg_debug || true",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
        if "udp_emg_server.py" in out.stdout:
            report.add("OK", "board_processes", "udp/main services visible")
        else:
            report.add("WARN", "board_processes", "core process list incomplete")
        if "heartbeat" in out.stdout:
            report.add("OK", "heartbeat", "/dev/shm/emg_heartbeat exists")
        else:
            report.add("WARN", "heartbeat", "/dev/shm/emg_heartbeat missing")
        if "raw_waveform" in out.stdout and "emg_debug" in out.stdout:
            report.add("OK", "transport_ok_probe", "raw waveform and debug snapshot exist")
        else:
            report.add("WARN", "transport_ok_probe", "raw waveform/debug snapshot missing")
    except Exception as exc:
        report.add("WARN", "board_probe", "SSH probe failed: %s" % exc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="IronBuddy Lane B readiness")
    parser.add_argument("--board-ip", default=DEFAULT_BOARD_IP)
    parser.add_argument("--sketch", default=str(DEFAULT_SKETCH))
    parser.add_argument("--probe-board", action="store_true")
    args = parser.parse_args(argv)
    report = Report()
    report.add("INFO", "board_ip", args.board_ip)
    check_sketch(report, Path(args.sketch), args.board_ip)
    parse_current_lock(report)
    check_sensor_lab_parse(report)
    latest_run_health(report, args.board_ip)
    if args.probe_board:
        probe_board(report, args.board_ip)
    else:
        report.add("INFO", "probe_board", "skipped; pass --probe-board after hotspot is online")
    return report.print()


if __name__ == "__main__":
    sys.exit(main())
