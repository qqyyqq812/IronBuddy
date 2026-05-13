# -*- coding: utf-8 -*-
"""Training session report helper for IronBuddy.

Pure stdlib, Python 3.7 compatible.  It aggregates runtime plan JSON, FSM
snapshots, and optional session history into a structured report plus Chinese
text/Feishu-card payloads.  No network, DB, Flask, numpy, or torch imports.
"""

from __future__ import absolute_import

import time

from hardware_engine.training_plan import (
    DEFAULT_FATIGUE_TARGET,
    DEFAULT_WEIGHT_KG,
    exercise_label,
    normalize_exercise,
    normalize_plan,
)


EXERCISE_MET = {
    "bicep_curl": 3.5,
    "squat": 5.0,
}

MAIN_MUSCLE_GROUPS = {
    "bicep_curl": ["肱二头肌", "肱肌", "肱桡肌", "前臂屈肌"],
    "squat": ["股四头肌", "臀大肌", "腘绳肌", "核心稳定肌群"],
}


def _to_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _to_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _first_number(data, keys, default=0):
    if not isinstance(data, dict):
        return default
    for key in keys:
        if data.get(key) is not None:
            return data.get(key)
    return default


def _extract_counts(data):
    good = _to_int(_first_number(data, (
        "good", "good_count", "standard", "standard_reps",
        "qualified", "qualified_reps",
    )), 0)
    failed = _to_int(_first_number(data, (
        "failed", "failed_count", "bad", "bad_count",
        "non_standard", "non_standard_reps",
    )), 0)
    comp = _to_int(_first_number(data, (
        "comp", "comp_count", "compensation", "compensation_count",
        "compensating", "comp_reps",
    )), 0)
    explicit_total = _to_int(_first_number(data, (
        "total_reps", "reps", "rep_count", "count",
    ), good + failed + comp), good + failed + comp)
    total = max(good + failed + comp, explicit_total)
    unknown = max(0, total - (good + failed + comp))
    return good, failed, comp, total, unknown


def _rate(good, total):
    if total <= 0:
        return 0.0
    return round(float(good) * 100.0 / float(total), 1)


def _duration_from_session(session_state, total_reps, actual_set_count):
    if not isinstance(session_state, dict):
        session_state = {}
    duration = _first_number(session_state, ("duration_s", "duration_sec", "duration"), None)
    if duration is not None:
        return max(0.0, _to_float(duration, 0.0)), False
    started = _to_float(_first_number(session_state, ("started_ts", "start_ts"), 0.0), 0.0)
    ended = _to_float(_first_number(session_state, ("ended_ts", "end_ts"), 0.0), 0.0)
    if started > 0.0 and ended >= started:
        return max(0.0, ended - started), False
    if total_reps <= 0:
        return 0.0, True
    rest_s = max(0, int(actual_set_count) - 1) * 45.0
    return max(60.0, float(total_reps) * 4.0 + rest_s), True


def _session_sets(session_state):
    if not isinstance(session_state, dict):
        return []
    for key in ("sets", "set_results", "completed_sets"):
        value = session_state.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _state_has_counts(data):
    if not isinstance(data, dict):
        return False
    good, failed, comp, total, _unknown = _extract_counts(data)
    return (good + failed + comp + total) > 0


def _make_set_report(raw, set_index, target_reps, target_fatigue=None):
    raw = raw if isinstance(raw, dict) else {}
    good, failed, comp, total, unknown = _extract_counts(raw)
    fatigue = _to_float(_first_number(raw, (
        "fatigue", "fatigue_peak", "fatigue_end", "total_fatigue_volume",
    ), 0.0), 0.0)
    rate = _rate(good, total)
    target = max(1, _to_int(raw.get("target_reps", target_reps), target_reps))
    fatigue_target = max(1, _to_int(
        raw.get("target_fatigue", target_fatigue or DEFAULT_FATIGUE_TARGET),
        target_fatigue or DEFAULT_FATIGUE_TARGET,
    ))
    if total <= 0 and fatigue <= 0.0:
        status = "未开始"
    elif fatigue < fatigue_target:
        status = "进行中"
    elif fatigue >= fatigue_target:
        status = "完成疲劳目标"
    elif rate >= 80.0:
        status = "质量基本达标"
    else:
        status = "质量需改进"
    return {
        "set_index": int(set_index),
        "target_reps": target,
        "target_fatigue": fatigue_target,
        "target_type": "fatigue",
        "good": good,
        "failed": failed,
        "comp": comp,
        "unknown_reps": unknown,
        "total_reps": total,
        "qualified_reps": good,
        "qualified_rate_pct": rate,
        "fatigue": round(fatigue, 1),
        "status": status,
    }


def _merge_actual_sets(plan, session_state, fsm_state):
    plan_sets = plan.get("sets") or []
    by_index = {}

    for raw in _session_sets(session_state):
        idx = _to_int(raw.get("set_index", raw.get("index", len(by_index) + 1)), len(by_index) + 1)
        if idx < 1:
            idx = len(by_index) + 1
        plan_set = plan_sets[idx - 1] if idx <= len(plan_sets) else {}
        target = plan_set.get("target_reps", raw.get("target_reps", 8))
        target_fatigue = plan_set.get("target_fatigue", raw.get("target_fatigue", DEFAULT_FATIGUE_TARGET))
        by_index[idx] = _make_set_report(raw, idx, target, target_fatigue)

    if not by_index and _state_has_counts(session_state):
        plan_set = plan_sets[0] if plan_sets else {}
        target = plan_set.get("target_reps", 8)
        target_fatigue = plan_set.get("target_fatigue", DEFAULT_FATIGUE_TARGET)
        by_index[1] = _make_set_report(session_state, 1, target, target_fatigue)

    if _state_has_counts(fsm_state):
        current_idx = _to_int(
            _first_number(fsm_state, ("current_set", "set_index"), plan.get("current_set", 1)),
            plan.get("current_set", 1),
        )
        if current_idx < 1:
            current_idx = 1
        plan_set = plan_sets[current_idx - 1] if current_idx <= len(plan_sets) else {}
        target = plan_set.get("target_reps", 8)
        target_fatigue = plan_set.get("target_fatigue", DEFAULT_FATIGUE_TARGET)
        if current_idx not in by_index:
            by_index[current_idx] = _make_set_report(fsm_state, current_idx, target, target_fatigue)

    reports = []
    max_index = max([len(plan_sets)] + list(by_index.keys() or [0]))
    for idx in range(1, max_index + 1):
        if idx in by_index:
            reports.append(by_index[idx])
            continue
        plan_set = plan_sets[idx - 1] if idx <= len(plan_sets) else {}
        target = plan_set.get("target_reps", 8)
        target_fatigue = plan_set.get("target_fatigue", DEFAULT_FATIGUE_TARGET)
        reports.append(_make_set_report({}, idx, target, target_fatigue))
    return reports


def _fatigue_trend(set_reports):
    values = [
        float(item.get("fatigue", 0.0))
        for item in set_reports
        if item.get("total_reps", 0) > 0 or item.get("fatigue", 0.0) > 0.0
    ]
    if len(values) < 2:
        return {
            "label": "暂无趋势",
            "values": [round(v, 1) for v in values],
            "delta": 0.0,
            "description": "有效组数不足，暂不判断疲劳趋势",
        }
    delta = values[-1] - values[0]
    if abs(delta) < 30.0:
        label = "基本稳定"
    elif delta > 0:
        label = "上升"
    else:
        label = "下降"
    return {
        "label": label,
        "values": [round(v, 1) for v in values],
        "delta": round(delta, 1),
        "description": "%s（首组 %.1f -> 末组 %.1f）" % (label, values[0], values[-1]),
    }


def _calorie_estimate(exercise, weight_kg, duration_s):
    ex = normalize_exercise(exercise)
    met = EXERCISE_MET.get(ex, EXERCISE_MET["squat"])
    minutes = max(0.0, float(duration_s) / 60.0)
    kcal = met * 3.5 * float(weight_kg) / 200.0 * minutes
    return {
        "value": round(kcal, 1),
        "unit": "kcal",
        "label": "估算消耗",
        "is_estimate": True,
        "weight_kg": round(float(weight_kg), 1),
        "met": met,
        "duration_min": round(minutes, 1),
        "method": "MET估算，非医疗或营养测量",
    }


def _tomorrow_advice(exercise, qualified_rate, total_reps, fatigue_trend):
    label = exercise_label(exercise)
    trend = fatigue_trend.get("label", "")
    if total_reps <= 0:
        return "明天先做轻量热身，再按3组8次启动%s，优先确认动作幅度。" % label
    if qualified_rate < 70.0:
        return "明天建议把%s降到每组6次或降低重量，先把动作轨迹做稳。" % label
    if trend == "上升" and qualified_rate < 85.0:
        return "明天保留%s，但减少一组或拉长组间休息，避免疲劳继续累积。" % label
    if qualified_rate >= 90.0 and trend in ("基本稳定", "下降", "暂无趋势"):
        return "明天可以维持当前计划；若热身状态好，再小幅增加每组1到2次。"
    return "明天维持3组8次，重点放慢节奏，保持最后两次的动作质量。"


def _set_line(item):
    return (
        "第%d组：目标疲劳%d，预计目标%d次，完成%d次，标准%d次，不标准%d次，代偿%d次，合格率%.1f%%，疲劳%.1f，%s"
        % (
            item["set_index"], item["target_fatigue"], item["target_reps"], item["total_reps"],
            item["good"], item["failed"], item["comp"],
            item["qualified_rate_pct"], item["fatigue"], item["status"],
        )
    )


def format_session_report_text(report):
    lines = [
        "IronBuddy训练报告",
        "动作：%s" % report.get("exercise_label", "深蹲"),
        "计划：%d组，目标总疲劳%d" % (
            len(report.get("sets", [])),
            sum(item.get("target_fatigue", 0) for item in report.get("sets", [])),
        ),
        "每组情况：",
    ]
    for item in report.get("sets", []):
        lines.append(_set_line(item))
    calories = report.get("calorie_estimate", {})
    lines.extend([
        "总reps：%d" % report.get("total_reps", 0),
        "合格率：%.1f%%" % report.get("qualified_rate_pct", 0.0),
        "疲劳趋势：%s" % report.get("fatigue_trend", {}).get("description", "暂无趋势"),
        "估算卡路里：约%.1f %s（估算，按%.1fkg、MET %.1f）" % (
            calories.get("value", 0.0),
            calories.get("unit", "kcal"),
            calories.get("weight_kg", DEFAULT_WEIGHT_KG),
            calories.get("met", 0.0),
        ),
        "主要肌群：%s" % "、".join(report.get("main_muscle_groups", [])),
        "明日建议：%s" % report.get("tomorrow_advice", ""),
    ])
    return "\n".join(lines)


def build_training_report(fsm_state=None, session_state=None, plan_state=None, now=None):
    """Aggregate plan/session/fsm state into report fields and text."""
    fsm_state = fsm_state if isinstance(fsm_state, dict) else {}
    session_state = session_state if isinstance(session_state, dict) else {}
    plan_exercise = plan_state.get("exercise") if isinstance(plan_state, dict) else None
    plan_active = bool(session_state.get("plan_active")) if isinstance(session_state, dict) else False
    if plan_active and plan_exercise:
        raw_exercise = plan_exercise
    else:
        raw_exercise = (
            fsm_state.get("exercise") or session_state.get("exercise") or
            plan_exercise or "squat"
        )
    exercise = normalize_exercise(raw_exercise)
    plan = normalize_plan(plan_state, exercise=exercise, now=now)
    if plan_active and fsm_state.get("exercise"):
        fsm_exercise = normalize_exercise(fsm_state.get("exercise"))
        if fsm_exercise != exercise:
            fsm_state = dict(fsm_state)
            fsm_state.pop("exercise", None)
            fsm_state.pop("current_set", None)
    weight_kg = _to_float(
        _first_number(session_state, ("weight_kg", "body_weight_kg"), plan.get("weight_kg", DEFAULT_WEIGHT_KG)),
        DEFAULT_WEIGHT_KG,
    )
    sets = _merge_actual_sets(plan, session_state, fsm_state)
    total_good = sum(item["good"] for item in sets)
    total_failed = sum(item["failed"] for item in sets)
    total_comp = sum(item["comp"] for item in sets)
    total_reps = sum(item["total_reps"] for item in sets)
    actual_set_count = len([item for item in sets if item["total_reps"] > 0])
    qualified_rate = _rate(total_good, total_reps)
    trend = _fatigue_trend(sets)
    duration_s, duration_estimated = _duration_from_session(
        session_state, total_reps, actual_set_count)
    calories = _calorie_estimate(exercise, weight_kg, duration_s)
    calories["duration_estimated"] = bool(duration_estimated)

    report = {
        "schema_version": 1,
        "generated_ts": time.time() if now is None else float(now),
        "exercise": exercise,
        "exercise_label": exercise_label(exercise),
        "plan": plan,
        "sets": sets,
        "total_reps": total_reps,
        "total_good": total_good,
        "total_failed": total_failed,
        "total_comp": total_comp,
        "qualified_rate_pct": qualified_rate,
        "fatigue_trend": trend,
        "calorie_estimate": calories,
        "main_muscle_groups": MAIN_MUSCLE_GROUPS.get(exercise, MAIN_MUSCLE_GROUPS["squat"]),
    }
    report["tomorrow_advice"] = _tomorrow_advice(
        exercise, qualified_rate, total_reps, trend)
    report["text"] = format_session_report_text(report)
    return report


def build_session_report(fsm_state=None, session_state=None, plan_state=None, now=None):
    return build_training_report(fsm_state, session_state, plan_state, now=now)


def aggregate_session_report(fsm_state=None, session_state=None, plan_state=None, now=None):
    return build_training_report(fsm_state, session_state, plan_state, now=now)


def _md(content):
    return {"tag": "markdown", "content": str(content)}


def _hr():
    return {"tag": "hr"}


def format_feishu_session_report_markdown(report):
    calories = report.get("calorie_estimate", {})
    overview = (
        "**动作**：%s\n"
        "**总reps**：%d\n"
        "**合格率**：%.1f%%\n"
        "**疲劳趋势**：%s\n"
        "**估算卡路里**：约%.1f %s（估算，%.1fkg，MET %.1f）\n"
        "**主要肌群**：%s\n"
        "**明日建议**：%s"
    ) % (
        report.get("exercise_label", "深蹲"),
        report.get("total_reps", 0),
        report.get("qualified_rate_pct", 0.0),
        report.get("fatigue_trend", {}).get("label", "暂无趋势"),
        calories.get("value", 0.0),
        calories.get("unit", "kcal"),
        calories.get("weight_kg", DEFAULT_WEIGHT_KG),
        calories.get("met", 0.0),
        "、".join(report.get("main_muscle_groups", [])),
        report.get("tomorrow_advice", ""),
    )
    sets_md = "\n".join("- " + _set_line(item) for item in report.get("sets", []))
    return overview + "\n\n**每组情况**\n" + sets_md


def build_feishu_session_report_card(report, title=None):
    """Return a Feishu interactive-card dict; caller decides whether to send."""
    card_title = title or ("IronBuddy训练报告 · " + report.get("exercise_label", "深蹲"))
    template = "green" if report.get("qualified_rate_pct", 0.0) >= 80.0 else "orange"
    set_lines = "\n".join("- " + _set_line(item) for item in report.get("sets", []))
    calories = report.get("calorie_estimate", {})
    recovery = (
        "**估算卡路里**：约%.1f %s（估算，按%.1fkg、MET %.1f）\n"
        "**主要肌群**：%s\n"
        "**明日建议**：%s"
    ) % (
        calories.get("value", 0.0),
        calories.get("unit", "kcal"),
        calories.get("weight_kg", DEFAULT_WEIGHT_KG),
        calories.get("met", 0.0),
        "、".join(report.get("main_muscle_groups", [])),
        report.get("tomorrow_advice", ""),
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": card_title},
        },
        "elements": [
            _md("**训练概览**\n动作：%s\n总reps：%d\n合格率：%.1f%%\n疲劳趋势：%s" % (
                report.get("exercise_label", "深蹲"),
                report.get("total_reps", 0),
                report.get("qualified_rate_pct", 0.0),
                report.get("fatigue_trend", {}).get("description", "暂无趋势"),
            )),
            _hr(),
            _md("**每组情况**\n" + (set_lines or "暂无完整组数据")),
            _hr(),
            _md("**恢复建议**\n" + recovery),
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "IronBuddy · 训练报告 helper"}
                ],
            },
        ],
    }


def build_feishu_session_report_message(report, title=None):
    """Webhook-friendly envelope for callers that need msg_type beside card."""
    return {
        "msg_type": "interactive",
        "card": build_feishu_session_report_card(report, title=title),
    }
