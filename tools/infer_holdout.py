# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.3 - Holdout 推理验证
================================

对某个 holdout 用户（如 user_04）的全部 rep_*.csv 做端到端推理：
  - 加载 DualBranchFusionModel + 预训练 encoder + fusion head
  - 复用 train_fusion_head 的 load_user_reps + extract_21d_feature
  - 算混淆矩阵 + per-class precision/recall/F1
  - 算 cos_sim 分布，与 metrics.json['cos_sim_p5_threshold'] 对比
  - 判定是否达成：comp recall ≥ 0.70 AND bad recall ≥ 0.80

Usage
-----
    python tools/infer_holdout.py \\
        --weights hardware_engine/cognitive/weights/v42_fusion_head_curl_da.pt \\
        --data-root data/v42 --user user_04 --exercise curl
"""

from __future__ import absolute_import, division, print_function

import argparse
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for p in (_THIS_DIR, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import torch
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

try:
    from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

from hardware_engine.cognitive.dual_branch_fusion import (  # noqa: E402
    DualBranchFusionModel,
    LABEL_NAMES,
)
from tools.train_fusion_head import (  # noqa: E402
    load_user_reps,
    extract_21d_feature,
)

COMP_RECALL_GATE = 0.70
BAD_RECALL_GATE = 0.80


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _locate_encoder_weights(weights_path):
    """在 weights/ 目录找 encoder 权重，返回其目录。

    优先 *_flex_pretrained.pt → 回退 *_local.pt。
    """
    weights_dir = os.path.dirname(os.path.abspath(weights_path))
    flex_v = os.path.join(weights_dir, "vision_encoder_flex_pretrained.pt")
    flex_e = os.path.join(weights_dir, "emg_encoder_flex_pretrained.pt")
    if os.path.isfile(flex_v) and os.path.isfile(flex_e):
        # 建临时目录 symlink 成 dual_branch_fusion 期望的文件名
        stage = os.path.join("/tmp", "ironbuddy_encoders_flex")
        if not os.path.isdir(stage):
            os.makedirs(stage)
        for src, dst_name in ((flex_v, "vision_encoder_local.pt"),
                              (flex_e, "emg_encoder_local.pt")):
            dst = os.path.join(stage, dst_name)
            if os.path.islink(dst) or os.path.isfile(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            try:
                os.symlink(src, dst)
            except OSError:
                # 不能 symlink 就 copy
                with open(src, "rb") as fi, open(dst, "wb") as fo:
                    fo.write(fi.read())
        return stage
    # fallback: 原目录（含 *_local.pt）
    return weights_dir


def _load_metrics_threshold(weights_path, exercise):
    """从 v42_fusion_head_<exercise>[_da]_metrics.json 读 cos_sim_p5_threshold。"""
    # 支持 _da 变体：去掉后再找 metrics 文件
    base = os.path.basename(weights_path)
    stem, _ = os.path.splitext(base)
    # 先试同名 _metrics.json
    d = os.path.dirname(weights_path)
    candidates = [
        os.path.join(d, stem + "_metrics.json"),
        os.path.join(d, "v42_fusion_head_%s_metrics.json" % exercise),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                return float(data.get("cos_sim_p5_threshold", 0.0)), path
            except (OSError, ValueError):
                pass
    return None, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args):
    if not _HAS_TORCH or not _HAS_NUMPY:
        print("[ERROR] 需要 torch + numpy", file=sys.stderr)
        sys.exit(2)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("[INFO] device = %s" % device)

    if not os.path.isfile(args.weights):
        print("[ERROR] fusion head 权重不存在: %s" % args.weights, file=sys.stderr)
        sys.exit(2)

    model = DualBranchFusionModel(deep_fusion=args.deep).to(device)
    enc_dir = _locate_encoder_weights(args.weights)
    print("[INFO] encoder 权重目录: %s" % enc_dir)
    model.load_pretrained_encoders(enc_dir)

    fusion_state = torch.load(args.weights, map_location=device)
    # fusion_state 可能是 FusionHead state_dict 或 full model state_dict
    try:
        model.fusion_head.load_state_dict(fusion_state)
        print("[INFO] fusion head 权重加载成功: %s" % args.weights)
    except Exception:
        # full model 回退
        try:
            model.load_state_dict(fusion_state, strict=False)
            print("[INFO] fusion head 权重 (partial strict=False) 加载完成")
        except Exception as ex:
            print("[ERROR] 无法加载 fusion head: %s" % ex, file=sys.stderr)
            sys.exit(2)
    model.eval()

    # 扫描 holdout 用户所有 rep
    reps = load_user_reps(args.data_root, args.user, args.exercise)
    if not reps:
        print("[ERROR] 未找到任何 rep: %s/%s/%s"
              % (args.data_root, args.user, args.exercise), file=sys.stderr)
        sys.exit(2)
    print("[INFO] 加载 %d rep" % len(reps))

    y_true = []
    y_pred = []
    cos_sims = []
    for rep in reps:
        feat, cs = extract_21d_feature(model, rep, device)
        with torch.no_grad():
            logits = model.fusion_head(feat.unsqueeze(0))  # (1, 3)
            pred = int(logits.argmax(dim=-1).item())
        y_true.append(rep["label"])
        y_pred.append(pred)
        cos_sims.append(cs)

    y_true = np.asarray(y_true)
    y_pred = np.asarray(y_pred)
    cos_sims = np.asarray(cos_sims)

    # 混淆矩阵
    if _HAS_SKLEARN:
        cm = confusion_matrix(y_true, y_pred, labels=[0, 1, 2])
        precision, recall, f1, _ = precision_recall_fscore_support(
            y_true, y_pred, labels=[0, 1, 2], zero_division=0,
        )
    else:
        cm = np.zeros((3, 3), dtype=int)
        for t, p in zip(y_true, y_pred):
            cm[int(t), int(p)] += 1
        recall = np.zeros(3, dtype=np.float32)
        precision = np.zeros(3, dtype=np.float32)
        f1 = np.zeros(3, dtype=np.float32)
        for c in range(3):
            tp = cm[c, c]
            row = cm[c, :].sum()
            col = cm[:, c].sum()
            recall[c] = tp / row if row > 0 else 0.0
            precision[c] = tp / col if col > 0 else 0.0
            denom = precision[c] + recall[c]
            f1[c] = 2 * precision[c] * recall[c] / denom if denom > 0 else 0.0

    # cos_sim 阈值检测
    cos_p5, metrics_path = _load_metrics_threshold(args.weights, args.exercise)
    if cos_p5 is not None:
        anomalies = int((cos_sims < cos_p5).sum())
    else:
        anomalies = 0

    # 打印报告
    print("")
    print("=== Holdout %s (%s) ===" % (args.user, args.exercise))
    print("Total reps: %d" % len(reps))
    print("Confusion matrix:")
    print("                 %10s %10s %10s" % tuple(LABEL_NAMES))
    for i, row in enumerate(cm):
        print("  %-12s %10d %10d %10d" % (LABEL_NAMES[i], row[0], row[1], row[2]))
    print("Per-class recall:     std=%.2f  comp=%.2f  bad=%.2f"
          % (recall[0], recall[1], recall[2]))
    print("Per-class precision:  std=%.2f  comp=%.2f  bad=%.2f"
          % (precision[0], precision[1], precision[2]))
    print("Per-class F1:         std=%.2f  comp=%.2f  bad=%.2f"
          % (f1[0], f1[1], f1[2]))
    if cos_p5 is not None:
        print("cos_sim 5%% threshold = %.4f  (from %s)" % (cos_p5, metrics_path))
        print("cos_sim anomalies (electrode loose): %d rep" % anomalies)
    else:
        print("cos_sim threshold: metrics.json 未找到，跳过异常检测")

    comp_ok = recall[1] >= COMP_RECALL_GATE
    bad_ok = recall[2] >= BAD_RECALL_GATE
    verdict = "PASS" if (comp_ok and bad_ok) else "FAIL"
    symbol = "[OK]" if verdict == "PASS" else "[FAIL]"
    print("%s verdict = %s  (comp>=%.2f && bad>=%.2f)"
          % (symbol, verdict, COMP_RECALL_GATE, BAD_RECALL_GATE))

    sys.exit(0 if verdict == "PASS" else 1)


def _parse_args():
    p = argparse.ArgumentParser(description="V4.3 holdout 推理验证")
    p.add_argument("--weights", required=True,
                   help="fusion head 权重路径 (.pt)")
    p.add_argument("--data-root", default="data/v42")
    p.add_argument("--user", required=True, help="holdout user id, e.g. user_04")
    p.add_argument("--exercise", default="curl", choices=["curl", "squat"])
    p.add_argument("--deep", action="store_true",
                   help="fusion head 用 deep 结构（须与训练时一致）")
    return p.parse_args()


def main():
    args = _parse_args()
    run(args)


if __name__ == "__main__":
    main()
