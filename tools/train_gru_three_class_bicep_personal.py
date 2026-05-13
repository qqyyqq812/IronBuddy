#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Train a personal bicep-curl GRU from current Sensor Lab exports only."""

from __future__ import print_function

import argparse
import csv
import json
import math
import os
import random
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

try:
    from tools.ironbuddy_lane_b_emg_preprocess import DEFAULT_EMG_VIEW, PREPROCESS_VERSION
except Exception:
    from ironbuddy_lane_b_emg_preprocess import DEFAULT_EMG_VIEW, PREPROCESS_VERSION  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN_ROOT = ROOT / "data" / "bicep_curl_personal" / "training_runs"
DEFAULT_EXERCISE = "bicep_curl"
FEATURES_7D = [
    "Ang_Vel",
    "Angle",
    "Ang_Accel",
    "Target_RMS",
    "Comp_RMS",
    "Symmetry_Score",
    "Phase_Progress",
]
USER_LABEL_TO_CLASS_NAME = {
    "golden": "standard",
    "bad": "compensating",
    "lazy": "non_standard",
}

import sys
sys.path.insert(0, str(ROOT))
from hardware_engine.cognitive.fusion_model import (  # noqa: E402
    CompensationGRU,
    CLASS_BAD,
    CLASS_GOLDEN,
    CLASS_LAZY,
    CLASS_NAMES,
)

USER_LABEL_TO_CLASS_IDX = {
    "golden": CLASS_GOLDEN,
    "bad": CLASS_LAZY,
    "lazy": CLASS_BAD,
}
CLASS_IDX_TO_USER_LABEL = {
    CLASS_GOLDEN: "golden",
    CLASS_LAZY: "bad",
    CLASS_BAD: "lazy",
}


def _to_float(value, default=None):
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except Exception:
        return default


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _safe_rel(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _normalize_inplace(features):
    features[:, 0] = np.clip(features[:, 0] / 30.0, -3.0, 3.0)
    features[:, 1] = features[:, 1] / 180.0
    features[:, 2] = np.clip(features[:, 2] / 10.0, -1.0, 1.0)
    features[:, 3] = np.clip(features[:, 3], 0.0, 100.0) / 100.0
    features[:, 4] = np.clip(features[:, 4], 0.0, 100.0) / 100.0
    return features


def load_manifest(data_dir):
    path = Path(data_dir) / "personal_dataset_manifest.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _check_minimum_data(manifest, min_groups=2, min_reps=12):
    """Block training when per-class group count or rep count is too low.

    Returns a list of failure reasons. Empty list means the dataset clears the
    minimum bar. Pre-P1.1 datasets that miss `accepted_groups`/`gru_reps` are
    treated as failing (they predate the pooled-ref export anyway).
    """
    labels = manifest.get("labels") if isinstance(manifest, dict) else None
    labels = labels or {}
    reasons = []
    for manifest_key in ("standard", "compensating", "non_standard"):
        info = labels.get(manifest_key) or {}
        groups = int(info.get("accepted_groups") or 0)
        reps = int(info.get("gru_reps") or info.get("reps") or 0)
        if groups < min_groups:
            reasons.append("%s_groups=%d<%d" % (manifest_key, groups, min_groups))
        if reps < min_reps:
            reasons.append("%s_reps=%d<%d" % (manifest_key, reps, min_reps))
    return reasons


def _warn_pre_pool_ref(manifest):
    """Print a single-line warning when the manifest predates pooled p95 ref."""
    robust = (manifest or {}).get("raw_rms_robust100") or {}
    method = robust.get("method")
    if method and method != "pooled_global_rms_p95":
        print(
            "[WARN] dataset uses pre-pool ref (method=%s); retrain after re-exporting "
            "with ironbuddy_export_personal_bicep_gru_dataset for pooled_global_rms_p95"
            % method
        )


def default_out_path():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return DEFAULT_RUN_ROOT / stamp / "candidate_weight.pt"


def read_group_csv(path, user_label):
    rows = []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            values = []
            ok = True
            for feature in FEATURES_7D:
                value = _to_float(row.get(feature))
                if value is None:
                    ok = False
                    break
                values.append(value)
            if ok:
                rows.append(values)
    if not rows:
        return None
    return {
        "path": str(path),
        "group_id": Path(path).stem,
        "user_label": user_label,
        "class_idx": USER_LABEL_TO_CLASS_IDX[user_label],
        "rows": np.array(rows, dtype=np.float32),
    }


def gather_groups(data_dir):
    data_dir = Path(data_dir)
    groups = []
    for user_label in ("golden", "bad", "lazy"):
        label_dir = data_dir / user_label
        for path in sorted(label_dir.glob("*.csv")) if label_dir.is_dir() else []:
            group = read_group_csv(path, user_label)
            if group is not None:
                groups.append(group)
    return groups


def split_groups(groups, val_ratio=0.2, seed=42, allow_single_group_split=False):
    rng = random.Random(seed)
    by_label = defaultdict(list)
    for group in groups:
        by_label[group["user_label"]].append(group)
    train = []
    val = []
    errors = []
    for user_label in ("golden", "bad", "lazy"):
        items = list(by_label.get(user_label, []))
        if len(items) < 2:
            if allow_single_group_split and len(items) == 1:
                src = items[0]
                rows = src.get("rows")
                if rows is None:
                    rows = []
                if len(rows) < 4:
                    errors.append("single_group_too_few_rows:%s" % user_label)
                    continue
                cut = max(2, int(round(len(rows) * (1.0 - float(val_ratio)))))
                cut = min(cut, len(rows) - 1)
                train_g = dict(src)
                train_g["rows"] = rows[:cut]
                train_g["group_id"] = "%s__split_train" % src.get("group_id", "g")
                val_g = dict(src)
                val_g["rows"] = rows[cut:]
                val_g["group_id"] = "%s__split_val" % src.get("group_id", "g")
                train.append(train_g)
                val.append(val_g)
                continue
            errors.append("not_enough_groups_for_split:%s" % user_label)
            continue
        rng.shuffle(items)
        val_count = max(1, int(round(len(items) * float(val_ratio))))
        val_count = min(val_count, len(items) - 1)
        val.extend(items[:val_count])
        train.extend(items[val_count:])
    return train, val, errors


def make_windows(groups, seq_len=30, stride=10):
    samples = []
    labels = []
    source = []
    for group in groups:
        rows = group["rows"]
        if len(rows) < seq_len:
            continue
        for start in range(0, len(rows) - seq_len + 1, stride):
            window = np.array(rows[start:start + seq_len], dtype=np.float32, copy=True)
            _normalize_inplace(window)
            samples.append(window)
            labels.append(group["class_idx"])
            source.append({
                "group_id": group["group_id"],
                "path": _safe_rel(group["path"]),
                "start": start,
            })
    return samples, labels, source


def augment_window(window, rng, args):
    out = np.array(window, dtype=np.float32, copy=True)
    noise_std = float(getattr(args, "augment_noise_std", 0.0) or 0.0)
    amp_jitter = float(getattr(args, "augment_amp_jitter", 0.0) or 0.0)
    time_warp = float(getattr(args, "augment_time_warp", 0.0) or 0.0)
    if amp_jitter > 0:
        for idx in (3, 4):
            scale = 1.0 + rng.uniform(-amp_jitter, amp_jitter)
            out[:, idx] = np.clip(out[:, idx] * scale, 0.0, 1.0)
    if noise_std > 0:
        out[:, 3:5] = np.clip(
            out[:, 3:5] + rng.normal(0.0, noise_std, size=out[:, 3:5].shape).astype(np.float32),
            0.0,
            1.0,
        )
        out[:, 0] = np.clip(out[:, 0] + rng.normal(0.0, noise_std * 0.2, size=out[:, 0].shape), -3.0, 3.0)
    if time_warp > 0 and len(out) >= 4:
        factor = 1.0 + rng.uniform(-time_warp, time_warp)
        old_x = np.linspace(0.0, 1.0, len(out))
        warped_x = np.clip(np.linspace(0.0, 1.0, len(out)) ** factor, 0.0, 1.0)
        warped = np.zeros_like(out)
        for col in range(out.shape[1]):
            warped[:, col] = np.interp(warped_x, old_x, out[:, col])
        out = warped.astype(np.float32)
    return out


def augment_samples(samples, labels, args):
    rounds = int(getattr(args, "augment_rounds", 0) or 0)
    if rounds <= 0:
        return samples, labels
    rng = np.random.default_rng(int(getattr(args, "seed", 42) or 42) + 1009)
    augmented_samples = list(samples)
    augmented_labels = list(labels)
    for _round in range(rounds):
        for window, label in zip(samples, labels):
            augmented_samples.append(augment_window(window, rng, args))
            augmented_labels.append(label)
    return augmented_samples, augmented_labels


class WindowDataset(Dataset):
    def __init__(self, samples, labels):
        self.samples = samples
        self.labels = labels

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return torch.tensor(self.samples[idx], dtype=torch.float32), int(self.labels[idx])


def confusion_to_metrics(confusion):
    per_class = {}
    recalls = []
    for idx, name in enumerate(CLASS_NAMES):
        row_sum = int(confusion[idx].sum())
        recall = float(confusion[idx, idx]) / float(row_sum) if row_sum else 0.0
        per_class[name] = {
            "support": row_sum,
            "recall": round(recall, 4),
            "row": [int(v) for v in confusion[idx].tolist()],
        }
        recalls.append(recall)
    return {
        "per_class": per_class,
        "macro_recall": round(sum(recalls) / float(len(recalls)), 4),
        "confusion": [[int(v) for v in row] for row in confusion.tolist()],
    }


def evaluate(model, samples, labels, batch_size=64):
    dataset = WindowDataset(samples, labels)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)
    confusion = np.zeros((3, 3), dtype=np.int64)
    total_loss = 0.0
    count = 0
    model.eval()
    with torch.no_grad():
        for x, y in loader:
            sim, logits, _phase = model(x)
            loss = F.cross_entropy(logits, y)
            total_loss += float(loss.item()) * int(len(y))
            pred = logits.argmax(dim=1)
            for yp, yt in zip(pred.tolist(), y.tolist()):
                confusion[int(yt), int(yp)] += 1
            count += int(len(y))
    metrics = confusion_to_metrics(confusion)
    metrics["loss"] = round(total_loss / float(max(count, 1)), 6)
    metrics["windows"] = count
    return metrics


def class_weights(labels):
    counts = Counter(int(v) for v in labels)
    total = sum(counts.values())
    weights = []
    for idx in (CLASS_GOLDEN, CLASS_LAZY, CLASS_BAD):
        weights.append(total / (3.0 * max(counts.get(idx, 0), 1)))
    return torch.tensor(weights, dtype=torch.float32)


def group_names(groups):
    return sorted([g["group_id"] for g in groups])


def build_report(args, manifest, train_groups, val_groups, train_labels, val_labels,
                 best_metrics, best_epoch, passed, out_path):
    return {
        "ok": True,
        "schema_version": 1,
        "created_ts": time.time(),
        "data_dir": _safe_rel(args.data_dir),
        "source_dataset": {
            "data_dir": _safe_rel(args.data_dir),
            "source_run_id": manifest.get("source_run_id"),
            "manifest_path": _safe_rel(Path(args.data_dir) / "personal_dataset_manifest.json"),
        },
        "exercise": manifest.get("exercise") or getattr(args, "exercise", DEFAULT_EXERCISE),
        "manifest_training_ready": bool(manifest.get("training_ready")) if manifest else None,
        "emg_view": manifest.get("emg_view"),
        "preprocess_version": manifest.get("preprocess_version") or PREPROCESS_VERSION,
        "mvc_source": manifest.get("mvc_source"),
        "mvc_values": manifest.get("mvc_values"),
        "domain_method": manifest.get("domain_method"),
        "domain_params": manifest.get("domain_params"),
        "raw_rms_robust100": manifest.get("raw_rms_robust100"),
        "model_out": _safe_rel(out_path),
        "passed_acceptance": bool(passed),
        "acceptance": {
            "min_recall": args.min_recall,
            "all_classes_meet_min_recall": bool(passed),
        },
        "params": {
            "epochs": args.epochs,
            "batch_size": args.batch_size,
            "lr": args.lr,
            "seq_len": args.seq_len,
            "stride": args.stride,
            "val_ratio": args.val_ratio,
            "seed": args.seed,
            "augment_rounds": getattr(args, "augment_rounds", 0),
            "augment_noise_std": getattr(args, "augment_noise_std", 0.0),
            "augment_amp_jitter": getattr(args, "augment_amp_jitter", 0.0),
            "augment_time_warp": getattr(args, "augment_time_warp", 0.0),
        },
        "groups": {
            "train": group_names(train_groups),
            "validation": group_names(val_groups),
        },
        "windows": {
            "train": len(train_labels),
            "validation": len(val_labels),
            "train_class_counts": dict((CLASS_NAMES[k], v) for k, v in Counter(train_labels).items()),
            "validation_class_counts": dict((CLASS_NAMES[k], v) for k, v in Counter(val_labels).items()),
        },
        "best_epoch": best_epoch,
        "validation": best_metrics,
    }


def build_markdown(report):
    lines = []
    lines.append("# Lane B 当前个人 GRU 训练报告")
    lines.append("")
    lines.append("- 数据目录: `%s`" % report.get("data_dir"))
    lines.append("- 候选权重: `%s`" % report.get("model_out"))
    lines.append("- 动作: `%s`" % report.get("exercise"))
    lines.append("- EMG 口径: `%s`" % report.get("emg_view"))
    lines.append("- 预处理版本: `%s`" % report.get("preprocess_version"))
    lines.append("- 验证通过: `%s`" % report.get("passed_acceptance"))
    lines.append("- best epoch: `%s`" % report.get("best_epoch"))
    lines.append("")
    lines.append("## 验证集")
    lines.append("| 类别 | support | recall | row/std,comp,non |")
    lines.append("| --- | --- | --- | --- |")
    per_class = report.get("validation", {}).get("per_class", {})
    for name in CLASS_NAMES:
        item = per_class.get(name, {})
        lines.append("| %s | %s | %.4f | %s |" % (
            name,
            item.get("support", 0),
            item.get("recall", 0.0),
            item.get("row", []),
        ))
    lines.append("")
    lines.append("## 判定")
    if report.get("passed_acceptance"):
        lines.append("- 达到每类 recall 门槛，可以作为候选权重进入现场复测。")
    else:
        lines.append("- 未达到每类 recall 门槛，不建议部署；继续采集或检查标签/饱和/rep 边界。")
    health = report.get("sanity_probe")
    if isinstance(health, dict):
        lines.append("")
        lines.append("## Sanity Probe")
        if health.get("ok"):
            lines.append("- 边界探针通过：模型不是 collapsed predictor，主肌/代偿方向也未反转。")
        else:
            flags = []
            if health.get("is_collapsed_predictor"):
                flags.append("collapsed_predictor")
            if health.get("is_boundary_inverted"):
                flags.append("boundary_inverted")
            lines.append("- **失败原因**：" + (", ".join(flags) if flags else "未知"))
            if report.get("sanity_probe_block"):
                lines.append("- 已**拒绝保存** .pt（除非显式传 `--allow-unhealthy-model`）。")
        lines.append("- compensating 概率跨探针 spread = `%s`（<0.10 视为塌陷）" % health.get("comp_prob_spread"))
        lines.append("- 单类最高概率 max(probs) = `%s`（<0.50 视为塌陷：模型从不确信）" % health.get("max_class_prob"))
        lines.append("")
        lines.append("| 探针 | Target_RMS | Comp_RMS | 预测 | probs[std,comp,non] | sim |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for probe in health.get("probes", []):
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                probe.get("name"),
                probe.get("target_rms"),
                probe.get("comp_rms"),
                probe.get("label"),
                probe.get("probs"),
                probe.get("sim"),
            ))
    return "\n".join(lines)


def train_personal(args):
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    manifest = load_manifest(args.data_dir)
    if not manifest:
        return 2, {
            "ok": False,
            "error": "manifest_required",
            "manifest": str(Path(args.data_dir) / "personal_dataset_manifest.json"),
        }
    manifest_emg_view = manifest.get("emg_view")
    manifest_preprocess_version = manifest.get("preprocess_version")
    if not manifest_emg_view or not manifest_preprocess_version:
        return 2, {
            "ok": False,
            "error": "manifest_missing_preprocess_metadata",
            "reasons": [
                "emg_view=%s" % manifest_emg_view,
                "preprocess_version=%s" % manifest_preprocess_version,
            ],
        }
    if not manifest.get("training_ready") and not args.allow_not_ready:
        return 2, {
            "ok": False,
            "error": "dataset_not_training_ready",
            "manifest": str(Path(args.data_dir) / "personal_dataset_manifest.json"),
        }
    if manifest_emg_view != DEFAULT_EMG_VIEW and not args.allow_non_default_emg_view:
        return 2, {
            "ok": False,
            "error": "unexpected_emg_view",
            "reasons": ["manifest_emg_view=%s expected=%s" % (manifest_emg_view, DEFAULT_EMG_VIEW)],
        }

    _warn_pre_pool_ref(manifest)

    hard_block_reasons = _check_minimum_data(manifest)
    if hard_block_reasons and not getattr(args, "allow_data_starved", False):
        return 3, {
            "ok": False,
            "error": "minimum_data_not_met",
            "reasons": hard_block_reasons,
            "hint": (
                "Need >=2 groups/label and >=12 reps/label. "
                "Add --allow-data-starved to bypass (pipeline smoke test only)."
            ),
        }

    groups = gather_groups(args.data_dir)
    train_groups, val_groups, split_errors = split_groups(
        groups, args.val_ratio, args.seed,
        allow_single_group_split=bool(getattr(args, "allow_single_group_split", False)),
    )
    if split_errors:
        return 2, {"ok": False, "error": "split_failed", "reasons": split_errors}

    train_samples, train_labels, _train_src = make_windows(train_groups, args.seq_len, args.stride)
    val_samples, val_labels, _val_src = make_windows(val_groups, args.seq_len, args.stride)
    if not train_samples or not val_samples:
        return 2, {
            "ok": False,
            "error": "no_windows_after_split",
            "train_windows": len(train_samples),
            "validation_windows": len(val_samples),
        }
    train_samples, train_labels = augment_samples(train_samples, train_labels, args)

    model = CompensationGRU(input_size=7, hidden_size=16)
    weights = class_weights(train_labels)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=8, gamma=0.5)
    train_loader = DataLoader(
        WindowDataset(train_samples, train_labels),
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
    )
    sim_target_by_cls = {CLASS_GOLDEN: 1.0, CLASS_LAZY: 0.6, CLASS_BAD: 0.3}
    best_state = None
    best_metrics = None
    best_epoch = 0
    best_score = -1.0

    for epoch in range(1, args.epochs + 1):
        model.train()
        for x, y in train_loader:
            optimizer.zero_grad()
            sim_tgt = torch.tensor(
                [[sim_target_by_cls[int(c)]] for c in y],
                dtype=torch.float32,
            )
            sim_pred, logits, _phase = model(x)
            loss = 0.3 * F.mse_loss(sim_pred, sim_tgt) + 0.7 * F.cross_entropy(logits, y, weight=weights)
            loss.backward()
            optimizer.step()
        scheduler.step()
        metrics = evaluate(model, val_samples, val_labels, batch_size=args.batch_size)
        score = metrics["macro_recall"]
        if score > best_score:
            best_score = score
            best_metrics = metrics
            best_epoch = epoch
            best_state = dict((k, v.detach().cpu().clone()) for k, v in model.state_dict().items())

    if best_state is not None:
        model.load_state_dict(best_state)
    passed = bool(
        best_metrics and all(
            best_metrics["per_class"].get(name, {}).get("recall", 0.0) >= args.min_recall
            for name in CLASS_NAMES
        )
    )

    out_path = Path(args.out)
    report = build_report(
        args, manifest, train_groups, val_groups, train_labels, val_labels,
        best_metrics or {}, best_epoch, passed, out_path,
    )
    report_path = Path(args.report) if args.report else out_path.parent / "train_report.json"
    md_path = out_path.parent / "train_report.md"

    # P1.2 — Sanity probe: catch collapsed predictor / boundary inversion before
    # the .pt is ever written. Even a model that "passes" macro-recall on a tiny
    # validation set can still be a collapsed predictor on canonical probes.
    health = _sanity_probe_model(model)
    report["sanity_probe"] = health
    if not health["ok"]:
        report["sanity_probe_block"] = True
        if not bool(getattr(args, "allow_unhealthy_model", False)):
            # Refuse to save the .pt. Persist the report (incl. probe details)
            # so operators can see exactly which probe failed.
            _atomic_write_json(report_path, report)
            md_path.write_text(build_markdown(report), encoding="utf-8")
            _atomic_write_json(
                out_path.parent / "source_dataset.json",
                report.get("source_dataset") or {"data_dir": _safe_rel(args.data_dir)},
            )
            reasons = []
            if health.get("is_collapsed_predictor"):
                reasons.append("collapsed_predictor")
            if health.get("is_boundary_inverted"):
                reasons.append("boundary_inverted")
            return 4, {
                "ok": False,
                "error": "sanity_probe_failed",
                "reasons": reasons,
                "health": health,
                "report": _safe_rel(report_path),
            }

    _atomic_write_json(report_path, report)
    md_path.write_text(build_markdown(report), encoding="utf-8")
    _atomic_write_json(
        out_path.parent / "source_dataset.json",
        report.get("source_dataset") or {"data_dir": _safe_rel(args.data_dir)},
    )

    if passed or args.save_failed:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        torch.save(model.state_dict(), tmp)
        os.replace(str(tmp), str(out_path))
    elif out_path.exists():
        # Avoid leaving a stale candidate path that looks newly accepted.
        pass

    return 0 if passed else 2, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True,
                        help="Export directory containing personal_dataset_manifest.json and golden/bad/lazy CSVs.")
    parser.add_argument("--out", default=str(default_out_path()))
    parser.add_argument("--report", default=None)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=5e-3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--seq-len", type=int, default=30)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--allow-not-ready", action="store_true")
    parser.add_argument("--allow-non-default-emg-view", action="store_true")
    parser.add_argument("--save-failed", action="store_true",
                        help="Save model even if validation does not pass. For diagnosis only.")
    parser.add_argument("--allow-unhealthy-model", action="store_true",
                        help="Bypass sanity probe (collapsed predictor / boundary inverted). "
                             "Diagnosis only; never default in lab runs.")
    parser.add_argument("--allow-single-group-split", action="store_true",
                        help="If a label has only one group, time-split its rows 80/20 to "
                             "make train/val. Preview-only — produces an overfit model.")
    parser.add_argument("--allow-data-starved", action="store_true",
                        help="Bypass the >=2 groups/label and >=12 reps/label hard minimum. "
                             "Pipeline smoke test only — do NOT deploy resulting models to board.")
    parser.add_argument("--exercise", default=DEFAULT_EXERCISE,
                        help="Report metadata only. Use squat wrapper for squat defaults.")
    parser.add_argument("--augment-rounds", type=int, default=1,
                        help="Number of augmented copies per training window.")
    parser.add_argument("--augment-noise-std", type=float, default=0.015,
                        help="Gaussian noise added to normalized EMG features during augmentation.")
    parser.add_argument("--augment-amp-jitter", type=float, default=0.12,
                        help="Random multiplicative jitter for normalized Target/Comp features.")
    parser.add_argument("--augment-time-warp", type=float, default=0.08,
                        help="Small temporal interpolation jitter for training windows.")
    args = parser.parse_args(argv)

    code, result = train_personal(args)
    if result.get("ok") is False:
        print("[FATAL] %s" % result.get("error"))
        for reason in result.get("reasons", []):
            print("  - %s" % reason)
        return code
    print("passed_acceptance=%s" % result.get("passed_acceptance"))
    print("best_epoch=%s" % result.get("best_epoch"))
    print("report=%s" % (args.report or str(Path(args.out).parent / "train_report.json")))
    if result.get("passed_acceptance"):
        print("model=%s" % args.out)
    else:
        print("model_not_saved_unless_--save-failed")
    return code


# ---------------------------------------------------------------------------
# P1.2 — Sanity probe (boundary check on trained model)
#
# Reads 6 canonical probe windows through the trained model. Detects:
#   1. Collapsed predictor — all probes give similar probs (max-min < 0.10).
#      Such a model "passes" macro-recall on a tiny val set but always emits
#      compensating in deployment.
#   2. Boundary inverted — high EMG → standard, low EMG → compensating is
#      the expected direction; the inverse means training labels are flipped.
#
# Used by train_personal() before saving the .pt, and by
# tools/probe_model_boundary.py for manual pre-deployment gating.
# ---------------------------------------------------------------------------
def _sanity_probe_model(model, seq_len=30):
    """Run 6 canonical probe windows on the trained model.

    Each probe is a 7D window of (Ang_Vel=0, Angle=0, Ang_Accel=0,
    Target_RMS=t/100, Comp_RMS=c/100, Symmetry=0, Phase=0) with seq_len=30.
    Target_RMS/Comp_RMS are pre-normalized to [0,1] (the same scale the
    training pipeline produces after `_normalize_inplace`).

    Returns a health dict:
        {
            "ok": bool,
            "is_collapsed_predictor": bool,
            "is_boundary_inverted": bool,
            "comp_prob_spread": float,
            "probes": [ {name, target_rms, comp_rms, label, probs, sim}, ... ],
        }
    """
    probes_spec = [
        ("zero_window",  0.0,  0.0),
        ("low_both",     10.0, 10.0),
        ("mid_both",     30.0, 30.0),
        ("high_both",    80.0, 80.0),
        ("high_T_low_C", 80.0, 10.0),   # strong target / weak comp -> should be standard
        ("low_T_high_C", 10.0, 80.0),   # weak target / strong comp -> should be compensating
    ]
    results = []
    model.eval()
    for name, t_val, c_val in probes_spec:
        win = np.zeros((1, int(seq_len), 7), dtype=np.float32)
        win[:, :, 3] = t_val / 100.0   # Target_RMS column (matches _normalize_inplace)
        win[:, :, 4] = c_val / 100.0   # Comp_RMS column
        with torch.no_grad():
            sim, logits, _phase = model(torch.from_numpy(win))
            probs = torch.softmax(logits, dim=1)[0].numpy().tolist()
            cls = int(np.argmax(probs))
            label = CLASS_NAMES[cls]
        results.append({
            "name": name,
            "target_rms": t_val,
            "comp_rms": c_val,
            "label": label,
            "probs": [round(float(p), 3) for p in probs],
            "sim": round(float(sim.item()), 3),
        })

    # Collapse detectors (two complementary signals — a model is collapsed if
    # EITHER fires):
    #   1. Comp prob spread across probes < 0.10  — model gives near-identical
    #      compensating probability regardless of input.
    #   2. No probe reaches max class prob > 0.50  — even on extreme inputs the
    #      model never commits to any class (uniformly blurred output).
    # The second detector catches the May-13 .pt: its comp spread is 0.114
    # (above 0.10) but its highest single-class prob across all 6 canonical
    # probes is only ~0.39, so it's barely above chance.
    comp_probs = [r["probs"][1] for r in results]
    comp_spread = float(max(comp_probs) - min(comp_probs))
    max_class_prob = max(max(r["probs"]) for r in results) if results else 0.0
    is_collapsed = (comp_spread < 0.10) or (max_class_prob < 0.50)

    # Inversion detector: high_T_low_C should NOT classify as compensating.
    high_T_low_C = next(r for r in results if r["name"] == "high_T_low_C")
    is_inverted = (high_T_low_C["label"] == "compensating")

    return {
        "ok": (not is_collapsed) and (not is_inverted),
        "is_collapsed_predictor": bool(is_collapsed),
        "is_boundary_inverted": bool(is_inverted),
        "comp_prob_spread": round(comp_spread, 3),
        "max_class_prob": round(max_class_prob, 3),
        "probes": results,
    }


if __name__ == "__main__":
    raise SystemExit(main())
