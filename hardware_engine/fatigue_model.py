# -*- coding: utf-8 -*-
"""Explainable fatigue scoring for IronBuddy.

The live contract stays simple: callers provide visual/EMG/context features and
receive a cumulative score plus an increment breakdown. The model is stdlib-only
and Python 3.7 compatible for the Toybrick board.
"""

from __future__ import absolute_import

import json
import os
import time


MODEL_VERSION = "dose_integral_v1"
DEFAULT_D_TARGET = 7.0
DEFAULT_TARGET_FATIGUE = 1500.0
DEFAULT_SNAPSHOT_PATH = (
    "/dev/shm/ironbuddy_fatigue_snapshots.jsonl"
    if os.path.isdir("/dev/shm")
    else "/tmp/ironbuddy_fatigue_snapshots.jsonl"
)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _clamp(value, lo, hi):
    value = _safe_float(value, lo)
    return max(float(lo), min(float(hi), value))


def _is_missing(value):
    if value is None:
        return True
    try:
        return float(value) <= 0.0
    except Exception:
        return True


def _component(name, value, status="ok", detail=""):
    item = {
        "name": name,
        "value": round(_safe_float(value), 3),
        "status": status,
    }
    if detail:
        item["detail"] = str(detail)
    return item


def _quality_key(features):
    return str(
        features.get("result") or
        features.get("final_result") or
        features.get("quality") or
        "unknown"
    ).strip()


def _phase_key(features):
    return str(
        features.get("phase") or
        features.get("fsm_state") or
        features.get("state") or
        "UNKNOWN"
    ).strip().upper()


def _phase_weight(features):
    phase = _phase_key(features)
    if not bool(features.get("is_training", True)):
        return 0.0
    if phase in ("REST", "HOLD", "STAND", "IDLE", "NO_PERSON", "STOP"):
        return 0.0
    if phase in ("DOWN", "LOWER", "ECCENTRIC"):
        return 0.75
    if phase in ("UP", "RAISE", "CONCENTRIC", "CURLING", "SQUAT"):
        return 1.0
    return 0.7


def _rom_weight(features):
    rom = _safe_float(features.get("rom"), 0.0)
    if rom <= 0.0:
        return 0.2
    return _clamp(rom / 90.0, 0.2, 1.0)


def _class_weight(features):
    result = _quality_key(features)
    if result in ("standard", "good", "qualified"):
        return 1.0
    if result in ("compensating", "compensation"):
        return 0.65
    if result in ("non_standard", "failed", "bad"):
        return 0.25
    return 0.5


def _visual_components(features):
    q_phase = _phase_weight(features)
    q_rom = _rom_weight(features)
    q_class = _class_weight(features)
    q_vis = _clamp(q_phase * q_rom * q_class, 0.0, 1.0)
    return q_vis, [
        _component("q_phase", q_phase),
        _component("q_rom", q_rom),
        _component("q_class", q_class),
        _component("q_vis", q_vis),
    ]


def _legacy_visual_increment(features):
    rep_count = max(1, _safe_int(features.get("rep_count") or features.get("rep_index"), 1))
    rom = _safe_float(features.get("rom"), 0.0)
    min_angle = _safe_float(features.get("min_angle"), 999.0)
    velocity = abs(_safe_float(features.get("angle_velocity"), 0.0))
    acceleration = abs(_safe_float(features.get("angle_acceleration"), 0.0))
    result = _quality_key(features)
    comp_count = max(0, _safe_int(features.get("compensation_count"), 0))

    base = 120.0
    rom_factor = _clamp(rom / 90.0, 0.35, 1.35)
    depth_factor = 1.0
    if min_angle < 999.0:
        depth_factor = _clamp((120.0 - min_angle) / 80.0, 0.35, 1.25)
    tempo_factor = _clamp(1.0 + velocity / 90.0 + acceleration / 220.0, 0.75, 1.65)
    quality_factor = 1.0
    if result in ("non_standard", "failed", "bad"):
        quality_factor = 0.72
    elif result in ("compensating", "compensation"):
        quality_factor = 1.18
    comp_factor = 1.0 + min(0.4, comp_count * 0.04)
    value = base * rom_factor * depth_factor * tempo_factor * quality_factor * comp_factor
    return value, [
        _component("visual_base", base),
        _component("rom_factor", rom_factor),
        _component("depth_factor", depth_factor, "ok" if min_angle < 999.0 else "missing"),
        _component("tempo_factor", tempo_factor),
        _component("quality_factor", quality_factor),
        _component("compensation_factor", comp_factor),
        _component("rep_count", rep_count),
    ]


def _signal_gate(features):
    simulated = bool(features.get("emg_simulated") or features.get("simulated"))
    floating = str(features.get("signal_mode") or "").strip() == "floating_no_contact"
    emg_valid = bool(features.get("emg_valid", True))
    pose_valid = bool(features.get("pose_valid", True))
    is_training = bool(features.get("is_training", True))
    if not is_training:
        return 0.0, "not_training", "rest_or_idle"
    if simulated:
        return 0.35, "missing", "simulated_emg"
    if floating:
        return 0.2, "missing", "floating_no_contact"
    if not emg_valid:
        return 0.2, "missing", "invalid_emg"
    if not pose_valid:
        return 0.45, "degraded", "pose_degraded"
    return 1.0, "ok", ""


def _integration_dt_seconds(features):
    raw_dt = features.get("dt_s", features.get("dt"))
    if raw_dt is None:
        return 1.0
    mode = str(features.get("integration_mode") or "rep").strip().lower()
    if mode in ("frame", "sample", "window"):
        return _clamp(raw_dt, 0.005, 0.2)
    return _clamp(raw_dt, 0.05, 4.0)


def _dose_terms(features):
    target_rms = _safe_float(features.get("target_rms"), 0.0)
    if target_rms <= 0.0:
        target_rms = _safe_float(features.get("activation_pct"), 0.0)
    comp_rms = _safe_float(features.get("compensation_rms"), 0.0)
    if comp_rms <= 0.0:
        comp_rms = _safe_float(features.get("comp_pct"), 0.0)
    target_mvc = _safe_float(features.get("target_mvc"), 0.0)
    if target_mvc <= 0.0:
        target_mvc = 100.0 if target_rms <= 100.0 else 400.0
    lambda_comp = _safe_float(features.get("lambda_comp"), 0.3)
    a_target = _clamp(target_rms / max(target_mvc, 1e-6), 0.0, 1.5)
    if a_target <= 0.0:
        # Keep pure-vision acceptance usable, but mark the dose as estimated.
        a_target = 0.28 * _class_weight(features)
    c_comp = _clamp(comp_rms / max(target_rms, 1e-6), 0.0, 2.0)
    p_comp = _clamp(1.0 - lambda_comp * c_comp, 0.0, 1.0)
    q_vis, visual_components = _visual_components(features)
    v_signal, signal_status, signal_detail = _signal_gate(features)
    if target_rms <= 0.0:
        v_signal = min(v_signal, 0.2)
        signal_status = "missing"
        signal_detail = signal_detail or "target_inactive"
    dt_s = _integration_dt_seconds(features)
    instant_load = a_target * q_vis * p_comp * v_signal
    increment_d_eff = instant_load * dt_s
    d_target = _safe_float(features.get("d_target"), DEFAULT_D_TARGET)
    if d_target <= 0.0:
        d_target = DEFAULT_D_TARGET
    target_fatigue = _safe_float(features.get("target_fatigue"), DEFAULT_TARGET_FATIGUE)
    if target_fatigue <= 0.0:
        target_fatigue = DEFAULT_TARGET_FATIGUE
    emg_status = "ok" if target_rms > 0.0 and signal_status == "ok" else "missing"
    return {
        "target_rms": target_rms,
        "comp_rms": comp_rms,
        "target_mvc": target_mvc,
        "a_target": a_target,
        "c_comp": c_comp,
        "p_comp": p_comp,
        "q_vis": q_vis,
        "v_signal": v_signal,
        "dt_s": dt_s,
        "instant_load": instant_load,
        "increment_d_eff": increment_d_eff,
        "d_target": d_target,
        "target_fatigue": target_fatigue,
        "visual_components": visual_components,
        "signal_status": signal_status,
        "signal_detail": signal_detail,
        "emg_status": emg_status,
    }


def compute_fatigue(features=None, previous_score=0.0, now=None):
    """Compute cumulative target-muscle effective activation dose."""
    features = features if isinstance(features, dict) else {}
    prev = max(0.0, _safe_float(previous_score, 0.0))
    terms = _dose_terms(features)
    scale = terms["target_fatigue"] / max(terms["d_target"], 1e-6)
    increment = max(0.0, terms["increment_d_eff"] * scale)
    score = prev + increment
    d_eff_total = score / max(scale, 1e-6)
    progress_pct = 100.0 * d_eff_total / max(terms["d_target"], 1e-6)
    components = {
        "dose": [
            _component("a_target", terms["a_target"]),
            _component("q_vis", terms["q_vis"]),
            _component("p_comp", terms["p_comp"]),
            _component("v_signal", terms["v_signal"], terms["signal_status"],
                       terms["signal_detail"]),
            _component("dt_s", terms["dt_s"]),
        ],
        "visual": terms["visual_components"],
        "emg": [
            _component("target_rms", terms["target_rms"], terms["emg_status"]),
            _component("target_mvc", terms["target_mvc"]),
            _component("comp_rms", terms["comp_rms"]),
            _component("c_comp", terms["c_comp"]),
        ],
        "signal": [
            _component("v_signal", terms["v_signal"], terms["signal_status"],
                       terms["signal_detail"]),
        ],
        "context": [
            _component("current_set", _safe_int(features.get("current_set"), 1)),
            _component("previous_set_fatigue", _safe_float(features.get("previous_set_fatigue"), 0.0)),
            _component("recent_fatigue_peak", _safe_float(features.get("recent_fatigue_peak"), 0.0)),
            _component("d_target", terms["d_target"]),
            _component("target_fatigue", terms["target_fatigue"]),
        ],
    }
    return {
        "fatigue_score": round(score, 3),
        "fatigue_increment": round(increment, 3),
        "fatigue_components": components,
        "fatigue_model_version": MODEL_VERSION,
        "d_eff": round(d_eff_total, 4),
        "d_target": round(terms["d_target"], 4),
        "fatigue_progress_pct": round(progress_pct, 2),
        "instant_load": round(terms["instant_load"], 4),
        "a_target": round(terms["a_target"], 4),
        "q_vis": round(terms["q_vis"], 4),
        "c_comp": round(terms["c_comp"], 4),
        "p_comp": round(terms["p_comp"], 4),
        "v_signal": round(terms["v_signal"], 4),
        "dt_s": round(terms["dt_s"], 4),
        "features": dict(features),
        "ts": time.time() if now is None else float(now),
    }


def append_feature_snapshot(snapshot, path=DEFAULT_SNAPSHOT_PATH):
    """Append one JSONL feature snapshot for later learned-model training."""
    if not isinstance(snapshot, dict):
        return False
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True) + "\n")
    return True


__all__ = [
    "DEFAULT_SNAPSHOT_PATH",
    "MODEL_VERSION",
    "append_feature_snapshot",
    "compute_fatigue",
]
