"""Shared MVC calibration helpers for EMG runtime and future UI/API flows.

The module is intentionally small and Python 3.7 compatible because it is
imported by board-side code.
"""

from __future__ import absolute_import, division, print_function

import json
import os
import time


DEFAULT_MVC = {"target": 400.0, "comp": 400.0}
MVC_MIN = 50.0
MVC_MAX = 2000.0


def _to_float(value, default):
    try:
        value = float(value)
        if value != value:
            return default
        return value
    except Exception:
        return default


def _clamp_mvc(value, default=400.0):
    value = _to_float(value, default)
    if value < MVC_MIN:
        return MVC_MIN
    if value > MVC_MAX:
        return MVC_MAX
    return value


def values_from_payload(payload):
    """Return normalized target/comp values from old or schema-v2 payloads."""
    payload = payload if isinstance(payload, dict) else {}
    nested = payload.get("mvc_values") if isinstance(payload.get("mvc_values"), dict) else {}
    peak = payload.get("peak_mvc") if isinstance(payload.get("peak_mvc"), dict) else {}
    target = (
        nested.get("target")
        if "target" in nested else
        payload.get("target")
        if "target" in payload else
        peak.get("ch0")
    )
    comp = (
        nested.get("comp")
        if "comp" in nested else
        payload.get("comp")
        if "comp" in payload else
        peak.get("ch1")
    )
    return {
        "target": _clamp_mvc(target, DEFAULT_MVC["target"]),
        "comp": _clamp_mvc(comp, DEFAULT_MVC["comp"]),
    }


def load_mvc_values(path):
    """Load MVC values, accepting legacy {target, comp} and schema v2."""
    try:
        with open(path, "r") as f:
            payload = json.load(f)
        return values_from_payload(payload), payload
    except Exception:
        return dict(DEFAULT_MVC), {}


def build_payload(target, comp, user_id="unknown", exercise="unknown",
                  source="udp_emg_server", ts=None, std_pct=None):
    ts = time.time() if ts is None else float(ts)
    target = _clamp_mvc(target, DEFAULT_MVC["target"])
    comp = _clamp_mvc(comp, DEFAULT_MVC["comp"])
    safe_user = str(user_id or "unknown").replace(":", "_")
    safe_exercise = str(exercise or "unknown").replace(":", "_")
    calibration_id = "%s:%s:%s" % (
        safe_user,
        safe_exercise,
        time.strftime("%Y%m%dT%H%M%S", time.localtime(ts)),
    )
    return {
        "schema_version": 2,
        "user_id": safe_user,
        "exercise": safe_exercise,
        "protocol": "SENIAM-2000",
        "peak_mvc": {"ch0": target, "ch1": comp},
        "mvc_values": {"target": target, "comp": comp},
        "target": target,
        "comp": comp,
        "std_pct": std_pct or {},
        "source": source,
        "calibration_id": calibration_id,
        "ts": ts,
    }


def atomic_write_json(path, payload):
    path = os.path.abspath(path)
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True)
    os.rename(tmp, path)
