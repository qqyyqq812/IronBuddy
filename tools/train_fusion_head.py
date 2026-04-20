# coding=utf-8
"""
V4.2 Fusion Head LOSO 训练
===========================

plan §4.5 Stage 2 + §5.3(c)

流程：
1. 加载预训练 Encoder (weights/vision_encoder_local.pt + emg_encoder_local.pt)
2. 冻结两 Encoder（requires_grad=False）
3. 从每 rep 算 8d vision_emb + 8d emg_emb + 5 手工标量 = 21d
4. FusionHead Linear(21, 3) = 66 参数（deep=True -> 203 参数）
5. LOSO 3 折（A+B->C, A+C->B, B+C->A），每折训练 90 + 验证 45
6. Adam lr=1e-3, batch=16, dropout=0.3, weight_decay=1e-3, early-stop patience=5
7. 过拟合硬闸门：train_f1 - val_f1 > 0.15 -> 立即终止
8. 弯举 + 深蹲各训一份：
     weights/v42_fusion_head_curl.pt
     weights/v42_fusion_head_squat.pt

Stage 3 欠拟合救援（仅 val_f1 < 0.65 时提示）:
  - 扩 deep=True: Linear(21, 8) -> ReLU -> Linear(8, 3) = 203 参数
  - 解锁 Encoder 最后一层 GRU, lr=1e-4, +20 epoch

使用：
    python tools/train_fusion_head.py --exercise curl
    python tools/train_fusion_head.py --exercise squat --deep

数据布局（data_root 默认 data/v42）：
    data_root/
      user_01/curl/standard/rep_000.csv
      user_01/curl/compensation/rep_000.csv
      user_01/curl/bad_form/rep_000.csv
      user_02/...

每个 rep_*.csv 11 列（plan §2.2/§2.3）:
    timestamp, Angle, Ang_Vel, Ang_Accel, Phase_Progress,
    Target_RMS_Norm, Comp_RMS_Norm, MDF, MNF, ZCR, Raw_Unfilt
  可选：Torso_Tilt（深蹲才有，弯举没有则填 0）

注意：
- 开发机脚本，可用 pandas / sklearn；板端 import 的 dual_branch_fusion.py 保持 3.7 纯净
- 数据缺失时降级为 2-fold 或跳过 bad 标签，smoke 必须通过
"""
import argparse
import glob
import json
import os
import sys
import warnings
from collections import Counter

import numpy as np

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False

try:
    from sklearn.metrics import f1_score, confusion_matrix
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hardware_engine.cognitive.dual_branch_fusion import (  # noqa: E402
    DualBranchFusionModel,
    FusionHead,
    HandCraftedFeatureExtractor,
    VISION_INPUT_DIM,
    EMG_INPUT_DIM,
    VISION_SEQ_LEN,
    EMG_SEQ_LEN,
    EMB_DIM,
    HAND_CRAFTED_DIM,
    FUSION_INPUT_DIM,
    NUM_CLASSES,
    LABEL_NAMES,
)

LABEL_MAP = {'standard': 0, 'compensation': 1, 'bad_form': 2}


# ===========================================================================
# Rep loading / tensor prep
# ===========================================================================
def _resample_series(arr, target_len):
    """把任意长度 1D 数组线性插值到 target_len。"""
    arr = np.asarray(arr, dtype=np.float32).ravel()
    if arr.size == 0:
        return np.zeros(target_len, dtype=np.float32)
    if arr.size == target_len:
        return arr
    src_x = np.linspace(0.0, 1.0, arr.size)
    dst_x = np.linspace(0.0, 1.0, target_len)
    return np.interp(dst_x, src_x, arr).astype(np.float32)


def _resample_matrix(mat, target_len):
    """把 (T, D) 线性插值到 (target_len, D)。"""
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat.reshape(-1, 1)
    T, D = mat.shape
    if T == target_len:
        return mat
    out = np.zeros((target_len, D), dtype=np.float32)
    for d in range(D):
        out[:, d] = _resample_series(mat[:, d], target_len)
    return out


def load_user_reps(data_root, user, exercise):
    """扫描 data_root/user/exercise/{standard,compensation,bad_form}/rep_*.csv。

    返回 list[dict]，每 dict 含：
      - vision_seq: np.ndarray (VISION_SEQ_LEN, VISION_INPUT_DIM)
      - emg_seq:    np.ndarray (EMG_SEQ_LEN, EMG_INPUT_DIM)
      - rep_data:   dict（喂给 HandCraftedFeatureExtractor.extract）
      - label:      int (0/1/2)
      - meta:       dict (user, exercise, class, path)
    """
    if not _HAS_PANDAS:
        raise RuntimeError("pandas required on dev machine")

    reps = []
    user_dir = os.path.join(data_root, user, exercise)
    if not os.path.isdir(user_dir):
        return reps

    for cls_name, cls_id in LABEL_MAP.items():
        cls_dir = os.path.join(user_dir, cls_name)
        if not os.path.isdir(cls_dir):
            continue
        csv_paths = sorted(glob.glob(os.path.join(cls_dir, 'rep_*.csv')))
        for p in csv_paths:
            try:
                df = pd.read_csv(p)
            except Exception as ex:
                warnings.warn("skip unreadable %s (%s)" % (p, ex))
                continue

            # --- 取 vision 4 列 ---
            v_cols = ['Angle', 'Ang_Vel', 'Ang_Accel', 'Phase_Progress']
            missing_v = [c for c in v_cols if c not in df.columns]
            if missing_v:
                warnings.warn("skip %s missing vision cols %s" % (p, missing_v))
                continue
            v_mat = df[v_cols].to_numpy(dtype=np.float32)
            vision_seq = _resample_matrix(v_mat, VISION_SEQ_LEN)

            # --- 取 EMG 7 列 ---
            e_cols = ['Target_RMS_Norm', 'Comp_RMS_Norm',
                      'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt']
            # Target/Comp_Ratio 派生
            missing_e = [c for c in e_cols if c not in df.columns]
            if missing_e:
                warnings.warn("skip %s missing emg cols %s" % (p, missing_e))
                continue
            target_rms = df['Target_RMS_Norm'].to_numpy(dtype=np.float32)
            comp_rms = df['Comp_RMS_Norm'].to_numpy(dtype=np.float32)
            ratio = target_rms / (comp_rms + 1e-6)
            e_mat = np.stack([
                target_rms,
                comp_rms,
                ratio,
                df['Target_MDF'].to_numpy(dtype=np.float32),
                df['Target_MNF'].to_numpy(dtype=np.float32),
                df['Target_ZCR'].to_numpy(dtype=np.float32),
                df['Target_Raw_Unfilt'].to_numpy(dtype=np.float32),
            ], axis=1)  # (T, 7)
            emg_seq = _resample_matrix(e_mat, EMG_SEQ_LEN)

            # --- 手工标量源 series ---
            torso = df['Torso_Tilt'].to_numpy(dtype=np.float32) if 'Torso_Tilt' in df.columns \
                else np.zeros(len(df), dtype=np.float32)
            rep_data = {
                'emg_target_series': target_rms,
                'emg_comp_series': comp_rms,
                'angle_series': df['Angle'].to_numpy(dtype=np.float32),
                'phase_progress': df['Phase_Progress'].to_numpy(dtype=np.float32),
                'ang_accel_series': df['Ang_Accel'].to_numpy(dtype=np.float32),
                'torso_tilt_series': torso,
            }

            reps.append({
                'vision_seq': vision_seq,
                'emg_seq': emg_seq,
                'rep_data': rep_data,
                'label': cls_id,
                'meta': {
                    'user': user,
                    'exercise': exercise,
                    'class': cls_name,
                    'path': p,
                },
            })
    return reps


# ===========================================================================
# 21d feature extraction via (frozen) encoders
# ===========================================================================
def extract_21d_feature(model, rep, device):
    """model 中 encoder 已冻结；单 rep -> (21,) tensor（在 device 上）+ cos_sim float。"""
    vision = torch.from_numpy(rep['vision_seq']).unsqueeze(0).to(device)  # (1, 30, 4)
    emg = torch.from_numpy(rep['emg_seq']).unsqueeze(0).to(device)        # (1, 200, 7)
    with torch.no_grad():
        v_emb = model.vision_encoder(vision).squeeze(0)  # (8,)
        e_emb = model.emg_encoder(emg).squeeze(0)        # (8,)
    handcrafted = HandCraftedFeatureExtractor.extract(
        rep['rep_data'],
        v_emb.cpu(),
        e_emb.cpu(),
    ).to(device)  # (5,)
    feat = torch.cat([v_emb, e_emb, handcrafted], dim=0)  # (21,)
    cos_sim = F.cosine_similarity(v_emb.unsqueeze(0), e_emb.unsqueeze(0), dim=1).item()
    return feat, cos_sim


def extract_all_features(model, reps, device):
    """对所有 rep 抽取 21d 特征。返回 (X (N,21) tensor, y (N,) long tensor, cos_sims list)。"""
    if not reps:
        return (torch.zeros((0, FUSION_INPUT_DIM)),
                torch.zeros((0,), dtype=torch.long),
                [])
    feats = []
    labels = []
    cos_sims = []
    for rep in reps:
        f, cs = extract_21d_feature(model, rep, device)
        feats.append(f)
        labels.append(rep['label'])
        cos_sims.append(cs)
    X = torch.stack(feats, dim=0).to(device)
    y = torch.tensor(labels, dtype=torch.long, device=device)
    return X, y, cos_sims


# ===========================================================================
# Train one LOSO fold
# ===========================================================================
def _macro_f1(y_true, y_pred):
    if not _HAS_SKLEARN:
        # 极简 fallback：仅 smoke 用
        correct = float((y_true == y_pred).sum())
        return correct / max(1, len(y_true))
    return f1_score(y_true, y_pred, labels=list(range(NUM_CLASSES)),
                    average='macro', zero_division=0)


def train_one_fold(model, train_X, train_y, val_X, val_y, args, fold_idx, n_folds, device):
    """返回 (best_val_f1, best_state_dict, best_train_f1, val_preds (np.ndarray))。"""
    # 重建 fusion head（每折独立权重）
    head = FusionHead(deep=args.deep, dropout=args.dropout).to(device)
    model.fusion_head = head

    # 冻结 encoder
    for p in model.vision_encoder.parameters():
        p.requires_grad = False
    for p in model.emg_encoder.parameters():
        p.requires_grad = False

    trainable = [p for p in head.parameters() if p.requires_grad]
    optim = torch.optim.Adam(trainable, lr=args.lr, weight_decay=args.weight_decay)
    loss_fn = nn.CrossEntropyLoss()

    best_val_f1 = -1.0
    best_train_f1 = 0.0
    best_state = None
    best_val_preds = None
    patience_left = args.patience

    n_train = train_X.shape[0]
    for epoch in range(1, args.epochs + 1):
        head.train()
        perm = torch.randperm(n_train, device=device)
        total_loss = 0.0
        for i in range(0, n_train, args.batch_size):
            idx = perm[i:i + args.batch_size]
            xb = train_X[idx]
            yb = train_y[idx]
            logits = head(xb)
            loss = loss_fn(logits, yb)
            optim.zero_grad()
            loss.backward()
            optim.step()
            total_loss += float(loss.item()) * xb.shape[0]

        # eval
        head.eval()
        with torch.no_grad():
            tr_pred = head(train_X).argmax(dim=1).cpu().numpy()
            va_pred = head(val_X).argmax(dim=1).cpu().numpy()
        tr_true = train_y.cpu().numpy()
        va_true = val_y.cpu().numpy()
        tr_f1 = _macro_f1(tr_true, tr_pred)
        va_f1 = _macro_f1(va_true, va_pred)
        gap = tr_f1 - va_f1
        print("[fold %d/%d][epoch %2d] loss=%.4f train_f1=%.3f val_f1=%.3f gap=%+.3f"
              % (fold_idx + 1, n_folds, epoch, total_loss / max(1, n_train),
                 tr_f1, va_f1, gap))

        # 过拟合硬闸门
        if gap > args.overfit_gap_threshold:
            warnings.warn("[fold %d] overfit gate triggered (gap=%.3f > %.3f) -> stop"
                          % (fold_idx + 1, gap, args.overfit_gap_threshold))
            break

        # early stop + best tracking
        if va_f1 > best_val_f1 + 1e-6:
            best_val_f1 = va_f1
            best_train_f1 = tr_f1
            best_state = {k: v.detach().cpu().clone() for k, v in head.state_dict().items()}
            best_val_preds = va_pred.copy()
            patience_left = args.patience
        else:
            patience_left -= 1
            if patience_left <= 0:
                print("[fold %d] early stop at epoch %d (best_val_f1=%.3f)"
                      % (fold_idx + 1, epoch, best_val_f1))
                break

    if best_state is None:
        # 一次都没成功 -> 用末状态兜底
        best_state = {k: v.detach().cpu().clone() for k, v in head.state_dict().items()}
        best_val_preds = va_pred if 'va_pred' in dir() else np.zeros((val_X.shape[0],), dtype=np.int64)
        best_val_f1 = max(best_val_f1, 0.0)

    return best_val_f1, best_state, best_train_f1, best_val_preds


# ===========================================================================
# Main orchestration
# ===========================================================================
def _select_device():
    if _HAS_TORCH and torch.cuda.is_available():
        return torch.device('cuda')
    return torch.device('cpu')


def _discover_users(data_root, exercise):
    """返回 data_root 下存在 exercise 子目录的 user 列表，按字典序。"""
    if not os.path.isdir(data_root):
        return []
    users = []
    for name in sorted(os.listdir(data_root)):
        p = os.path.join(data_root, name, exercise)
        if os.path.isdir(p):
            users.append(name)
    return users


def _drop_tiny_classes(reps, min_count=10):
    """若某 class 总量 < min_count，整体剔除 bad_form（最少见）并 warn。"""
    counter = Counter(r['label'] for r in reps)
    for cls_id, cnt in counter.items():
        if cnt < min_count:
            warnings.warn("class %s (id=%d) has only %d reps (<%d) -> dropping that class"
                          % (LABEL_NAMES[cls_id], cls_id, cnt, min_count))
            reps = [r for r in reps if r['label'] != cls_id]
    return reps


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exercise', choices=['curl', 'squat'], required=True)
    parser.add_argument('--data-root', default=os.path.join(ROOT, 'data/v42'))
    parser.add_argument('--weights-dir', default=os.path.join(ROOT, 'hardware_engine/cognitive/weights'))
    parser.add_argument('--encoder-dir', default=None,
                        help='预训练 encoder 目录（默认等同 --weights-dir）')
    parser.add_argument('--deep', action='store_true', help='Stage 3 欠拟合救援模式')
    parser.add_argument('--unfreeze-encoder', action='store_true', help='解锁 Encoder 最后一层 GRU')
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--weight-decay', type=float, default=1e-3)
    parser.add_argument('--dropout', type=float, default=0.3)
    parser.add_argument('--patience', type=int, default=5)
    parser.add_argument('--overfit-gap-threshold', type=float, default=0.15,
                        help='train_f1 - val_f1 > 此值 -> 立即回炉')
    parser.add_argument('--min-reps-per-class', type=int, default=10)
    args = parser.parse_args()

    if args.encoder_dir is None:
        args.encoder_dir = args.weights_dir

    print("=" * 64)
    print("[train_fusion_head] V4.2 LOSO 融合头训练")
    print("  exercise         = %s" % args.exercise)
    print("  data_root        = %s" % args.data_root)
    print("  weights_dir      = %s" % args.weights_dir)
    print("  deep_fusion      = %s" % args.deep)
    print("=" * 64)

    if not _HAS_TORCH:
        print("FATAL: PyTorch not installed")
        return 2
    if not _HAS_PANDAS:
        print("FATAL: pandas not installed (dev-machine only)")
        return 2

    device = _select_device()
    print("[train_fusion_head] device = %s" % device)

    # --- 1. 建模 + 加载 encoder ---
    os.makedirs(args.weights_dir, exist_ok=True)
    model = DualBranchFusionModel(deep_fusion=args.deep).to(device)
    loaded = model.load_pretrained_encoders(args.encoder_dir)
    if not loaded:
        print("\n" + "!" * 64)
        print("!!  WARN: 未加载预训练 Encoder，本次训练融合头仅为 smoke 验证，")
        print("!!        真跑前请先执行 pretrain_encoders.py")
        print("!" * 64 + "\n")
    print("[train_fusion_head] model params = %d (budget <= 800)" % model.param_count())

    # --- 2. 发现 users ---
    users = _discover_users(args.data_root, args.exercise)
    print("[train_fusion_head] discovered users: %s" % users)

    if len(users) == 0:
        warnings.warn("no users found under %s/<user>/%s — smoke mode: exit cleanly"
                      % (args.data_root, args.exercise))
        print("[train_fusion_head] nothing to train. Exit 0 (smoke).")
        return 0

    # 降级：<3 user -> 2-fold or LOO on 2 users
    n_folds = min(3, len(users))
    if n_folds < 3:
        warnings.warn("only %d user(s) available, degrading to %d-fold LOSO"
                      % (len(users), n_folds))

    # --- 3. 预加载每个 user 的所有 rep ---
    user_reps = {}
    for u in users:
        reps = load_user_reps(args.data_root, u, args.exercise)
        if not reps:
            warnings.warn("user %s has no reps for %s" % (u, args.exercise))
            continue
        user_reps[u] = reps
        print("  [%s] loaded %d reps: %s" % (u, len(reps), Counter(r['label'] for r in reps)))

    users = [u for u in users if u in user_reps]
    if not users:
        print("[train_fusion_head] no usable reps. Exit 0 (smoke).")
        return 0

    n_folds = min(n_folds, len(users))

    # --- 4. LOSO 循环 ---
    fold_results = []
    all_train_cos_sims = []  # 汇聚用于计算 5 百分位阈值
    fold_conf_mats = []

    for fold_idx in range(n_folds):
        val_user = users[fold_idx]
        train_users = [u for u in users if u != val_user]

        train_reps = []
        for u in train_users:
            train_reps.extend(user_reps[u])
        val_reps = list(user_reps[val_user])

        # 降级：class 太少
        train_reps = _drop_tiny_classes(train_reps, min_count=args.min_reps_per_class)
        if not train_reps:
            warnings.warn("fold %d: no train reps after class filter, skip" % (fold_idx + 1))
            continue

        print("\n--- Fold %d/%d ---" % (fold_idx + 1, n_folds))
        print("  val_user    = %s  (%d reps)" % (val_user, len(val_reps)))
        print("  train_users = %s (%d reps)" % (train_users, len(train_reps)))

        train_X, train_y, train_cs = extract_all_features(model, train_reps, device)
        val_X, val_y, _ = extract_all_features(model, val_reps, device)
        all_train_cos_sims.extend(train_cs)

        if train_X.shape[0] == 0 or val_X.shape[0] == 0:
            warnings.warn("fold %d: empty tensors, skip" % (fold_idx + 1))
            continue

        best_val_f1, best_state, best_train_f1, val_preds = train_one_fold(
            model, train_X, train_y, val_X, val_y, args, fold_idx, n_folds, device
        )

        # 混淆矩阵（仅评估用）
        if _HAS_SKLEARN:
            cm = confusion_matrix(val_y.cpu().numpy(), val_preds,
                                  labels=list(range(NUM_CLASSES)))
        else:
            cm = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
        fold_conf_mats.append(cm)

        fold_results.append({
            'fold': fold_idx + 1,
            'val_user': val_user,
            'n_train': int(train_X.shape[0]),
            'n_val': int(val_X.shape[0]),
            'train_f1': float(best_train_f1),
            'val_f1': float(best_val_f1),
            'best_state': best_state,
            'confusion_matrix': cm.tolist(),
        })
        print("  fold %d best: train_f1=%.3f val_f1=%.3f" %
              (fold_idx + 1, best_train_f1, best_val_f1))

    if not fold_results:
        print("[train_fusion_head] no successful fold. Exit.")
        return 1

    # --- 5. 汇总 ---
    avg_val_f1 = float(np.mean([r['val_f1'] for r in fold_results]))
    avg_train_f1 = float(np.mean([r['train_f1'] for r in fold_results]))
    total_cm = np.sum(np.stack(fold_conf_mats, axis=0), axis=0) if fold_conf_mats \
        else np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)

    print("\n" + "=" * 64)
    print("[train_fusion_head] LOSO 汇总 (%d folds)" % len(fold_results))
    print("  avg train_f1 = %.3f" % avg_train_f1)
    print("  avg val_f1   = %.3f" % avg_val_f1)
    print("  confusion matrix (sum across folds, rows=true, cols=pred):")
    print("                  " + "  ".join("%10s" % n for n in LABEL_NAMES))
    for i, row in enumerate(total_cm):
        print("    %-12s" % LABEL_NAMES[i] + "  ".join("%10d" % v for v in row))
    print("=" * 64)

    if avg_val_f1 < 0.65:
        print("\n[train_fusion_head] avg_val_f1=%.3f < 0.65 -> 建议：python tools/train_fusion_head.py "
              "--exercise %s --deep  （Stage 3 欠拟合救援）\n" % (avg_val_f1, args.exercise))

    # --- 6. 保存最佳折 state（val_f1 最高）+ metrics ---
    best = max(fold_results, key=lambda r: r['val_f1'])
    head_path = os.path.join(args.weights_dir, 'v42_fusion_head_%s.pt' % args.exercise)
    torch.save(best['best_state'], head_path)

    # cos_sim 5 百分位（低 5% 视作模态不一致 / 电极脱落）
    if all_train_cos_sims:
        cos_sim_p5 = float(np.percentile(np.asarray(all_train_cos_sims), 5))
    else:
        cos_sim_p5 = 0.0

    metrics = {
        'exercise': args.exercise,
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
            }
            for r in fold_results
        ],
        'confusion_matrix_total': total_cm.tolist(),
        'cos_sim_p5_threshold': cos_sim_p5,
        'label_names': LABEL_NAMES,
        'best_fold_val_user': best['val_user'],
    }
    metrics_path = os.path.join(args.weights_dir, 'v42_fusion_head_%s_metrics.json' % args.exercise)
    with open(metrics_path, 'w', encoding='utf-8') as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    print("[train_fusion_head] saved head -> %s" % head_path)
    print("[train_fusion_head] saved metrics -> %s" % metrics_path)
    print("[train_fusion_head] cos_sim 5%% threshold (for runtime anomaly) = %.4f" % cos_sim_p5)
    return 0


if __name__ == '__main__':
    sys.exit(main())
