# FLEX_AQA_Dataset 接入指引

IronBuddy V4.3 · `flex-curl-only-pivot.md` §1.5 DA 范式配套

---

## 数据集简介

- **全名**：FLEX (Fitness Life EXercise) AQA Dataset
- **来源**：NeurIPS 2025，Yin 等，arXiv:2506.03198
- **仓库**：`HaoYin116/FLEX_AQA_Dataset`（仅含代码 2.9 MB，无 raw 数据）
- **规模**：38 志愿者 × 20 健身动作 (A01..A20) × ≥ 7500 rep
- **模态**：
  - **sEMG**：**4 通道 @ 200 Hz**（`EMG/A0X/<rep:03d>/EMG.csv` 无 header）
  - **视频**：5 视角（View-1 到 View-5）
  - **骨架**：21 关键点 × (x,y,z) per 帧（`Skeleton/A0X/<rep:03d>/skeleton_points.csv`）
  - **打分**：FKG（Fitness Knowledge Graph）连续质量分 0-100，存于 `Split_4/split_4_{train,test}_list.mat`（key `consolidated_train_list` / `consolidated_test_list`，每行 `[class, rep, final_score, ...]`）

---

## 申请流程（24-72h 审批）

1. 访问：[Google Form](https://docs.google.com/forms/d/e/1FAIpQLSdVWgFO3XSlkvKnaLIEaCkedJ-QDb4wclVgE2LhLQ4nGPBmRQ/viewform)
2. 用途建议填写：
   > Academic research on sEMG-based fitness coaching at edge devices, ZJU embedded systems competition.
3. 邮箱、机构字段据实填写。审批后会收到 Google Drive 或 OneDrive 链接。
4. 解压后得目录结构（以下简称 `<FLEX_ROOT>`）：
   ```text
   <FLEX_ROOT>/
   ├── EMG/A07/001/EMG.csv
   ├── EMG/A07/001/...
   ├── Skeleton/A07/001/skeleton_points.csv
   ├── ...
   └── Split_4/
       ├── split_4_train_list.mat
       └── split_4_test_list.mat
   ```

**论文引用**：
```bibtex
@article{yin2025flex,
  title={FLEX: A Benchmark for Evaluating Code Agents on Fitness Life EXercise Quality Assessment},
  author={Yin, Hao and others},
  journal={arXiv preprint arXiv:2506.03198},
  year={2025}
}
```

---

## 类别 ID 映射速查（plan §1.2 推断）

| 类别 id | SevenPair 分组（`SevenPair.py:84-99`） | 通道读法 | 弯举可用性 |
|---------|--------------------------------------|---------|-----------|
| A01 A02 A04 A05 A14 A16 | `class in [1,2,4,5,14,16]` | `col[0]` L_main / `col[2]` R_main | 否（左右双臂） |
| A03 A06 A15 A20 | `class in [3,6,15,20]` | `col[1]` / `col[3]` | 否（辅助肌群） |
| **A07 A17 A18 A19** | `class in [7,17,18,19]` **Single** | **`col[0]` L_main / `col[1]` L_sub** | **✅ 单边上肢主力——弯举候选** |
| A08 A09 A10 A11 A12 A13 | `class in [8..13]` Average | 双侧平均 | 否 |

> **本项目默认 `--curl-class-ids 7,17,18,19`**。真正的弯举 id 需论文 supplementary 或邮件作者确认后回填。

### 通道分组规则（直接摘自 `SevenPair.load_EMG`）

```python
# Single 组（A07/A17/A18/A19）：只读单边
L_muscle = max(abs(raw_data.iloc[:, 0]))  # col[0] = L_main
R_muscle = max(abs(raw_data.iloc[:, 1]))  # col[1] = L_sub
```

故弯举默认：`FLEX_CH_TARGET = 0`（biceps brachii），`FLEX_CH_COMP = 1`（forearm flexor）。
置信度验证：`python tools/flex_preprocess.py --validate-channels` 会抽前 5 rep 算 `RMS(col) vs elbow_angle` 的 Pearson |r|，挑最高的 4 组合之一。

---

## score → 三类阈值化规则（plan §1.3）

| score 范围 | label id | 类名 |
|-----------|----------|------|
| `≥ 80` | 0 | standard |
| `50 - 79` | 1 | compensation |
| `< 50` | 2 | bad_form |

阈值依据：SENIAM 经验「standard ≥ 80% MVC efficiency」+ FLEX `score_range=100` 满分制。

**调整建议**：
- 若脚本启动提示某类 <10%（严重不均衡）→ `--thresholds 75,45` 或 `--thresholds 85,55`
- 也可关注 `tools/flex_preprocess.py` 尾部打印的三类占比：理想 ~33/33/33。

---

## 标准使用流程

```bash
cd /home/qq/projects/embedded-fullstack

# 1. 申请 + 下载 + 解压到 <FLEX_ROOT>
#    （审批 24-72h，见上面 Google Form 链接）

# 2. 先跑通道验证（5 分钟），挑最高相关性的通道组合
python tools/flex_preprocess.py \
    --flex-root <FLEX_ROOT> \
    --validate-channels
#    → data/flex/_channel_mapping.json 写入决策

# 3. 全量预处理（几分钟～几十分钟，视 rep 数）
python tools/flex_preprocess.py \
    --flex-root <FLEX_ROOT> \
    --out data/flex \
    --thresholds 80,50 \
    --curl-class-ids 7,17,18,19
#    → data/flex/curl/{standard,compensation,bad_form}/rep_NNNN.csv

# 4. FLEX base pretrain（30 epoch ~ 10-30 分钟 CPU / <5 分钟 GPU）
python tools/pretrain_encoders.py --source flex --epochs 30
#    → weights/vision_encoder_flex_pretrained.pt
#    → weights/emg_encoder_flex_pretrained.pt

# 5. 本地 DA 微调（LOSO 3 折）
python tools/finetune_with_local.py \
    --pretrained-encoder hardware_engine/cognitive/weights/emg_encoder_flex_pretrained.pt \
    --freeze-base \
    --epochs 30 \
    --output-name v42_fusion_head_curl_da
#    → weights/v42_fusion_head_curl_da.pt + _metrics.json
```

---

## FLEX 未到位时的 fallback

回退 V4.2 本地主路径（已验证可跑）：

```bash
python tools/pretrain_encoders.py --source local --exercise curl --epochs 30
python tools/train_fusion_head.py  --exercise curl --epochs 50
```

---

## 脚本自检

不需要 FLEX 真数据也能验证流程：

```bash
python tools/flex_preprocess.py --mock
# → data/flex/curl/{standard,compensation,bad_form}/rep_NNNN.csv  共 30 fake reps
# → 13 列完全对齐 V4.2 contract
```

---

## 相关文件

- `tools/flex_preprocess.py` — raw → 13 列 rep CSV
- `tools/pretrain_encoders.py --source flex` — FLEX masked AE 预训练
- `tools/finetune_with_local.py` — FLEX encoder → 本地 LOSO DA 微调
- `.claude/plans/flex-curl-only-pivot.md` §1.5 — DA 范式决策链
