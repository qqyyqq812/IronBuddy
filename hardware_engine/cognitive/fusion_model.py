import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
import numpy as np

# ---------------------------------------------------------------------------
# Feature constants
# ---------------------------------------------------------------------------
FEATURES_4D = ['Ang_Vel', 'Angle', 'Target_RMS', 'Comp_RMS']
FEATURES_7D = ['Ang_Vel', 'Angle', 'Ang_Accel', 'Target_RMS', 'Comp_RMS',
               'Symmetry_Score', 'Phase_Progress']

CLASS_NAMES = ['standard', 'compensating', 'non_standard']
CLASS_GOLDEN = 0
CLASS_LAZY   = 1
CLASS_BAD    = 2

PHASE_LABELS = ['standing', 'descending', 'bottom', 'ascending']


class CompensationGRU(nn.Module):
    """
    Agent 3 升级版 GRU — 同时输出：
      1. 相似度分 (0-1, 越接近黄金标准越高)
      2. 3 类别分类 (standard / compensating / non_standard)

    向后兼容：input_size=4 时接受旧格式数据。
    模型大小保持 < 100 KB，推理 < 5 ms。
    """

    def __init__(self, input_size: int = 7, hidden_size: int = 16, num_layers: int = 1):
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size

        # 如果输入是旧 4D 格式，先用线性层升维到内部维度
        self._needs_proj = (input_size != 7)
        if self._needs_proj:
            self.input_proj = nn.Linear(input_size, 7)

        self.gru = nn.GRU(7, hidden_size, num_layers, batch_first=True)

        # 黄金标准嵌入 — 与 GRU 隐状态做余弦相似度
        self.golden_embed = nn.Parameter(torch.randn(hidden_size))

        # 相似度校准头：把余弦值映射到 [0, 1]（加一层非线性 + bias 增强表达）
        self.sim_head = nn.Sequential(
            nn.Linear(hidden_size + 1, 8),
            nn.ReLU(),
            nn.Linear(8, 1),
            nn.Sigmoid(),
        )

        # 3-class 分类头
        self.cls_head = nn.Linear(hidden_size, 3)

        # 运动阶段估计头（4 类：standing / descending / bottom / ascending）
        self.phase_head = nn.Linear(hidden_size, 4)

    # ------------------------------------------------------------------
    def forward(self, x: torch.Tensor):
        """
        x: (batch, seq_len, input_size)

        Returns:
            similarity  : (batch, 1)  — 0..1
            cls_logits  : (batch, 3)  — raw logits for 3 classes
            phase_logits: (batch, 4)  — raw logits for 4 phases
        """
        if self._needs_proj:
            x = self.input_proj(x)

        out, _ = self.gru(x)
        h = out[:, -1, :]                          # (batch, hidden_size)

        # --- 余弦相似度 ---
        h_norm     = F.normalize(h, dim=-1)        # (batch, H)
        g_norm     = F.normalize(self.golden_embed.unsqueeze(0), dim=-1)  # (1, H)
        cos_sim    = (h_norm * g_norm).sum(dim=-1, keepdim=True)          # (batch, 1)

        # 拼接隐状态 + 余弦值，输入校准头
        sim_input  = torch.cat([h, cos_sim], dim=-1)   # (batch, H+1)
        similarity = self.sim_head(sim_input)           # (batch, 1)

        cls_logits   = self.cls_head(h)    # (batch, 3)
        phase_logits = self.phase_head(h)  # (batch, 4)

        return similarity, cls_logits, phase_logits

    # ------------------------------------------------------------------
    @torch.no_grad()
    def infer(self, window: np.ndarray) -> dict:
        """
        单次推理接口，供主循环调用。

        window: numpy array, shape (seq_len, feature_dim)
                feature_dim 可以是 4（向后兼容）或 7
        Returns:
            {
                "similarity"     : float  0-1
                "classification" : str    "standard" | "compensating" | "non_standard"
                "confidence"     : float  0-1
                "phase"          : str    "standing" | "descending" | "bottom" | "ascending"
            }
        """
        self.eval()
        t = torch.tensor(window, dtype=torch.float32).unsqueeze(0)  # (1, seq, feat)

        sim, cls_logits, phase_logits = self.forward(t)

        similarity   = float(sim[0, 0].item())
        cls_probs    = torch.softmax(cls_logits[0], dim=0)
        cls_idx      = int(cls_probs.argmax().item())
        confidence   = float(cls_probs[cls_idx].item())
        classification = CLASS_NAMES[cls_idx]

        phase_idx = int(phase_logits[0].argmax().item())
        phase     = PHASE_LABELS[phase_idx]

        return {
            "similarity"    : round(similarity, 4),
            "classification": classification,
            "confidence"    : round(confidence, 4),
            "phase"         : phase,
        }


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def _compute_derived_features(features_4d: np.ndarray) -> np.ndarray:
    """
    4D 数组 → 7D 数组。
    columns: Ang_Vel, Angle, Ang_Accel, Target_RMS, Comp_RMS, Symmetry_Score, Phase_Progress
    """
    n = len(features_4d)
    ang_vel  = features_4d[:, 0]
    angle    = features_4d[:, 1]
    t_rms    = features_4d[:, 2]
    c_rms    = features_4d[:, 3]

    # Ang_Accel: 角速度的一阶差分（首帧补 0）
    ang_accel = np.zeros(n, dtype=np.float32)
    ang_accel[1:] = ang_vel[1:] - ang_vel[:-1]

    # Symmetry_Score: 占位 1.0（左右对称，数据来源于单侧 EMG 比值）
    symmetry = np.ones(n, dtype=np.float32)

    # Phase_Progress: 用角度归一化估计 rep 进度
    #   angle 越小 → 越接近深蹲底部(1.0)；angle 越大 → 越接近站立(0.0)
    a_min = angle.min() if angle.min() < angle.max() else 0.0
    a_max = angle.max() if angle.max() > a_min else 180.0
    phase_prog = np.clip(1.0 - (angle - a_min) / max(a_max - a_min, 1e-6), 0.0, 1.0).astype(np.float32)

    out = np.stack([ang_vel, angle, ang_accel, t_rms, c_rms, symmetry, phase_prog], axis=1)
    return out.astype(np.float32)


class SquatDataset(Dataset):
    """
    滑动窗口数据集。

    data_list: [(df, label), ...]
        label: 0=golden, 1=lazy, 2=bad
    支持 4 列（Ang_Vel, Angle, Target_RMS, Comp_RMS）和
         7 列（含 Ang_Accel, Symmetry_Score, Phase_Progress）的 CSV。
    """

    def __init__(self, data_list: list, seq_len: int = 30):
        self.seq_len = seq_len
        self.samples: list[np.ndarray] = []
        self.labels:  list[int]        = []

        for df, label in data_list:
            has_7d = all(c in df.columns for c in FEATURES_7D)

            if has_7d:
                features = df[FEATURES_7D].values.astype(np.float32)
            else:
                # 4D CSV — 旧格式，计算派生特征
                features_4d = df[FEATURES_4D].values.astype(np.float32)
                features = _compute_derived_features(features_4d)

            # 归一化
            features[:, 1] = features[:, 1] / 180.0     # Angle
            features[:, 3] = features[:, 3] / 100.0     # Target_RMS
            features[:, 4] = features[:, 4] / 100.0     # Comp_RMS
            features[:, 2] = np.clip(features[:, 2] / 10.0, -1.0, 1.0)   # Ang_Accel

            for i in range(len(features) - seq_len):
                self.samples.append(features[i : i + seq_len])
                self.labels.append(label)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int):
        x = torch.tensor(self.samples[idx], dtype=torch.float32)
        y = self.labels[idx]
        return x, y


# ---------------------------------------------------------------------------
# Training helper
# ---------------------------------------------------------------------------

def train_fusion_network(csv_dir: str = ".", epochs: int = 20) -> None:
    print("\n" + "="*50)
    print("Agent 3: 启动升级版 GRU 相似度评分训练阵列")
    print("="*50)

    label_map = {
        "train_squat_golden.csv": CLASS_GOLDEN,
        "train_squat_lazy.csv":   CLASS_LAZY,
        "train_squat_bad.csv":    CLASS_BAD,
    }

    data_list = []
    for fname, label in label_map.items():
        path = os.path.join(csv_dir, fname)
        if os.path.exists(path):
            import csv
            rows = []
            with open(path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append({k: float(v) for k, v in row.items()})
            if rows:
                class _SimpleDF:
                    """Minimal pandas-like DataFrame for CSV data."""
                    def __init__(self, rows):
                        self._rows = rows
                        self.columns = list(rows[0].keys()) if rows else []
                    def __len__(self):
                        return len(self._rows)
                    def __getitem__(self, key):
                        if isinstance(key, list):
                            # Multi-column select: return new _SimpleDF-like with .values
                            class _Cols:
                                def __init__(s, rows, cols):
                                    s._rows, s._cols = rows, cols
                                @property
                                def values(s):
                                    return np.array([[r[c] for c in s._cols] for r in s._rows])
                            return _Cols(self._rows, key)
                        return [r[key] for r in self._rows]
                df = _SimpleDF(rows)
                data_list.append((df, label))
                print(f"  Loaded {fname}: {len(df)} frames, label={label} ({CLASS_NAMES[label]})")

    if not data_list:
        print("No CSV files found. Run data collection first.")
        return

    dataset   = SquatDataset(data_list, seq_len=30)
    if len(dataset) == 0:
        print("Dataset is empty after windowing.")
        return

    dataloader = DataLoader(dataset, batch_size=64, shuffle=True)
    model      = CompensationGRU(input_size=7)

    # 相似度回归用 MSE；分类用交叉熵；多目标加权求和
    cls_criterion  = nn.CrossEntropyLoss()
    optimizer      = optim.Adam(model.parameters(), lr=0.005)
    scheduler      = optim.lr_scheduler.StepLR(optimizer, step_size=8, gamma=0.5)

    print(f"\nTraining {epochs} epochs on {len(dataset)} windows...")

    for epoch in range(epochs):
        model.train()
        total_loss = 0.0
        correct    = 0

        for x, y_cls in dataloader:
            optimizer.zero_grad()

            # 相似度目标：golden=1.0, lazy=0.5, bad=0.2
            sim_targets = torch.where(
                y_cls == CLASS_GOLDEN,
                torch.ones_like(y_cls, dtype=torch.float32),
                torch.where(y_cls == CLASS_LAZY,
                            torch.full_like(y_cls, 0.5, dtype=torch.float32),
                            torch.full_like(y_cls, 0.2, dtype=torch.float32))
            ).unsqueeze(1)

            sim_pred, cls_logits, _ = model(x)

            loss_sim = F.mse_loss(sim_pred, sim_targets)
            loss_cls = cls_criterion(cls_logits, y_cls)
            loss     = 0.4 * loss_sim + 0.6 * loss_cls

            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            predicted   = cls_logits.argmax(dim=1)
            correct    += (predicted == y_cls).sum().item()

        scheduler.step()
        acc = correct / len(dataset)
        print(f"  Epoch {epoch+1:02d}/{epochs} | Loss: {total_loss/len(dataloader):.4f} | Acc: {acc*100:.1f}%")

    out_path = os.path.join(csv_dir, "extreme_fusion_gru.pt")
    torch.save(model.state_dict(), out_path)
    print(f"\nModel saved: {out_path}")
    print(f"Model size : {os.path.getsize(out_path) / 1024:.1f} KB")


# ---------------------------------------------------------------------------
# Loader utility
# ---------------------------------------------------------------------------

def load_model(model_path: str, input_size: int = 7) -> CompensationGRU:
    """Load a saved model for inference."""
    model = CompensationGRU(input_size=input_size)
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    return model


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    working_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    train_fusion_network(working_dir)
