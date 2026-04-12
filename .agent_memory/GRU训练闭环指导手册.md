# IronBuddy GRU 训练闭环指导手册

> 从数据采集到模型部署的完整操作手册
> 最后更新: 2026-04-12

---

## 整体数据流

```
[板端摄像头]          [ESP32传感器]
    |                      |
    v                      v
cloud_rtmpose_client   udp_emg_server.py
    |                      |
    v                      v
/dev/shm/pose_data.json  /dev/shm/muscle_activation.json
    |                      |
    +----------+-----------+
               |
               v
  collect_training_data.py   <--- 你在板端运行的采集脚本
               |
               v
  train_squat_golden_*.csv   <--- 7维特征 + 标签
               |
               v
     validate_data.py        <--- 数据质量检查
               |
               v
      train_model.py         <--- GRU训练 (WSL/云端GPU)
               |
               v
   extreme_fusion_gru.pt     <--- 模型文件 (~6KB)
               |
               v
    main_claw_loop.py        <--- 板端实时推理 (加载模型)
               |
               v
  /dev/shm/fsm_state.json   <--- 推理结果写入前端
               |
               v
     前端网页 + 面板          <--- 实时展示
```

---

## 阶段一：数据采集

### 1.1 模拟数据采集（无传感器）

**现在就能做。** 系统会自动从骨架角度生成模拟EMG。

```bash
# 在板端终端执行
ssh toybrick@10.105.245.224

# 确保系统在运行（从WSL启动）
# WSL端: bash start_validation.sh

# 交互式采集（推荐，有键盘控制）
cd ~/streamer_v3
python3 tools/collect_training_data.py --exercise squat --mode golden --out ~/training_data

# 或自动模式（60秒无人值守）
python3 tools/collect_training_data.py --exercise squat --mode golden --out ~/training_data --auto 60
```

**6组数据清单（一键批处理）:**
```bash
cd ~/streamer_v3/tools && bash batch_collect.sh
```

| # | 命令 | 你做什么 | 时长 |
|---|------|---------|------|
| 1 | `--exercise squat --mode golden` | 标准全幅深蹲 | 60s |
| 2 | `--exercise squat --mode lazy` | 蹲到一半就起来 | 60s |
| 3 | `--exercise squat --mode bad` | 膝盖内扣/重心偏 | 60s |
| 4 | `--exercise bicep_curl --mode golden` | 标准弯举到顶 | 60s |
| 5 | `--exercise bicep_curl --mode lazy` | 幅度不够就放下 | 60s |
| 6 | `--exercise bicep_curl --mode bad` | 借力耸肩晃动 | 60s |

### 1.2 真实传感器采集（有队友ESP32）

**唯一的区别：EMG数据来源变了。**

```
模拟模式: cloud_rtmpose → 角度 → udp_emg_server 自动生成 EMG
真实模式: ESP32 → UDP:8080 → udp_emg_server 接收真实EMG → emg_heartbeat 标记
```

**队友需要做的：**
1. ESP32 固件中设置 `TARGET_IP = "10.105.245.224"`，`PORT = 8080`
2. 启动 ESP32 发送 ADC 数据

**你需要做的：零代码修改。** `udp_emg_server.py` 收到真实数据后会：
- 自动写入 `/dev/shm/emg_heartbeat`
- 模拟数据生成器看到此文件后自动停止
- `collect_training_data.py` 无感切换，CSV 格式完全一致

**验证传感器连接：**
```bash
# 查看heartbeat是否存在
ls /dev/shm/emg_heartbeat
# 查看实时EMG数据
watch -n 0.3 cat /dev/shm/muscle_activation.json
```

### 1.3 CSV 文件结构

每行 = 一帧（20Hz采样），10列：

| 列 | 含义 | 范围 |
|----|------|------|
| Timestamp | Unix时间戳 | — |
| Ang_Vel | 角速度(度/帧) | [-20, 20] |
| Angle | 关节角度(度) | [0, 180] |
| Ang_Accel | 角加速度 | [-10, 10] |
| Target_RMS | 目标肌肉EMG强度 | [0, 100] |
| Comp_RMS | 代偿肌肉EMG强度 | [0, 100] |
| Symmetry_Score | 左右对称性 | [0, 1] |
| Phase_Progress | 动作阶段进度 | [0, 1] |
| pose_score | 人体检测置信度 | [0, 1] |
| label | 标签 | golden/lazy/bad |

---

## 阶段二：数据验证

### 2.1 命令行快检
```bash
python3 tools/validate_data.py ~/training_data/
```

### 2.2 Streamlit 面板深度检查
```bash
streamlit run tools/dashboard.py
# 浏览器打开 → 标签页1「数据探索」
```

**检查清单：**
- [ ] 每个文件 ≥ 600 帧（30秒）
- [ ] 角度范围 ≥ 15度（说明有完整动作）
- [ ] 三种标签的 Angle 分布明显不同
- [ ] EMG 非全零（传感器在工作）
- [ ] 时间序列无明显断层或异常跳变

**不合格怎么办：** 删掉该文件，重新采集对应的组。

---

## 阶段三：模型训练

### 3.1 传输数据到 WSL
```bash
# WSL端执行
mkdir -p ~/projects/embedded-fullstack/data
scp -i ~/.ssh/id_rsa_toybrick -r toybrick@10.105.245.224:~/training_data/ \
    ~/projects/embedded-fullstack/data/
```

### 3.2 本地训练（WSL CPU）
```bash
cd ~/projects/embedded-fullstack
python3 tools/train_model.py \
    --data ./data/training_data \
    --out ./models \
    --epochs 25 \
    --batch 64 \
    --lr 0.005
```

### 3.3 云端训练（AutoDL GPU，大数据量推荐）
```bash
# 上传数据+代码
scp -i ~/.ssh/id_cloud_autodl -P 14191 -r data/ tools/train_model.py \
    hardware_engine/cognitive/fusion_model.py \
    root@connect.westd.seetacloud.com:/root/ironbuddy_cloud/

# SSH登录训练
ssh -i ~/.ssh/id_cloud_autodl -p 14191 root@connect.westd.seetacloud.com
cd /root/ironbuddy_cloud
python train_model.py --data ./data --out ./models --epochs 25
```

### 3.4 训练监控

**方法一：TensorBoard（推荐）**
```bash
# 训练的同时开另一个终端
tensorboard --logdir models/tb_logs
# 浏览器访问 http://localhost:6006
```

**方法二：Streamlit 面板**
```bash
streamlit run tools/dashboard.py
# → 标签页2「训练监控」
```

**看什么：**
- Loss/train 和 Loss/val 同步下降 → 正常
- Loss/val 开始上升而 train 继续降 → 过拟合，减少 epochs
- val_acc > 80% → 可接受
- Similarity 直方图三类分开 → 模型区分力强

---

## 阶段四：模型评估

### 4.1 Streamlit 面板评估
```bash
streamlit run tools/dashboard.py
# → 标签页3「模型评估」
# 指定模型路径和数据目录，点击「开始评估」
```

**理想指标：**
| 指标 | 目标值 |
|------|--------|
| 总体准确率 | > 80% |
| 标准动作 相似度均值 | > 0.8 |
| 错误动作 相似度均值 | < 0.3 |
| 混淆矩阵对角线占比 | > 70% |

### 4.2 不达标怎么办

| 问题 | 解决方案 |
|------|---------|
| 准确率 < 70% | 数据量不够，每组加到90秒重采 |
| standard 和 lazy 混淆 | lazy 的动作幅度要做得更明显 |
| 过拟合（train高val低） | 减少 epochs 到 15，或增加数据 |
| EMG 特征无区分度 | 检查传感器位置，或暂时忽略EMG列 |

---

## 阶段五：部署到板端

```bash
# 从WSL上传模型
scp -i ~/.ssh/id_rsa_toybrick \
    ~/projects/embedded-fullstack/models/extreme_fusion_gru.pt \
    toybrick@10.105.245.224:~/streamer_v3/hardware_engine/cognitive/

# 重启系统（模型自动加载）
bash start_validation.sh
```

**验证部署：**
1. 网页上出现「动作相似度」指标
2. 做标准动作 → 相似度 > 80%
3. 做偷懒动作 → 相似度下降到 40-60%
4. 做错误动作 → 相似度 < 30%

或用 Streamlit 面板 → 标签页4「实时推理」实时查看。

---

## 阶段六：迭代优化

```
数据不够 → 重复阶段一（采集更多数据）
         ↓
模型效果差 → 重复阶段三（调参重训）
         ↓
新的运动类型 → 扩展 collect_training_data.py 的 --exercise 选项
         ↓
真实传感器 → 替换模拟数据，重采一轮，重训
```

---

## 关键文件索引

| 文件 | 作用 | 修改频率 |
|------|------|---------|
| `tools/collect_training_data.py` | 板端数据采集 | 低 |
| `tools/batch_collect.sh` | 批量采集脚本 | 低 |
| `tools/validate_data.py` | 数据质量检查 | 低 |
| `tools/train_model.py` | GRU训练 (+TensorBoard) | 调参时 |
| `tools/dashboard.py` | Streamlit 可视化面板 | 低 |
| `hardware_engine/cognitive/fusion_model.py` | GRU模型定义 | 改架构时 |
| `hardware_engine/main_claw_loop.py` | 板端主循环(加载模型推理) | 低 |
| `hardware_engine/sensor/udp_emg_server.py` | EMG接收(UDP:8080) | 传感器对接时 |

---

## 时间规划

| 阶段 | 耗时 | 依赖 |
|------|------|------|
| 模拟数据采集(6组) | 约15分钟 | 板端系统运行 |
| 数据验证 | 5分钟 | 面板或命令行 |
| WSL本地训练 | 2-5分钟 | CPU够用 |
| AutoDL训练 | 1-2分钟 | GPU |
| 模型评估 | 2分钟 | 面板 |
| 部署+验证 | 5分钟 | 板端重启 |
| 真实传感器重采+重训 | 20分钟 | 队友在场 |
| **总计** | **约50分钟** | |
