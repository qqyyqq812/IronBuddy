# coding=utf-8
"""
V4.3 Domain Adaptation：FLEX-pretrained encoder → 本地 data/v42 LOSO 微调
======================================================================

plan §1.5 Stage 2 (flex-curl-only-pivot.md)

流程：
  1. 构造 DualBranchFusionModel
  2. 加载指定路径的预训练 encoder（vision + emg 两个 .pt）
     - 失败 → 醒目 warning + 回到 xavier 初始化（兜底不 crash）
  3. --freeze-base: 冻结 encoder 全部参数
     --unfreeze-last-gru-layer: 仅解锁 encoder 的 head Linear 层（GRU 1 层结构里最接近 fusion 的最后一层）
  4. LOSO 3 折（3 个 user）- 直接复用 train_fusion_head 的辅助函数（_resample_matrix / load_user_reps /
     extract_all_features / train_one_fold / _discover_users / _drop_tiny_classes）
  5. 过拟合硬闸门 train_f1 - val_f1 > 0.15 立即 break（继承 train_fusion_head 行为）
  6. 保存 weights/<output-name>.pt + <output-name>_metrics.json（含三折 + 混淆矩阵 + cos_sim 5% 阈值）

关键差异 vs train_fusion_head.py：
  - 加载指定 path 的 encoder（而非默认 vision/emg_encoder_local.pt）
  - 支持解锁 encoder 最后一层
  - output_name 由 --output-name 参数控制
  - 当 --unfreeze-last-gru-layer 时，一起训 head Linear（模型最后一层）+ fusion head

用法：
    # FLEX → v42 主路径
    python tools/finetune_with_local.py \\
        --pretrained-encoder hardware_engine/cognitive/weights/emg_encoder_flex_pretrained.pt \\
        --pretrained-vision hardware_engine/cognitive/weights/vision_encoder_flex_pretrained.pt \\
        --freeze-base \\
        --epochs 30 \\
        --output-name v42_fusion_head_curl_da

    # 仅传 EMG encoder + 自动推断同目录 vision encoder
    python tools/finetune_with_local.py \\
        --pretrained-encoder hardware_engine/cognitive/weights/emg_encoder_flex_pretrained.pt \\
        --freeze-base

Python 3.7 兼容（不用 `X | None` / 海象 / match-case）。
"""
from __future__ import absolute_import, division, print_function

import argparse
import json
import os
import sys
import warnings
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS_DIR)

try:
    import torch
    import torch.nn as nn
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

try:
    import pandas as pd  # noqa: F401
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    from sklearn.metrics import confusion_matrix
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

from hardware_engine.cognitive.dual_branch_fusion import (  # noqa: E402
    DualBranchFusionModel,
    FusionHead,
    NUM_CLASSES,
    LABEL_NAMES,
    FUSION_INPUT_DIM,
)

# 复用 train_fusion_head 里已经稳定的辅助函数（TOOLS_DIR 已入 sys.path）
import train_fusion_head as tfh  # type: ignore  # noqa: E402


# =============================================================================
# 特殊 helper：加载指定路径的 encoder 权重（不污染 dual_branch_fusion.py）
# =============================================================================
def _xavier_reset(module):
    """对一个 encoder 模块做 xavier_uniform 初始化（兜底）。"""
    for m in module.modules():
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.zeros_(m.bias)
        elif isinstance(m, nn.GRU):
            for name, p in m.named_parameters():
                if 'weight' in name:
                    nn.init.xavier_uniform_(p)
                elif 'bias' in name:
                    nn.init.zeros_(p)


def _load_specific_path(encoder, path, label):
    """从 path 加载 encoder state_dict。失败 → xavier init + warning，不 raise。"""
    if not path:
        print("[finetune] %s: 未指定路径 → xavier 初始化" % label)
        _xavier_reset(encoder)
        return False
    if not os.path.isfile(path):
        print("\n" + "!" * 70)
        print("!!  WARN: %s 权重文件缺失: %s" % (label, path))
        print("!!        回退到 xavier_uniform 初始化。真跑前先确认 FLEX pretrain 已完成。")
        print("!" * 70 + "\n")
        _xavier_reset(encoder)
        return False
    try:
        state = torch.load(path, map_location='cpu')
        encoder.load_state_dict(state)
        print("[finetune] %s: 加载成功 %s" % (label, path))
        return True
    except Exception as ex:
        print("\n" + "!" * 70)
        print("!!  WARN: %s load_state_dict 失败: %s" % (label, ex))
        print("!!        回退到 xavier_uniform 初始化。")
        print("!" * 70 + "\n")
        _xavier_reset(encoder)
        return False


def load_pretrained_encoders_from_paths(model, vision_path, emg_path):
    """对 model.vision_encoder / emg_encoder 各自指定 path 加载。"""
    v_ok = _load_specific_path(model.vision_encoder, vision_path, "vision_encoder")
    e_ok = _load_specific_path(model.emg_encoder, emg_path, "emg_encoder")
    return v_ok and e_ok


def _infer_sibling_vision_path(emg_path):
    """给 emg_encoder_flex_pretrained.pt → 同目录 vision_encoder_flex_pretrained.pt。"""
    if not emg_path:
        return None
    d = os.path.dirname(emg_path)
    base = os.path.basename(emg_path)
    guess = base.replace('emg_encoder', 'vision_encoder')
    if guess == base:
        return None
    return os.path.join(d, guess)


# =============================================================================
# Freeze / unfreeze 控制
# =============================================================================
def set_freeze_policy(model, freeze_base, unfreeze_last_gru_layer):
    """
    freeze_base=True 且 unfreeze_last_gru_layer=False → 完全冻结 encoder
    freeze_base=True 且 unfreeze_last_gru_layer=True  → 只解锁 encoder 的 head Linear
    freeze_base=False                                  → 不冻结
    """
    if not freeze_base:
        for p in model.vision_encoder.parameters():
            p.requires_grad = True
        for p in model.emg_encoder.parameters():
            p.requires_grad = True
        print("[finetune] freeze_policy: 全解锁（vision + emg encoder）")
        return

    # 先全冻
    for p in model.vision_encoder.parameters():
        p.requires_grad = False
    for p in model.emg_encoder.parameters():
        p.requires_grad = False

    if unfreeze_last_gru_layer:
        # dual_branch_fusion.VisionEncoder/EMGEncoder 是 GRU(1 层) + head Linear
        # "最后一层 GRU" 在单层结构中即整个 GRU；为稳健起见，我们解锁 head Linear
        # （参数量约 vision=56 + emg=56）+ 任一层均拟合范围内。
        for p in model.vision_encoder.head.parameters():
            p.requires_grad = True
        for p in model.emg_encoder.head.parameters():
            p.requires_grad = True
        print("[finetune] freeze_policy: 冻结 encoder GRU，仅解锁 head Linear")
    else:
        print("[finetune] freeze_policy: 完全冻结 encoder")


# =============================================================================
# 手动多折训练循环（head + 可选的 encoder head 一起训）
# =============================================================================
def _macro_f1(y_true, y_pred):
    return tfh._macro_f1(y_true, y_pred)


def extract_all_21d(model, reps, device):
    """复用 train_fusion_head.extract_all_features（inner with torch.no_grad）。
    注意：若解锁 encoder，我们此处仍然一次性 cache 21d 会错过 encoder 的梯度更新。
    所以我们另写一个 trainable 版本的 train_one_fold。
    """
    return tfh.extract_all_features(model, reps, device)


def train_one_fold_trainable(model, train_reps, val_reps, args, fold_idx, n_folds, device):
    """若 encoder 被解锁，forward 必须在 loop 里跑（不能 cache）。
    所有 requires_grad=True 的参数 → 放进 optimizer。
    """
    # 重建 fusion head（每折独立）
    head = FusionHead(deep=args.deep, dropout=args.dropout).to(device)
    model.fusion_head = head

    # 收集所有 trainable 参数
    trainable = [p for p in model.parameters() if p.requires_grad]
    n_trainable = sum(p.numel() for p in trainable)
    print("  [fold %d] trainable params = %d" % (fold_idx + 1, n_trainable))
    optim = torch.optim.Adam(trainable, lr=args.lr, weight_decay=args.weight_decay)
    loss_fn = nn.CrossEntropyLoss()

    # 先把 vision_seq/emg_seq/手工scalar/label 堆成 tensor batch
    def _pack(reps):
        vs = np.stack([r['vision_seq'] for r in reps], axis=0)
        es = np.stack([r['emg_seq'] for r in reps], axis=0)
        ys = np.asarray([r['label'] for r in reps], dtype=np.int64)
        return (torch.from_numpy(vs).float().to(device),
                torch.from_numpy(es).float().to(device),
                torch.from_numpy(ys).to(device))

    train_V, train_E, train_y = _pack(train_reps)
    val_V, val_E, val_y = _pack(val_reps)

    # 手工 5d 一次性算好（它不受 encoder 梯度影响，但和 encoder 输出有关，
    # cos_sim 需要当下 embedding；策略：每 epoch 重算一次，冻结 encoder 场景下也可）
    from hardware_engine.cognitive.dual_branch_fusion import HandCraftedFeatureExtractor

    def _handcrafted(reps, V_batch, E_batch):
        # 当前权重下算 embedding
        model.eval()
        with torch.no_grad():
            v_emb = model.vision_encoder(V_batch)
            e_emb = model.emg_encoder(E_batch)
        model.train()
        hc = []
        for i, r in enumerate(reps):
            hc.append(HandCraftedFeatureExtractor.extract(
                r['rep_data'], v_emb[i].cpu(), e_emb[i].cpu()
            ))
        return torch.stack(hc, dim=0).to(device)

    best_val_f1 = -1.0
    best_train_f1 = 0.0
    best_state_head = None
    best_state_enc_v = None
    best_state_enc_e = None
    best_val_preds = None
    patience_left = args.patience

    n_train = train_V.shape[0]
    for epoch in range(1, args.epochs + 1):
        model.train()
        # per-epoch 重算 handcrafted（embedding 随训练变化）
        train_H = _handcrafted(train_reps, train_V, train_E)
        val_H = _handcrafted(val_reps, val_V, val_E)
        perm = torch.randperm(n_train, device=device)
        total_loss = 0.0
        for i in range(0, n_train, args.batch_size):
            idx = perm[i:i + args.batch_size]
            vV = train_V[idx]
            eE = train_E[idx]
            hH = train_H[idx]
            yB = train_y[idx]
            out = model(vV, eE, hH)
            loss = loss_fn(out['logits'], yB)
            optim.zero_grad()
            loss.backward()
            optim.step()
            total_loss += float(loss.item()) * vV.shape[0]

        # eval
        model.eval()
        with torch.no_grad():
            tr_logits = model(train_V, train_E, train_H)['logits']
            va_logits = model(val_V, val_E, val_H)['logits']
            tr_pred = tr_logits.argmax(dim=1).cpu().numpy()
            va_pred = va_logits.argmax(dim=1).cpu().numpy()
        tr_true = train_y.cpu().numpy()
        va_true = val_y.cpu().numpy()
        tr_f1 = _macro_f1(tr_true, tr_pred)
        va_f1 = _macro_f1(va_true, va_pred)
        gap = tr_f1 - va_f1
        print("[fold %d/%d][epoch %2d] loss=%.4f train_f1=%.3f val_f1=%.3f gap=%+.3f"
              % (fold_idx + 1, n_folds, epoch, total_loss / max(1, n_train),
                 tr_f1, va_f1, gap))

        if gap > args.overfit_gap_threshold:
            warnings.warn("[fold %d] overfit gate (gap=%.3f > %.3f) → stop"
                          % (fold_idx + 1, gap, args.overfit_gap_threshold))
            break

        if va_f1 > best_val_f1 + 1e-6:
            best_val_f1 = va_f1
            best_train_f1 = tr_f1
            best_state_head = {k: v.detach().cpu().clone()
                                for k, v in model.fusion_head.state_dict().items()}
            best_state_enc_v = {k: v.detach().cpu().clone()
                                 for k, v in model.vision_encoder.state_dict().items()}
            best_state_enc_e = {k: v.detach().cpu().clone()
                                 for k, v in model.emg_encoder.state_dict().items()}
            best_val_preds = va_pred.copy()
            patience_left = args.patience
        else:
            patience_left -= 1
            if patience_left <= 0:
                print("[fold %d] early stop at epoch %d (best_val_f1=%.3f)"
                      % (fold_idx + 1, epoch, best_val_f1))
                break

    if best_state_head is None:
        best_state_head = {k: v.detach().cpu().clone()
                            for k, v in model.fusion_head.state_dict().items()}
        best_state_enc_v = {k: v.detach().cpu().clone()
                             for k, v in model.vision_encoder.state_dict().items()}
        best_state_enc_e = {k: v.detach().cpu().clone()
                             for k, v in model.emg_encoder.state_dict().items()}
        best_val_preds = va_pred if 'va_pred' in dir() else np.zeros((val_V.shape[0],), dtype=np.int64)
        best_val_f1 = max(best_val_f1, 0.0)

    return (best_val_f1, best_state_head, best_state_enc_v, best_state_enc_e,
            best_train_f1, best_val_preds)


# =============================================================================
# main
# =============================================================================
def main():
    ap = argparse.ArgumentParser(description="V4.3 DA 微调：FLEX encoder → 本地 LOSO")
    ap.add_argument('--pretrained-encoder', default=None,
                    help='EMG encoder .pt 路径；同目录同 prefix 的 vision_encoder_*.pt 自动推断')
    ap.add_argument('--pretrained-vision', default=None,
                    help='vision_encoder .pt 路径（若不想用自动推断）')
    ap.add_argument('--data-root', default=os.path.join(ROOT, 'data/v42'))
    ap.add_argument('--weights-dir', default=os.path.join(ROOT, 'hardware_engine/cognitive/weights'))
    ap.add_argument('--exercise', choices=['curl'], default='curl',
                    help='V4.3 只做 curl（深蹲已砍）')
    ap.add_argument('--output-name', default='v42_fusion_head_curl_da',
                    help='输出文件主名（.pt 和 _metrics.json 会在此基础上加后缀）')
    ap.add_argument('--freeze-base', action='store_true', default=False)
    ap.add_argument('--unfreeze-last-gru-layer', action='store_true', default=False)
    ap.add_argument('--deep', action='store_true')
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch-size', type=int, default=16)
    ap.add_argument('--lr', type=float, default=1e-3)
    ap.add_argument('--weight-decay', type=float, default=1e-3)
    ap.add_argument('--dropout', type=float, default=0.3)
    ap.add_argument('--patience', type=int, default=5)
    ap.add_argument('--overfit-gap-threshold', type=float, default=0.15)
    ap.add_argument('--min-reps-per-class', type=int, default=10)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    print("=" * 72)
    print("[finetune_with_local] V4.3 DA 微调")
    print("  pretrained_encoder  = %s" % args.pretrained_encoder)
    print("  pretrained_vision   = %s" % args.pretrained_vision)
    print("  data_root           = %s" % args.data_root)
    print("  output_name         = %s" % args.output_name)
    print("  freeze_base         = %s" % args.freeze_base)
    print("  unfreeze_last_gru   = %s" % args.unfreeze_last_gru_layer)
    print("=" * 72)

    if not _HAS_TORCH:
        print("FATAL: PyTorch 未安装")
        return 2
    if not _HAS_PANDAS:
        print("FATAL: pandas 未安装（dev 机）")
        return 2

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("[finetune_with_local] device = %s" % device)

    # ---- 1. 建模 + 加载 encoder ----
    os.makedirs(args.weights_dir, exist_ok=True)
    model = DualBranchFusionModel(deep_fusion=args.deep).to(device)

    # 推断 vision 路径
    v_path = args.pretrained_vision
    if v_path is None:
        v_path = _infer_sibling_vision_path(args.pretrained_encoder)
    e_path = args.pretrained_encoder
    load_pretrained_encoders_from_paths(model, v_path, e_path)

    # ---- 2. Freeze 策略 ----
    set_freeze_policy(model, args.freeze_base, args.unfreeze_last_gru_layer)

    print("[finetune_with_local] 模型总参数 = %d (budget <= 800)" % model.param_count())

    # ---- 3. 发现 users + 加载 reps ----
    users = tfh._discover_users(args.data_root, args.exercise)
    print("[finetune_with_local] discovered users: %s" % users)
    if len(users) == 0:
        print("[finetune_with_local] 无 user 数据 → smoke exit 0")
        return 0

    user_reps = {}
    for u in users:
        reps = tfh.load_user_reps(args.data_root, u, args.exercise)
        if not reps:
            continue
        user_reps[u] = reps
        print("  [%s] loaded %d reps: %s" % (u, len(reps), Counter(r['label'] for r in reps)))
    users = [u for u in users if u in user_reps]
    if not users:
        print("[finetune_with_local] 无可用 reps → exit 0")
        return 0
    n_folds = min(3, len(users))

    # 判断是否有可训练的 encoder 参数 → 决定 fold 路径
    any_encoder_trainable = any(
        p.requires_grad for p in list(model.vision_encoder.parameters()) + list(model.emg_encoder.parameters())
    )

    # ---- 4. LOSO ----
    fold_results = []
    all_train_cos_sims = []
    fold_conf_mats = []

    for fold_idx in range(n_folds):
        val_user = users[fold_idx]
        train_users = [u for u in users if u != val_user]
        train_reps = []
        for u in train_users:
            train_reps.extend(user_reps[u])
        val_reps = list(user_reps[val_user])

        train_reps = tfh._drop_tiny_classes(train_reps, min_count=args.min_reps_per_class)
        if not train_reps:
            warnings.warn("fold %d: no train reps after filter" % (fold_idx + 1))
            continue

        print("\n--- Fold %d/%d ---" % (fold_idx + 1, n_folds))
        print("  val_user    = %s  (%d reps)" % (val_user, len(val_reps)))
        print("  train_users = %s (%d reps)" % (train_users, len(train_reps)))

        if any_encoder_trainable:
            # encoder 参与梯度更新
            (best_val_f1, best_state_head, best_state_enc_v, best_state_enc_e,
             best_train_f1, val_preds) = train_one_fold_trainable(
                model, train_reps, val_reps, args, fold_idx, n_folds, device)
        else:
            # encoder 完全冻结 → 复用 tfh 的 feature-cached 训练
            train_X, train_y, train_cs = tfh.extract_all_features(model, train_reps, device)
            val_X, val_y, _ = tfh.extract_all_features(model, val_reps, device)
            all_train_cos_sims.extend(train_cs)
            if train_X.shape[0] == 0 or val_X.shape[0] == 0:
                warnings.warn("fold %d: empty tensors" % (fold_idx + 1))
                continue
            best_val_f1, best_state_head, best_train_f1, val_preds = tfh.train_one_fold(
                model, train_X, train_y, val_X, val_y, args, fold_idx, n_folds, device)
            best_state_enc_v = None
            best_state_enc_e = None

        if _HAS_SKLEARN:
            cm = confusion_matrix(np.asarray([r['label'] for r in val_reps]),
                                  val_preds, labels=list(range(NUM_CLASSES)))
        else:
            cm = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
        fold_conf_mats.append(cm)

        fold_results.append({
            'fold': fold_idx + 1,
            'val_user': val_user,
            'n_train': len(train_reps),
            'n_val': len(val_reps),
            'train_f1': float(best_train_f1),
            'val_f1': float(best_val_f1),
            'best_state_head': best_state_head,
            'best_state_enc_v': best_state_enc_v,
            'best_state_enc_e': best_state_enc_e,
            'confusion_matrix': cm.tolist(),
        })
        print("  fold %d best: train_f1=%.3f val_f1=%.3f" %
              (fold_idx + 1, best_train_f1, best_val_f1))

    if not fold_results:
        print("[finetune_with_local] 无成功折 → exit 1")
        return 1

    # ---- 5. 汇总 ----
    avg_val_f1 = float(np.mean([r['val_f1'] for r in fold_results]))
    avg_train_f1 = float(np.mean([r['train_f1'] for r in fold_results]))
    total_cm = np.sum(np.stack(fold_conf_mats, axis=0), axis=0) if fold_conf_mats \
        else np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)

    print("\n" + "=" * 72)
    print("[finetune_with_local] LOSO 汇总 (%d folds)" % len(fold_results))
    print("  avg train_f1 = %.3f" % avg_train_f1)
    print("  avg val_f1   = %.3f" % avg_val_f1)
    print("  confusion matrix (rows=true, cols=pred):")
    print("                " + "  ".join("%10s" % n for n in LABEL_NAMES))
    for i, row in enumerate(total_cm):
        print("    %-12s" % LABEL_NAMES[i] + "  ".join("%10d" % v for v in row))
    print("=" * 72)

    # ---- 6. 保存最佳折 ----
    best = max(fold_results, key=lambda r: r['val_f1'])
    head_path = os.path.join(args.weights_dir, args.output_name + '.pt')
    torch.save(best['best_state_head'], head_path)
    print("[finetune_with_local] saved head → %s" % head_path)

    if best['best_state_enc_v'] is not None:
        vpath = os.path.join(args.weights_dir, args.output_name + '_vision_encoder.pt')
        epath = os.path.join(args.weights_dir, args.output_name + '_emg_encoder.pt')
        torch.save(best['best_state_enc_v'], vpath)
        torch.save(best['best_state_enc_e'], epath)
        print("[finetune_with_local] saved tuned encoders → %s / %s" % (vpath, epath))

    if all_train_cos_sims:
        cos_sim_p5 = float(np.percentile(np.asarray(all_train_cos_sims), 5))
    else:
        cos_sim_p5 = 0.0

    metrics = {
        'exercise': args.exercise,
        'source': 'flex_da',
        'pretrained_vision': v_path,
        'pretrained_emg': e_path,
        'freeze_base': args.freeze_base,
        'unfreeze_last_gru_layer': args.unfreeze_last_gru_layer,
        'deep_fusion': bool(args.deep),
        'n_folds': len(fold_results),
        'avg_train_f1': avg_train_f1,
        'avg_val_f1': avg_val_f1,
        'folds': [
            {
                'fold': r['fold'],
                'val_user': r['val_user'],
                'n_train': r['n_train'],
                'n_val': r['n_val'],
                'train_f1': r['train_f1'],
                'val_f1': r['val_f1'],
                'confusion_matrix': r['confusion_matrix'],
            } for r in fold_results
        ],
        'confusion_matrix_total': total_cm.tolist(),
        'cos_sim_p5_threshold': cos_sim_p5,
        'label_names': LABEL_NAMES,
        'best_fold_val_user': best['val_user'],
    }
    metrics_path = os.path.join(args.weights_dir, args.output_name + '_metrics.json')
    with open(metrics_path, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print("[finetune_with_local] saved metrics → %s" % metrics_path)
    print("[finetune_with_local] cos_sim 5%% threshold = %.4f" % cos_sim_p5)
    return 0


if __name__ == '__main__':
    sys.exit(main())
