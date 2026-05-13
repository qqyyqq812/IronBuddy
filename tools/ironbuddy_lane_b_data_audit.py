#!/usr/bin/env python3
"""Read-only Lane B data index and preprocessing audit."""

from __future__ import print_function

import argparse
import csv
import json
import time
from collections import defaultdict
from pathlib import Path

try:
    import tools.ironbuddy_export_personal_bicep_gru_dataset as export_tool
except Exception:
    import ironbuddy_export_personal_bicep_gru_dataset as export_tool  # type: ignore

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
OPERATOR_RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_operator"
DOC_ROOT = ROOT / "docs" / "给用户看的交付" / "LaneB_传感模型底座"
DEFAULT_MD_OUT = DOC_ROOT / "数据总览.md"
PERSONAL_ROOT = ROOT / "data" / "bicep_curl_personal"
DATASETS_ROOT = PERSONAL_ROOT / "datasets"
TRAINING_ROOT = PERSONAL_ROOT / "training_runs"
DEFAULT_JSON_OUT = PERSONAL_ROOT / "index.json"
OLD_BICEP_ROOT = ROOT / "data" / "bicep_curl"
OLD_BICEP_AUG_ROOT = ROOT / "data" / "bicep_curl_augmented"
V42_ROOT = ROOT / "data" / "v42"
MIA_SQUAT_ROOT = ROOT / "data" / "mia" / "squat"
MIA_REPORT = MIA_SQUAT_ROOT / "_conversion_report.json"

LABELS = ("standard", "compensating", "non_standard")
LABEL_CN = {
    "standard": "标准",
    "compensating": "代偿",
    "non_standard": "不标准",
}
OLD_LABEL_CN = {
    "golden": "标准",
    "bad": "代偿",
    "lazy": "不标准",
}


class _EvalArgs(object):
    emg_view = DEFAULT_EMG_VIEW
    seq_len = 30
    stride = 10
    min_groups_per_label = 5
    min_reps_per_group = 4
    min_reps_per_label = 25
    min_windows_per_label = 120
    saturation_threshold = 99.0
    max_saturation_ratio = 0.60
    allow_mode_mismatch = False
    allow_invalid_signal_gate = False
    allow_non_gru_reps = False


def _read_json(path, default=None):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _rel(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _count_csv_rows(path):
    count = 0
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for idx, _ in enumerate(reader):
            if idx == 0:
                continue
            count += 1
    return count


def _load_readiness_for_run(run_id):
    if not run_id:
        return {}
    path = OPERATOR_RUNS_ROOT / ("lane_b_longrun_readiness_%s.json" % run_id)
    if not path.exists():
        return {}
    payload = _read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def _scan_old_root(root):
    by_label = {}
    total_files = 0
    total_rows = 0
    for label in ("golden", "bad", "lazy"):
        label_dir = root / label
        csv_files = sorted(label_dir.glob("*.csv")) if label_dir.is_dir() else []
        rows = sum(_count_csv_rows(path) for path in csv_files)
        by_label[label] = {
            "label_cn": OLD_LABEL_CN.get(label, label),
            "files": len(csv_files),
            "rows": rows,
        }
        total_files += len(csv_files)
        total_rows += rows
    return {
        "path": _rel(root),
        "exists": root.is_dir(),
        "total_files": total_files,
        "total_rows": total_rows,
        "by_label": by_label,
    }


def _scan_v42():
    users = {}
    total_csv = 0
    total_raw_reps = 0
    total_aug_csv = 0
    for user_dir in sorted(V42_ROOT.glob("user_*")):
        if not user_dir.is_dir():
            continue
        user_info = {
            "anthropometry_generated": (_read_json(user_dir / "anthropometry.json", {}) or {}).get("generated"),
            "mvc_source": (_read_json(user_dir / "mvc_calibration.json", {}) or {}).get("source"),
            "exercises": {},
        }
        for exercise in ("curl", "squat"):
            label_info = {}
            for label in ("standard", "compensation", "bad_form"):
                label_dir = user_dir / exercise / label
                csv_files = sorted(label_dir.glob("rep_*.csv")) if label_dir.is_dir() else []
                raw_files = [path for path in csv_files if "_aug" not in path.stem]
                aug_files = [path for path in csv_files if "_aug" in path.stem]
                label_info[label] = {
                    "csv_files": len(csv_files),
                    "raw_reps": len(raw_files),
                    "aug_csv": len(aug_files),
                }
                total_csv += len(csv_files)
                total_raw_reps += len(raw_files)
                total_aug_csv += len(aug_files)
            user_info["exercises"][exercise] = label_info
        users[user_dir.name] = user_info
    return {
        "path": _rel(V42_ROOT),
        "exists": V42_ROOT.is_dir(),
        "user_count": len(users),
        "users": users,
        "total_csv": total_csv,
        "total_raw_reps": total_raw_reps,
        "total_aug_csv": total_aug_csv,
    }


def _scan_mia():
    report = _read_json(MIA_REPORT, {})
    label_counts = report.get("label_counts") if isinstance(report.get("label_counts"), dict) else {}
    csv_files = sorted(MIA_SQUAT_ROOT.glob("*/*.csv")) if MIA_SQUAT_ROOT.is_dir() else []
    return {
        "path": _rel(MIA_SQUAT_ROOT),
        "exists": MIA_SQUAT_ROOT.is_dir(),
        "csv_files": len(csv_files),
        "label_counts": label_counts,
        "conversion_report": _rel(MIA_REPORT),
    }


def _strip_group_eval(item):
    keep = {}
    for key in (
        "group_id",
        "file",
        "path",
        "label",
        "label_cn",
        "accepted",
        "reasons",
        "rep_count",
        "gru_rep_count",
        "exact_rows",
        "estimated_windows",
        "emg_view",
        "target_sat_ratio",
        "comp_sat_ratio",
        "max_sat_ratio",
        "preprocess_version",
        "mvc_source",
        "domain_method",
    ):
        keep[key] = item.get(key)
    return keep


def _scan_sensor_lab():
    run_dirs = sorted([path for path in RUNS_ROOT.glob("20*") if path.is_dir()])
    runs = []
    for run_dir in run_dirs:
        session = _read_json(run_dir / "session_index.json", {})
        group_paths = sorted((run_dir / "groups").glob("*.json"))
        labels_present = defaultdict(int)
        groups = []
        for path in group_paths:
            payload = _read_json(path, {})
            info = export_tool.evaluate_group(path, payload, _EvalArgs())
            groups.append(_strip_group_eval(info))
            if info.get("label") in LABELS:
                labels_present[info["label"]] += 1
        group_count = max(len(group_paths), len(session.get("groups") or []))
        accepted_count = sum(1 for item in groups if item.get("accepted"))
        if group_count == 0:
            verdict = "空跑"
        elif accepted_count > 0:
            verdict = "可导出"
        elif set(label for label, count in labels_present.items() if count) == set(LABELS):
            verdict = "三类齐全，但只可复盘"
        else:
            verdict = "诊断用"
        runs.append({
            "run_id": run_dir.name,
            "path": _rel(run_dir),
            "board_ip_recorded": session.get("board_ip"),
            "group_count": group_count,
            "labels_present": dict(labels_present),
            "accepted_group_count": accepted_count,
            "verdict": verdict,
            "groups": groups,
        })
    non_empty = [run for run in runs if run["group_count"] > 0]
    three_label = [
        run for run in non_empty
        if set(label for label, count in run["labels_present"].items() if count) == set(LABELS)
    ]
    return {
        "path": _rel(RUNS_ROOT),
        "total_runs": len(runs),
        "non_empty_runs": len(non_empty),
        "latest_run": runs[-1]["run_id"] if runs else None,
        "latest_non_empty_run": non_empty[-1]["run_id"] if non_empty else None,
        "latest_three_label_run": three_label[-1]["run_id"] if three_label else None,
        "runs": runs,
    }


def _scan_personal_datasets():
    items = []
    for dataset_dir in sorted(DATASETS_ROOT.glob("*")) if DATASETS_ROOT.is_dir() else []:
        manifest = _read_json(dataset_dir / "personal_dataset_manifest.json", {})
        items.append({
            "dataset_id": dataset_dir.name,
            "path": _rel(dataset_dir),
            "training_ready": bool(manifest.get("training_ready")),
            "source_run_id": manifest.get("source_run_id"),
            "emg_view": manifest.get("emg_view"),
            "preprocess_version": manifest.get("preprocess_version"),
            "labels": manifest.get("labels") or {},
        })
    legacy = []
    if PERSONAL_ROOT.is_dir():
        for child in sorted(PERSONAL_ROOT.iterdir()):
            if not child.is_dir():
                continue
            if child.name in ("datasets", "training_runs"):
                continue
            if (child / "personal_dataset_manifest.json").exists():
                legacy.append(_rel(child))
    return {
        "path": _rel(DATASETS_ROOT),
        "count": len(items),
        "items": items,
        "legacy_exports": legacy,
    }


def _scan_training_runs():
    items = []
    for run_dir in sorted(TRAINING_ROOT.glob("*")) if TRAINING_ROOT.is_dir() else []:
        report = _read_json(run_dir / "train_report.json", {})
        items.append({
            "run_id": run_dir.name,
            "path": _rel(run_dir),
            "candidate_weight_exists": (run_dir / "candidate_weight.pt").exists(),
            "report_exists": (run_dir / "train_report.json").exists(),
            "passed_acceptance": report.get("passed_acceptance"),
            "source_dataset": (report.get("source_dataset") or {}).get("data_dir"),
            "emg_view": report.get("emg_view"),
            "preprocess_version": report.get("preprocess_version"),
        })
    return {
        "path": _rel(TRAINING_ROOT),
        "count": len(items),
        "items": items,
    }


def _build_preprocess_findings(sensor_lab, mvc_runtime):
    key_run_id = sensor_lab.get("latest_three_label_run") or sensor_lab.get("latest_non_empty_run")
    key_run = None
    readiness = _load_readiness_for_run(key_run_id)
    readiness_mvc = readiness.get("mvc") if isinstance(readiness.get("mvc"), dict) else {}
    for run in sensor_lab.get("runs", []):
        if run.get("run_id") == key_run_id:
            key_run = run
            break
    target_sat_max = None
    comp_sat_max = None
    mapping_suspected = False
    group_count = 0
    stable_target_mean = None
    run_domain_methods = set()
    if key_run:
        target_sats = []
        comp_sats = []
        stable_means = []
        for group in key_run.get("groups", []):
            payload = _read_json(ROOT / group["path"], {})
            mapping = payload.get("emg_mapping_summary") if isinstance(payload.get("emg_mapping_summary"), dict) else {}
            preprocess = payload.get("emg_preprocess") if isinstance(payload.get("emg_preprocess"), dict) else {}
            if isinstance(preprocess, dict) and preprocess.get("domain_method"):
                run_domain_methods.add(preprocess.get("domain_method"))
            if mapping:
                for ch in mapping.get("channels") or []:
                    if ch.get("channel") == 0:
                        if ch.get("current_pct_sat100_ratio") is not None:
                            target_sats.append(ch.get("current_pct_sat100_ratio"))
                    if ch.get("channel") == 1:
                        if ch.get("current_pct_sat100_ratio") is not None:
                            comp_sats.append(ch.get("current_pct_sat100_ratio"))
                    mapping_suspected = mapping_suspected or bool(ch.get("mvc_or_domain_saturation_suspected"))
            if not preprocess and payload.get("stream_samples"):
                preprocess = {
                    "view_summary": summarize_stream_views(
                        payload.get("stream_samples") or [],
                        preprocess_meta=load_runtime_preprocess_meta(),
                    )
                }
            view_summary = preprocess.get("view_summary") if isinstance(preprocess, dict) else {}
            stable = ((view_summary.get("views") or {}).get("stable_remap_pct") or {}).get("target") if isinstance(view_summary, dict) else {}
            if isinstance(stable, dict) and stable.get("mean") is not None:
                stable_means.append(stable.get("mean"))
        target_sat_max = max(target_sats) if target_sats else None
        comp_sat_max = max(comp_sats) if comp_sats else None
        stable_target_mean = round(sum(stable_means) / float(len(stable_means)), 3) if stable_means else None
        group_count = len(key_run.get("groups") or [])
    readiness_mvc_valid = readiness_mvc.get("runtime_mvc_values_valid")
    if readiness_mvc_valid is None:
        runtime_mvc_missing = not bool(mvc_runtime.get("mvc_valid"))
    else:
        runtime_mvc_missing = not bool(readiness_mvc_valid)
    domain_method = readiness_mvc.get("domain_method")
    if not domain_method and "stretch" in run_domain_methods:
        domain_method = "stretch"
    if not domain_method:
        domain_method = mvc_runtime.get("domain_method")
    domain_is_stretch = domain_method == "stretch"
    saturation = bool((target_sat_max or 0.0) >= 0.60 or (comp_sat_max or 0.0) >= 0.60)
    verdict = (
        "raw_rms_robust100_preferred_no_mvc"
        if DEFAULT_EMG_VIEW == "raw_rms_robust100" and (saturation or runtime_mvc_missing)
        else ("current_pct_not_safe_for_training" if (saturation or runtime_mvc_missing) else "current_pct_may_be_usable")
    )
    return {
        "key_run_id": key_run_id,
        "key_run_group_count": group_count,
        "key_run_review_only": True,
        "key_run_runtime_mvc_ready": not runtime_mvc_missing,
        "key_run_domain_method": domain_method,
        "target_current_pct_sat100_ratio_max": target_sat_max,
        "comp_current_pct_sat100_ratio_max": comp_sat_max,
        "stable_remap_target_mean_estimate": stable_target_mean,
        "saturation_detected": saturation,
        "runtime_mvc_missing": runtime_mvc_missing,
        "domain_stretch_configured": domain_is_stretch,
        "domain_stretch_amplification_suspected": bool(mapping_suspected and domain_is_stretch),
        "recommended_training_emg_view": DEFAULT_EMG_VIEW,
        "preprocess_version": PREPROCESS_VERSION,
        "verdict": verdict,
    }


def build_report():
    sensor_lab = _scan_sensor_lab()
    historical_assets = {
        "old_7d": _scan_old_root(OLD_BICEP_ROOT),
        "old_7d_augmented": _scan_old_root(OLD_BICEP_AUG_ROOT),
        "v42": _scan_v42(),
        "mia_squat": _scan_mia(),
    }
    personal_datasets = _scan_personal_datasets()
    training_runs = _scan_training_runs()
    mvc_runtime = load_runtime_preprocess_meta()
    preprocess_findings = _build_preprocess_findings(sensor_lab, mvc_runtime)
    summary = {
        "latest_run": sensor_lab.get("latest_run"),
        "latest_non_empty_run": sensor_lab.get("latest_non_empty_run"),
        "latest_three_label_run": sensor_lab.get("latest_three_label_run"),
        "dataset_count": personal_datasets.get("count"),
        "training_run_count": training_runs.get("count"),
        "mvc_runtime_ready": bool(mvc_runtime.get("mvc_valid")),
        "recommended_training_emg_view": DEFAULT_EMG_VIEW,
    }
    return {
        "ok": True,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "historical_assets": historical_assets,
        "sensor_lab_runs": sensor_lab,
        "personal_datasets": personal_datasets,
        "training_runs": training_runs,
        "mvc_runtime": mvc_runtime,
        "preprocess_findings": preprocess_findings,
    }


def render_markdown(report):
    sensor_lab = report["sensor_lab_runs"]
    mvc = report["mvc_runtime"]
    findings = report["preprocess_findings"]
    latest_run_id = sensor_lab.get("latest_run")
    latest_run = {}
    key_run = {}
    for run in sensor_lab.get("runs", []):
        if run.get("run_id") == latest_run_id:
            latest_run = run
        if run.get("run_id") == findings.get("key_run_id"):
            key_run = run
    zero_rep_groups = [
        group.get("file")
        for group in key_run.get("groups", [])
        if int(group.get("rep_count") or 0) == 0
    ]
    lines = [
        "# Lane B 数据总览",
        "",
        "这页只回答一件事：现在有没有能训练个人弯举 GRU 的数据。",
        "",
        "## 结论",
        "",
        "- 现在没有可直接训练的个人 7D 数据集。",
        "- `%s` 只能复盘，不能训练。"
        % (findings.get("key_run_id") or "最近关键 run"),
    ]
    if zero_rep_groups:
        lines.append(
            "- `%s` 是 0 rep，作废。"
            % "`, `".join(zero_rep_groups)
        )
    if latest_run_id:
        lines.append(
            "- `%s` 是最新 run；当前结论是 `%s`。"
            % (latest_run_id, latest_run.get("verdict") or "待判断")
        )
    lines.extend([
        "- 默认训练口径是 `%s`。`current_pct` 只做对照。"
        % findings.get("recommended_training_emg_view"),
        "- runtime MVC 就绪: `%s`。"
        % ("是" if mvc.get("mvc_valid") else "否"),
        "- `current_pct` 饱和上限: target `%s`，comp `%s`。"
        % (
            findings.get("target_current_pct_sat100_ratio_max"),
            findings.get("comp_current_pct_sat100_ratio_max"),
        ),
        "- `domain stretch` 放大怀疑: `%s`。"
        % findings.get("domain_stretch_amplification_suspected"),
        "",
        "## 固定目录",
        "",
        "- 全局索引：`data/bicep_curl_personal/index.json`",
        "- 训练导出：`data/bicep_curl_personal/datasets/<stamp>_<runid>/`",
        "- 训练结果：`data/bicep_curl_personal/training_runs/<stamp>/`",
        "- 原始 Lab：`docs/test_runs/ironbuddy_sensor_lab/<runid>/`",
        "",
        "## 当前 run 判断",
        "",
        "| run | groups | accepted | 结论 |",
        "| --- | --- | --- | --- |",
    ])
    for run in (key_run, latest_run):
        if not run:
            continue
        if run.get("run_id") == key_run.get("run_id") and run.get("run_id") == latest_run.get("run_id"):
            label = "%s（关键/最新）" % run.get("run_id")
        else:
            label = run.get("run_id")
        lines.append(
            "| `%s` | %s | %s | %s |"
            % (
                label,
                run.get("group_count"),
                run.get("accepted_group_count"),
                run.get("verdict"),
            )
        )
    lines.extend([
        "",
        "旧目录 `data/bicep_curl/`、`data/bicep_curl_augmented/`、`data/v42/` 和",
        "`data/mia/squat/` 只做历史参考，不并入这轮个人 7D GRU。",
        "",
        "## 刷新命令",
        "",
        "```bash",
        "python3 tools/ironbuddy_lane_b_data_audit.py \\",
        "  --json-out data/bicep_curl_personal/index.json \\",
        "  --markdown-out docs/给用户看的交付/LaneB_传感模型底座/数据总览.md",
        "```",
        "",
    ])
    return "\n".join(lines)


def print_human(report):
    summary = report["summary"]
    findings = report["preprocess_findings"]
    print("Lane B 数据审计")
    print("=" * 18)
    print("latest_run: %s" % summary.get("latest_run"))
    print("latest_three_label_run: %s" % summary.get("latest_three_label_run"))
    print("dataset_count: %s" % summary.get("dataset_count"))
    print("training_run_count: %s" % summary.get("training_run_count"))
    print("mvc_runtime_ready: %s" % summary.get("mvc_runtime_ready"))
    print("recommended_training_emg_view: %s" % summary.get("recommended_training_emg_view"))
    print("key_run_target_sat100_max: %s" % findings.get("target_current_pct_sat100_ratio_max"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional JSON report path. Example: %s" % _rel(DEFAULT_JSON_OUT),
    )
    parser.add_argument(
        "--markdown-out",
        default=None,
        help="Optional markdown report path. Example: %s" % _rel(DEFAULT_MD_OUT),
    )
    args = parser.parse_args(argv)

    report = build_report()
    print_human(report)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print("json_out: %s" % _rel(out))
    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_markdown(report), encoding="utf-8")
        print("markdown_out: %s" % _rel(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
