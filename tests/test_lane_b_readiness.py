"""Source checks for Lane B readiness CLI."""

import os
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "ironbuddy_lane_b_readiness.py")


def _read():
    with open(TOOL, "r", encoding="utf-8") as f:
        return f.read()


def test_readiness_tool_has_offline_and_probe_modes():
    src = _read()
    assert "--probe-board" in src
    assert "status: READY" in src
    assert "status: READY WITH WARNINGS" in src
    assert "status: NOT READY" in src


def test_readiness_checks_sensor_lab_and_lock():
    src = _read()
    assert "tools/ironbuddy_sensor_lab.py" in src
    assert "CURRENT.md" in src
    assert "lock_owner" in src
    assert "ProxyHandler({})" in src
    assert "transport_ok" in src
    assert "valid_for_gru" in src
    assert "transport_ok_probe" in src


def test_readiness_help_runs():
    out = subprocess.run(
        [sys.executable, TOOL, "--help"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    assert out.returncode == 0
    assert "--board-ip" in out.stdout
