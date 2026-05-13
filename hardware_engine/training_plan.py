# -*- coding: utf-8 -*-
"""Runtime training-plan helper for IronBuddy.

This module is intentionally small and stdlib-only so it can run on the
Toybrick Python 3.7 environment.  It owns only the editable plan JSON contract;
FSM/session code can consume the dict without importing Flask or database code.
"""

from __future__ import absolute_import

import json
import os
import time


DEFAULT_SET_COUNT = 3
DEFAULT_REPS_PER_SET = 8
MIN_FATIGUE_TARGET = 300
MAX_FATIGUE_TARGET = 1500
DEFAULT_FATIGUE_TARGET = 600
RECOVERY_FATIGUE_TARGET = 450
ADVANCED_FATIGUE_TARGET = 750
FATIGUE_TARGET_STEP = 100
DEFAULT_WEIGHT_KG = 70.0
SCHEMA_VERSION = 1

DEFAULT_STATE_PATH = (
    "/dev/shm/ironbuddy_training_plan.json"
    if os.path.isdir("/dev/shm")
    else "/tmp/ironbuddy_training_plan.json"
)

EXERCISE_LABELS = {
    "squat": "深蹲",
    "bicep_curl": "哑铃弯举",
}


def normalize_exercise(exercise):
    """Return IronBuddy canonical exercise id."""
    raw = str(exercise or "").strip().lower()
    if raw in ("curl", "bicep_curl", "biceps_curl", "bicep", "biceps", "dumbbell_curl"):
        return "bicep_curl"
    if raw in ("squat", "deep_squat"):
        return "squat"
    return "squat"


def exercise_label(exercise):
    return EXERCISE_LABELS.get(normalize_exercise(exercise), "深蹲")


def _to_int(value, default):
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value, default):
    try:
        return float(value)
    except Exception:
        return float(default)


def _clean_target_reps(value):
    reps = _to_int(value, DEFAULT_REPS_PER_SET)
    return max(1, min(200, reps))


def _clean_target_fatigue(value):
    fatigue = _to_int(value, DEFAULT_FATIGUE_TARGET)
    return max(MIN_FATIGUE_TARGET, min(MAX_FATIGUE_TARGET, fatigue))


def _clean_set_count(value):
    count = _to_int(value, DEFAULT_SET_COUNT)
    return max(1, min(20, count))


def _clean_current_set(value, set_count):
    current = _to_int(value, 1)
    return max(1, min(int(set_count), current))


def _make_sets(set_count, reps_per_set, fatigue_target=DEFAULT_FATIGUE_TARGET):
    sets = []
    for idx in range(1, int(set_count) + 1):
        sets.append({
            "set_index": idx,
            "target_reps": _clean_target_reps(reps_per_set),
            "target_fatigue": _clean_target_fatigue(fatigue_target + (idx - 1) * FATIGUE_TARGET_STEP),
            "target_type": "fatigue",
        })
    return sets


def create_default_plan(exercise="squat", set_count=DEFAULT_SET_COUNT,
                        reps_per_set=DEFAULT_REPS_PER_SET,
                        weight_kg=DEFAULT_WEIGHT_KG, now=None):
    """Create the editable default plan: current exercise, 3 sets x 8 reps."""
    count = _clean_set_count(set_count)
    ts = time.time() if now is None else float(now)
    ex = normalize_exercise(exercise)
    return {
        "schema_version": SCHEMA_VERSION,
        "exercise": ex,
        "exercise_label": exercise_label(ex),
        "current_set": 1,
        "sets": _make_sets(count, reps_per_set),
        "target_type": "fatigue",
        "weight_kg": _to_float(weight_kg, DEFAULT_WEIGHT_KG),
        "updated_ts": ts,
        "src": "training_plan_helper",
    }


def normalize_plan(plan_state=None, exercise=None, now=None):
    """Normalize a possibly partial JSON plan into the stable contract."""
    base_exercise = exercise
    if isinstance(plan_state, dict) and plan_state.get("exercise"):
        base_exercise = plan_state.get("exercise")
    plan = create_default_plan(
        exercise=base_exercise or "squat",
        now=time.time() if now is None else float(now),
    )

    if not isinstance(plan_state, dict):
        return plan

    raw_sets = plan_state.get("sets")
    cleaned_sets = []
    if isinstance(raw_sets, list):
        for idx, raw in enumerate(raw_sets, 1):
            item = raw if isinstance(raw, dict) else {}
            set_index = _to_int(item.get("set_index", idx), idx)
            if set_index < 1:
                set_index = idx
            cleaned_sets.append({
                "set_index": set_index,
                "target_reps": _clean_target_reps(
                    item.get("target_reps", item.get("reps", DEFAULT_REPS_PER_SET))
                ),
                "target_fatigue": _clean_target_fatigue(
                    item.get("target_fatigue", item.get("fatigue_target", DEFAULT_FATIGUE_TARGET))
                ),
                "target_type": "fatigue",
            })

    if not cleaned_sets:
        count = _clean_set_count(plan_state.get("set_count", DEFAULT_SET_COUNT))
        reps = _clean_target_reps(
            plan_state.get("reps_per_set", plan_state.get("target_reps", DEFAULT_REPS_PER_SET))
        )
        cleaned_sets = _make_sets(count, reps)

    cleaned_sets = sorted(cleaned_sets, key=lambda item: item["set_index"])
    for pos, item in enumerate(cleaned_sets, 1):
        item["set_index"] = pos

    plan["sets"] = cleaned_sets
    plan["target_type"] = str(plan_state.get("target_type") or "fatigue")
    plan["current_set"] = _clean_current_set(
        plan_state.get("current_set", plan_state.get("set_index", 1)),
        len(cleaned_sets),
    )
    plan["weight_kg"] = _to_float(plan_state.get("weight_kg", DEFAULT_WEIGHT_KG),
                                  DEFAULT_WEIGHT_KG)
    plan["updated_ts"] = _to_float(plan_state.get("updated_ts", plan["updated_ts"]),
                                   plan["updated_ts"])
    if plan_state.get("src"):
        plan["src"] = str(plan_state.get("src"))
    return plan


def set_set_target(plan_state, set_index, target_reps, now=None):
    """Return a copy of plan_state with one set target edited."""
    plan = normalize_plan(plan_state, now=now)
    idx = _clean_current_set(set_index, len(plan["sets"]))
    plan["sets"][idx - 1]["target_reps"] = _clean_target_reps(target_reps)
    plan["updated_ts"] = time.time() if now is None else float(now)
    return plan


def set_set_fatigue_target(plan_state, set_index, target_fatigue, now=None):
    """Return a copy of plan_state with one set fatigue target edited."""
    plan = normalize_plan(plan_state, now=now)
    idx = _clean_current_set(set_index, len(plan["sets"]))
    plan["sets"][idx - 1]["target_fatigue"] = _clean_target_fatigue(target_fatigue)
    plan["sets"][idx - 1]["target_type"] = "fatigue"
    plan["target_type"] = "fatigue"
    plan["updated_ts"] = time.time() if now is None else float(now)
    return plan


def set_current_set(plan_state, set_index, now=None):
    """Return a copy of plan_state with current_set changed."""
    plan = normalize_plan(plan_state, now=now)
    plan["current_set"] = _clean_current_set(set_index, len(plan["sets"]))
    plan["updated_ts"] = time.time() if now is None else float(now)
    return plan


def read_json_state(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_state(path, payload):
    """Atomic JSON write using a same-directory temp file."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = "%s.tmp.%s" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
    try:
        os.replace(tmp, path)
    except AttributeError:
        os.rename(tmp, path)
    return path


def read_plan_state(path=DEFAULT_STATE_PATH, exercise="squat"):
    data = read_json_state(path, default=None)
    return normalize_plan(data, exercise=exercise)


def write_plan_state(plan_state, path=DEFAULT_STATE_PATH):
    plan = normalize_plan(plan_state)
    write_json_state(path, plan)
    return plan


def update_plan_state(path=DEFAULT_STATE_PATH, exercise=None, set_count=None,
                      reps_per_set=None, current_set=None, set_targets=None,
                      fatigue_targets=None, weight_kg=None, now=None):
    """Read, edit, write, and return the runtime plan state.

    set_targets may be a dict like {1: 10, 2: 8}. fatigue_targets may be a
    dict like {1: 600, 2: 700}; fatigue is the primary runtime target.
    """
    plan = read_plan_state(path, exercise=exercise or "squat")
    if exercise is not None:
        plan["exercise"] = normalize_exercise(exercise)
        plan["exercise_label"] = exercise_label(plan["exercise"])
    if set_count is not None or reps_per_set is not None:
        count = _clean_set_count(set_count if set_count is not None else len(plan["sets"]))
        reps = _clean_target_reps(reps_per_set if reps_per_set is not None else plan["sets"][0]["target_reps"])
        plan["sets"] = _make_sets(count, reps)
    if weight_kg is not None:
        plan["weight_kg"] = _to_float(weight_kg, DEFAULT_WEIGHT_KG)
    if isinstance(set_targets, dict):
        for raw_idx, reps in set_targets.items():
            idx = _clean_current_set(raw_idx, len(plan["sets"]))
            plan["sets"][idx - 1]["target_reps"] = _clean_target_reps(reps)
    if isinstance(fatigue_targets, dict):
        for raw_idx, fatigue in fatigue_targets.items():
            idx = _clean_current_set(raw_idx, len(plan["sets"]))
            plan["sets"][idx - 1]["target_fatigue"] = _clean_target_fatigue(fatigue)
            plan["sets"][idx - 1]["target_type"] = "fatigue"
        plan["target_type"] = "fatigue"
    if current_set is not None:
        plan["current_set"] = _clean_current_set(current_set, len(plan["sets"]))
    else:
        plan["current_set"] = _clean_current_set(plan.get("current_set", 1), len(plan["sets"]))
    plan["updated_ts"] = time.time() if now is None else float(now)
    write_json_state(path, plan)
    return plan
