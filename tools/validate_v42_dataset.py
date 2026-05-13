# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.2 — Dataset Validator
===================================
校验 data/v42/ 目录下采集结果的完整性 / schema / 数值合理性。

参考：
- 计划 `.claude/plans/home-qq-projects-embedded-fullstack-ind-velvet-ember.md` §2.3/§4.4
- 执行细节 `.claude/plans/distributed-puzzling-wilkinson.md` Agent-A 部分

检查项：
  1. 每 user 下 anthropometry.json / mvc_calibration.json 存在
  2. mvc_calibration.json 的 protocol/peak_mvc 合规
  3. 六个子目录 {curl,squat} × {standard,compensation,bad_form} 各 ≥ min_reps CSV
  4. 每 CSV：header 完全匹配 13 列；行数 ∈ [50, 500]；无 NaN/Inf；
     label 列与目录名一致；Target_RMS_Norm ∈ [0, 3]
  5. 跨用户统计：user 数 ≥ min_users；总 rep 数 ≥ min_users*2*3*min_reps
     （目标 270 只作为 warning）

输出：彩色终端报告 + data/v42/_validation_report.json
Exit code：0 全过；1 有 FAIL
"""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

EXPECTED_HEADER = [
    "Timestamp",
    "Ang_Vel", "Angle", "Ang_Accel",
    "Target_RMS_Norm", "Comp_RMS_Norm", "Symmetry_Score", "Phase_Progress",
    "Target_MDF", "Target_MNF", "Target_ZCR", "Target_Raw_Unfilt",
    "label",
]
EXERCISES = ["curl", "squat"]
LABELS = ["standard", "compensation", "bad_form"]
LABEL_INT = {"standard": 0, "compensation": 1, "bad_form": 2}

MIN_ROWS = 50
MAX_ROWS = 500
TARGET_RMS_MAX = 3.0

# ANSI 颜色
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _pass(msg):
    print("{0}[PASS]{1} {2}".format(GREEN, RESET, msg))


def _fail(msg):
    print("{0}[FAIL]{1} {2}".format(RED, RESET, msg))


def _warn(msg):
    print("{0}[WARN]{1} {2}".format(YELLOW, RESET, msg))


def _info(msg):
    print("{0}[INFO]{1} {2}".format(CYAN, RESET, msg))


class Report(object):
    def __init__(self):
        self.passes = 0
        self.fails = 0
        self.warns = 0
        self.details = []

    def add(self, level, msg, extra=None):
        self.details.append({
            "level": level,
            "msg": msg,
            "extra": extra or {},
        })
        if level == "PASS":
            self.passes += 1
            _pass(msg)
        elif level == "FAIL":
            self.fails += 1
            _fail(msg)
        elif level == "WARN":
            self.warns += 1
            _warn(msg)

    def to_json(self):
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "summary": {
                "passes": self.passes,
                "fails": self.fails,
                "warns": self.warns,
            },
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Per-user checks
# ---------------------------------------------------------------------------
def check_user_metadata(user_dir, report):
    user = os.path.basename(user_dir)
    anth = os.path.join(user_dir, "anthropometry.json")
    if os.path.isfile(anth):
        report.add("PASS", "{0}: anthropometry.json 存在".format(user))
    else:
        report.add("FAIL", "{0}: anthropometry.json 缺失".format(user),
                   {"path": anth})

    mvc_path = os.path.join(user_dir, "mvc_calibration.json")
    if not os.path.isfile(mvc_path):
        report.add("FAIL", "{0}: mvc_calibration.json 缺失".format(user),
                   {"path": mvc_path})
        return
    try:
        with open(mvc_path, "r") as f:
            mvc = json.load(f)
    except (OSError, ValueError) as e:
        report.add("FAIL", "{0}: mvc_calibration.json 解析失败".format(user),
                   {"err": str(e)})
        return

    protocol = mvc.get("protocol")
    if protocol == "SENIAM-2000":
        report.add("PASS", "{0}: MVC protocol = SENIAM-2000".format(user))
    else:
        report.add("FAIL", "{0}: MVC protocol != SENIAM-2000 (实际 {1})".format(
            user, protocol))

    peak = mvc.get("peak_mvc") or {}
    ch0 = peak.get("ch0")
    ch1 = peak.get("ch1")
    if ch0 is not None and ch1 is not None and ch0 > 0 and ch1 > 0:
        report.add("PASS", "{0}: peak_mvc ch0={1:.1f} ch1={2:.1f}".format(
            user, float(ch0), float(ch1)))
    else:
        report.add("FAIL", "{0}: peak_mvc 无效 (ch0={1}, ch1={2})".format(
            user, ch0, ch1))


# ---------------------------------------------------------------------------
# Per-CSV checks
# ---------------------------------------------------------------------------
def check_csv(path, label_str, report):
    user = path.split(os.sep)[-4]
    tag = "{0}/{1}/{2}/{3}".format(
        user, path.split(os.sep)[-3], label_str, os.path.basename(path))

    try:
        df = pd.read_csv(path)
    except Exception as e:
        report.add("FAIL", "{0}: 读取失败 ({1})".format(tag, e))
        return False

    # Header
    actual_header = list(df.columns)
    if actual_header != EXPECTED_HEADER:
        report.add("FAIL", "{0}: header 不匹配".format(tag),
                   {"expected": EXPECTED_HEADER, "actual": actual_header})
        return False

    # 行数
    n = len(df)
    if n < MIN_ROWS or n > MAX_ROWS:
        report.add("FAIL", "{0}: 行数 {1} ∉ [{2}, {3}]".format(
            tag, n, MIN_ROWS, MAX_ROWS))
        return False

    # NaN / Inf
    numeric = df.drop(columns=["label"]).select_dtypes(include=[np.number])
    if numeric.isna().any().any():
        report.add("FAIL", "{0}: 存在 NaN".format(tag))
        return False
    if not np.isfinite(numeric.to_numpy()).all():
        report.add("FAIL", "{0}: 存在 Inf".format(tag))
        return False

    # label 一致性
    expected_int = LABEL_INT[label_str]
    if not (df["label"] == expected_int).all():
        uniq = df["label"].unique().tolist()
        report.add("FAIL", "{0}: label 列与目录不符 (期望 {1}, 实际 {2})".format(
            tag, expected_int, uniq))
        return False

    # 数值范围
    if "Target_RMS_Norm" in df.columns:
        vmin = float(df["Target_RMS_Norm"].min())
        vmax = float(df["Target_RMS_Norm"].max())
        if vmin < 0 or vmax > TARGET_RMS_MAX:
            report.add("FAIL", "{0}: Target_RMS_Norm ∈ [{1:.3f}, {2:.3f}] 超阈 [0, {3}]".format(
                tag, vmin, vmax, TARGET_RMS_MAX))
            return False

    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def validate(data_root, min_reps, min_users, target_total):
    report = Report()

    if not os.path.isdir(data_root):
        report.add("FAIL", "data_root 不存在: {0}".format(data_root))
        return report, 0

    user_dirs = sorted([d for d in glob.glob(os.path.join(data_root, "user_*"))
                        if os.path.isdir(d)])
    if not user_dirs:
        report.add("FAIL", "未找到任何 user_* 子目录 in {0}".format(data_root))
        return report, 0

    _info("扫描到 {0} 个用户: {1}".format(
        len(user_dirs), [os.path.basename(u) for u in user_dirs]))

    if len(user_dirs) >= min_users:
        report.add("PASS", "用户数 {0} ≥ 要求 {1}".format(len(user_dirs), min_users))
    else:
        report.add("FAIL", "用户数 {0} < 要求 {1}".format(len(user_dirs), min_users))

    total_reps = 0

    for user_dir in user_dirs:
        user = os.path.basename(user_dir)
        check_user_metadata(user_dir, report)

        for ex in EXERCISES:
            for lb in LABELS:
                sub = os.path.join(user_dir, ex, lb)
                csvs = sorted(glob.glob(os.path.join(sub, "rep_*.csv")))
                tag = "{0}/{1}/{2}".format(user, ex, lb)
                if not os.path.isdir(sub):
                    report.add("FAIL", "{0}: 目录不存在".format(tag))
                    continue
                if len(csvs) < min_reps:
                    report.add("FAIL", "{0}: {1} rep < 要求 {2}".format(
                        tag, len(csvs), min_reps))
                else:
                    report.add("PASS", "{0}: {1} rep ≥ {2}".format(
                        tag, len(csvs), min_reps))

                ok_count = 0
                for csv_path in csvs:
                    if check_csv(csv_path, lb, report):
                        ok_count += 1
                total_reps += ok_count

    # 总 rep 数
    required_total = min_users * len(EXERCISES) * len(LABELS) * min_reps
    if total_reps >= required_total:
        report.add("PASS", "总 rep 数 {0} ≥ 最低要求 {1}".format(
            total_reps, required_total))
    else:
        report.add("FAIL", "总 rep 数 {0} < 最低要求 {1}".format(
            total_reps, required_total))

    if total_reps >= target_total:
        report.add("PASS", "总 rep 数 {0} ≥ 目标 {1}".format(
            total_reps, target_total))
    else:
        report.add("WARN", "总 rep 数 {0} < 目标 {1}（仅 warning，不 fail）".format(
            total_reps, target_total))

    return report, total_reps


def main():
    p = argparse.ArgumentParser(
        description="IronBuddy V4.2 数据集校验器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-root", default="data/v42",
                   help="数据根目录（默认 data/v42）")
    p.add_argument("--min-reps-per-class", type=int, default=10,
                   help="每 user×exercise×label 最低 rep 数（默认 10）")
    p.add_argument("--min-users", type=int, default=3,
                   help="最低用户数（默认 3）")
    p.add_argument("--target-total-reps", type=int, default=270,
                   help="总 rep 目标（默认 270；不达只 warn）")
    args = p.parse_args()

    data_root = os.path.abspath(args.data_root)
    _info("校验 {0}".format(data_root))
    _info("  min_reps_per_class = {0}".format(args.min_reps_per_class))
    _info("  min_users          = {0}".format(args.min_users))
    _info("  target_total_reps  = {0}".format(args.target_total_reps))
    print("")

    report, total = validate(
        data_root,
        args.min_reps_per_class,
        args.min_users,
        args.target_total_reps,
    )

    print("")
    print("=" * 60)
    print("校验汇总")
    print("  PASS : {0}".format(report.passes))
    print("  WARN : {0}".format(report.warns))
    print("  FAIL : {0}".format(report.fails))
    print("  总 rep 数: {0}".format(total))
    print("=" * 60)

    # 输出 JSON 报告
    if os.path.isdir(data_root):
        out_json = os.path.join(data_root, "_validation_report.json")
        try:
            with open(out_json, "w") as f:
                json.dump(report.to_json(), f, indent=2, ensure_ascii=False)
            _info("JSON 报告: {0}".format(out_json))
        except OSError as e:
            _warn("JSON 报告写入失败: {0}".format(e))

    sys.exit(1 if report.fails > 0 else 0)


if __name__ == "__main__":
    main()
