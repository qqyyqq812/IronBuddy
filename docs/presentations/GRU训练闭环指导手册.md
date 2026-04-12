# IronBuddy GRU 训练闭环指导手册

> 单标签、分步骤、可复用的数据采集→训练→验证流程
> 最后更新: 2026-04-12

---

## 一、总览

每次只做一件事：**采集一种动作的一种质量标签**，验证数据合格后，再进入训练。

```
启动采集模式 → 录制数据 → 回传WSL → 验证质量 → 训练模型 → 部署测试
      ↑                                     |
      └──── 不合格则重新录制 ←──────────────┘
```

### 数据存放位置

```
板端:   ~/training_data/<运动>/<标签>/train_*.csv
WSL端:  ~/projects/embedded-fullstack/data/<运动>/<标签>/train_*.csv
```

示例:
```
~/training_data/squat/golden/train_squat_golden_20260412_150000.csv
~/training_data/squat/lazy/train_squat_lazy_20260412_151000.csv
~/training_data/bicep_curl/golden/train_bicep_curl_golden_20260412_152000.csv
```

### 采集量要求

| 指标 | 最低要求 | 推荐 |
|------|---------|------|
| 每组时长 | 30秒 | **60-90秒** |
| 每组帧数 | 600帧 | 1200-1800帧 |
| 完整动作次数 | 10次 | **20-30次** |
| 每种标签至少 | 1个文件 | 2-3个文件 |

> 20Hz采样，60秒 = 1200帧。做深蹲每次约3秒，60秒可做20个。

---

## 二、操作流程（以标准深蹲为例）

### 步骤1: 启动采集模式

```bash
# 在 WSL 终端执行（不是板端）
cd ~/projects/embedded-fullstack
tclsh start_collect.tcl
```

这会启动精简模式：只有摄像头推理+网页+EMG，**不启动语音和DeepSeek**，省 CPU。

启动后打开 http://10.105.245.224:5000/ 确认画面正常。

### 步骤2: 录制数据

```bash
# 在 WSL 终端执行
bash collect_one.sh squat golden 60
```

参数说明:
- `squat` — 运动类型（squat 或 bicep_curl）
- `golden` — 标签（golden=标准, lazy=偷懒, bad=错误）
- `60` — 录制秒数

脚本会自动:
1. SSH 到板端创建目录
2. 运行 60 秒自动采集
3. 将 CSV 回传到 WSL 的 `data/squat/golden/`
4. 运行数据验证
5. 打印结果

**录制时你要做的：** 站在摄像头前做标准深蹲，60秒内尽量做20个以上。

### 步骤3: 确认数据质量

采集脚本会自动验证，但你也可以用面板查看:

```bash
streamlit run tools/dashboard.py
# → 标签页1「数据探索」→ 指向 data/squat/golden/
```

检查:
- [ ] 帧数 ≥ 600
- [ ] 角度范围 ≥ 40度（深蹲应有 180°→90° 的变化）
- [ ] 时间序列波形有明显的上下周期

**不合格就重新录:** `bash collect_one.sh squat golden 60`

### 步骤4: 训练模型

```bash
cd ~/projects/embedded-fullstack

# 只用 golden 数据训练（先验证流程通了）
python3 tools/train_model.py \
    --data ./data/squat/golden \
    --out ./models \
    --epochs 15
```

> 注意: 只有 golden 一个标签时，模型只能学到"标准动作长什么样"，
> 后续加入 lazy 和 bad 后才能区分三种。这一步是验证流程可行。

### 步骤5: 部署到板端

```bash
scp -i ~/.ssh/id_rsa_toybrick \
    models/extreme_fusion_gru.pt \
    toybrick@10.105.245.224:~/streamer_v3/hardware_engine/cognitive/
```

### 步骤6: 用正常模式验证

```bash
# 切换到完整模式（包含语音+DeepSeek）
tclsh start_validation.tcl
```

网页上会出现"动作相似度"指标。做标准深蹲时应该接近 100%。

### 步骤7: 停止采集模式

```bash
tclsh stop_collect.tcl
```

---

## 三、复用——录制其他标签

完成标准深蹲后，用完全相同的流程录其他标签:

```bash
# 偷懒深蹲（蹲一半就起来）
bash collect_one.sh squat lazy 60

# 错误深蹲（膝盖内扣/重心偏）
bash collect_one.sh squat bad 60

# 标准弯举
bash collect_one.sh bicep_curl golden 60

# 偷懒弯举
bash collect_one.sh bicep_curl lazy 60

# 错误弯举
bash collect_one.sh bicep_curl bad 60
```

每录完一个就能用现有数据训练:

```bash
# 用已有的所有数据训练
python3 tools/train_model.py --data ./data --out ./models --epochs 25
```

---

## 四、从模拟切换到真实传感器

**零代码修改。** 队友的 ESP32 连上后:

1. ESP32 固件设置 `TARGET_IP = "10.105.245.224"`, `PORT = 8080`
2. 启动 ESP32
3. 板端 `/dev/shm/emg_heartbeat` 文件自动出现
4. 模拟 EMG 自动让位，真实数据接管
5. 采集命令完全一样: `bash collect_one.sh squat golden 60`

验证: `ssh toybrick@10.105.245.224 "cat /dev/shm/muscle_activation.json"` 看数据是否在变化。

---

## 五、数据管理

### 目录结构
```
data/
├── squat/
│   ├── golden/       ← 标准深蹲 CSV
│   ├── lazy/         ← 偷懒深蹲 CSV
│   └── bad/          ← 错误深蹲 CSV
├── bicep_curl/
│   ├── golden/
│   ├── lazy/
│   └── bad/
└── models/           ← 训练输出
    ├── extreme_fusion_gru.pt
    └── tb_logs/      ← TensorBoard 日志
```

### 命名规则
`train_<运动>_<标签>_<日期>_<时间>.csv`

### 清理数据
不合格的 CSV 直接删除，不影响其他文件。

---

## 六、关键命令速查

| 操作 | 命令 |
|------|------|
| 启动采集模式 | `tclsh start_collect.tcl` |
| 停止采集模式 | `tclsh stop_collect.tcl` |
| 录制60秒标准深蹲 | `bash collect_one.sh squat golden 60` |
| 录制60秒偷懒深蹲 | `bash collect_one.sh squat lazy 60` |
| 录制60秒错误深蹲 | `bash collect_one.sh squat bad 60` |
| 手动验证数据 | `python3 tools/validate_data.py data/squat/golden/` |
| 训练(单标签) | `python3 tools/train_model.py --data ./data/squat/golden --out ./models --epochs 15` |
| 训练(全部数据) | `python3 tools/train_model.py --data ./data --out ./models --epochs 25` |
| 查看训练曲线 | `tensorboard --logdir models/tb_logs` |
| 可视化面板 | `streamlit run tools/dashboard.py` |
| 部署模型 | `scp -i ~/.ssh/id_rsa_toybrick models/extreme_fusion_gru.pt toybrick@10.105.245.224:~/streamer_v3/hardware_engine/cognitive/` |
| 启动完整模式 | `tclsh start_validation.tcl` |

---

## 七、7维特征说明

每帧 CSV 记录 7 个特征 + 1 个标签:

| 特征 | 含义 | 范围 | 深蹲典型值 |
|------|------|------|-----------|
| Ang_Vel | 角速度(度/帧) | [-20, 20] | 下蹲时负，起身时正 |
| Angle | 膝/肘角度(度) | [0, 180] | 站立180°，蹲到底~90° |
| Ang_Accel | 角加速度 | [-10, 10] | 变速时非零 |
| Target_RMS | 目标肌肉EMG | [0, 100] | 发力时高 |
| Comp_RMS | 代偿肌肉EMG | [0, 100] | 错误动作时高 |
| Symmetry | 左右对称性 | [0, 1] | 正常>0.9 |
| Phase | 动作阶段进度 | [0, 1] | 0=站立, 1=蹲底 |

---

## 八、训练参数参考

| 参数 | 默认值 | 建议调整 |
|------|--------|---------|
| epochs | 25 | 数据少时减到15 |
| batch | 64 | 数据少时减到32 |
| lr | 0.005 | 一般不需要动 |
| seq | 30 | 30帧=1.5秒窗口 |

### 训练脚本纯面向数据
`train_model.py` 只做一件事：读 CSV → 训练 GRU → 输出 .pt 模型文件。不连接板端，不启动任何服务。在 WSL 或云端 GPU 上运行。
