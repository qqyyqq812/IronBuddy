#!/usr/bin/env python3
"""IronBuddy Lane B Sensor Lab.

Local-only validation console for filtered EMG, GRU labels, and vision+sensor
acceptance. Raw ADC is viewed through docs/hardware_ref/freq.py during live
hardware checks.
"""

from __future__ import print_function

import argparse
import csv
import json
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, request

try:
    from tools.ironbuddy_lane_b_emg_preprocess import (
        DEFAULT_EMG_VIEW,
        PREPROCESS_VERSION,
        build_stream_view_rows,
        load_runtime_preprocess_meta,
        summarize_stream_views,
    )
except Exception:
    from ironbuddy_lane_b_emg_preprocess import (  # type: ignore
        DEFAULT_EMG_VIEW,
        PREPROCESS_VERSION,
        build_stream_view_rows,
        load_runtime_preprocess_meta,
        summarize_stream_views,
    )


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_sensor_lab"
DEFAULT_BOARD_IP = os.environ.get("IRONBUDDY_BOARD_IP", "10.29.10.224")
BOARD_KEY = os.path.expanduser("~/.ssh/id_rsa_toybrick")
REMOTE_ROOT = "/home/toybrick/streamer_v3"
REMOTE_BICEP_MODEL = REMOTE_ROOT + "/hardware_engine/extreme_fusion_gru_bicep.pt"
REMOTE_RUNTIME_PREPROCESS = REMOTE_ROOT + "/hardware_engine/sensor/lane_b_runtime_preprocess.json"
CURRENT = ROOT / "docs" / "test_runs" / "ironbuddy_operator" / "CURRENT.md"
SCOPE_WINDOW_S = 1.0
STREAM_TARGET_FPS = 60
RAW_RING_LIMIT = 1000
FILTERED_RING_LIMIT = 1000
GROUP_MAX_STREAM_ROWS = 30000
VISION_CAPTURE_INTERVAL_S = 0.15
VISION_MAX_SAMPLES = 10000
GRU_7D_MAX_SAMPLES = 10000
NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DISPLAY_MVC_FALLBACK = 400.0
PERSONAL_DATASET_ROOTS = {
    "bicep_curl": ROOT / "data" / "bicep_curl_personal",
    "squat": ROOT / "data" / "squat_personal",
}
PERSONAL_DATASET_ROOT_LABELS = {
    "bicep_curl": "data/bicep_curl_personal",
    "squat": "data/squat_personal",
}
PERSONAL_EXPORT_TOOLS = {
    "bicep_curl": ROOT / "tools" / "ironbuddy_export_personal_bicep_gru_dataset.py",
    "squat": ROOT / "tools" / "ironbuddy_export_personal_squat_gru_dataset.py",
}
PERSONAL_TRAIN_TOOLS = {
    "bicep_curl": ROOT / "tools" / "train_gru_three_class_bicep_personal.py",
    "squat": ROOT / "tools" / "train_gru_three_class_squat_personal.py",
}
CUSTOM_ACTION_ROOT = ROOT / "data" / "custom_actions"

LABELS = ("standard", "compensating", "non_standard")
BUILTIN_EXERCISES = ("squat", "bicep_curl")
SIGNAL_MODES = ("udp_missing", "floating_no_contact", "contact_rest_candidate", "active_candidate")
LABEL_CN = {
    "standard": "标准",
    "compensating": "代偿",
    "non_standard": "不标准",
    "unknown": "未知",
}
SOURCE_CN = {
    "gru": "GRU",
    "visual": "视觉",
    "visual_fallback_no_emg": "fallback: EMG无效",
    "visual_fallback_no_model": "fallback: 模型未加载",
    "visual_fallback_no_window": "fallback: 窗口不足",
    "visual_fallback_model_error": "fallback: 模型异常",
}
SOURCE_DEBUG_HINT = {
    "gru": "GRU已参与，本rep可计入准确率",
    "visual_fallback_no_emg": "EMG输入链路不过关，先查贴皮、rail/jump和raw age",
    "visual_fallback_no_model": "弯举GRU权重未加载，先查模型文件和main loop日志",
    "visual_fallback_no_window": "动作前有效窗口不足，先贴皮静息3-5秒再开始",
    "visual_fallback_model_error": "模型推理异常，先查main loop日志",
    "visual": "当前是纯视觉或GRU未启用，不计入GRU准确率",
}
REFERENCE_FILES = {
    "standard": ROOT / "data" / "v42" / "user_03" / "curl" / "standard" / "rep_001.csv",
    "compensating": ROOT / "data" / "v42" / "user_03" / "curl" / "compensation" / "rep_001.csv",
    "non_standard": ROOT / "data" / "v42" / "user_03" / "curl" / "bad_form" / "rep_001.csv",
}
OLD_BICEP_LABEL_DIRS = {
    "standard": ROOT / "data" / "bicep_curl" / "golden",
    "compensating": ROOT / "data" / "bicep_curl" / "bad",
    "non_standard": ROOT / "data" / "bicep_curl" / "lazy",
}
OLD_BICEP_AUG_DIRS = {
    "standard": ROOT / "data" / "bicep_curl_augmented" / "golden",
    "compensating": ROOT / "data" / "bicep_curl_augmented" / "bad",
    "non_standard": ROOT / "data" / "bicep_curl_augmented" / "lazy",
}
V42_LABEL_DIRS = {
    "standard": ROOT / "data" / "v42" / "user_03" / "curl" / "standard",
    "compensating": ROOT / "data" / "v42" / "user_03" / "curl" / "compensation",
    "non_standard": ROOT / "data" / "v42" / "user_03" / "curl" / "bad_form",
}
GRU_7D_COLUMNS = [
    "Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
    "Symmetry_Score", "Phase_Progress",
]


def _now():
    return time.time()


def _json_default(obj):
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def _safe_rel_path(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=_json_default),
                   encoding="utf-8")
    tmp.replace(path)


def _safe_json(text, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(text)
    except Exception:
        return default


def current_lock_owner():
    try:
        src = CURRENT.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "unknown"
    m = re.search(r"\|\s*`lock_owner`\s*\|\s*`?([^|` ]+)", src)
    return m.group(1).strip() if m else "unknown"


def _to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _age_s(now, ts):
    ts = _to_float(ts)
    if ts is None:
        return None
    return round(max(0.0, now - ts), 3)


def _mean(values):
    values = [float(v) for v in values if v is not None]
    return sum(values) / float(len(values)) if values else None


def _std(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    m = sum(values) / float(len(values))
    return math.sqrt(sum((v - m) ** 2 for v in values) / float(len(values)))


def _rms(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return math.sqrt(sum(v * v for v in values) / float(len(values)))


def _corr(values_a, values_b):
    n = min(len(values_a or []), len(values_b or []))
    if n < 3:
        return None
    a = [float(v) for v in values_a[:n]]
    b = [float(v) for v in values_b[:n]]
    ma = _mean(a)
    mb = _mean(b)
    sa = _std(a)
    sb = _std(b)
    if not sa or not sb:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / float(n * sa * sb)


def _pct(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(round((len(sorted_vals) - 1) * q))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def _value_stats(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return {
            "n": 0,
            "mean": None,
            "p50": None,
            "p95": None,
            "max": None,
            "min": None,
        }
    ordered = sorted(values)
    return {
        "n": len(values),
        "mean": _round_metric(_mean(values)),
        "p50": _round_metric(_pct(ordered, 0.50)),
        "p95": _round_metric(_pct(ordered, 0.95)),
        "max": _round_metric(max(values)),
        "min": _round_metric(min(values)),
    }


def _extract_raw_channels(samples):
    timestamps = []
    channels = [[], []]
    for row in samples or []:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            ts = _to_float(row[0])
            if ts is not None:
                timestamps.append(ts)
            for ch in (0, 1):
                v = _to_float(row[ch + 1])
                if v is not None:
                    channels[ch].append(v)
        elif isinstance(row, dict):
            ts = _to_float(row.get("ts") or row.get("time"))
            if ts is not None:
                timestamps.append(ts)
            for ch in (0, 1):
                v = _to_float(row.get("ch%d" % ch, row.get(str(ch))))
                if v is not None:
                    channels[ch].append(v)
    return timestamps, channels


def raw_wave_stats(samples):
    timestamps, channels = _extract_raw_channels(samples)
    time_stats = {
        "span_s": None,
        "rate_hz": None,
        "dt_p50_ms": None,
        "dt_p95_ms": None,
        "dt_max_ms": None,
        "burst_ratio": None,
    }
    if len(timestamps) >= 2:
        dts = [max(0.0, timestamps[i] - timestamps[i - 1])
               for i in range(1, len(timestamps))]
        dts_sorted = sorted(dts)
        span_s = max(0.0, timestamps[-1] - timestamps[0])
        time_stats.update({
            "span_s": round(span_s, 3),
            "rate_hz": round((len(timestamps) - 1) / span_s, 1) if span_s > 0 else None,
            "dt_p50_ms": round((_pct(dts_sorted, 0.50) or 0.0) * 1000.0, 2),
            "dt_p95_ms": round((_pct(dts_sorted, 0.95) or 0.0) * 1000.0, 2),
            "dt_max_ms": round(max(dts) * 1000.0, 2) if dts else None,
            "burst_ratio": round(sum(1 for d in dts if d < 0.001) / float(len(dts)), 3) if dts else None,
        })

    channel_stats = []
    for vals in channels:
        if not vals:
            channel_stats.append({
                "n": 0,
                "min": None,
                "p05": None,
                "p50": None,
                "p95": None,
                "max": None,
                "mean": None,
                "std": None,
                "zero_ratio": None,
                "low_ratio": None,
                "high_ratio": None,
                "railish_ratio": None,
                "mean_abs_jump": None,
            })
            continue
        ordered = sorted(vals)
        n = len(vals)
        mean = sum(vals) / float(n)
        std = math.sqrt(sum((x - mean) ** 2 for x in vals) / float(n))
        jumps = [abs(vals[i] - vals[i - 1]) for i in range(1, n)]
        channel_stats.append({
            "n": n,
            "min": round(min(vals), 3),
            "p05": round(_pct(ordered, 0.05), 3),
            "p50": round(_pct(ordered, 0.50), 3),
            "p95": round(_pct(ordered, 0.95), 3),
            "max": round(max(vals), 3),
            "mean": round(mean, 3),
            "std": round(std, 3),
            "zero_ratio": round(sum(1 for x in vals if x <= 5) / float(n), 3),
            "low_ratio": round(sum(1 for x in vals if x <= 100) / float(n), 3),
            "high_ratio": round(sum(1 for x in vals if x >= 3500) / float(n), 3),
            "railish_ratio": round(sum(1 for x in vals if x <= 100 or x >= 3500) / float(n), 3),
            "mean_abs_jump": round(sum(jumps) / float(len(jumps)), 3) if jumps else 0.0,
        })

    return {
        "time": time_stats,
        "channels": channel_stats,
    }


def stream_wave_stats(samples):
    raw_rows = []
    filtered_rows = []
    for row in samples or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        raw_rows.append([row[0], row[1], row[2]])
        filtered_rows.append([row[0], row[3], row[4]])
    stats = raw_wave_stats(raw_rows)
    filtered_channels = [[], []]
    for row in filtered_rows:
        for ch in (0, 1):
            val = _to_float(row[ch + 1])
            if val is not None:
                filtered_channels[ch].append(val)
    filtered_stats = []
    for vals in filtered_channels:
        if not vals:
            filtered_stats.append({"min": None, "max": None, "span": None})
            continue
        filtered_stats.append({
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "span": round(max(vals) - min(vals), 3),
        })
    stats["filtered_channels"] = filtered_stats
    return stats


def _stream_col(samples, idx):
    vals = []
    for row in samples or []:
        if isinstance(row, (list, tuple)) and len(row) > idx:
            val = _to_float(row[idx])
            if val is not None:
                vals.append(val)
    return vals


def _saturation_ratio(values, threshold=100.0):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return round(sum(1 for v in values if v >= threshold) / float(len(values)), 3)


def emg_raw_summary(stream_samples):
    """Summarize raw ADC evidence without MVC/domain remapping."""
    raw_stats = stream_wave_stats(stream_samples)
    channels = []
    for ch in (0, 1):
        raw = _stream_col(stream_samples, 1 + ch)
        filtered = _stream_col(stream_samples, 3 + ch)
        rms_vals = _stream_col(stream_samples, 5 + ch)
        centered = []
        raw_mean = _mean(raw)
        if raw_mean is not None:
            centered = [v - raw_mean for v in raw]
        gate_stats = {}
        try:
            gate_stats = raw_stats.get("channels", [])[ch] or {}
        except Exception:
            gate_stats = {}
        channels.append({
            "channel": ch,
            "name": "target_ch0" if ch == 0 else "comp_ch1",
            "raw_adc": _value_stats(raw),
            "raw_centered_rms": _round_metric(_rms(centered)),
            "raw_centered_rms_norm_2048": _round_metric((_rms(centered) or 0.0) / 2048.0),
            "filtered_rms": _round_metric(_rms(filtered)),
            "rms": _value_stats(rms_vals),
            "railish_ratio": gate_stats.get("railish_ratio"),
            "mean_abs_jump": gate_stats.get("mean_abs_jump"),
        })
    return {
        "ok": bool(stream_samples),
        "samples": len(stream_samples or []),
        "time": raw_stats.get("time", {}),
        "channels": channels,
        "raw_corr_01": _round_metric(_corr(_stream_col(stream_samples, 1), _stream_col(stream_samples, 2))),
        "filtered_corr_01": _round_metric(_corr(_stream_col(stream_samples, 3), _stream_col(stream_samples, 4))),
    }


def emg_mapping_summary(stream_samples):
    """Compare old training /400 pct with current pct after MVC/domain mapping."""
    channels = []
    suspected = False
    for ch in (0, 1):
        rms_vals = _stream_col(stream_samples, 5 + ch)
        current_pct = _stream_col(stream_samples, 7 + ch)
        old_pct_400 = [
            max(0.0, min(100.0, (v / DISPLAY_MVC_FALLBACK) * 100.0))
            for v in rms_vals
        ]
        old_sat = _saturation_ratio(old_pct_400)
        current_sat = _saturation_ratio(current_pct)
        old_mean = _mean(old_pct_400) or 0.0
        current_mean = _mean(current_pct) or 0.0
        ch_suspected = bool(
            current_pct and (
                (current_sat or 0.0) - (old_sat or 0.0) >= 0.15 or
                current_mean - old_mean >= 20.0
            )
        )
        suspected = suspected or ch_suspected
        channels.append({
            "channel": ch,
            "name": "target_ch0" if ch == 0 else "comp_ch1",
            "old_pct_400": _value_stats(old_pct_400),
            "old_pct_400_sat100_ratio": old_sat,
            "current_pct": _value_stats(current_pct),
            "current_pct_sat100_ratio": current_sat,
            "mvc_or_domain_saturation_suspected": ch_suspected,
        })
    return {
        "ok": bool(stream_samples),
        "samples": len(stream_samples or []),
        "mvc_fallback_denominator": DISPLAY_MVC_FALLBACK,
        "current_pct_note": "current pct may include MVC values and domain_calibration mapping before clipping",
        "mvc_or_domain_saturation_suspected": suspected,
        "channels": channels,
    }


def emg_preprocess_summary(stream_samples):
    meta = load_runtime_preprocess_meta()
    views = summarize_stream_views(stream_samples, preprocess_meta=meta)
    rows = build_stream_view_rows(stream_samples, preprocess_meta=meta)
    return {
        "ok": bool(stream_samples),
        "preprocess_version": PREPROCESS_VERSION,
        "default_training_view": DEFAULT_EMG_VIEW,
        "mvc_source": meta.get("mvc_source"),
        "mvc_values": meta.get("mvc_values"),
        "mvc_valid": meta.get("mvc_valid"),
        "domain_method": meta.get("domain_method"),
        "domain_params": meta.get("domain_params"),
        "stream_view_row_count": len(rows),
        "view_summary": views,
    }


def filtered_display_rows(samples, limit=1000):
    rows = []
    for row in samples or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        rows.append([
            float(_to_float(row[0], 0.0) or 0.0),
            float(_to_float(row[3], 0.0) or 0.0),
            float(_to_float(row[4], 0.0) or 0.0),
            float(_to_float(row[5], 0.0) or 0.0),
            float(_to_float(row[6], 0.0) or 0.0),
            float(_to_float(row[7], 0.0) or 0.0),
            float(_to_float(row[8], 0.0) or 0.0),
            float(_to_float(row[9], 0.0) or 0.0) if len(row) >= 10 else 0.0,
        ])
    return rows[-int(limit):]


def fallback_filtered_rows(raw_samples, debug=None):
    debug = debug if isinstance(debug, dict) else {}
    filtered = debug.get("filtered") if isinstance(debug.get("filtered"), list) else [0.0, 0.0]
    rms = debug.get("rms") if isinstance(debug.get("rms"), list) else [0.0, 0.0]
    pct = debug.get("pct") if isinstance(debug.get("pct"), list) else [0.0, 0.0]
    packet_base = _safe_int(debug.get("packet_count"), 0)
    rows = []
    for i, row in enumerate(raw_samples or []):
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        ts = float(_to_float(row[0], 0.0) or 0.0)
        # The old board snapshot only contains raw ADC. Use the latest debug
        # filtered/RMS values as a visible status fallback until stream_buffer is deployed.
        rows.append([
            ts,
            float(_to_float(filtered[0] if len(filtered) > 0 else 0.0, 0.0) or 0.0),
            float(_to_float(filtered[1] if len(filtered) > 1 else 0.0, 0.0) or 0.0),
            float(_to_float(rms[0] if len(rms) > 0 else 0.0, 0.0) or 0.0),
            float(_to_float(rms[1] if len(rms) > 1 else 0.0, 0.0) or 0.0),
            float(_to_float(pct[0] if len(pct) > 0 else 0.0, 0.0) or 0.0),
            float(_to_float(pct[1] if len(pct) > 1 else 0.0, 0.0) or 0.0),
            float(packet_base + i),
        ])
    return rows[-FILTERED_RING_LIMIT:]


def fallback_stream_rows(raw_samples, debug=None):
    rows = []
    filtered_rows = fallback_filtered_rows(raw_samples, debug)
    raw_rows = [
        row for row in (raw_samples or [])
        if isinstance(row, (list, tuple)) and len(row) >= 3
    ]
    for i, row in enumerate(filtered_rows):
        raw = raw_rows[i] if i < len(raw_rows) else [row[0], 0.0, 0.0]
        rows.append([
            row[0],
            float(_to_float(raw[1], 0.0) or 0.0),
            float(_to_float(raw[2], 0.0) or 0.0),
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        ])
    return rows


def signal_gate(transport_ok, debug=None, raw_stats=None, simulated=False):
    debug = debug if isinstance(debug, dict) else {}
    raw_stats = raw_stats if isinstance(raw_stats, dict) else {}
    if not transport_ok:
        return {
            "transport_ok": False,
            "signal_mode": "udp_missing",
            "valid_for_gru": False,
            "reason": "udp_missing_or_stale",
            "railish_max": None,
            "mean_abs_jump_max": None,
            "pct_saturated": False,
        }

    pct_vals = debug.get("pct") if isinstance(debug.get("pct"), list) else []
    if not pct_vals:
        pct_vals = [debug.get("target_pct"), debug.get("comp_pct")]
    pct_nums = [_to_float(v, 0.0) for v in pct_vals[:2]]
    while len(pct_nums) < 2:
        pct_nums.append(0.0)

    channel_stats = raw_stats.get("channels") if isinstance(raw_stats, dict) else []
    railish_vals = []
    jump_vals = []
    if isinstance(channel_stats, list):
        for ch in channel_stats[:2]:
            if isinstance(ch, dict):
                railish_vals.append(_to_float(ch.get("railish_ratio"), 0.0) or 0.0)
                jump_vals.append(_to_float(ch.get("mean_abs_jump"), 0.0) or 0.0)
    railish_max = max(railish_vals) if railish_vals else 0.0
    jump_max = max(jump_vals) if jump_vals else 0.0
    pct_saturated = all(v >= 99.0 for v in pct_nums[:2])
    has_raw_gate_stats = bool(railish_vals or jump_vals)
    floating = pct_saturated and (
        not has_raw_gate_stats or railish_max > 0.25 or jump_max > 300.0
    )

    if floating:
        mode = "floating_no_contact"
        reason = (
            "pct_saturated_without_raw_stats"
            if not has_raw_gate_stats else "pct_saturated_with_rail_or_jump"
        )
    elif max(pct_nums) >= 20.0:
        mode = "active_candidate"
        reason = "fresh_signal_with_activation"
    else:
        mode = "contact_rest_candidate"
        reason = "fresh_low_activation"

    return {
        "transport_ok": True,
        "signal_mode": mode,
        "valid_for_gru": bool(not simulated and mode in ("contact_rest_candidate", "active_candidate")),
        "reason": reason,
        "railish_max": round(railish_max, 3),
        "mean_abs_jump_max": round(jump_max, 3),
        "pct_saturated": pct_saturated,
    }


def load_reference_waveforms():
    labels = {}
    for label, path in REFERENCE_FILES.items():
        rows = []
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    target = _to_float(row.get("Target_RMS_Norm"))
                    comp = _to_float(row.get("Comp_RMS_Norm"))
                    raw = _to_float(row.get("Target_Raw_Unfilt"))
                    angle = _to_float(row.get("Angle"))
                    phase = _to_float(row.get("Phase_Progress"))
                    if target is None:
                        target = (_to_float(row.get("Target_RMS"), 0.0) or 0.0) / 100.0
                    if comp is None:
                        comp = (_to_float(row.get("Comp_RMS"), 0.0) or 0.0) / 100.0
                    rows.append([
                        phase if phase is not None else len(rows) / 200.0,
                        max(0.0, min(1.0, target or 0.0)),
                        max(0.0, min(1.0, comp or 0.0)),
                        max(0.0, min(1.0, raw or 0.0)),
                        max(0.0, min(1.0, (angle or 0.0) / 180.0)),
                    ])
        except Exception:
            rows = []
        labels[label] = {
            "path": str(path.relative_to(ROOT)),
            "rows": rows,
            "samples": len(rows),
        }
    return {"ok": True, "source": "data/v42/user_03/curl/*/rep_001.csv", "labels": labels}


def _clean_kpts(kpts):
    cleaned = []
    for pt in (kpts or [])[:17]:
        if not isinstance(pt, (list, tuple)) or len(pt) < 3:
            cleaned.append([0.0, 0.0, 0.0])
            continue
        cleaned.append([
            _round_metric(_to_float(pt[0], 0.0), 3),
            _round_metric(_to_float(pt[1], 0.0), 3),
            _round_metric(_to_float(pt[2], 0.0), 4),
        ])
    return cleaned


def _kpt_conf_values(sample):
    out = []
    objects = sample.get("objects") if isinstance(sample, dict) else []
    if not isinstance(objects, list):
        return out
    for obj in objects[:1]:
        if not isinstance(obj, dict):
            continue
        for pt in obj.get("kpts") or []:
            if isinstance(pt, (list, tuple)) and len(pt) >= 3:
                conf = _to_float(pt[2])
                if conf is not None:
                    out.append(conf)
    return out


def _bbox_from_kpts(kpts, conf_min=0.05):
    pts = [
        pt for pt in (kpts or [])
        if isinstance(pt, (list, tuple)) and len(pt) >= 3 and (_to_float(pt[2], 0.0) or 0.0) > conf_min
    ]
    if len(pts) < 2:
        return {}
    xs = [_to_float(pt[0], 0.0) or 0.0 for pt in pts]
    ys = [_to_float(pt[1], 0.0) or 0.0 for pt in pts]
    return {
        "x_min": _round_metric(min(xs), 2),
        "y_min": _round_metric(min(ys), 2),
        "x_max": _round_metric(max(xs), 2),
        "y_max": _round_metric(max(ys), 2),
        "w": _round_metric(max(xs) - min(xs), 2),
        "h": _round_metric(max(ys) - min(ys), 2),
    }


def sanitize_pose_sample(pose_data, capture_ts=None, remote_now=None):
    capture_ts = capture_ts or _now()
    pose_data = pose_data if isinstance(pose_data, dict) else {}
    objects = []
    for obj in pose_data.get("objects") or []:
        if not isinstance(obj, dict):
            continue
        kpts = _clean_kpts(obj.get("kpts") or [])
        objects.append({
            "score": _round_metric(obj.get("score")),
            "kpt_count": len(kpts),
            "kpts": kpts,
            "bbox": _bbox_from_kpts(kpts),
        })
    pose_ts = _to_float(pose_data.get("timestamp"))
    sample = {
        "capture_ts": capture_ts,
        "pose_timestamp": pose_ts,
        "age_s": _age_s(remote_now or capture_ts, pose_ts),
        "frame_idx": pose_data.get("frame_idx"),
        "source": pose_data.get("source") or pose_data.get("mode") or "pose_data",
        "valid_person": bool(objects),
        "objects": objects,
    }
    confs = _kpt_conf_values(sample)
    sample["kpt_conf_mean"] = _round_metric(_mean(confs))
    sample["low_conf_ratio"] = (
        round(sum(1 for c in confs if c < 0.10) / float(len(confs)), 3)
        if confs else None
    )
    return sample


def sanitize_angle_debug(angle_debug, capture_ts=None, remote_now=None):
    capture_ts = capture_ts or _now()
    angle_debug = angle_debug if isinstance(angle_debug, dict) else {}
    out = dict(angle_debug)
    out["capture_ts"] = capture_ts
    out["age_s"] = _age_s(remote_now or capture_ts, angle_debug.get("ts"))
    return out


def pose_sample_key(sample):
    if not isinstance(sample, dict):
        return None
    frame_idx = sample.get("frame_idx")
    pose_ts = sample.get("pose_timestamp")
    if frame_idx is not None:
        return ("idx", frame_idx, round(_to_float(pose_ts, 0.0) or 0.0, 4))
    if pose_ts is not None:
        return ("ts", round(_to_float(pose_ts, 0.0) or 0.0, 4))
    return None


def angle_debug_key(snapshot):
    if not isinstance(snapshot, dict):
        return None
    ts = snapshot.get("ts")
    raw = snapshot.get("raw_angle")
    if ts is not None:
        return ("ts", round(_to_float(ts, 0.0) or 0.0, 4), raw)
    return None


def summarize_vision_evidence(pose_samples, angle_debug_snapshots, fsm_snapshots=None):
    pose_samples = [s for s in (pose_samples or []) if isinstance(s, dict)]
    angle_debug_snapshots = [s for s in (angle_debug_snapshots or []) if isinstance(s, dict)]
    fsm_snapshots = [s for s in (fsm_snapshots or []) if isinstance(s, dict)]
    timestamps = [
        _to_float(s.get("pose_timestamp") or s.get("capture_ts"))
        for s in pose_samples
        if _to_float(s.get("pose_timestamp") or s.get("capture_ts")) is not None
    ]
    rate_hz = None
    if len(timestamps) >= 2 and max(timestamps) > min(timestamps):
        rate_hz = (len(timestamps) - 1) / (max(timestamps) - min(timestamps))
    confs = []
    bbox_w = []
    bbox_h = []
    source_counts = {}
    for sample in pose_samples:
        source = sample.get("source") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        confs.extend(_kpt_conf_values(sample))
        objects = sample.get("objects") or []
        if objects:
            bbox = objects[0].get("bbox") or {}
            if bbox.get("w") is not None:
                bbox_w.append(float(bbox.get("w")))
            if bbox.get("h") is not None:
                bbox_h.append(float(bbox.get("h")))
    selected_side_counts = {}
    for snap in angle_debug_snapshots:
        side = snap.get("selected_side") or snap.get("active_side")
        if side:
            selected_side_counts[side] = selected_side_counts.get(side, 0) + 1
    drop_reasons = {}
    for snap in fsm_snapshots:
        fsm = snap.get("fsm") if isinstance(snap.get("fsm"), dict) else {}
        reason = fsm.get("last_drop_reason")
        if reason:
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
    for snap in angle_debug_snapshots:
        reason = snap.get("drop_reason")
        if reason:
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
    valid_count = sum(1 for s in pose_samples if s.get("valid_person"))
    return {
        "ok": bool(pose_samples or angle_debug_snapshots),
        "pose_sample_count": len(pose_samples),
        "angle_debug_count": len(angle_debug_snapshots),
        "pose_rate_hz": _round_metric(rate_hz),
        "valid_person_ratio": _round_metric(_safe_div(valid_count, len(pose_samples))),
        "kpt_conf_mean": _round_metric(_mean(confs)),
        "low_conf_ratio": (
            round(sum(1 for c in confs if c < 0.10) / float(len(confs)), 3)
            if confs else None
        ),
        "bbox_w_mean": _round_metric(_mean(bbox_w)),
        "bbox_h_mean": _round_metric(_mean(bbox_h)),
        "source_counts": source_counts,
        "selected_side_counts": selected_side_counts,
        "drop_reasons": drop_reasons,
    }


def old_bicep_training_compare():
    def read_dir(path):
        target = []
        comp = []
        files = sorted(Path(path).glob("*.csv")) if Path(path).is_dir() else []
        for fp in files:
            try:
                with fp.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        t = _to_float(row.get("Target_RMS"))
                        c = _to_float(row.get("Comp_RMS"))
                        if t is not None:
                            target.append(t)
                        if c is not None:
                            comp.append(c)
            except Exception:
                continue
        return {
            "path": str(Path(path).relative_to(ROOT)) if Path(path).exists() else str(path),
            "files": len(files),
            "samples": max(len(target), len(comp)),
            "Target_RMS": _value_stats(target),
            "Comp_RMS": _value_stats(comp),
        }
    labels = {}
    for label in LABELS:
        labels[label] = {
            "base": read_dir(OLD_BICEP_LABEL_DIRS[label]),
            "augmented": read_dir(OLD_BICEP_AUG_DIRS[label]),
        }
    return {
        "ok": True,
        "source": "data/bicep_curl/{golden,bad,lazy}",
        "features": [
            "Ang_Vel", "Angle", "Ang_Accel", "Target_RMS",
            "Comp_RMS", "Symmetry_Score", "Phase_Progress",
        ],
        "raw_adc_status": "raw_adc_not_available_in_old_training_csv",
        "labels": labels,
    }


def v42_reference_compare(label, stream_samples):
    current_raw = emg_raw_summary(stream_samples)
    current_ch0 = None
    try:
        current_ch0 = current_raw["channels"][0].get("raw_centered_rms_norm_2048")
    except Exception:
        current_ch0 = None
    refs = {}
    for ref_label, ref_dir in V42_LABEL_DIRS.items():
        vals = []
        files = sorted(Path(ref_dir).glob("rep_*.csv")) if Path(ref_dir).is_dir() else []
        files = [fp for fp in files if "_aug" not in fp.stem]
        for fp in files:
            try:
                with fp.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        raw = _to_float(row.get("Target_Raw_Unfilt"))
                        if raw is not None:
                            vals.append(raw)
            except Exception:
                continue
        refs[ref_label] = {
            "path": str(Path(ref_dir).relative_to(ROOT)) if Path(ref_dir).exists() else str(ref_dir),
            "files": len(files),
            "Target_Raw_Unfilt": _value_stats(vals),
        }
    selected_ref = refs.get(label, {}).get("Target_Raw_Unfilt", {})
    ref_mean = selected_ref.get("mean")
    return {
        "ok": bool(refs),
        "source": "data/v42/user_03/curl/*",
        "current_ch0_raw_centered_rms_norm_2048": current_ch0,
        "selected_label": label,
        "selected_label_ref_mean": ref_mean,
        "selected_label_delta": _round_metric(
            (current_ch0 or 0.0) - (ref_mean or 0.0)
        ) if current_ch0 is not None and ref_mean is not None else None,
        "reference_by_label": refs,
    }


def build_training_compare(label, stream_samples):
    return {
        "old_bicep_training_compare": old_bicep_training_compare(),
        "v42_reference_compare": v42_reference_compare(label, stream_samples),
    }


def _gru_7d_values(sample):
    sample = sample if isinstance(sample, dict) else {}
    values = sample.get("values")
    if isinstance(values, list) and len(values) >= len(GRU_7D_COLUMNS):
        return [_to_float(v) for v in values[:len(GRU_7D_COLUMNS)]]
    features = sample.get("features") if isinstance(sample.get("features"), dict) else {}
    vals = [_to_float(features.get(col)) for col in GRU_7D_COLUMNS]
    return vals if any(v is not None for v in vals) else []


def sanitize_gru_7d_sample(sample):
    sample = sample if isinstance(sample, dict) else {}
    values = _gru_7d_values(sample)
    if not values:
        return {}
    clean_values = [float(v if v is not None else 0.0) for v in values]
    ts = _to_float(sample.get("ts"), _now())
    return {
        "ts": ts,
        "exercise": sample.get("exercise"),
        "inference_mode": sample.get("inference_mode"),
        "fsm_state": sample.get("fsm_state"),
        "rep_count": _safe_int(sample.get("rep_count"), 0),
        "columns": list(GRU_7D_COLUMNS),
        "values": clean_values,
        "features": dict(zip(GRU_7D_COLUMNS, clean_values)),
    }


def gru_7d_sample_key(sample):
    if not isinstance(sample, dict):
        return None
    ts = _to_float(sample.get("ts"))
    if ts is None:
        return None
    return ("ts", round(ts, 4), _safe_int(sample.get("rep_count"), 0))


def gru_window_key(window):
    if not isinstance(window, dict):
        return None
    rep = _safe_int(window.get("rep_index"), 0)
    ts = _to_float(window.get("ts"))
    if rep or ts is not None:
        return ("rep", rep, round(ts or 0.0, 3))
    return None


def summarize_gru_7d(samples, windows=None):
    samples = [s for s in (samples or []) if isinstance(s, dict)]
    values_by_feature = {col: [] for col in GRU_7D_COLUMNS}
    for sample in samples:
        values = _gru_7d_values(sample)
        if not values:
            continue
        for idx, col in enumerate(GRU_7D_COLUMNS):
            if idx < len(values) and values[idx] is not None:
                values_by_feature[col].append(values[idx])
    return {
        "ok": bool(samples or windows),
        "sample_count": len(samples),
        "last_window_count": len(windows or []),
        "columns": list(GRU_7D_COLUMNS),
        "stats": {col: _value_stats(vals) for col, vals in values_by_feature.items()},
        "source": "/dev/shm/gru_7d_buffer.json + /dev/shm/gru_last_window.json",
    }


def group_feature_points(groups):
    points = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        summary = group.get("gru_7d_summary") if isinstance(group.get("gru_7d_summary"), dict) else {}
        stats = summary.get("stats") if isinstance(summary.get("stats"), dict) else {}
        target = stats.get("Target_RMS") if isinstance(stats.get("Target_RMS"), dict) else {}
        comp = stats.get("Comp_RMS") if isinstance(stats.get("Comp_RMS"), dict) else {}
        angle = stats.get("Angle") if isinstance(stats.get("Angle"), dict) else {}
        points.append({
            "label": group.get("label"),
            "label_cn": group.get("label_cn") or LABEL_CN.get(group.get("label"), group.get("label")),
            "group_id": group.get("group_id"),
            "target_mean": target.get("mean"),
            "comp_mean": comp.get("mean"),
            "angle_min": angle.get("min"),
            "sample_count": summary.get("sample_count"),
            "save_path": group.get("save_path"),
        })
    return points


def run_local_command(args, timeout=90):
    try:
        proc = subprocess.run(
            args,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-6000:],
            "stderr": proc.stderr[-6000:],
            "cmd": " ".join(str(a) for a in args),
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "cmd": " ".join(str(a) for a in args),
        }


def latest_dataset_dir(exercise):
    exercise = "squat" if exercise == "squat" else "bicep_curl"
    root = PERSONAL_DATASET_ROOTS[exercise] / "datasets"
    if not root.is_dir():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "personal_dataset_manifest.json").exists()]
    return sorted(candidates)[-1] if candidates else None


def latest_candidate_model(exercise):
    exercise = "squat" if exercise == "squat" else "bicep_curl"
    root = PERSONAL_DATASET_ROOTS[exercise] / "training_runs"
    if not root.is_dir():
        return None
    pattern = "candidate_extreme_fusion_gru.pt" if exercise == "squat" else "candidate_extreme_fusion_gru_bicep.pt"
    candidates = sorted(root.glob("*/%s" % pattern))
    return candidates[-1] if candidates else None


def action_slug(value):
    clean = str(value or "").strip()
    if not clean:
        return "custom_action"
    ascii_slug = re.sub(r"[^a-z0-9]+", "_", clean.lower()).strip("_")
    if ascii_slug:
        return ascii_slug[:80]
    encoded = "_".join(("%x" % ord(ch)) for ch in clean)
    return ("custom_" + encoded)[:80]


def _read_json_file(path):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _table_rows(table_body):
    if not isinstance(table_body, dict):
        return []
    columns = table_body.get("columns") or []
    rows = table_body.get("rows") or []
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, list):
            out.append(dict(zip(columns, row)))
    return out


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _safe_div(num, den):
    try:
        den = float(den)
        if den == 0.0:
            return None
        return float(num) / den
    except Exception:
        return None


def _resample(values, count=80):
    values = [float(v) for v in values if v is not None]
    if not values:
        return []
    if len(values) == 1:
        return [values[0]] * count
    out = []
    last = len(values) - 1
    for i in range(count):
        pos = (float(i) / float(max(1, count - 1))) * last
        lo = int(math.floor(pos))
        hi = min(last, lo + 1)
        frac = pos - lo
        out.append(values[lo] * (1.0 - frac) + values[hi] * frac)
    return out


def _series_area(values):
    if not values:
        return 0.0
    return sum(values) / float(len(values))


def _peak_phase(values):
    if not values:
        return None
    idx = max(range(len(values)), key=lambda i: values[i])
    return _safe_div(idx, max(1, len(values) - 1))


def _rmse(values_a, values_b):
    if not values_a or not values_b:
        return None
    n = min(len(values_a), len(values_b))
    if n <= 0:
        return None
    return math.sqrt(sum((values_a[i] - values_b[i]) ** 2 for i in range(n)) / float(n))


def _round_metric(value, digits=3):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def compare_filtered_to_reference(label, samples, references):
    """Return compact distance metrics between live filtered history and v42 reference."""
    ref = ((references or {}).get("labels") or {}).get(label) or {}
    ref_rows = ref.get("rows") or []
    live_rows = [r for r in (samples or []) if isinstance(r, (list, tuple)) and len(r) >= 5]
    if len(live_rows) < 2 or len(ref_rows) < 2:
        return {
            "ok": False,
            "sample_count": len(live_rows),
            "reference_samples": len(ref_rows),
            "reason": "not_enough_waveform",
        }

    live_target = _resample([(_to_float(r[1], 0.0) or 0.0) / 100.0 for r in live_rows])
    live_comp = _resample([(_to_float(r[2], 0.0) or 0.0) / 100.0 for r in live_rows])
    ref_target = _resample([_to_float(r[1], 0.0) or 0.0 for r in ref_rows])
    ref_comp = _resample([_to_float(r[2], 0.0) or 0.0 for r in ref_rows])

    target_rmse = _rmse(live_target, ref_target)
    comp_rmse = _rmse(live_comp, ref_comp)
    avg_rmse = None
    if target_rmse is not None and comp_rmse is not None:
        avg_rmse = (target_rmse + comp_rmse) / 2.0
    similarity = max(0.0, min(1.0, 1.0 - avg_rmse)) if avg_rmse is not None else None

    live_target_peak = max(live_target) if live_target else None
    live_comp_peak = max(live_comp) if live_comp else None
    ref_target_peak = max(ref_target) if ref_target else None
    ref_comp_peak = max(ref_comp) if ref_comp else None
    live_target_phase = _peak_phase(live_target)
    ref_target_phase = _peak_phase(ref_target)

    return {
        "ok": True,
        "label": label,
        "sample_count": len(live_rows),
        "reference_samples": len(ref_rows),
        "similarity": _round_metric(similarity),
        "target_rmse": _round_metric(target_rmse),
        "comp_rmse": _round_metric(comp_rmse),
        "target_peak": _round_metric(live_target_peak),
        "ref_target_peak": _round_metric(ref_target_peak),
        "target_peak_delta": _round_metric((live_target_peak or 0.0) - (ref_target_peak or 0.0)),
        "comp_peak": _round_metric(live_comp_peak),
        "ref_comp_peak": _round_metric(ref_comp_peak),
        "comp_peak_delta": _round_metric((live_comp_peak or 0.0) - (ref_comp_peak or 0.0)),
        "target_area": _round_metric(_series_area(live_target)),
        "ref_target_area": _round_metric(_series_area(ref_target)),
        "target_area_delta": _round_metric(_series_area(live_target) - _series_area(ref_target)),
        "comp_area": _round_metric(_series_area(live_comp)),
        "ref_comp_area": _round_metric(_series_area(ref_comp)),
        "comp_area_delta": _round_metric(_series_area(live_comp) - _series_area(ref_comp)),
        "target_peak_phase_delta": _round_metric(
            (live_target_phase or 0.0) - (ref_target_phase or 0.0)
        ),
    }


def summarize_group(label, reps, curve):
    confusion = {k: {kk: 0 for kk in LABELS + ("unknown",)} for k in LABELS}
    source_counts = {}
    eligible = 0
    correct = 0
    fallback_reasons = {}
    for rep in reps:
        source = rep.get("classification_source") or "unknown"
        pred = rep.get("prediction") or "unknown"
        source_counts[source] = source_counts.get(source, 0) + 1
        if source == "gru":
            eligible += 1
            if pred == label:
                correct += 1
            if label in confusion:
                confusion[label][pred if pred in confusion[label] else "unknown"] += 1
        else:
            fallback_reasons[source] = fallback_reasons.get(source, 0) + 1
    return {
        "label": label,
        "label_cn": LABEL_CN.get(label, label),
        "rep_count": len(reps),
        "gru_rep_count": eligible,
        "correct": correct,
        "accuracy": _round_metric(_safe_div(correct, eligible)),
        "source_counts": source_counts,
        "fallback_reasons": fallback_reasons,
        "confusion": confusion,
        "curve": curve,
        "decision": "可评价GRU" if eligible else "未触发GRU，不评价准确率",
    }


class SensorLabSession(object):
    def __init__(self, board_ip, runs_root):
        self.board_ip = board_ip
        self.board_url = "http://%s:5000" % board_ip
        self.runs_root = Path(runs_root)
        self.run_dir = self.runs_root / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.state_path = self.run_dir / "state.json"
        self.group_index_path = self.run_dir / "session_index.json"
        self.raw_lock = threading.Lock()
        self.stream_lock = threading.Lock()
        self.stream_samples = deque(maxlen=GROUP_MAX_STREAM_ROWS)
        self.raw_cache = {
            "ok": False,
            "samples": [],
            "samples_count": 0,
            "age_s": None,
            "detail": "not loaded",
        }
        self.stream_cache = {
            "ok": False,
            "samples": [],
            "samples_count": 0,
            "age_s": None,
            "detail": "not loaded",
        }
        self.reference_waveforms = load_reference_waveforms()
        self.latest = {}
        self.validation = {
            "active": False,
            "phase": "idle",
            "exercise": "bicep_curl",
            "label": "standard",
            "group_id": None,
            "reps": [],
            "summary": {},
            "last_group_result": {},
            "last_group_save_path": None,
            "capture": {},
            "baseline": {},
        }
        self.recorded_groups = []
        self.recording_status_snapshots = []
        self.recording_fsm_snapshots = []
        self.recording_pose_samples = []
        self.recording_angle_debug_snapshots = []
        self.recording_gru_7d_samples = []
        self.recording_gru_last_windows = []
        self.recording_pose_keys = set()
        self.recording_angle_keys = set()
        self.recording_gru_7d_keys = set()
        self.recording_gru_window_keys = set()
        self.vision_cache = {
            "ok": False,
            "pose_sample": {},
            "angle_debug": {},
            "detail": "not loaded",
        }
        self.gru_7d_cache = {
            "ok": False,
            "samples": [],
            "last_window": {},
            "detail": "not loaded",
        }
        self.group_sequence = 0
        self.lab_session_start_ts = time.time()
        self.write_session_index()
        self.stop_event = threading.Event()
        self._log_event("start", {"board_ip": board_ip, "run_dir": str(self.run_dir)})

    def _log_event(self, event, payload=None):
        row = {"ts": _now(), "event": event, "payload": payload or {}}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")

    def http_json(self, path, method="GET", payload=None, timeout=4):
        url = self.board_url + path
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            raw = NO_PROXY_OPENER.open(req, timeout=timeout).read().decode("utf-8", "replace")
            body = _safe_json(raw, {"raw": raw})
            if isinstance(body, dict):
                body.setdefault("ok_http", True)
            return body
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            body = _safe_json(raw, {"error": raw})
            if isinstance(body, dict):
                body.setdefault("ok_http", False)
                body.setdefault("http_status", exc.code)
            return body
        except Exception as exc:
            return {"ok_http": False, "error": str(exc)}

    def query_rep_events(self, exercise="bicep_curl", limit=120):
        body = self.http_json(
            "/api/db/query/rep_events?exercise=%s&limit=%d" % (exercise, int(limit)),
            timeout=4,
        )
        rows = _table_rows(body)
        rows.sort(key=lambda r: _safe_int(r.get("id"), 0))
        return rows, body

    def latest_rep_id(self, exercise="bicep_curl"):
        rows, _body = self.query_rep_events(exercise=exercise, limit=1)
        ids = [_safe_int(r.get("id"), 0) for r in rows]
        return max(ids) if ids else 0

    def normalize_rep_event(self, row, label):
        source = row.get("classification_source") or "unknown"
        model_class = row.get("model_class")
        visual_result = row.get("visual_result")
        prediction = model_class if source == "gru" and model_class else (model_class or visual_result or "unknown")
        return {
            "id": _safe_int(row.get("id"), 0),
            "session_id": row.get("session_id"),
            "ts": row.get("ts"),
            "rep_index": row.get("rep_index"),
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "prediction": prediction or "unknown",
            "prediction_cn": LABEL_CN.get(prediction or "unknown", prediction or "unknown"),
            "visual_result": visual_result,
            "model_class": model_class,
            "classification_source": source,
            "classification_source_cn": SOURCE_CN.get(source, source),
            "debug_hint": SOURCE_DEBUG_HINT.get(source, "未知来源，先查DB rep_events和main loop日志"),
            "confidence": _round_metric(row.get("model_confidence")),
            "similarity": _round_metric(row.get("model_similarity")),
            "emg_ok": bool(_safe_int(row.get("emg_ok"), 0)),
            "target_peak": _round_metric(row.get("emg_target")),
            "comp_peak": _round_metric(row.get("emg_comp")),
            "rom": _round_metric(row.get("rom")),
            "angle_metric": row.get("angle_metric"),
            "eligible_for_gru_accuracy": source == "gru",
            "correct": bool(source == "gru" and prediction == label),
        }

    def collect_group_reps(self):
        exercise = self.validation.get("exercise", "bicep_curl")
        label = self.validation.get("label", "standard")
        baseline_id = _safe_int(self.validation.get("baseline_rep_id"), 0)
        rows, body = self.query_rep_events(exercise=exercise, limit=200)
        reps = [
            self.normalize_rep_event(row, label)
            for row in rows
            if _safe_int(row.get("id"), 0) > baseline_id
        ]
        return reps, body

    def group_filtered_samples(self):
        start_ts = _to_float(self.validation.get("started_ts"), None)
        end_ts = _to_float(self.validation.get("completed_ts"), None)
        with self.stream_lock:
            rows = [
                [r[0], r[7], r[8], r[5], r[6], r[3], r[4]]
                for r in self.stream_samples
                if isinstance(r, (list, tuple)) and len(r) >= 9
            ]
        if start_ts is None:
            return rows
        return [
            row for row in rows
            if isinstance(row, (list, tuple)) and row and
            _to_float(row[0], 0.0) >= start_ts and
            (end_ts is None or _to_float(row[0], 0.0) <= end_ts)
        ]

    def group_stream_samples(self):
        start_ts = _to_float(self.validation.get("started_ts"), None)
        end_ts = _to_float(self.validation.get("completed_ts"), None)
        with self.stream_lock:
            rows = list(self.stream_samples)
        if start_ts is None:
            return rows
        return [
            row for row in rows
            if isinstance(row, (list, tuple)) and row and
            _to_float(row[0], 0.0) >= start_ts and
            (end_ts is None or _to_float(row[0], 0.0) <= end_ts)
        ]

    def custom_action_dir(self, exercise, label):
        return CUSTOM_ACTION_ROOT / action_slug(exercise) / (label if label in LABELS else "unknown")

    def save_group_result(self, result):
        label = result.get("label", "unknown")
        exercise = result.get("exercise", "custom_action")
        group_id = result.get("group_id")
        if not group_id:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            group_id = "%s_%s" % (stamp, label)
        safe_group_id = re.sub(r"[^0-9A-Za-z_\\-]+", "_", str(group_id))
        path = self.run_dir / "groups" / ("%s.json" % safe_group_id)
        custom_path = self.custom_action_dir(exercise, label) / ("%s.json" % safe_group_id)
        portable_path = ROOT / "data" / "bicep_curl_personal" / "raw_groups" / ("%s.json" % safe_group_id)
        result["custom_action_save_path"] = str(custom_path)
        result["custom_action_save_path_rel"] = _safe_rel_path(custom_path)
        result["portable_path"] = str(portable_path)
        result["portable_path_rel"] = _safe_rel_path(portable_path)
        _atomic_write(path, result)
        _atomic_write(custom_path, result)
        _atomic_write(portable_path, result)
        return str(path)

    def write_session_index(self):
        payload = {
            "ok": True,
            "board_ip": self.board_ip,
            "board_url": self.board_url,
            "run_dir": str(self.run_dir),
            "updated_ts": _now(),
            "groups": self.recorded_groups,
        }
        _atomic_write(self.group_index_path, payload)
        return payload

    def recording_state(self):
        current = {
            "group_id": self.validation.get("group_id"),
            "label": self.validation.get("label"),
            "label_cn": LABEL_CN.get(self.validation.get("label"), self.validation.get("label")),
            "started_ts": self.validation.get("started_ts"),
            "baseline_rep_id": self.validation.get("baseline_rep_id"),
            "status_snapshot_count": len(self.recording_status_snapshots),
            "fsm_snapshot_count": len(self.recording_fsm_snapshots),
            "pose_sample_count": len(self.recording_pose_samples),
            "angle_debug_snapshot_count": len(self.recording_angle_debug_snapshots),
            "gru_7d_sample_count": len(self.recording_gru_7d_samples),
            "gru_last_window_count": len(self.recording_gru_last_windows),
        } if self.validation.get("active") else {}
        return {
            "active": bool(self.validation.get("active")),
            "phase": self.validation.get("phase", "idle"),
            "exercise": self.validation.get("exercise", "bicep_curl"),
            "label": self.validation.get("label", "standard"),
            "label_cn": LABEL_CN.get(self.validation.get("label", "standard"), "标准"),
            "group_id": self.validation.get("group_id"),
            "baseline_rep_id": self.validation.get("baseline_rep_id"),
            "started_ts": self.validation.get("started_ts"),
            "completed_ts": self.validation.get("completed_ts"),
            "last_group_result": self.validation.get("last_group_result", {}),
            "last_group_save_path": self.validation.get("last_group_save_path"),
            "capture_out_dir": str(self.run_dir / "groups"),
            "custom_action_out_dir": str(self.custom_action_dir(
                self.validation.get("exercise", "bicep_curl"),
                self.validation.get("label", "standard"),
            )),
            "custom_action_out_dir_rel": _safe_rel_path(self.custom_action_dir(
                self.validation.get("exercise", "bicep_curl"),
                self.validation.get("label", "standard"),
            )),
            "groups": list(self.recorded_groups),
            "session_index_path": str(self.group_index_path),
            "current": current,
            "feature_points": group_feature_points(self.recorded_groups),
        }

    def export_personal_dataset(self, exercise="bicep_curl"):
        exercise = "squat" if exercise == "squat" else "bicep_curl"
        tool = PERSONAL_EXPORT_TOOLS[exercise]
        out_root = PERSONAL_DATASET_ROOTS[exercise] / "datasets"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = out_root / ("%s_%s" % (stamp, self.run_dir.name))
        cmd = [
            "python3",
            str(tool),
            "--run-dir",
            str(self.run_dir),
            "--out-dir",
            str(out_dir),
            "--exercise",
            exercise,
            "--emg-view",
            "raw_rms_robust100",
            "--allow-non-gru-reps",
        ]
        # First personal squat/curl collection may use visual/fallback rep
        # boundaries before a trustworthy personal GRU exists.
        result = run_local_command(cmd, timeout=120)
        manifest = _read_json_file(out_dir / "personal_dataset_manifest.json")
        payload = {
            "ok": result.get("ok") and bool(manifest),
            "exercise": exercise,
            "out_dir": str(out_dir),
            "out_dir_rel": _safe_rel_path(out_dir),
            "manifest_path": str(out_dir / "personal_dataset_manifest.json"),
            "feature_distribution_html": str(out_dir / "feature_distribution.html"),
            "feature_distribution_json": str(out_dir / "feature_distribution.json"),
            "command": result,
            "manifest": manifest,
        }
        self._log_event("personal_dataset_export", payload)
        return payload

    def train_personal_gru(self, exercise="bicep_curl", data_dir=None, epochs=30):
        exercise = "squat" if exercise == "squat" else "bicep_curl"
        if not data_dir:
            data_dir = latest_dataset_dir(exercise)
        data_dir = Path(data_dir) if data_dir else None
        if data_dir is None:
            return {"ok": False, "error": "dataset_required", "exercise": exercise}
        tool = PERSONAL_TRAIN_TOOLS[exercise]
        run_root = PERSONAL_DATASET_ROOTS[exercise] / "training_runs"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = "candidate_extreme_fusion_gru.pt" if exercise == "squat" else "candidate_extreme_fusion_gru_bicep.pt"
        out_path = run_root / stamp / out_name
        cmd = [
            "python3",
            str(tool),
            "--data-dir",
            str(data_dir),
            "--out",
            str(out_path),
            "--epochs",
            str(int(epochs or 30)),
            "--allow-not-ready",
            "--save-failed",
            "--allow-single-group-split",
        ]
        result = run_local_command(cmd, timeout=180)
        report = _read_json_file(out_path.parent / "train_report.json")
        payload = {
            "ok": result.get("ok") and bool(report),
            "exercise": exercise,
            "data_dir": str(data_dir),
            "data_dir_rel": _safe_rel_path(data_dir),
            "model_path": str(out_path),
            "model_path_rel": _safe_rel_path(out_path),
            "report_path": str(out_path.parent / "train_report.json"),
            "report": report,
            "command": result,
        }
        self._log_event("personal_gru_train", payload)
        return payload

    def live_reps(self, exercise="bicep_curl", label=None, limit=30):
        exercise = "bicep_curl" if exercise in ("bicep_curl", "curl", None, "") else exercise
        db_exercise = exercise if exercise in BUILTIN_EXERCISES else "bicep_curl"
        label = label if label in LABELS else self.validation.get("label", "unknown")
        rows, body = self.query_rep_events(exercise=db_exercise, limit=limit)
        reps = [self.normalize_rep_event(row, label) for row in rows]
        latest = reps[-1] if reps else {}
        return {
            "ok": bool(body.get("ok_http", True)) if isinstance(body, dict) else True,
            "exercise": db_exercise,
            "label": label,
            "count": len(reps),
            "latest": latest,
            "reps": reps[-int(limit):],
            "query": body,
        }

    def switch_board_mode(self, exercise=None, inference_mode=None):
        owner = current_lock_owner()
        if owner not in ("free", "lane_b"):
            return {
                "ok": False,
                "blocked": True,
                "error": "lane_lock_not_owned",
                "lock_owner": owner,
            }
        actions = []
        if exercise in ("bicep_curl", "curl", "squat"):
            actions.append({
                "kind": "exercise",
                "request": exercise,
                "response": self.http_json(
                    "/api/exercise_mode",
                    method="POST",
                    payload={"mode": exercise, "src": "sensor_lab"},
                    timeout=4,
                ),
            })
        if inference_mode in ("pure_vision", "vision_sensor"):
            actions.append({
                "kind": "inference",
                "request": inference_mode,
                "response": self.http_json(
                    "/api/switch_inference_mode",
                    method="POST",
                    payload={"mode": inference_mode, "src": "sensor_lab"},
                    timeout=4,
                ),
            })
        self.read_board_snapshot()
        payload = {
            "ok": bool(actions) and all((a.get("response") or {}).get("ok_http", True) for a in actions),
            "actions": actions,
            "status": self.latest,
        }
        self._log_event("board_mode_switch", payload)
        return payload

    def _runtime_preprocess_payload(self, data_dir=None):
        manifest = _read_json_file(Path(data_dir) / "personal_dataset_manifest.json") if data_dir else {}
        payload = {
            "ok": True,
            "exercise": "bicep_curl",
            "default_training_view": manifest.get("emg_view") or "raw_rms_robust100",
            "preprocess_version": manifest.get("preprocess_version") or PREPROCESS_VERSION,
            "raw_rms_robust100": manifest.get("raw_rms_robust100") or {},
            "source_dataset": manifest.get("out_dir") or str(data_dir or ""),
            "created_ts": _now(),
            "uses_mvc": False,
        }
        return payload

    def deploy_personal_gru(self, candidate_path=None, data_dir=None):
        owner = current_lock_owner()
        if owner not in ("free", "lane_b"):
            return {
                "ok": False,
                "blocked": True,
                "error": "lane_lock_not_owned",
                "lock_owner": owner,
            }
        candidate = Path(candidate_path) if candidate_path else latest_candidate_model("bicep_curl")
        if candidate is None or not candidate.exists():
            return {"ok": False, "error": "candidate_missing", "candidate_path": str(candidate or "")}
        preprocess = self._runtime_preprocess_payload(data_dir=data_dir)
        if preprocess.get("default_training_view") != "raw_rms_robust100":
            return {
                "ok": False,
                "error": "unexpected_preprocess_view",
                "preprocess": preprocess,
            }
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_preprocess = self.run_dir / ("lane_b_runtime_preprocess_%s.json" % stamp)
        _atomic_write(local_preprocess, preprocess)
        backup_dir = REMOTE_ROOT + "/.deploy_backups/lane_b_sensor_lab_model_%s" % stamp
        backup_script = (
            "set -e; mkdir -p '%s/hardware_engine'; "
            "if [ -f '%s' ]; then cp '%s' '%s/hardware_engine/extreme_fusion_gru_bicep.pt'; fi"
            % (backup_dir, REMOTE_BICEP_MODEL, REMOTE_BICEP_MODEL, backup_dir)
        )
        backup = self.ssh_text(backup_script, timeout=8)
        if not backup.get("ok"):
            return {"ok": False, "error": "remote_backup_failed", "backup": backup}
        scp_model = run_local_command([
            "scp",
            "-i", BOARD_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            str(candidate),
            "toybrick@%s:%s" % (self.board_ip, REMOTE_BICEP_MODEL),
        ], timeout=30)
        if not scp_model.get("ok"):
            return {"ok": False, "error": "scp_model_failed", "command": scp_model}
        scp_pre = run_local_command([
            "scp",
            "-i", BOARD_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            str(local_preprocess),
            "toybrick@%s:%s" % (self.board_ip, REMOTE_RUNTIME_PREPROCESS),
        ], timeout=30)
        if not scp_pre.get("ok"):
            return {"ok": False, "error": "scp_preprocess_failed", "command": scp_pre}
        restart_script = """
set -e
mkdir -p /dev/shm
cp '%s' /dev/shm/lane_b_runtime_preprocess.json
PID=$(pgrep -f '[m]ain_claw_loop.py' || true)
if [ -n "$PID" ]; then kill $PID 2>/dev/null || true; sleep 0.8; fi
cd '%s'
nohup python3 hardware_engine/main_claw_loop.py > /tmp/fsm.log 2>&1 &
echo $!
""" % (REMOTE_RUNTIME_PREPROCESS, REMOTE_ROOT)
        restart = self.ssh_text(restart_script, timeout=12)
        switch = self.switch_board_mode(exercise="bicep_curl", inference_mode="vision_sensor")
        payload = {
            "ok": bool(restart.get("ok") and switch.get("ok")),
            "exercise": "bicep_curl",
            "candidate_path": str(candidate),
            "candidate_path_rel": _safe_rel_path(candidate),
            "preprocess_path": str(local_preprocess),
            "preprocess": preprocess,
            "remote_model": REMOTE_BICEP_MODEL,
            "remote_preprocess": REMOTE_RUNTIME_PREPROCESS,
            "remote_backup": backup_dir,
            "backup": backup,
            "scp_model": scp_model,
            "scp_preprocess": scp_pre,
            "restart": restart,
            "switch": switch,
        }
        self._log_event("personal_gru_deploy", payload)
        return payload

    def _append_recording_vision(self, vision):
        if not self.validation.get("active"):
            return
        vision = vision if isinstance(vision, dict) else {}
        pose_sample = vision.get("pose_sample") if isinstance(vision.get("pose_sample"), dict) else {}
        angle_debug = vision.get("angle_debug") if isinstance(vision.get("angle_debug"), dict) else {}
        pose_key = pose_sample_key(pose_sample)
        if pose_sample and pose_key not in self.recording_pose_keys:
            self.recording_pose_samples.append(pose_sample)
            if pose_key is not None:
                self.recording_pose_keys.add(pose_key)
        angle_key = angle_debug_key(angle_debug)
        if angle_debug and angle_key not in self.recording_angle_keys:
            self.recording_angle_debug_snapshots.append(angle_debug)
            if angle_key is not None:
                self.recording_angle_keys.add(angle_key)
        if len(self.recording_pose_samples) > VISION_MAX_SAMPLES:
            self.recording_pose_samples = self.recording_pose_samples[-VISION_MAX_SAMPLES:]
        if len(self.recording_angle_debug_snapshots) > VISION_MAX_SAMPLES:
            self.recording_angle_debug_snapshots = self.recording_angle_debug_snapshots[-VISION_MAX_SAMPLES:]

    def _append_recording_gru_7d(self, evidence):
        if not self.validation.get("active"):
            return
        evidence = evidence if isinstance(evidence, dict) else {}
        for sample in evidence.get("samples") or []:
            clean = sanitize_gru_7d_sample(sample)
            key = gru_7d_sample_key(clean)
            if clean and key not in self.recording_gru_7d_keys:
                self.recording_gru_7d_samples.append(clean)
                if key is not None:
                    self.recording_gru_7d_keys.add(key)
        last_window = evidence.get("last_window") if isinstance(evidence.get("last_window"), dict) else {}
        key = gru_window_key(last_window)
        if last_window and key not in self.recording_gru_window_keys:
            self.recording_gru_last_windows.append(last_window)
            if key is not None:
                self.recording_gru_window_keys.add(key)
        if len(self.recording_gru_7d_samples) > GRU_7D_MAX_SAMPLES:
            self.recording_gru_7d_samples = self.recording_gru_7d_samples[-GRU_7D_MAX_SAMPLES:]
        if len(self.recording_gru_last_windows) > GRU_7D_MAX_SAMPLES:
            self.recording_gru_last_windows = self.recording_gru_last_windows[-GRU_7D_MAX_SAMPLES:]

    def _sync_latest_recording(self):
        if isinstance(self.latest, dict) and self.latest:
            self.latest["validation"] = dict(self.validation)
            self.latest["recording"] = self.recording_state()

    def _gate_summary(self, health):
        health = health if isinstance(health, dict) else {}
        stats = health.get("raw_stats") if isinstance(health.get("raw_stats"), dict) else {}
        channels = stats.get("channels") if isinstance(stats.get("channels"), list) else []
        rail = max([_to_float(c.get("railish_ratio"), 0.0) or 0.0 for c in channels], default=0.0)
        jump = max([_to_float(c.get("mean_abs_jump"), 0.0) or 0.0 for c in channels], default=0.0)
        debug = health.get("emg_debug") if isinstance(health.get("emg_debug"), dict) else {}
        return {
            "signal_mode": health.get("signal_mode"),
            "valid_for_gru": bool(health.get("valid_for_gru")),
            "transport_ok": bool(health.get("transport_ok")),
            "raw_age_s": health.get("raw_age_s"),
            "heartbeat_age_s": health.get("heartbeat_age_s"),
            "debug_age_s": health.get("debug_age_s"),
            "rail": _round_metric(rail),
            "jump": _round_metric(jump),
            "pct": debug.get("pct"),
            "rms": debug.get("rms"),
            "filtered": debug.get("filtered"),
            "raw_values": debug.get("raw_values"),
        }

    def _append_recording_snapshot(self, latest):
        if not self.validation.get("active"):
            return
        latest = latest if isinstance(latest, dict) else {}
        ts = latest.get("ts", _now())
        health = latest.get("health") if isinstance(latest.get("health"), dict) else {}
        fsm = latest.get("fsm") if isinstance(latest.get("fsm"), dict) else {}
        inference = latest.get("inference_mode") if isinstance(latest.get("inference_mode"), dict) else {}
        status_snapshot = {
            "ts": ts,
            "board_mode": {
                "exercise": health.get("fsm_exercise") or fsm.get("exercise"),
                "inference_mode": health.get("inference_mode") or fsm.get("inference_mode") or inference.get("mode"),
                "state": fsm.get("state"),
                "angle": fsm.get("angle"),
                "last_drop_reason": fsm.get("last_drop_reason"),
            },
            "gate": self._gate_summary(health),
        }
        self.recording_status_snapshots.append(status_snapshot)
        self.recording_fsm_snapshots.append({
            "ts": ts,
            "fsm": fsm,
        })
        self._append_recording_vision(latest.get("vision_evidence"))
        self._append_recording_gru_7d(latest.get("gru_7d_evidence"))
        if len(self.recording_status_snapshots) > 5000:
            self.recording_status_snapshots = self.recording_status_snapshots[-5000:]
        if len(self.recording_fsm_snapshots) > 5000:
            self.recording_fsm_snapshots = self.recording_fsm_snapshots[-5000:]

    def ssh_text(self, script, timeout=5):
        cmd = [
            "ssh",
            "-i", BOARD_KEY,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=4",
            "toybrick@%s" % self.board_ip,
            script,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return {
                "ok": out.returncode == 0,
                "stdout": out.stdout,
                "stderr": out.stderr,
                "returncode": out.returncode,
            }
        except Exception as exc:
            return {"ok": False, "stdout": "", "stderr": str(exc), "returncode": -1}

    def read_board_snapshot(self):
        fsm = self.http_json("/api/fsm_state", timeout=3)
        muscle = self.http_json("/api/muscle_activation", timeout=3)
        inference = self.http_json("/api/inference_mode", timeout=3)
        capture = self.http_json("/api/test_capture/status", timeout=3)
        script = (
            "echo __CLOCK__; python3 - <<'PY'\nimport time\nprint('%.6f' % time.time())\nPY\n"
            "echo __HB__; cat /dev/shm/emg_heartbeat 2>/dev/null || true; "
            "echo; echo __DEBUG__; cat /dev/shm/emg_debug_snapshot.json 2>/dev/null || true; "
            "echo; echo __RAW__; stat -c '%Y %s' /dev/shm/emg_raw_waveform.json 2>/dev/null || true; "
            "echo; echo __PROC__; pgrep -af '[u]dp_emg_server|[m]ain_claw_loop|[c]loud_rtmpose_client|[s]treamer_app|[v]oice_daemon' || true; "
            "echo; echo __POSE__; cat /dev/shm/pose_data.json 2>/dev/null || true; "
            "echo; echo __ANGLE_DEBUG__; cat /dev/shm/angle_debug.json 2>/dev/null || true; "
            "echo; echo __GRU_7D_BUFFER__; cat /dev/shm/gru_7d_buffer.json 2>/dev/null || true; "
            "echo; echo __GRU_LAST_WINDOW__; cat /dev/shm/gru_last_window.json 2>/dev/null || true; "
            "echo; echo __END__"
        )
        ssh = self.ssh_text(script, timeout=5)
        ssh_out = ssh.get("stdout", "")
        health = self._derive_health(fsm, muscle, inference, capture, ssh_out)
        vision_evidence = self._vision_from_ssh_text(ssh_out)
        gru_7d_evidence = self._gru_7d_from_ssh_text(ssh_out)
        latest = {
            "ok": True,
            "board_ip": self.board_ip,
            "board_url": self.board_url,
            "stream_url": self.board_url + "/api/emg_stream",
            "lock_owner": current_lock_owner(),
            "fsm": fsm,
            "muscle_activation": muscle,
            "inference_mode": inference,
            "capture": capture,
            "health": health,
            "vision_evidence": vision_evidence,
            "gru_7d_evidence": gru_7d_evidence,
            "validation": dict(self.validation),
            "recording": self.recording_state(),
            "ssh_ok": ssh.get("ok", False),
            "ssh_tail": ssh.get("stdout", "")[-2000:],
            "run_dir": str(self.run_dir),
            "ts": _now(),
        }
        self.latest = latest
        self._append_recording_snapshot(latest)
        _atomic_write(self.state_path, latest)
        return latest

    def _vision_from_ssh_text(self, ssh_text):
        clock_raw = self._section(ssh_text, "__CLOCK__", "__HB__").strip()
        remote_now = _to_float(clock_raw, _now()) or _now()
        pose_raw = self._section(ssh_text, "__POSE__", "__ANGLE_DEBUG__").strip()
        angle_raw = self._section(ssh_text, "__ANGLE_DEBUG__", "__GRU_7D_BUFFER__").strip()
        pose = _safe_json(pose_raw, {}) if pose_raw else {}
        angle_debug = _safe_json(angle_raw, {}) if angle_raw else {}
        body = self._build_vision_evidence(pose, angle_debug, remote_now=remote_now)
        self.vision_cache = body
        return body

    def _gru_7d_from_ssh_text(self, ssh_text):
        clock_raw = self._section(ssh_text, "__CLOCK__", "__HB__").strip()
        remote_now = _to_float(clock_raw, _now()) or _now()
        buffer_raw = self._section(ssh_text, "__GRU_7D_BUFFER__", "__GRU_LAST_WINDOW__").strip()
        last_raw = self._section(ssh_text, "__GRU_LAST_WINDOW__", "__END__").strip()
        buffer_payload = _safe_json(buffer_raw, {}) if buffer_raw else {}
        last_window = _safe_json(last_raw, {}) if last_raw else {}
        samples = []
        for sample in (buffer_payload.get("samples") if isinstance(buffer_payload, dict) else []) or []:
            clean = sanitize_gru_7d_sample(sample)
            if clean:
                samples.append(clean)
        buffer_ts = _to_float(buffer_payload.get("ts")) if isinstance(buffer_payload, dict) else None
        last_ts = _to_float(last_window.get("ts")) if isinstance(last_window, dict) else None
        body = {
            "ok": bool(samples or last_window),
            "samples": samples,
            "sample_count": len(samples),
            "buffer_age_s": _age_s(remote_now, buffer_ts),
            "last_window_age_s": _age_s(remote_now, last_ts),
            "last_window": last_window if isinstance(last_window, dict) else {},
            "summary": summarize_gru_7d(samples, [last_window] if isinstance(last_window, dict) and last_window else []),
            "source": "/dev/shm/gru_7d_buffer.json + /dev/shm/gru_last_window.json",
            "detail": "ok" if (samples or last_window) else "no exact gru 7d evidence",
        }
        self.gru_7d_cache = body
        return body

    def _build_vision_evidence(self, pose, angle_debug, remote_now=None):
        capture_ts = _now()
        pose_sample = sanitize_pose_sample(pose, capture_ts=capture_ts, remote_now=remote_now)
        angle_snapshot = sanitize_angle_debug(angle_debug, capture_ts=capture_ts, remote_now=remote_now)
        objects = pose_sample.get("objects") or []
        first = objects[0] if objects else {}
        kpts = first.get("kpts") or []
        confs = [pt[2] for pt in kpts if isinstance(pt, (list, tuple)) and len(pt) >= 3]
        body = {
            "ok": bool(pose or angle_debug),
            "pose_age_s": pose_sample.get("age_s"),
            "frame_idx": pose_sample.get("frame_idx"),
            "valid_person": pose_sample.get("valid_person"),
            "person_score": first.get("score"),
            "kpt_count": len(kpts),
            "kpt_quality": {
                "mean": _round_metric(_mean(confs)),
                "low_conf_ratio": (
                    round(sum(1 for c in confs if c < 0.10) / float(len(confs)), 3)
                    if confs else None
                ),
            },
            "angle_debug": angle_snapshot,
            "pose_sample": pose_sample,
            "detail": "ok" if (pose or angle_debug) else "no pose evidence",
        }
        return body

    def _derive_health(self, fsm, muscle, inference, capture, ssh_text):
        clock_raw = self._section(ssh_text, "__CLOCK__", "__HB__").strip()
        hb = self._section(ssh_text, "__HB__", "__DEBUG__").strip()
        debug_raw = self._section(ssh_text, "__DEBUG__", "__RAW__").strip()
        raw_stat = self._section(ssh_text, "__RAW__", "__PROC__").strip()
        debug = _safe_json(debug_raw, {})
        local_now = _now()
        remote_now = _to_float(clock_raw, local_now) or local_now
        clock_skew_s = round(local_now - remote_now, 3)
        hb_ts = None
        if hb:
            hb_json = _safe_json(hb, None)
            if isinstance(hb_json, dict):
                hb_ts = hb_json.get("ts")
            else:
                try:
                    hb_ts = float(hb)
                except Exception:
                    hb_ts = None
        debug_ts = debug.get("ts") if isinstance(debug, dict) else None
        heartbeat_age_s = _age_s(remote_now, hb_ts)
        debug_age_s = _age_s(remote_now, debug_ts)
        raw_mtime = None
        raw_size = 0
        parts = raw_stat.split()
        if len(parts) >= 2:
            try:
                raw_mtime = float(parts[0])
                raw_size = int(parts[1])
            except Exception:
                pass
        raw_age_s = _age_s(remote_now, raw_mtime)
        simulated = bool(muscle.get("simulated") or muscle.get("sensor_simulated"))
        transport_ok = bool(
            heartbeat_age_s is not None and heartbeat_age_s < 3.0 and
            raw_age_s is not None and raw_age_s < 3.0
        )
        with self.raw_lock:
            raw_stats = dict(self.raw_cache.get("stats") or {})
        with self.stream_lock:
            stream_samples = list(self.stream_samples)[-RAW_RING_LIMIT:]
        raw_summary = emg_raw_summary(stream_samples)
        mapping_summary = emg_mapping_summary(stream_samples)
        gate = signal_gate(transport_ok, debug=debug, raw_stats=raw_stats, simulated=simulated)
        # The SSE stream is optimized for filtered display and may arrive before
        # /api/emg_fast has fetched the debug snapshot. Keep the cached fast
        # status aligned with the freshest SSH debug so fallback callers do not
        # mistake a saturated floating signal for a contact-rest candidate.
        with self.raw_lock:
            if self.raw_cache:
                cache_gate = signal_gate(
                    self.raw_cache.get("transport_ok", transport_ok),
                    debug=debug,
                    raw_stats=self.raw_cache.get("stats") or raw_stats,
                    simulated=False,
                )
                self.raw_cache["debug"] = debug if isinstance(debug, dict) else {}
                self.raw_cache["signal_mode"] = cache_gate.get("signal_mode")
                self.raw_cache["valid_for_gru"] = cache_gate.get("valid_for_gru")
                self.raw_cache["signal_reason"] = cache_gate.get("reason")
        real_emg = bool(transport_ok and not simulated)
        classification = fsm.get("classification") or fsm.get("last_rep_result") or "unknown"
        health = {
            "udp_online": bool(heartbeat_age_s is not None and heartbeat_age_s < 3.0),
            "transport_ok": transport_ok,
            "real_emg": real_emg,
            "sensor_simulated": simulated,
            "clock_skew_s": clock_skew_s,
            "heartbeat_age_s": heartbeat_age_s,
            "debug_age_s": debug_age_s,
            "raw_age_s": raw_age_s,
            "raw_size": raw_size,
            "raw_stats": raw_stats,
            "emg_raw_summary": raw_summary,
            "emg_mapping_summary": mapping_summary,
            "has_emg_debug": bool(debug),
            "classification": classification,
            "confidence": fsm.get("confidence") or fsm.get("nn_confidence"),
            "similarity": fsm.get("similarity"),
            "target_peak": debug.get("target_pct") if isinstance(debug, dict) else None,
            "comp_peak": debug.get("comp_pct") if isinstance(debug, dict) else None,
            "fsm_exercise": fsm.get("exercise"),
            "capture_active": bool(capture.get("active")),
            "inference_mode": inference.get("mode"),
            "emg_debug": debug,
        }
        health.update(gate)
        return health

    @staticmethod
    def _section(text, start, end):
        s = text.find(start)
        if s == -1:
            return ""
        s += len(start)
        e = text.find(end, s)
        if e == -1:
            e = len(text)
        return text[s:e]

    def refresh_fast_wave(self):
        script = r"""python3 - <<'PY'
import json, os, time
raw_path = "/dev/shm/emg_stream_buffer.json"
fallback_path = "/dev/shm/emg_raw_waveform.json"
debug_path = "/dev/shm/emg_debug_snapshot.json"
out = {"remote_now": time.time(), "raw_path": raw_path, "debug_path": debug_path}
try:
    out["raw_mtime"] = os.path.getmtime(raw_path)
    with open(raw_path, "r") as f:
        out["raw"] = json.load(f)
except Exception as exc:
    out["raw_error"] = str(exc)
    try:
        out["raw_path"] = fallback_path
        out["raw_mtime"] = os.path.getmtime(fallback_path)
        with open(fallback_path, "r") as f:
            out["raw"] = json.load(f)
    except Exception as fallback_exc:
        out["fallback_error"] = str(fallback_exc)
try:
    out["debug_mtime"] = os.path.getmtime(debug_path)
    with open(debug_path, "r") as f:
        out["debug"] = json.load(f)
except Exception as exc:
    out["debug_error"] = str(exc)
print(json.dumps(out))
PY"""
        ssh = self.ssh_text(script, timeout=4)
        wrapper = _safe_json(ssh.get("stdout", ""), {})
        data = wrapper.get("raw") if isinstance(wrapper, dict) else {}
        debug = wrapper.get("debug") if isinstance(wrapper, dict) else {}
        remote_now = _to_float(wrapper.get("remote_now") if isinstance(wrapper, dict) else None, _now())
        samples = data.get("samples") if isinstance(data, dict) else []
        if not isinstance(samples, list):
            samples = []
        samples = samples[-RAW_RING_LIMIT:]
        stream_like = bool(samples and isinstance(samples[-1], (list, tuple)) and len(samples[-1]) >= 9)
        raw_stats_samples = (
            [[r[0], r[1], r[2]] for r in samples if isinstance(r, (list, tuple)) and len(r) >= 3]
            if stream_like else samples
        )
        ts = data.get("ts") if isinstance(data, dict) else None
        if not ts and samples:
            try:
                ts = float(samples[-1][0])
            except Exception:
                ts = None
        if not isinstance(ts, (int, float)):
            ts = wrapper.get("raw_mtime") if isinstance(wrapper, dict) else None
        age_s = _age_s(remote_now, ts)
        stats = raw_wave_stats(raw_stats_samples)
        gate = signal_gate(
            age_s is not None and age_s < 3.0,
            debug=debug,
            raw_stats=stats,
            simulated=False,
        )
        body = {
            "ok": bool(samples),
            "samples": raw_stats_samples[-RAW_RING_LIMIT:],
            "stream_samples": samples if stream_like else [],
            "filtered_samples": filtered_display_rows(samples) if stream_like else fallback_filtered_rows(raw_stats_samples, debug),
            "samples_count": len(raw_stats_samples),
            "samples_returned": len(raw_stats_samples[-RAW_RING_LIMIT:]),
            "age_s": age_s,
            "packet_count": data.get("packet_count") if isinstance(data, dict) else None,
            "sample_count": data.get("sample_count") if isinstance(data, dict) else len(raw_stats_samples),
            "channels": data.get("channels") if isinstance(data, dict) else None,
            "stats": stats,
            "debug": debug if isinstance(debug, dict) else {},
            "transport_ok": gate.get("transport_ok"),
            "signal_mode": gate.get("signal_mode"),
            "valid_for_gru": gate.get("valid_for_gru"),
            "signal_reason": gate.get("reason"),
            "source": wrapper.get("raw_path") if isinstance(wrapper, dict) else "/dev/shm/emg_stream_buffer.json",
            "detail": "ok" if samples else "no raw waveform samples",
        }
        if stream_like:
            self.ingest_stream_samples(samples)
        with self.raw_lock:
            self.raw_cache = body
        return body

    def refresh_vision_evidence(self):
        script = r"""python3 - <<'PY'
import json, os, time
out = {"remote_now": time.time()}
for key, path in (
    ("pose", "/dev/shm/pose_data.json"),
    ("angle_debug", "/dev/shm/angle_debug.json"),
):
    try:
        out[key + "_mtime"] = os.path.getmtime(path)
        with open(path, "r") as f:
            out[key] = json.load(f)
    except Exception as exc:
        out[key + "_error"] = str(exc)
print(json.dumps(out))
PY"""
        ssh = self.ssh_text(script, timeout=2)
        wrapper = _safe_json(ssh.get("stdout", ""), {})
        remote_now = _to_float(wrapper.get("remote_now") if isinstance(wrapper, dict) else None, _now())
        pose = wrapper.get("pose") if isinstance(wrapper.get("pose"), dict) else {}
        angle_debug = wrapper.get("angle_debug") if isinstance(wrapper.get("angle_debug"), dict) else {}
        body = self._build_vision_evidence(pose, angle_debug, remote_now=remote_now)
        body["ssh_ok"] = ssh.get("ok", False)
        body["source"] = "/dev/shm/pose_data.json + /dev/shm/angle_debug.json"
        if not body.get("ok"):
            body["detail"] = wrapper.get("pose_error") or wrapper.get("angle_debug_error") or body.get("detail")
        self.vision_cache = body
        self._append_recording_vision(body)
        return body

    def ingest_stream_samples(self, rows):
        clean = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 9:
                continue
            pkt = _safe_int(row[9], 0) if len(row) >= 10 else 0
            ts = float(_to_float(row[0], 0.0) or 0.0)
            clean.append([
                ts,
                float(_to_float(row[1], 0.0) or 0.0),
                float(_to_float(row[2], 0.0) or 0.0),
                float(_to_float(row[3], 0.0) or 0.0),
                float(_to_float(row[4], 0.0) or 0.0),
                float(_to_float(row[5], 0.0) or 0.0),
                float(_to_float(row[6], 0.0) or 0.0),
                float(_to_float(row[7], 0.0) or 0.0),
                float(_to_float(row[8], 0.0) or 0.0),
                pkt,
            ])
        if not clean:
            return self.stream_cache
        with self.stream_lock:
            existing_keys = set()
            for row in list(self.stream_samples)[-1500:]:
                if isinstance(row, (list, tuple)) and len(row) >= 10:
                    pkt = _safe_int(row[9], 0)
                    key = ("pkt", pkt) if pkt else ("ts", int((_to_float(row[0], 0.0) or 0.0) * 1000000))
                    existing_keys.add(key)
            appended = 0
            for row in clean:
                pkt = _safe_int(row[9], 0)
                key = ("pkt", pkt) if pkt else ("ts", int((_to_float(row[0], 0.0) or 0.0) * 1000000))
                if key in existing_keys:
                    continue
                self.stream_samples.append(row)
                existing_keys.add(key)
                appended += 1
            samples = list(self.stream_samples)
        stats = stream_wave_stats(samples[-RAW_RING_LIMIT:])
        age_s = None
        if samples:
            age_s = _age_s(_now(), samples[-1][0])
        cache = {
            "ok": bool(samples),
            "samples": samples[-RAW_RING_LIMIT:],
            "samples_count": len(samples),
            "samples_returned": len(samples[-RAW_RING_LIMIT:]),
            "age_s": age_s,
            "appended": appended,
            "stats": stats,
            "columns": [
                "ts", "raw0", "raw1", "filtered0", "filtered1",
                "rms0", "rms1", "pct0", "pct1", "packet_count"
            ],
            "source": "board_sse_or_snapshot",
            "detail": "ok",
        }
        self.stream_cache = cache
        raw_rows = [[r[0], r[1], r[2]] for r in samples[-RAW_RING_LIMIT:] if isinstance(r, (list, tuple)) and len(r) >= 3]
        with self.raw_lock:
            previous = dict(self.raw_cache)
            debug = previous.get("debug") if isinstance(previous.get("debug"), dict) else {}
            gate = signal_gate(age_s is not None and age_s < 3.0, debug=debug, raw_stats=stats, simulated=False)
            self.raw_cache = {
                "ok": bool(raw_rows),
                "samples": raw_rows,
                "samples_count": len(raw_rows),
                "samples_returned": len(raw_rows),
                "age_s": age_s,
                "packet_count": samples[-1][9] if samples and len(samples[-1]) >= 10 else None,
                "sample_count": len(raw_rows),
                "channels": ["target_ch0", "comp_ch1"],
                "stats": stats,
                "debug": debug,
                "transport_ok": gate.get("transport_ok"),
                "signal_mode": gate.get("signal_mode"),
                "valid_for_gru": gate.get("valid_for_gru"),
                "signal_reason": gate.get("reason"),
                "source": "sse_stream",
                "detail": "ok",
            }
        return cache

    def snapshot_stream_payload(self, reason):
        snapshot = self.http_json("/api/emg_fast?full=1&limit=1000", timeout=1.2)
        if not snapshot.get("ok") and not snapshot.get("samples"):
            snapshot = self.refresh_fast_wave()
        rows = snapshot.get("stream_samples") or []
        debug = snapshot.get("debug") if isinstance(snapshot.get("debug"), dict) else {}
        if not rows:
            rows = fallback_stream_rows(snapshot.get("samples") or [], debug)
        if rows:
            self.ingest_stream_samples(rows)
        filtered_rows = filtered_display_rows(rows)
        if not filtered_rows:
            filtered_rows = fallback_filtered_rows(snapshot.get("samples") or [], debug)
        return {
            "ok": bool(rows),
            "ts": _now(),
            "age_s": snapshot.get("age_s"),
            "packet_count": snapshot.get("packet_count"),
            "samples": filtered_rows[-220:],
            "stream_samples": rows[-220:],
            "samples_returned": len(filtered_rows[-220:]),
            "samples_count": len(rows),
            "columns": [
                "ts", "filtered0", "filtered1",
                "rms0", "rms1", "pct0", "pct1", "packet_count"
            ],
            "stream_columns": [
                "ts", "raw0", "raw1", "filtered0", "filtered1",
                "rms0", "rms1", "pct0", "pct1", "packet_count"
            ],
            "channels": ["target_ch0", "comp_ch1"],
            "source": "lab_snapshot_fallback",
            "detail": "fallback: " + str(reason),
        }

    def stream_events(self):
        url = self.board_url + "/api/emg_stream?interval_ms=20"
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            upstream = NO_PROXY_OPENER.open(req, timeout=8)
            for raw_line in upstream:
                line = raw_line.decode("utf-8", "replace")
                if line.startswith("data:"):
                    payload = _safe_json(line[5:].strip(), {})
                    if isinstance(payload, dict):
                        rows = payload.get("samples") or []
                        if rows:
                            self.ingest_stream_samples(rows)
                            payload["stream_samples"] = [
                                row for row in rows[-220:]
                                if isinstance(row, (list, tuple)) and len(row) >= 9
                            ]
                            payload["samples"] = filtered_display_rows(rows, limit=220)
                            payload["columns"] = [
                                "ts", "filtered0", "filtered1",
                                "rms0", "rms1", "pct0", "pct1", "packet_count"
                            ]
                            payload["stream_columns"] = [
                                "ts", "raw0", "raw1", "filtered0", "filtered1",
                                "rms0", "rms1", "pct0", "pct1", "packet_count"
                            ]
                            line = "data: %s\n\n" % json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                        elif not payload.get("ok"):
                            payload = self.snapshot_stream_payload(payload.get("detail", "empty upstream stream"))
                            line = "data: %s\n\n" % json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                yield line
        except Exception as exc:
            self._log_event("emg_stream_fallback", {"error": str(exc)})
            while not self.stop_event.is_set():
                try:
                    payload = self.snapshot_stream_payload(exc)
                    yield "event: emg\n"
                    yield "data: %s\n\n" % json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
                except Exception as inner_exc:
                    yield "event: emg\n"
                    yield "data: %s\n\n" % json.dumps({
                        "ok": False,
                        "samples": [],
                        "detail": "fallback_error: " + str(inner_exc),
                    }, ensure_ascii=False, separators=(',', ':'))
                time.sleep(0.12)

    def start_recording(self, label, exercise="bicep_curl"):
        requested_exercise = action_slug(exercise)
        if not requested_exercise:
            return {"ok": False, "error": "invalid exercise"}
        if label not in LABELS:
            return {"ok": False, "error": "invalid label"}
        if self.validation.get("active"):
            return {
                "ok": False,
                "blocked": True,
                "error": "already_recording",
                "reason": "current_group=%s" % (self.validation.get("group_id") or "unknown"),
                "recording": self.recording_state(),
            }
        canonical_exercise = "bicep_curl" if requested_exercise in ("bicep_curl", "curl") else ("squat" if requested_exercise == "squat" else requested_exercise)
        db_exercise = canonical_exercise if canonical_exercise in BUILTIN_EXERCISES else "bicep_curl"
        latest = self.read_board_snapshot()
        health = latest.get("health", {}) if isinstance(latest, dict) else {}
        baseline_rep_id = self.latest_rep_id(db_exercise)
        started_ts = _now()
        self.group_sequence += 1
        group_id = "%s_%03d_%s" % (
            datetime.now().strftime("%Y%m%d_%H%M%S"),
            self.group_sequence,
            label,
        )
        self.recording_status_snapshots = []
        self.recording_fsm_snapshots = []
        self.recording_pose_samples = []
        self.recording_angle_debug_snapshots = []
        self.recording_gru_7d_samples = []
        self.recording_gru_last_windows = []
        self.recording_pose_keys = set()
        self.recording_angle_keys = set()
        self.recording_gru_7d_keys = set()
        self.recording_gru_window_keys = set()
        self.validation.update({
            "active": True,
            "phase": "recording",
            "exercise": canonical_exercise,
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "group_id": group_id,
            "started_ts": started_ts,
            "completed_ts": None,
            "baseline_started_ts": started_ts,
            "baseline_rep_id": baseline_rep_id,
            "baseline_duration_s": 0,
            "reps": [],
            "summary": {},
            "last_group_result": {},
            "last_group_save_path": None,
            "capture": {"mode": "lab_local_recording"},
            "custom_action_out_dir": str(self.custom_action_dir(canonical_exercise, label)),
            "custom_action_out_dir_rel": _safe_rel_path(self.custom_action_dir(canonical_exercise, label)),
            "baseline": {},
            "start_gate": self._gate_summary(health),
            "board_mode_at_start": {
                "exercise": health.get("fsm_exercise"),
                "inference_mode": health.get("inference_mode"),
            },
        })
        with self.stream_lock:
            started_stream_count = len(self.stream_samples)
        capture = {
            "ok": True,
            "mode": "lab_local_recording",
            "started_stream_count": started_stream_count,
            "run_dir": str(self.run_dir),
            "note": "local recording only; board mode is not changed by Sensor Lab",
        }
        self.validation.update({
            "capture": capture,
            "capture_out_dir": str(self.run_dir / "groups"),
            "custom_action_out_dir": str(self.custom_action_dir(canonical_exercise, label)),
            "custom_action_out_dir_rel": _safe_rel_path(self.custom_action_dir(canonical_exercise, label)),
            "capture_session_id": group_id,
        })
        self._append_recording_snapshot(latest)
        result = {
            "ok": True,
            "mode": "local_recording",
            "group_id": group_id,
            "exercise": canonical_exercise,
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "baseline_rep_id": baseline_rep_id,
            "started_ts": started_ts,
            "capture": capture,
            "start_gate": self.validation.get("start_gate"),
            "board_mode_at_start": self.validation.get("board_mode_at_start"),
            "warnings": [] if health.get("valid_for_gru") else ["signal_not_valid_for_gru"],
            "custom_action_out_dir": str(self.custom_action_dir(canonical_exercise, label)),
            "custom_action_out_dir_rel": _safe_rel_path(self.custom_action_dir(canonical_exercise, label)),
            "recording": self.recording_state(),
        }
        self._sync_latest_recording()
        self._log_event("recording_start", result)
        return result

    def stop_recording(self):
        if not self.validation.get("active"):
            return {
                "ok": False,
                "blocked": True,
                "error": "no_active_recording",
                "recording": self.recording_state(),
            }
        completed_ts = _now()
        self.validation["completed_ts"] = completed_ts
        self.read_board_snapshot()
        current_exercise = self.validation.get("exercise", "bicep_curl")
        db_exercise = current_exercise if current_exercise in BUILTIN_EXERCISES else "bicep_curl"
        rows, rep_query = self.query_rep_events(
            exercise=db_exercise,
            limit=200,
        )
        label = self.validation.get("label", "standard")
        baseline_id = _safe_int(self.validation.get("baseline_rep_id"), 0)
        rep_events = [
            row for row in rows
            if _safe_int(row.get("id"), 0) > baseline_id
        ]
        reps = [self.normalize_rep_event(row, label) for row in rep_events]
        curve_samples = self.group_filtered_samples()
        stream_samples = self.group_stream_samples()
        vision_pose_samples = list(self.recording_pose_samples)
        angle_debug_snapshots = list(self.recording_angle_debug_snapshots)
        gru_7d_samples = list(self.recording_gru_7d_samples)
        gru_last_windows = list(self.recording_gru_last_windows)
        curve = compare_filtered_to_reference(label, curve_samples, self.reference_waveforms)
        summary = summarize_group(label, reps, curve)
        vision_summary = summarize_vision_evidence(
            vision_pose_samples,
            angle_debug_snapshots,
            self.recording_fsm_snapshots,
        )
        gru_7d_summary = summarize_gru_7d(gru_7d_samples, gru_last_windows)
        raw_summary = emg_raw_summary(stream_samples)
        mapping_summary = emg_mapping_summary(stream_samples)
        preprocess_summary = emg_preprocess_summary(stream_samples)
        training_compare = build_training_compare(label, stream_samples)
        latest_health = (self.latest.get("health") if isinstance(self.latest, dict) else {}) or {}
        capture = {
            "ok": True,
            "mode": "lab_local_recording",
            "stream_samples_count": len(stream_samples),
            "status_snapshot_count": len(self.recording_status_snapshots),
            "fsm_snapshot_count": len(self.recording_fsm_snapshots),
            "vision_pose_sample_count": len(vision_pose_samples),
            "angle_debug_snapshot_count": len(angle_debug_snapshots),
            "gru_7d_sample_count": len(gru_7d_samples),
            "gru_last_window_count": len(gru_last_windows),
            "completed_ts": completed_ts,
        }
        group_result = {
            "ok": True,
            "mode": "local_recording",
            "group_id": self.validation.get("group_id"),
            "exercise": self.validation.get("exercise", "bicep_curl"),
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "started_ts": self.validation.get("started_ts"),
            "completed_ts": completed_ts,
            "baseline_rep_id": self.validation.get("baseline_rep_id"),
            "capture_start": self.validation.get("capture", {}),
            "capture_stop": capture,
            "rep_query_ok": bool(rep_query.get("ok_http", True)) if isinstance(rep_query, dict) else False,
            "rep_events": rep_events,
            "reps": reps,
            "summary": summary,
            "start_gate": self.validation.get("start_gate"),
            "end_gate": self._gate_summary(latest_health),
            "board_mode_at_start": self.validation.get("board_mode_at_start"),
            "board_mode_at_end": {
                "exercise": latest_health.get("fsm_exercise"),
                "inference_mode": latest_health.get("inference_mode"),
            },
            "curve_samples_count": len(curve_samples),
            "stream_columns": [
                "ts", "raw0", "raw1", "filtered0", "filtered1",
                "rms0", "rms1", "pct0", "pct1", "packet_count"
            ],
            "stream_samples": stream_samples,
            "status_snapshots": list(self.recording_status_snapshots),
            "fsm_snapshots": list(self.recording_fsm_snapshots),
            "vision_pose_samples": vision_pose_samples,
            "angle_debug_snapshots": angle_debug_snapshots,
            "vision_summary": vision_summary,
            "gru_7d_samples": gru_7d_samples,
            "gru_last_windows": gru_last_windows,
            "gru_7d_summary": gru_7d_summary,
            "emg_raw_summary": raw_summary,
            "emg_mapping_summary": mapping_summary,
            "emg_preprocess": preprocess_summary,
            "training_compare": training_compare,
            "run_dir": str(self.run_dir),
            "custom_action_save_path": None,
            "custom_action_save_path_rel": None,
        }
        group_result["save_path"] = self.save_group_result(group_result)
        group_summary = {
            "group_id": group_result["group_id"],
            "exercise": group_result.get("exercise"),
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "started_ts": group_result["started_ts"],
            "completed_ts": group_result["completed_ts"],
            "rep_count": summary.get("rep_count", 0),
            "gru_rep_count": summary.get("gru_rep_count", 0),
            "correct": summary.get("correct", 0),
            "accuracy": summary.get("accuracy"),
            "save_path": group_result["save_path"],
            "custom_action_save_path": group_result.get("custom_action_save_path"),
            "custom_action_save_path_rel": group_result.get("custom_action_save_path_rel"),
            "start_gate": group_result.get("start_gate"),
            "end_gate": group_result.get("end_gate"),
            "gru_7d_summary": group_result.get("gru_7d_summary"),
        }
        self.recorded_groups.append(group_summary)
        self.write_session_index()
        self.validation.update({
            "active": False,
            "phase": "complete",
            "completed_ts": group_result["completed_ts"],
            "reps": reps,
            "summary": summary,
            "last_group_result": group_result,
            "last_group_save_path": group_result["save_path"],
            "capture_stop": capture,
        })
        self.recording_status_snapshots = []
        self.recording_fsm_snapshots = []
        self.recording_pose_samples = []
        self.recording_angle_debug_snapshots = []
        self.recording_gru_7d_samples = []
        self.recording_gru_last_windows = []
        self.recording_pose_keys = set()
        self.recording_angle_keys = set()
        self.recording_gru_7d_keys = set()
        self.recording_gru_window_keys = set()
        result = {
            "ok": True,
            "capture": capture,
            "group": group_result,
            "recording": self.recording_state(),
            "validation": dict(self.validation),
        }
        self._sync_latest_recording()
        self._log_event("recording_stop", {
            "group_id": group_result.get("group_id"),
            "label": label,
            "save_path": group_result.get("save_path"),
            "rep_count": summary.get("rep_count", 0),
            "gru_rep_count": summary.get("gru_rep_count", 0),
        })
        return result

    def start_validation(self, exercise, label):
        return self.start_recording(label=label, exercise=exercise)

    def stop_validation(self):
        return self.stop_recording()

    def list_lab_session_recordings(self):
        groups_dir = self.run_dir / "groups"
        items = []
        if groups_dir.exists():
            for json_path in groups_dir.glob("*.json"):
                try:
                    with json_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as exc:
                    items.append({
                        "group_id": json_path.stem,
                        "label": "unknown",
                        "label_cn": LABEL_CN.get("unknown", "未知"),
                        "created_ts": json_path.stat().st_mtime,
                        "created_ts_iso": datetime.fromtimestamp(json_path.stat().st_mtime).strftime("%H:%M:%S"),
                        "duration_s": 0.0,
                        "stream_rows": 0,
                        "gru_7d_rows": 0,
                        "rep_count": 0,
                        "start_gate_ok": False,
                        "end_gate_ok": False,
                        "board_mode_at_start": {},
                        "is_this_session": False,
                        "session_path": str(self.run_dir),
                        "group_path": str(json_path),
                        "portable_path": "",
                        "error": "read_failed: %s" % exc,
                    })
                    continue
                try:
                    started_ts = float(data.get("started_ts") or 0.0)
                except Exception:
                    started_ts = 0.0
                try:
                    completed_ts = float(data.get("completed_ts") or 0.0)
                except Exception:
                    completed_ts = 0.0
                duration_s = max(0.0, completed_ts - started_ts)
                created_ts = completed_ts or started_ts or json_path.stat().st_mtime
                label = data.get("label", "unknown")
                start_gate = data.get("start_gate") or {}
                end_gate = data.get("end_gate") or {}
                portable_path = data.get("portable_path") or str(
                    ROOT / "data" / "bicep_curl_personal" / "raw_groups" / json_path.name
                )
                items.append({
                    "group_id": data.get("group_id") or json_path.stem,
                    "label": label,
                    "label_cn": LABEL_CN.get(label, label),
                    "created_ts": created_ts,
                    "created_ts_iso": datetime.fromtimestamp(created_ts).strftime("%H:%M:%S"),
                    "duration_s": duration_s,
                    "stream_rows": len(data.get("stream_samples") or []),
                    "gru_7d_rows": len(data.get("gru_7d_samples") or []),
                    "rep_count": len(data.get("rep_events") or []),
                    "start_gate_ok": bool(start_gate.get("ok") if isinstance(start_gate, dict) else False),
                    "end_gate_ok": bool(end_gate.get("ok") if isinstance(end_gate, dict) else False),
                    "board_mode_at_start": data.get("board_mode_at_start") or {},
                    "is_this_session": created_ts >= float(self.lab_session_start_ts),
                    "session_path": str(self.run_dir),
                    "group_path": str(json_path),
                    "portable_path": portable_path,
                })
        items.sort(key=lambda r: r.get("created_ts") or 0.0, reverse=True)
        return {
            "ok": True,
            "lab_session_start_ts": float(self.lab_session_start_ts),
            "count": len(items),
            "items": items,
        }

    def build_personal_dataset_from_groups(self, group_paths, exercise="bicep_curl"):
        exercise = "squat" if exercise == "squat" else "bicep_curl"
        if not group_paths:
            return {"ok": False, "error": "no_groups_selected", "exercise": exercise}
        resolved = []
        for raw in group_paths:
            if not raw:
                continue
            p = Path(raw)
            if not p.is_absolute():
                p = ROOT / p
            if not p.exists():
                return {"ok": False, "error": "group_missing", "missing": str(p), "exercise": exercise}
            resolved.append(str(p))
        if not resolved:
            return {"ok": False, "error": "no_groups_selected", "exercise": exercise}
        tool = PERSONAL_EXPORT_TOOLS[exercise]
        out_root = PERSONAL_DATASET_ROOTS[exercise] / "datasets"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = out_root / ("%s_merged_%d" % (stamp, len(resolved)))
        cmd = [
            "python3",
            str(tool),
            "--group-files",
            ",".join(resolved),
            "--out-dir",
            str(out_dir),
            "--exercise",
            exercise,
            "--emg-view",
            "raw_rms_robust100",
            "--allow-non-gru-reps",
        ]
        result = run_local_command(cmd, timeout=120)
        manifest = _read_json_file(out_dir / "personal_dataset_manifest.json")
        payload = {
            "ok": result.get("ok") and bool(manifest),
            "exercise": exercise,
            "out_dir": str(out_dir),
            "out_dir_rel": _safe_rel_path(out_dir),
            "manifest_path": str(out_dir / "personal_dataset_manifest.json"),
            "feature_distribution_html": str(out_dir / "feature_distribution.html"),
            "feature_distribution_json": str(out_dir / "feature_distribution.json"),
            "command": result,
            "manifest": manifest,
            "group_count": len(resolved),
            "group_paths": resolved,
        }
        self._log_event("personal_dataset_build_from_groups", {
            "exercise": exercise,
            "out_dir": str(out_dir),
            "group_count": len(resolved),
            "ok": payload["ok"],
        })
        return payload

    def background_loop(self):
        while not self.stop_event.is_set():
            try:
                self.read_board_snapshot()
            except Exception as exc:
                self.latest = {
                    "ok": False,
                    "error": str(exc),
                    "board_ip": self.board_ip,
                    "ts": _now(),
                }
            self.stop_event.wait(1.0)

    def raw_loop(self):
        while not self.stop_event.is_set():
            try:
                self.refresh_fast_wave()
            except Exception:
                pass
            self.stop_event.wait(0.12)

    def vision_loop(self):
        while not self.stop_event.is_set():
            try:
                self.refresh_vision_evidence()
            except Exception:
                pass
            self.stop_event.wait(VISION_CAPTURE_INTERVAL_S)


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lane B 弯举一体化工作台</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:#070a0a;
      color:#dfe8e3;
      --bg:#070a0a; --panel:#0d1312; --panel2:#101817; --line:#263632;
      --muted:#8ca09a; --text:#dfe8e3; --blue:#37b7ff; --amber:#ffb42e;
      --green:#39d181; --red:#ff6969; --violet:#b090ff;
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); }
    main { width:min(1480px, 100vw); margin:0 auto; padding:12px; }
    header { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:4px 0 10px; border-bottom:1px solid var(--line); }
    h1 { margin:0; font-size:20px; line-height:1.15; letter-spacing:0; }
    .muted { color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
    .topbar { justify-content:flex-end; }
    .pill {
      display:inline-flex; align-items:center; min-height:24px; padding:0 8px;
      border-radius:999px; background:#17211f; border:1px solid #2d403b;
      color:#b8c8c3; font-size:12px; font-weight:750; white-space:nowrap;
    }
    .pill.ok { color:var(--green); border-color:#286f4b; background:#0d2118; }
    .pill.warn { color:var(--amber); border-color:#6f551d; background:#20190c; }
    .pill.bad { color:var(--red); border-color:#743333; background:#241313; }
    .tabs { display:flex; gap:8px; margin:12px 0; align-items:center; flex-wrap:wrap; }
    button {
      border:1px solid #344641; background:#111917; color:#dce8e3;
      border-radius:6px; min-height:34px; padding:7px 11px;
      font-weight:800; cursor:pointer;
    }
    button.active { border-color:#438466; background:#173226; color:#72f0aa; }
    button.primary { background:#dfe8e3; border-color:#dfe8e3; color:#07100d; }
    button.warn { border-color:#725523; color:#ffbf48; background:#21170a; }
    button.compact { min-height:28px; padding:5px 8px; font-size:12px; }
    .grid { display:grid; grid-template-columns:1fr 380px; gap:12px; align-items:start; }
    .scope { display:grid; gap:10px; }
    .scope-layer { display:grid; gap:8px; }
    .scope-layer-title {
      display:flex; align-items:center; justify-content:space-between; gap:10px;
      color:#f3faf7; font-weight:850; font-size:13px; letter-spacing:0;
    }
    .scope-layer-title span { color:var(--muted); font-weight:750; font-size:12px; }
    .scope-pair { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; }
    .section-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:7px; }
    .section-head strong { font-size:14px; }
    .legend { display:flex; gap:10px; flex-wrap:wrap; align-items:center; color:var(--muted); font-size:12px; }
    .dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:4px; vertical-align:-1px; }
    .scope-hint { color:var(--amber); font-weight:800; }
    .scope-channel { padding:10px; }
    .scope-channel canvas {
      width:100%; height:220px; min-height:190px; max-height:260px;
      background:#050807; border:1px solid #22332e; border-radius:6px; display:block;
    }
    .scope-channel.raw canvas { height:250px; min-height:220px; max-height:300px; }
    .side { display:grid; gap:10px; }
    .metrics { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
    .metric { background:var(--panel2); border:1px solid var(--line); border-radius:7px; padding:9px; min-height:66px; }
    .metric span { display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }
    .metric strong { display:block; color:#f3faf7; font-size:15px; line-height:1.25; overflow-wrap:anywhere; }
    .metric.wide { grid-column:1 / -1; }
    .metric.primary-state { border-color:#375d4c; background:#0d2118; }
    .metric.primary-state strong { color:#72f0aa; font-size:18px; }
    .metric.primary-state.warn { border-color:#6f551d; background:#20190c; }
    .metric.primary-state.warn strong { color:var(--amber); }
    .metric.primary-state.bad { border-color:#743333; background:#241313; }
    .metric.primary-state.bad strong { color:var(--red); }
	    .controls { display:grid; gap:9px; }
	    .inline-input {
	      flex:1 1 150px; min-height:34px; padding:7px 10px; border-radius:6px;
	      border:1px solid #344641; background:#070f0d; color:#dce8e3;
	      font-weight:750; outline:none;
	    }
	    .mini-chart {
      width:100%; height:220px; background:#07100d; border:1px solid #243530;
      border-radius:6px; display:block;
    }
    .dataset-actions { display:grid; gap:8px; }
    .lab-rec-row {
      display:grid; grid-template-columns:18px 64px 60px 50px 70px 1fr; gap:6px; align-items:center;
      padding:5px 7px; background:var(--panel2); border:1px solid var(--line);
      border-radius:6px; font-size:12px;
    }
    .lab-rec-row.this-session { border-left:3px solid var(--green); }
    .lab-rec-row input[type=checkbox] { margin:0; }
    .lab-rec-badge {
      display:inline-block; padding:1px 6px; border-radius:4px; font-weight:800;
      font-size:11px; text-align:center;
    }
    .lab-rec-badge.standard { background:#0d2118; color:var(--green); border:1px solid #286f4b; }
    .lab-rec-badge.compensating { background:#20190c; color:var(--amber); border:1px solid #6f551d; }
    .lab-rec-badge.non_standard { background:#241313; color:var(--red); border:1px solid #743333; }
    .lab-rec-badge.unknown { background:#17211f; color:var(--muted); border:1px solid #2d403b; }
    .lab-rec-fresh { color:var(--green); font-weight:800; font-size:11px; }
    .lab-rec-gate-ok { color:var(--green); }
    .lab-rec-gate-bad { color:var(--red); }
    table { width:100%; border-collapse:collapse; font-size:12px; }
    td, th { border-bottom:1px solid #243530; padding:7px 5px; text-align:left; vertical-align:top; }
    th { color:#9fb2ac; font-weight:800; }
    .result-ok { color:var(--green); font-weight:850; }
    .result-warn { color:var(--amber); font-weight:850; }
    .result-bad { color:var(--red); font-weight:850; }
    .mono { font-family:"SFMono-Regular", Consolas, monospace; font-size:12px; }
    .hidden { display:none !important; }
    @media (max-width:1050px) {
      .grid { grid-template-columns:1fr; }
      .scope-pair { grid-template-columns:1fr; }
      header { align-items:flex-start; flex-direction:column; }
      .topbar { justify-content:flex-start; }
      .scope-channel canvas { height:260px; min-height:220px; }
    }
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Lane B 弯举一体化工作台</h1>
      <div class="muted" id="runInfo">--</div>
    </div>
    <div class="row topbar">
      <span id="streamPill" class="pill warn">SSE --</span>
      <span id="hzPill" class="pill">Hz --</span>
      <span id="latencyPill" class="pill">延迟 --</span>
      <span id="signalPill" class="pill">信号 --</span>
      <span id="gruPill" class="pill">GRU --</span>
      <span id="repPill" class="pill">reps --</span>
    </div>
  </header>

  <div class="tabs">
    <button id="scopeTab" class="active" onclick="setMode('scope')">验收示波器</button>
    <button id="gruTab" onclick="setMode('gru')">GRU验收</button>
    <div class="row">
      <span class="pill">纵轴</span>
      <button class="compact" data-range="200" onclick="setFilteredRange(200)">±200</button>
      <button class="compact" data-range="500" onclick="setFilteredRange(500)">±500</button>
      <button class="compact active" data-range="1000" onclick="setFilteredRange(1000)">±1000</button>
      <button class="compact" data-range="4096" onclick="setFilteredRange(4096)">±4096</button>
    </div>
  </div>

  <div class="grid">
    <div class="scope">
      <div class="scope-layer">
        <div class="scope-layer-title">raw ADC 输入质量 <span>1000点 sample 示波器，固定 0-4095，对齐 freq.py</span></div>
        <div class="scope-pair">
          <section class="scope-channel raw">
            <div class="section-head">
              <strong>raw target_ch0</strong>
              <div class="legend">
                <span><i class="dot" style="background:#37b7ff"></i>ADC ch0</span>
                <span id="rawCh0StatsText">--</span>
                <span id="scopeHintText" class="scope-hint"></span>
              </div>
            </div>
            <canvas id="rawCh0Chart" width="680" height="280"></canvas>
          </section>
          <section class="scope-channel raw">
            <div class="section-head">
              <strong>raw comp_ch1</strong>
              <div class="legend">
                <span><i class="dot" style="background:#ffb42e"></i>ADC ch1</span>
                <span id="rawCh1StatsText">--</span>
              </div>
            </div>
            <canvas id="rawCh1Chart" width="680" height="280"></canvas>
          </section>
        </div>
      </div>

      <div class="scope-layer">
        <div class="scope-layer-title">processed 信号 <span>filtered / RMS / 激活，用来判断能否进入 7D 和 GRU</span></div>
        <div class="scope-pair">
          <section class="scope-channel">
            <div class="section-head">
              <strong>filtered target_ch0</strong>
              <div class="legend">
                <span><i class="dot" style="background:#37b7ff"></i>filtered ch0</span>
                <span id="ch0StatsText">--</span>
              </div>
            </div>
            <canvas id="filteredCh0Chart" width="680" height="240"></canvas>
          </section>
          <section class="scope-channel">
            <div class="section-head">
              <strong>filtered comp_ch1</strong>
              <div class="legend">
                <span><i class="dot" style="background:#ffb42e"></i>filtered ch1</span>
                <span id="ch1StatsText">--</span>
              </div>
            </div>
            <canvas id="filteredCh1Chart" width="680" height="240"></canvas>
          </section>
        </div>
      </div>
    </div>

    <aside class="side">
      <section>
        <div class="metrics">
          <div class="metric wide primary-state"><span>采集资格</span><strong id="captureEligibilityVal">等待数据</strong></div>
          <div class="metric"><span>传感器状态</span><strong id="sensorStateVal">--</strong></div>
          <div class="metric"><span>当前模式</span><strong id="modeVal">--</strong></div>
          <div class="metric"><span>raw gate</span><strong id="railVal">--</strong></div>
          <div class="metric"><span>RMS / 激活</span><strong id="rmsPctVal">--</strong></div>
          <div class="metric wide"><span>视觉证据</span><strong id="visionVal">--</strong></div>
          <div class="metric wide"><span>训练口径</span><strong id="mappingVal">stable_remap_pct · --</strong></div>
          <div class="metric wide"><span>当前组</span><strong id="currentGroupVal">未记录</strong></div>
          <div class="metric wide"><span>保存位置</span><strong class="mono" id="savePathVal">--</strong></div>
          <div class="metric wide"><span>下一步</span><strong id="nextStepVal">贴皮静息</strong></div>
        </div>
      </section>

	  <section id="gruPanel">
	    <div class="controls">
	      <div class="row">
	        <button data-exercise="bicep_curl" class="active" onclick="selectExercise('bicep_curl')">弯举</button>
	        <button data-exercise="squat" onclick="selectExercise('squat')">深蹲</button>
	      </div>
          <div class="row">
            <button onclick="switchBoardMode('bicep_curl', null)">切弯举</button>
            <button onclick="switchBoardMode(null, 'vision_sensor')">切GRU</button>
            <button onclick="switchBoardMode(null, 'pure_vision')">纯视觉</button>
          </div>
	      <div class="row">
	        <input id="customExerciseName" class="inline-input" placeholder="新动作名称" autocomplete="off">
	        <button onclick="createCustomExercise()">新建动作</button>
	      </div>
	      <div class="row">
	        <button data-label="standard" class="active" onclick="selectLabel('standard')">标准</button>
	        <button data-label="compensating" onclick="selectLabel('compensating')">代偿</button>
            <button data-label="non_standard" onclick="selectLabel('non_standard')">不标准</button>
          </div>
          <div class="row">
            <button class="primary" onclick="startRecording()">开始记录</button>
            <button class="warn" onclick="stopRecording()">结束记录</button>
          </div>
          <div class="metric wide"><span>本组标签</span><strong id="groupSourceVal">标准</strong></div>
          <div class="metric wide"><span>结果</span><strong id="diffVal">结束后保存文件</strong></div>
        </div>
      </section>

      <section id="lab-session-recordings-card">
        <div class="section-head">
          <strong id="labRecHeading">📦 本次录制 (0 组)</strong>
          <span class="pill" id="labRecPill">--</span>
        </div>
        <div class="metric wide"><span>分布</span><strong class="mono" id="labRecSummary">尚未录制</strong></div>
        <div class="row" style="margin:6px 0;">
          <button class="compact" onclick="labRecSelectAll(true)">全选</button>
          <button class="compact" onclick="labRecSelectAll(false)">清空</button>
          <button class="compact" onclick="labRecInvert()">反选</button>
          <button class="primary" onclick="labRecTrainSelected()" id="labRecTrainBtn">训练所选 0 组</button>
        </div>
        <div id="labRecRows" style="display:grid; gap:5px; max-height:280px; overflow-y:auto; margin-bottom:6px;"></div>
        <div class="metric wide"><span>合并数据集</span><strong class="mono" id="labRecBuildVal">等待选择</strong></div>
        <div class="metric wide"><span>训练</span><strong class="mono" id="labRecTrainVal">等待合并</strong></div>
        <div class="metric wide"><span>部署</span><strong class="mono" id="labRecDeployVal">等待训练</strong></div>
        <div class="metric wide" id="labRecErrorBox" style="display:none; border-color:#743333; background:#241313;"><span>错误</span><strong class="mono" id="labRecErrorVal" style="color:var(--red); white-space:pre-wrap;"></strong></div>
      </section>

      <section>
        <div class="section-head">
          <strong>数据集与训练</strong>
          <span class="pill" id="datasetPill">未导出</span>
        </div>
        <canvas id="featureChart" class="mini-chart" width="360" height="220"></canvas>
        <div class="dataset-actions">
          <div class="row">
            <button onclick="exportDataset()">导出数据集</button>
            <button class="primary" onclick="trainPersonalGru()">一键训练</button>
            <button class="warn" onclick="deployPersonalGru()">部署并测试</button>
          </div>
          <div class="metric wide"><span>数据集</span><strong class="mono" id="datasetVal">先录三类数据</strong></div>
          <div class="metric wide"><span>训练</span><strong class="mono" id="trainVal">等待导出</strong></div>
          <div class="metric wide"><span>部署</span><strong class="mono" id="deployVal">等待候选模型</strong></div>
        </div>
      </section>

      <section>
        <div class="section-head">
          <strong>live reps / GRU</strong>
          <span class="pill" id="liveRepPill">--</span>
        </div>
        <table>
          <thead><tr><th>#</th><th>视觉</th><th>GRU</th><th>来源</th><th>conf</th><th>ok</th></tr></thead>
          <tbody id="liveRepsRows"></tbody>
        </table>
      </section>

      <section>
        <div class="section-head">
          <strong>已保存组列表</strong>
          <span class="pill" id="summaryPill">--</span>
        </div>
        <table>
          <thead><tr><th>标签</th><th>rep</th><th>GRU</th><th>文件</th></tr></thead>
          <tbody id="groupsRows"></tbody>
        </table>
      </section>
    </aside>
  </div>
</main>
<script>
  let selectedLabel = 'standard';
  let selectedExercise = 'bicep_curl';
  let currentMode = 'scope';
  let latest = null;
  let source = null;
  let streamOk = false;
  let streamLastTs = 0;
  let streamRows = [];
  let lastDataset = null;
  let lastTrain = null;
  let lastDeploy = null;
  let liveReps = null;
  let labRecData = null;
  let labRecSelected = {};
  let labRecBuild = null;
  let labRecTrain = null;
  let labRecDeploy = null;
  let displayEndTs = null;
  let displayClockMs = 0;
  const MAX_ROWS = 2200;
  const RAW_WINDOW = 1000;
  const WINDOW_S = 1.0;
  const DISPLAY_LAG_S = 0.16;
  const RAW_ADC_MIN = 0;
  const RAW_ADC_MAX = 4095;
	  const FILTERED_Y_OPTIONS = [200, 500, 1000, 4096];
	  let filteredYRange = 1000;
	  const labelText = {standard:'标准', compensating:'代偿', non_standard:'不标准', unknown:'未知'};
	  const exerciseText = {squat:'深蹲', bicep_curl:'弯举'};
  const signalText = {
    udp_missing:'UDP缺失',
    floating_no_contact:'悬空态',
    contact_rest_candidate:'贴皮静息',
    active_candidate:'可测信号'
  };
  const sourceText = {
    gru:'GRU',
    visual:'视觉',
    visual_fallback_no_emg:'EMG无效',
    visual_fallback_no_model:'模型未加载',
    visual_fallback_no_window:'窗口不足',
    visual_fallback_model_error:'模型异常',
    unknown:'--'
  };

  function fmt(v, digits) {
    if (v === undefined || v === null || Number.isNaN(Number(v))) return '--';
    const n = Number(v);
    const d = digits === undefined ? (Math.abs(n) >= 10 ? 1 : 2) : digits;
    return n.toFixed(d);
  }
  function pct(v) {
    if (v === undefined || v === null || Number.isNaN(Number(v))) return '--';
    return `${Math.round(Number(v) * 100)}%`;
  }
  function setMode(mode) {
    currentMode = mode;
    document.getElementById('scopeTab').classList.toggle('active', mode === 'scope');
    document.getElementById('gruTab').classList.toggle('active', mode === 'gru');
    document.getElementById('gruPanel').classList.remove('hidden');
  }
  function setFilteredRange(range) {
    filteredYRange = FILTERED_Y_OPTIONS.includes(Number(range)) ? Number(range) : 1000;
    document.querySelectorAll('button[data-range]').forEach(b => b.classList.toggle('active', Number(b.dataset.range) === filteredYRange));
  }
  function selectLabel(v) {
    selectedLabel = v;
    document.querySelectorAll('button[data-label]').forEach(b => b.classList.toggle('active', b.dataset.label === v));
    render();
  }
	  function selectExercise(v) {
	    selectedExercise = actionSlug(v);
	    document.querySelectorAll('button[data-exercise]').forEach(b => b.classList.toggle('active', b.dataset.exercise === selectedExercise));
	    render();
	  }
	  function actionSlug(v) {
	    const clean = String(v || '').trim();
	    if (!clean) return 'custom_action';
	    const ascii = clean.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
	    if (ascii) return ascii.slice(0, 80);
	    return 'custom_' + Array.from(clean).map(ch => ch.charCodeAt(0).toString(16)).join('_').slice(0, 72);
	  }
	  function exerciseName(v) {
	    return exerciseText[v] || v || '--';
	  }
	  function createCustomExercise() {
	    const input = document.getElementById('customExerciseName');
	    const name = (input && input.value || '').trim();
	    if (!name) {
	      document.getElementById('nextStepVal').textContent = '请输入动作名称';
	      return;
	    }
	    const id = actionSlug(name);
	    if (!exerciseText[id]) exerciseText[id] = name;
	    let existing = document.querySelector(`button[data-exercise="${id}"]`);
	    if (!existing) {
	      const btn = document.createElement('button');
	      btn.dataset.exercise = id;
	      btn.textContent = name;
	      btn.onclick = () => selectExercise(id);
	      const firstRow = document.querySelector('#gruPanel .controls .row');
	      if (firstRow) firstRow.appendChild(btn);
	    }
	    if (input) input.value = '';
	    selectExercise(id);
	  }
  function latestSampleTs() {
    if (!streamRows.length) return null;
    return Number(streamRows[streamRows.length - 1][0]) || 0;
  }
  function updateDisplayClock() {
    const latestTs = latestSampleTs();
    if (!latestTs) return null;
    const targetTs = Math.max(0, latestTs - DISPLAY_LAG_S);
    const nowMs = performance.now();
    const elapsedS = displayClockMs ? Math.max(0, (nowMs - displayClockMs) / 1000) : 0;
    displayClockMs = nowMs;
    if (displayEndTs === null || targetTs - displayEndTs > WINDOW_S * 2 || displayEndTs - targetTs > 0.25) {
      displayEndTs = targetTs;
    } else if (targetTs > displayEndTs) {
      displayEndTs = Math.min(targetTs, displayEndTs + Math.max(elapsedS, 1 / 60) * 1.15);
    }
    return displayEndTs;
  }
  function rowsInWindow() {
    const endTs = updateDisplayClock();
    if (!endTs) return [];
    const startTs = endTs - WINDOW_S;
    let prior = null;
    const rows = [];
    for (const r of streamRows) {
      const ts = Number(r[0]) || 0;
      if (ts < startTs) {
        prior = r;
      } else if (ts <= endTs) {
        rows.push(r);
      }
    }
    if (prior && rows.length) rows.unshift(prior);
    return rows;
  }
  function rawScopeRows() {
    return streamRows.slice(-RAW_WINDOW);
  }
  function addRows(rows) {
    if (!Array.isArray(rows) || !rows.length) return;
    for (const r of rows) {
      const full = normalizeStreamRow(r);
      if (full) streamRows.push(full);
    }
    if (streamRows.length > MAX_ROWS) streamRows = streamRows.slice(-MAX_ROWS);
    streamLastTs = Date.now();
    streamOk = true;
  }
  function normalizeStreamRow(r) {
    if (!Array.isArray(r)) return null;
    if (r.length >= 10) {
      return [
        Number(r[0]) || 0,
        Number(r[1]) || 0,
        Number(r[2]) || 0,
        Number(r[3]) || 0,
        Number(r[4]) || 0,
        Number(r[5]) || 0,
        Number(r[6]) || 0,
        Number(r[7]) || 0,
        Number(r[8]) || 0,
        Number(r[9]) || 0
      ];
    }
    if (r.length >= 8) {
      return [
        Number(r[0]) || 0,
        0,
        0,
        Number(r[1]) || 0,
        Number(r[2]) || 0,
        Number(r[3]) || 0,
        Number(r[4]) || 0,
        Number(r[5]) || 0,
        Number(r[6]) || 0,
        Number(r[7]) || 0
      ];
    }
    return null;
  }
  function statRows(rows) {
    if (!rows.length) return {};
    const firstTs = Number(rows[0][0]) || 0;
    const lastTs = Number(rows[rows.length - 1][0]) || firstTs;
    const span = Math.max(0.001, lastTs - firstTs);
    return {
      hz:(rows.length - 1) / span,
      count:rows.length,
      raw0:Number(rows[rows.length - 1][1]) || 0,
      raw1:Number(rows[rows.length - 1][2]) || 0,
      rms0:Number(rows[rows.length - 1][5]) || 0,
      rms1:Number(rows[rows.length - 1][6]) || 0,
      pct0:Number(rows[rows.length - 1][7]) || 0,
      pct1:Number(rows[rows.length - 1][8]) || 0,
      packet:Number(rows[rows.length - 1][9]) || 0
    };
  }
  function channelStats(rows, filteredIdx, rmsIdx, pctIdx) {
    if (!rows.length) return {};
    const last = rows[rows.length - 1];
    return {
      filtered:Number(last[filteredIdx]) || 0,
      rms:Number(last[rmsIdx]) || 0,
      pct:Number(last[pctIdx]) || 0
    };
  }
  function rawGateText(h) {
    const stats = h.raw_stats || {};
    const channels = Array.isArray(stats.channels) ? stats.channels : [];
    const rail = channels.length ? Math.max(...channels.map(c => Number(c.railish_ratio) || 0), 0) : null;
    const jump = channels.length ? Math.max(...channels.map(c => Number(c.mean_abs_jump) || 0), 0) : null;
    const pct = h.emg_debug && Array.isArray(h.emg_debug.pct) ? h.emg_debug.pct : [];
    const pctText = pct.length >= 2 ? `${fmt(pct[0],0)}/${fmt(pct[1],0)}%` : '--';
    const extra = rail !== null ? `rail ${fmt(rail,2)} · jump ${fmt(jump,1)}` : `pct ${pctText}`;
    return `age ${fmt(h.raw_age_s,1)}s · ${signalText[h.signal_mode] || h.signal_mode || '--'} · ${extra}`;
  }
  function rawScopeStats(rows) {
    if (!rows.length) return {hz:null, rail0:null, rail1:null, jump0:null, jump1:null, bad:0};
    const recent = rows.slice(-RAW_WINDOW);
    const firstTs = Number(recent[0][0]) || 0;
    const lastTs = Number(recent[recent.length - 1][0]) || firstTs;
    const span = Math.max(0.001, lastTs - firstTs);
    function rail(idx) {
      return recent.filter(r => Number(r[idx]) <= 100 || Number(r[idx]) >= 3500).length / Math.max(1, recent.length);
    }
    function jump(idx) {
      if (recent.length < 2) return 0;
      let total = 0;
      for (let i=1;i<recent.length;i++) total += Math.abs((Number(recent[i][idx])||0) - (Number(recent[i-1][idx])||0));
      return total / (recent.length - 1);
    }
    return {
      hz:(recent.length - 1) / span,
      rail0:rail(1),
      rail1:rail(2),
      jump0:jump(1),
      jump1:jump(2),
      count:recent.length,
      bad:0
    };
  }
  function captureEligibility(h, rec) {
	    if (rec.active) return ['记录中', 'ok'];
	    if (!streamOk || !h.transport_ok) return ['可记录：无传感', 'warn'];
	    if (h.signal_mode === 'floating_no_contact') return ['可记录：悬空态', 'warn'];
	    if (!h.valid_for_gru) return ['可记录：EMG未达标', 'warn'];
	    return ['可采', 'ok'];
	  }
  function canvasReady(canvas) {
    const ratio = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(300, Math.floor(rect.width * ratio));
    const h = Math.max(180, Math.floor(rect.height * ratio));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w; canvas.height = h;
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {ctx, w:rect.width, h:rect.height};
  }
  function grid(ctx, w, h) {
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle = '#050807';
    ctx.fillRect(0,0,w,h);
    ctx.strokeStyle = '#17342d';
    ctx.lineWidth = 1;
    for (let i=0;i<=4;i++) {
      const y = i*h/4;
      ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(w,y); ctx.stroke();
    }
    for (let i=0;i<=10;i++) {
      const x = i*w/10;
      ctx.beginPath(); ctx.moveTo(x,0); ctx.lineTo(x,h); ctx.stroke();
    }
  }
  function drawSeries(ctx, rows, idx, color, minY, maxY, w, h, startTs, endTs) {
    if (rows.length < 2) return;
    const den = Math.max(1e-6, maxY - minY);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    let drawn = false;
    let previousTs = null;
    rows.forEach((r, i) => {
      const ts = Number(r[0]) || 0;
      const x = ((ts - startTs) / Math.max(0.001, endTs - startTs)) * w;
      const y = h - ((Number(r[idx]) || 0) - minY) / den * h;
      if (!drawn || previousTs === null || ts - previousTs > 0.25) {
        ctx.moveTo(x, y);
        drawn = true;
      } else {
        ctx.lineTo(x, y);
      }
      previousTs = ts;
    });
    ctx.stroke();
  }
  function drawChannel(canvasId, rows, idx, color, title) {
    const canvas = document.getElementById(canvasId);
    const {ctx, w, h} = canvasReady(canvas);
    grid(ctx, w, h);
    if (rows.length < 2) {
      ctx.fillStyle = '#6f817b'; ctx.font = '700 13px sans-serif';
      ctx.fillText('等待滤波波形', 14, 24); return;
    }
    const endTs = displayEndTs || Number(rows[rows.length - 1][0]) || 0;
    const startTs = endTs - WINDOW_S;
    drawSeries(ctx, rows, idx, color, -filteredYRange, filteredYRange, w, h, startTs, endTs);
    ctx.fillStyle = '#78918a'; ctx.font = '11px monospace';
    ctx.fillText(`${title}  ±${filteredYRange}  ${WINDOW_S.toFixed(1)}s`, 8, 15);
  }
  function drawRawChannel(canvasId, rows, idx, color, title) {
    const canvas = document.getElementById(canvasId);
    const {ctx, w, h} = canvasReady(canvas);
    grid(ctx, w, h);
    if (rows.length < 2) {
      ctx.fillStyle = '#6f817b'; ctx.font = '700 13px sans-serif';
      ctx.fillText('等待 raw ADC', 14, 24); return;
    }
    const den = RAW_ADC_MAX - RAW_ADC_MIN;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.0;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    rows.forEach((r, i) => {
      const x = rows.length > 1 ? (i / (rows.length - 1)) * w : 0;
      const y = h - ((Number(r[idx]) || 0) - RAW_ADC_MIN) / den * h;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.fillStyle = '#78918a'; ctx.font = '11px monospace';
    ctx.fillText(`${title}  0-4095  sample ${rows.length}/${RAW_WINDOW}`, 8, 15);
  }
  function drawFiltered() {
    const rows = rowsInWindow();
    const rawRows = rawScopeRows();
    drawRawChannel('rawCh0Chart', rawRows, 1, '#37b7ff', 'raw ch0');
    drawRawChannel('rawCh1Chart', rawRows, 2, '#ffb42e', 'raw ch1');
    drawChannel('filteredCh0Chart', rows, 3, '#37b7ff', 'filtered ch0');
    drawChannel('filteredCh1Chart', rows, 4, '#ffb42e', 'filtered ch1');
  }
  function drawFeatureChart(points) {
    const canvas = document.getElementById('featureChart');
    if (!canvas) return;
    const {ctx, w, h} = canvasReady(canvas);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#07100d';
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = '#243530';
    ctx.lineWidth = 1;
    for (let i=0;i<=4;i++) {
      const x = 34 + i * (w - 54) / 4;
      const y = 18 + i * (h - 48) / 4;
      ctx.beginPath(); ctx.moveTo(x, 18); ctx.lineTo(x, h - 30); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(34, y); ctx.lineTo(w - 20, y); ctx.stroke();
    }
    const valid = (points || []).filter(p => p && p.target_mean !== null && p.comp_mean !== null);
    if (!valid.length) {
      ctx.fillStyle = '#7f918b';
      ctx.font = '700 13px sans-serif';
      ctx.fillText('录完三类后显示特征差异', 14, 26);
      return;
    }
    const maxX = Math.max(100, ...valid.map(p => Number(p.target_mean) || 0)) * 1.12;
    const maxY = Math.max(100, ...valid.map(p => Number(p.comp_mean) || 0)) * 1.12;
    const colors = {standard:'#37b7ff', compensating:'#ff6969', non_standard:'#39d181'};
    function sx(x){ return 34 + (Number(x)||0) / maxX * (w - 54); }
    function sy(y){ return h - 30 - (Number(y)||0) / maxY * (h - 48); }
    valid.forEach(p => {
      ctx.fillStyle = colors[p.label] || '#ddd';
      ctx.beginPath(); ctx.arc(sx(p.target_mean), sy(p.comp_mean), 5, 0, Math.PI * 2); ctx.fill();
    });
    ctx.fillStyle = '#9fb2ac';
    ctx.font = '11px monospace';
    ctx.fillText('Target', w - 72, h - 8);
    ctx.fillText('Comp', 5, 18);
  }
  function shortPath(path) {
    if (!path) return '--';
    const parts = String(path).split('/');
    return parts.slice(-2).join('/');
  }
  function nextStep(h, rec) {
	    if (rec.active) return '做完后点结束记录';
	    if (!streamOk) return '可记录，本轮不计传感波形';
	    if (!h.transport_ok) return '可记录，本轮不计EMG';
	    if (h.signal_mode === 'floating_no_contact') return '悬空态：突刺不代表可用肌电';
	    if (!h.valid_for_gru) return '可记录，但不计GRU';
	    return '选标签，点开始记录';
	  }
  function render() {
    const h = latest && latest.health || {};
    const rec = latest && (latest.recording || latest.validation) || {};
    const groups = Array.isArray(rec.groups) ? rec.groups : [];
    const lastGroup = groups.length ? groups[groups.length - 1] : {};
    const rows = rowsInWindow();
    const rawRows = rawScopeRows();
    const st = statRows(rows);
    const rawSt = rawScopeStats(rawRows);
    const ch0 = channelStats(rows, 3, 5, 7);
    const ch1 = channelStats(rows, 4, 6, 8);
    const fsm = latest && latest.fsm || {};
    const streamAge = streamLastTs ? (Date.now() - streamLastTs) / 1000 : null;
    const vision = latest && latest.vision_evidence || {};
    const kq = vision.kpt_quality || {};
    const angle = vision.angle_debug || {};
    const mapping = h.emg_mapping_summary || {};
    const mappingChannels = Array.isArray(mapping.channels) ? mapping.channels : [];
    const m0 = mappingChannels[0] || {};
    const m1 = mappingChannels[1] || {};
    const eligibility = captureEligibility(h, rec);
    document.getElementById('runInfo').textContent = latest ? `${latest.board_url} · ${latest.run_dir}` : '--';
    document.getElementById('streamPill').textContent = streamOk ? `SSE ${fmt(streamAge)}s` : 'SSE --';
    document.getElementById('streamPill').className = streamOk && streamAge < 1.5 ? 'pill ok' : 'pill warn';
    document.getElementById('hzPill').textContent = `Hz ${fmt(st.hz,0)}`;
    document.getElementById('latencyPill').textContent = `延迟 ${fmt(streamAge)}s`;
    document.getElementById('signalPill').textContent = signalText[h.signal_mode] || h.signal_mode || '信号 --';
    document.getElementById('signalPill').className = h.signal_mode === 'floating_no_contact' ? 'pill warn' : (h.valid_for_gru ? 'pill ok' : 'pill bad');
    const latestRep = liveReps && liveReps.latest || {};
    document.getElementById('gruPill').textContent = latestRep.id ? `${sourceText[latestRep.classification_source] || latestRep.classification_source || '--'} ${latestRep.prediction_cn || latestRep.prediction || '--'}` : (rec.active ? '记录中' : `已保存 ${groups.length}`);
    document.getElementById('gruPill').className = latestRep.classification_source === 'gru' ? 'pill ok' : (rec.active ? 'pill ok' : 'pill');
    document.getElementById('repPill').textContent = `reps ${(liveReps && liveReps.count) || 0}`;
    document.getElementById('captureEligibilityVal').textContent = eligibility[0];
    document.getElementById('captureEligibilityVal').parentElement.className = `metric wide primary-state ${eligibility[1]}`;
    document.getElementById('sensorStateVal').textContent = `${signalText[h.signal_mode] || '--'} / ${h.valid_for_gru ? '可验收' : '仅记录'}`;
    document.getElementById('modeVal').textContent = `${h.fsm_exercise || fsm.exercise || '--'} / ${h.inference_mode || fsm.inference_mode || '--'}`;
    document.getElementById('railVal').textContent = rawGateText(h);
    document.getElementById('rmsPctVal').textContent = `${fmt(st.rms0,1)}/${fmt(st.rms1,1)} · ${fmt(st.pct0,0)}/${fmt(st.pct1,0)}%`;
    document.getElementById('visionVal').textContent =
      `pose ${vision.valid_person ? '有效' : '无'} · frame ${vision.frame_idx ?? '--'} · conf ${fmt(kq.mean,2)} · angle ${fmt(angle.smooth_angle ?? angle.angle ?? angle.raw_angle,1)} · side ${angle.selected_side || angle.active_side || '--'}`;
    document.getElementById('mappingVal').textContent =
      `raw_rms_robust100 · old/400 ${fmt((m0.old_pct_400 || {}).mean,0)}/${fmt((m1.old_pct_400 || {}).mean,0)}% · pct ${fmt((m0.current_pct || {}).mean,0)}/${fmt((m1.current_pct || {}).mean,0)}%`;
    document.getElementById('currentGroupVal').textContent = rec.active ? `${labelText[rec.label] || rec.label} / ${rec.group_id || '--'}` : '未记录';
    document.getElementById('savePathVal').textContent = rec.last_group_save_path || rec.capture_out_dir || (latest && latest.run_dir) || '--';
    document.getElementById('nextStepVal').textContent = nextStep(h, rec);
	    document.getElementById('groupSourceVal').textContent = `${exerciseName(selectedExercise)} / ${labelText[selectedLabel] || selectedLabel}`;
    document.getElementById('diffVal').textContent = lastGroup.save_path ? `已保存 ${shortPath(lastGroup.save_path)}` : '结束后保存文件';
    document.getElementById('summaryPill').textContent = groups.length ? `${groups.length} 组` : '--';
    document.getElementById('datasetPill').textContent = lastDataset ? (lastDataset.ok ? '已导出' : '导出失败') : '未导出';
    document.getElementById('datasetPill').className = lastDataset ? (lastDataset.ok ? 'pill ok' : 'pill bad') : 'pill';
    document.getElementById('datasetVal').textContent = lastDataset ? (lastDataset.out_dir_rel || lastDataset.out_dir || '--') : '先录三类数据';
    const trainPassed = lastTrain && lastTrain.report && lastTrain.report.passed_acceptance;
    document.getElementById('trainVal').textContent = lastTrain ? `${trainPassed ? '通过' : '已生成'} · ${lastTrain.model_path_rel || lastTrain.model_path || '--'}` : '等待导出';
    document.getElementById('deployVal').textContent = lastDeploy ? `${lastDeploy.ok ? '已部署' : '失败'} · ${lastDeploy.remote_model || lastDeploy.error || '--'}` : '等待候选模型';
    drawFeatureChart(rec.feature_points || []);
    document.getElementById('rawCh0StatsText').textContent = `Hz≈${fmt(rawSt.hz,0)} · rail ${fmt(rawSt.rail0,2)} · jump ${fmt(rawSt.jump0,1)}`;
    document.getElementById('rawCh1StatsText').textContent = `Hz≈${fmt(rawSt.hz,0)} · rail ${fmt(rawSt.rail1,2)} · jump ${fmt(rawSt.jump1,1)} · bad_line ${rawSt.bad || 0}`;
    document.getElementById('ch0StatsText').textContent = `filtered ${fmt(ch0.filtered,1)} · RMS ${fmt(ch0.rms,1)} · 激活 ${fmt(ch0.pct,0)}%`;
    document.getElementById('ch1StatsText').textContent = `filtered ${fmt(ch1.filtered,1)} · RMS ${fmt(ch1.rms,1)} · 激活 ${fmt(ch1.pct,0)}%`;
    document.getElementById('scopeHintText').textContent = h.signal_mode === 'floating_no_contact' ? '悬空态，突刺不代表可用肌电' : '';
    document.getElementById('groupsRows').innerHTML = groups.slice().reverse().map(g => {
      const score = g.gru_rep_count ? `${g.correct || 0}/${g.gru_rep_count}` : '0/0';
	      const saved = g.custom_action_save_path_rel || g.custom_action_save_path || g.save_path;
	      return `<tr><td>${exerciseName(g.exercise)} ${g.label_cn || labelText[g.label] || g.label}</td><td>${g.rep_count || 0}</td><td>${score}</td><td class="mono">${shortPath(saved)}</td></tr>`;
	    }).join('');
    document.getElementById('liveRepPill').textContent = liveReps ? `${liveReps.count || 0} reps` : '--';
    document.getElementById('liveRepsRows').innerHTML = (liveReps && Array.isArray(liveReps.reps) ? liveReps.reps.slice().reverse().slice(0, 12) : []).map(r => {
      const ok = r.correct ? '<span class="result-ok">✓</span>' : (r.classification_source === 'gru' ? '<span class="result-bad">×</span>' : '<span class="result-warn">--</span>');
      return `<tr><td>${r.rep_index || r.id || '--'}</td><td>${labelText[r.visual_result] || r.visual_result || '--'}</td><td>${r.prediction_cn || r.prediction || '--'}</td><td>${sourceText[r.classification_source] || r.classification_source || '--'}</td><td>${fmt(r.confidence,2)}</td><td>${ok}</td></tr>`;
    }).join('');
	  }
  async function loadStatus() {
    try { latest = await (await fetch('/api/status', {cache:'no-store'})).json(); render(); } catch(e) {}
  }
  async function loadLiveReps() {
    try {
      liveReps = await (await fetch(`/api/live_reps?exercise=${encodeURIComponent(selectedExercise)}&label=${encodeURIComponent(selectedLabel)}&limit=30`, {cache:'no-store'})).json();
      render();
    } catch(e) {}
  }
  async function switchBoardMode(exercise, inferenceMode) {
    document.getElementById('nextStepVal').textContent = '切换中...';
    const res = await fetch('/api/board_mode/switch', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({exercise:exercise, inference_mode:inferenceMode})
    });
    const body = await res.json();
    document.getElementById('nextStepVal').textContent = body.ok ? '模式已同步' : `切换失败：${body.error || '--'}`;
    await loadStatus();
    await loadLiveReps();
  }
  async function startRecording() {
    const res = await fetch('/api/recording/start', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({label:selectedLabel, exercise:selectedExercise})
    });
    const body = await res.json();
    if (body && body.blocked) document.getElementById('nextStepVal').textContent = `禁止：${body.reason || signalText[body.signal_mode] || body.signal_mode}`;
    await loadStatus();
  }
  async function stopRecording() {
    await fetch('/api/recording/stop', {method:'POST'});
    await loadStatus();
  }
  async function exportDataset() {
    document.getElementById('datasetVal').textContent = '导出中...';
    const res = await fetch('/api/personal_dataset/export', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({exercise:selectedExercise})
    });
    lastDataset = await res.json();
    render();
  }
  async function trainPersonalGru() {
    document.getElementById('trainVal').textContent = '训练中...';
    const res = await fetch('/api/personal_dataset/train', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        exercise:selectedExercise,
        data_dir:lastDataset && lastDataset.out_dir,
        epochs:30
      })
    });
    lastTrain = await res.json();
    render();
  }
  async function deployPersonalGru() {
    document.getElementById('deployVal').textContent = '部署中...';
    const res = await fetch('/api/personal_dataset/deploy', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        model_path:lastTrain && lastTrain.model_path,
        data_dir:lastDataset && lastDataset.out_dir
      })
    });
    lastDeploy = await res.json();
    render();
    await loadStatus();
    await loadLiveReps();
  }
	  function connectStream() {
    try {
      if (source) source.close();
      source = new EventSource('/api/emg_stream');
      source.addEventListener('emg', ev => {
        const body = JSON.parse(ev.data || '{}');
        addRows(body.stream_samples || body.samples || []);
      });
      source.onerror = () => { streamOk = false; };
	    } catch(e) {
	      streamOk = false;
	    }
	  }
	  function frame() {
    drawFiltered();
    render();
    requestAnimationFrame(frame);
  }
  function labRecSetError(msg) {
    const box = document.getElementById('labRecErrorBox');
    const val = document.getElementById('labRecErrorVal');
    if (!box || !val) return;
    if (msg) { val.textContent = String(msg); box.style.display = ''; }
    else { val.textContent = ''; box.style.display = 'none'; }
  }
  function labRecBadgeClass(label) {
    if (label === 'standard' || label === 'compensating' || label === 'non_standard') return label;
    return 'unknown';
  }
  function labRecRenderRows() {
    const rowsEl = document.getElementById('labRecRows');
    if (!rowsEl) return;
    const items = (labRecData && Array.isArray(labRecData.items)) ? labRecData.items : [];
    rowsEl.innerHTML = items.map(it => {
      const checked = labRecSelected[it.group_path] ? 'checked' : '';
      const fresh = it.is_this_session ? '<span class="lab-rec-fresh">·刚录</span>' : '';
      const startG = it.start_gate_ok ? '<span class="lab-rec-gate-ok">S✓</span>' : '<span class="lab-rec-gate-bad">S×</span>';
      const endG = it.end_gate_ok ? '<span class="lab-rec-gate-ok">E✓</span>' : '<span class="lab-rec-gate-bad">E×</span>';
      const cls = 'lab-rec-row' + (it.is_this_session ? ' this-session' : '');
      const dur = fmt(it.duration_s, 1);
      const counts = `${it.stream_rows||0}/${it.gru_7d_rows||0}行 · ${it.rep_count||0}rep`;
      return `<div class="${cls}">
        <input type="checkbox" ${checked} data-path="${it.group_path}" onchange="labRecToggle(this)">
        <span class="mono">${it.created_ts_iso || '--'}</span>
        <span class="lab-rec-badge ${labRecBadgeClass(it.label)}">${it.label_cn || it.label}</span>
        <span class="mono">${dur}s</span>
        <span class="mono">${startG} ${endG}</span>
        <span class="mono" style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${counts}${fresh}</span>
      </div>`;
    }).join('');
  }
  function labRecRenderSummary() {
    const items = (labRecData && Array.isArray(labRecData.items)) ? labRecData.items : [];
    const cnt = {standard:0, compensating:0, non_standard:0};
    items.forEach(it => { if (cnt[it.label] !== undefined) cnt[it.label] += 1; });
    const missing = [];
    if (cnt.standard === 0) missing.push('标准');
    if (cnt.compensating === 0) missing.push('代偿');
    if (cnt.non_standard === 0) missing.push('不标准');
    document.getElementById('labRecHeading').textContent = `📦 本次录制 (${items.length} 组)`;
    const pill = document.getElementById('labRecPill');
    if (items.length === 0) { pill.textContent = '空'; pill.className = 'pill'; }
    else if (missing.length === 0) { pill.textContent = '三类齐'; pill.className = 'pill ok'; }
    else { pill.textContent = '缺' + missing.join('/'); pill.className = 'pill warn'; }
    document.getElementById('labRecSummary').textContent =
      items.length === 0 ? '尚未录制'
      : `${cnt.standard}标准 / ${cnt.compensating}代偿 / ${cnt.non_standard}不标准` +
        (missing.length ? ` · 缺 ${missing.join('/')}` : '');
    const selCount = Object.values(labRecSelected).filter(Boolean).length;
    document.getElementById('labRecTrainBtn').textContent = `训练所选 ${selCount} 组`;
  }
  function labRecToggle(el) {
    labRecSelected[el.dataset.path] = !!el.checked;
    labRecRenderSummary();
  }
  function labRecSelectAll(value) {
    const items = (labRecData && Array.isArray(labRecData.items)) ? labRecData.items : [];
    if (value) { items.forEach(it => { labRecSelected[it.group_path] = true; }); }
    else { labRecSelected = {}; }
    labRecRenderRows();
    labRecRenderSummary();
  }
  function labRecInvert() {
    const items = (labRecData && Array.isArray(labRecData.items)) ? labRecData.items : [];
    items.forEach(it => { labRecSelected[it.group_path] = !labRecSelected[it.group_path]; });
    labRecRenderRows();
    labRecRenderSummary();
  }
  async function loadLabSessionRecordings() {
    try {
      const res = await fetch('/api/lab_session_recordings', {cache:'no-store'});
      labRecData = await res.json();
      labRecRenderRows();
      labRecRenderSummary();
    } catch(e) {}
  }
  async function labRecTrainSelected() {
    labRecSetError(null);
    const paths = Object.keys(labRecSelected).filter(k => labRecSelected[k]);
    if (paths.length === 0) { labRecSetError('请先勾选至少一组数据'); return; }
    document.getElementById('labRecBuildVal').textContent = `合并 ${paths.length} 组中...`;
    document.getElementById('labRecTrainVal').textContent = '等待合并';
    document.getElementById('labRecDeployVal').textContent = '等待训练';
    let buildRes;
    try {
      const r = await fetch('/api/personal_dataset/build_from_groups', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({group_paths:paths, exercise:selectedExercise})
      });
      buildRes = await r.json();
    } catch(e) { labRecSetError('build 请求失败: '+e); document.getElementById('labRecBuildVal').textContent='失败'; return; }
    labRecBuild = buildRes;
    if (!buildRes.ok) {
      labRecSetError((buildRes.error || 'build_failed') + (buildRes.missing ? (' :: '+buildRes.missing) : ''));
      document.getElementById('labRecBuildVal').textContent = '失败: ' + (buildRes.error || '--');
      return;
    }
    document.getElementById('labRecBuildVal').textContent = `已合并 ${buildRes.group_count} 组 · ${buildRes.out_dir_rel || buildRes.out_dir}`;
    document.getElementById('labRecTrainVal').textContent = '训练中...';
    let trainRes;
    try {
      const r2 = await fetch('/api/personal_dataset/train', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({exercise:selectedExercise, data_dir:buildRes.out_dir, epochs:30})
      });
      trainRes = await r2.json();
    } catch(e) { labRecSetError('train 请求失败: '+e); document.getElementById('labRecTrainVal').textContent='失败'; return; }
    labRecTrain = trainRes;
    if (!trainRes.ok) {
      labRecSetError(trainRes.error || (trainRes.report && trainRes.report.error) || 'train_failed');
      document.getElementById('labRecTrainVal').textContent = '失败: ' + (trainRes.error || '--');
      return;
    }
    document.getElementById('labRecTrainVal').textContent = `已生成 · ${trainRes.model_path_rel || trainRes.model_path}`;
    document.getElementById('labRecDeployVal').textContent = '部署中...';
    let deployRes;
    try {
      const r3 = await fetch('/api/personal_dataset/deploy', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({model_path:trainRes.model_path, data_dir:buildRes.out_dir})
      });
      deployRes = await r3.json();
    } catch(e) { labRecSetError('deploy 请求失败: '+e); document.getElementById('labRecDeployVal').textContent='失败'; return; }
    labRecDeploy = deployRes;
    if (!deployRes.ok) {
      labRecSetError(deployRes.error || 'deploy_failed');
      document.getElementById('labRecDeployVal').textContent = '失败: ' + (deployRes.error || '--');
      return;
    }
    document.getElementById('labRecDeployVal').textContent = `已部署 · ${deployRes.remote_model || '--'}`;
  }
  setMode('gru');
  connectStream();
  setInterval(loadStatus, 1000);
  setInterval(loadLiveReps, 700);
  setInterval(loadLabSessionRecordings, 1500);
  loadStatus();
  loadLiveReps();
  loadLabSessionRecordings();
  requestAnimationFrame(frame);
</script>
</body>
</html>
"""


def create_app(session):
    app = Flask(__name__)

    @app.after_request
    def add_cors_headers(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp

    @app.route("/")
    def index():
        return HTML

    @app.route("/favicon.ico")
    def favicon():
        return Response(status=204)

    @app.route("/api/status")
    def api_status():
        if not session.latest:
            session.read_board_snapshot()
        return jsonify(session.latest)

    @app.route("/api/emg_fast")
    def api_emg_fast():
        with session.raw_lock:
            data = dict(session.raw_cache)
        if not data.get("ok"):
            data = session.refresh_fast_wave()
        return jsonify(data)

    @app.route("/api/ingest_stream", methods=["POST"])
    def api_ingest_stream():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(session.ingest_stream_samples(body.get("samples") or []))

    @app.route("/api/emg_stream")
    def api_emg_stream():
        resp = Response(session.stream_events(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    @app.route("/api/reference_waveforms")
    def api_reference_waveforms():
        return jsonify(session.reference_waveforms)

    @app.route("/api/recording/start", methods=["POST"])
    def api_recording_start():
        body = request.get_json(force=True, silent=True) or {}
        label = body.get("label", "standard")
        exercise = body.get("exercise", "bicep_curl")
        return jsonify(session.start_recording(label=label, exercise=exercise))

    @app.route("/api/recording/stop", methods=["POST"])
    def api_recording_stop():
        return jsonify(session.stop_recording())

    @app.route("/api/recording/groups")
    def api_recording_groups():
        return jsonify(session.recording_state())

    @app.route("/api/live_reps")
    def api_live_reps():
        exercise = request.args.get("exercise", "bicep_curl")
        label = request.args.get("label")
        limit = _safe_int(request.args.get("limit"), 30)
        return jsonify(session.live_reps(exercise=exercise, label=label, limit=limit))

    @app.route("/api/board_mode/switch", methods=["POST"])
    def api_board_mode_switch():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(session.switch_board_mode(
            exercise=body.get("exercise"),
            inference_mode=body.get("inference_mode"),
        ))

    @app.route("/api/personal_dataset/export", methods=["POST"])
    def api_personal_dataset_export():
        body = request.get_json(force=True, silent=True) or {}
        exercise = body.get("exercise", "bicep_curl")
        return jsonify(session.export_personal_dataset(exercise=exercise))

    @app.route("/api/personal_dataset/train", methods=["POST"])
    def api_personal_dataset_train():
        body = request.get_json(force=True, silent=True) or {}
        exercise = body.get("exercise", "bicep_curl")
        data_dir = body.get("data_dir")
        epochs = _safe_int(body.get("epochs"), 30)
        return jsonify(session.train_personal_gru(exercise=exercise, data_dir=data_dir, epochs=epochs))

    @app.route("/api/personal_dataset/deploy", methods=["POST"])
    def api_personal_dataset_deploy():
        body = request.get_json(force=True, silent=True) or {}
        return jsonify(session.deploy_personal_gru(
            candidate_path=body.get("candidate_path") or body.get("model_path"),
            data_dir=body.get("data_dir"),
        ))

    @app.route("/api/lab_session_recordings")
    def api_lab_session_recordings():
        return jsonify(session.list_lab_session_recordings())

    @app.route("/api/personal_dataset/build_from_groups", methods=["POST"])
    def api_personal_dataset_build_from_groups():
        body = request.get_json(force=True, silent=True) or {}
        exercise = body.get("exercise", "bicep_curl")
        group_paths = body.get("group_paths") or []
        return jsonify(session.build_personal_dataset_from_groups(
            group_paths=group_paths,
            exercise=exercise,
        ))

    @app.route("/api/start_validation", methods=["POST"])
    def api_start_validation():
        body = request.get_json(force=True, silent=True) or {}
        exercise = body.get("exercise", "bicep_curl")
        label = body.get("label", "standard")
        return jsonify(session.start_validation(exercise, label))

    @app.route("/api/stop_validation", methods=["POST"])
    def api_stop_validation():
        return jsonify(session.stop_validation())

    return app


def main():
    parser = argparse.ArgumentParser(description="IronBuddy Lane B Sensor Lab")
    parser.add_argument("--board-ip", default=DEFAULT_BOARD_IP)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-raw-poll", action="store_true",
                        help="disable background SSH raw snapshot polling; SSE fallback may be less informative")
    args = parser.parse_args()
    session = SensorLabSession(args.board_ip, RUNS_ROOT)
    threading.Thread(target=session.background_loop, daemon=True).start()
    threading.Thread(target=session.vision_loop, daemon=True).start()
    if not args.no_raw_poll:
        threading.Thread(target=session.raw_loop, daemon=True).start()
    app = create_app(session)
    print("Sensor Lab: http://%s:%s/ board=%s run=%s" % (
        args.host, args.port, args.board_ip, session.run_dir))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
