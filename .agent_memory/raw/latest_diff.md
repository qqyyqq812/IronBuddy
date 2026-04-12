# Latest Commit Diff
Commit Hash: d94fc475078d71a0b36b98e6b69066d1e7a3090e
Timestamp: Sun Apr 12 14:45:58 CST 2026
```diff
commit d94fc475078d71a0b36b98e6b69066d1e7a3090e
Author: qqyyqq812 <2957131097@qq.com>
Date:   Sun Apr 12 14:45:58 2026 +0800

    feat: GRU训练闭环工具链 + Streamlit可视化面板 + Bug修复
    
    - 数据采集: collect_training_data.py 支持 bicep_curl + --auto 模式
    - 批量采集: batch_collect.sh 一键6组
    - 数据验证: validate_data.py 质量检查
    - 训练: train_model.py 集成 TensorBoard (loss/acc/混淆矩阵/相似度直方图)
    - 可视化: dashboard.py Streamlit 4标签页 (数据探索/训练监控/模型评估/实时推理)
    - Bug修复: 前端标签动态切换, DeepSeek <think>剥离, CSS骨架10Hz同步
    - 语音: TTS缓存机制, 麦克风增益自动调满, ASR重试
    - 文档: GRU训练闭环指导手册, 数据采集与训练指南, 可视化面板使用指南
---
 ...214\207\345\257\274\346\211\213\345\206\214.md" | 299 +++++++++++++++++
 .agent_memory/index.md                             |  56 +++-
 ...275\277\347\224\250\346\214\207\345\215\227.md" | 142 ++++++++
 ...256\255\347\273\203\346\214\207\345\215\227.md" | 249 ++++++++++++++
 hardware_engine/main_claw_loop.py                  |   3 +
 hardware_engine/voice_daemon.py                    | 109 +++++--
 templates/index.html                               |  19 +-
 tools/batch_collect.sh                             |  68 ++++
 tools/collect_training_data.py                     | 154 +++++++--
 tools/dashboard.py                                 | 363 +++++++++++++++++++++
 tools/train_model.py                               |  82 ++++-
 tools/validate_data.py                             |  77 +++++
 12 files changed, 1541 insertions(+), 80 deletions(-)

diff --git "a/.agent_memory/GRU\350\256\255\347\273\203\351\227\255\347\216\257\346\214\207\345\257\274\346\211\213\345\206\214.md" "b/.agent_memory/GRU\350\256\255\347\273\203\351\227\255\347\216\257\346\214\207\345\257\274\346\211\213\345\206\214.md"
new file mode 100644
index 0000000..fcfe2c5
--- /dev/null
+++ "b/.agent_memory/GRU\350\256\255\347\273\203\351\227\255\347\216\257\346\214\207\345\257\274\346\211\213\345\206\214.md"
@@ -0,0 +1,299 @@
+# IronBuddy GRU 训练闭环指导手册
+
+> 从数据采集到模型部署的完整操作手册
+> 最后更新: 2026-04-12
+
+---
+
+## 整体数据流
+
+```
+[板端摄像头]          [ESP32传感器]
+    |                      |
+    v                      v
+cloud_rtmpose_client   udp_emg_server.py
+    |                      |
+    v                      v
+/dev/shm/pose_data.json  /dev/shm/muscle_activation.json
+    |                      |
+    +----------+-----------+
+               |
+               v
+  collect_training_data.py   <--- 你在板端运行的采集脚本
+               |
+               v
+  train_squat_golden_*.csv   <--- 7维特征 + 标签
+               |
+               v
+     validate_data.py        <--- 数据质量检查
+               |
+               v
+      train_model.py         <--- GRU训练 (WSL/云端GPU)
+               |
+               v
+   extreme_fusion_gru.pt     <--- 模型文件 (~6KB)
+               |
+               v
+    main_claw_loop.py        <--- 板端实时推理 (加载模型)
+               |
+               v
+  /dev/shm/fsm_state.json   <--- 推理结果写入前端
+               |
+               v
+     前端网页 + 面板          <--- 实时展示
+```
+
+---
+
+## 阶段一：数据采集
+
+### 1.1 模拟数据采集（无传感器）
+
+**现在就能做。** 系统会自动从骨架角度生成模拟EMG。
+
+```bash
+# 在板端终端执行
+ssh toybrick@10.105.245.224
+
+# 确保系统在运行（从WSL启动）
+# WSL端: bash start_validation.sh
+
+# 交互式采集（推荐，有键盘控制）
+cd ~/streamer_v3
+python3 tools/collect_training_data.py --exercise squat --mode golden --out ~/training_data
+
+# 或自动模式（60秒无人值守）
+python3 tools/collect_training_data.py --exercise squat --mode golden --out ~/training_data --auto 60
+```
+
+**6组数据清单（一键批处理）:**
+```bash
+cd ~/streamer_v3/tools && bash batch_collect.sh
+```
+
+| # | 命令 | 你做什么 | 时长 |
+|---|------|---------|------|
+| 1 | `--exercise squat --mode golden` | 标准全幅深蹲 | 60s |
+| 2 | `--exercise squat --mode lazy` | 蹲到一半就起来 | 60s |
+| 3 | `--exercise squat --mode bad` | 膝盖内扣/重心偏 | 60s |
+| 4 | `--exercise bicep_curl --mode golden` | 标准弯举到顶 | 60s |
+| 5 | `--exercise bicep_curl --mode lazy` | 幅度不够就放下 | 60s |
+| 6 | `--exercise bicep_curl --mode bad` | 借力耸肩晃动 | 60s |
+
+### 1.2 真实传感器采集（有队友ESP32）
+
+**唯一的区别：EMG数据来源变了。**
+
+```
+模拟模式: cloud_rtmpose → 角度 → udp_emg_server 自动生成 EMG
+真实模式: ESP32 → UDP:8080 → udp_emg_server 接收真实EMG → emg_heartbeat 标记
+```
+
+**队友需要做的：**
+1. ESP32 固件中设置 `TARGET_IP = "10.105.245.224"`，`PORT = 8080`
+2. 启动 ESP32 发送 ADC 数据
+
+**你需要做的：零代码修改。** `udp_emg_server.py` 收到真实数据后会：
+- 自动写入 `/dev/shm/emg_heartbeat`
+- 模拟数据生成器看到此文件后自动停止
+- `collect_training_data.py` 无感切换，CSV 格式完全一致
+
+**验证传感器连接：**
+```bash
+# 查看heartbeat是否存在
+ls /dev/shm/emg_heartbeat
+# 查看实时EMG数据
+watch -n 0.3 cat /dev/shm/muscle_activation.json
+```
+
+### 1.3 CSV 文件结构
+
+每行 = 一帧（20Hz采样），10列：
+
+| 列 | 含义 | 范围 |
+|----|------|------|
+| Timestamp | Unix时间戳 | — |
+| Ang_Vel | 角速度(度/帧) | [-20, 20] |
+| Angle | 关节角度(度) | [0, 180] |
+| Ang_Accel | 角加速度 | [-10, 10] |
+| Target_RMS | 目标肌肉EMG强度 | [0, 100] |
+| Comp_RMS | 代偿肌肉EMG强度 | [0, 100] |
+| Symmetry_Score | 左右对称性 | [0, 1] |
+| Phase_Progress | 动作阶段进度 | [0, 1] |
+| pose_score | 人体检测置信度 | [0, 1] |
+| label | 标签 | golden/lazy/bad |
+
+---
+
+## 阶段二：数据验证
+
+### 2.1 命令行快检
+```bash
+python3 tools/validate_data.py ~/training_data/
+```
+
+### 2.2 Streamlit 面板深度检查
+```bash
+streamlit run tools/dashboard.py
+# 浏览器打开 → 标签页1「数据探索」
+```
+
+**检查清单：**
+- [ ] 每个文件 ≥ 600 帧（30秒）
+- [ ] 角度范围 ≥ 15度（说明有完整动作）
+- [ ] 三种标签的 Angle 分布明显不同
+- [ ] EMG 非全零（传感器在工作）
+- [ ] 时间序列无明显断层或异常跳变
+
+**不合格怎么办：** 删掉该文件，重新采集对应的组。
+
+---
+
+## 阶段三：模型训练
+
+### 3.1 传输数据到 WSL
+```bash
+# WSL端执行
+mkdir -p ~/projects/embedded-fullstack/data
+scp -i ~/.ssh/id_rsa_toybrick -r toybrick@10.105.245.224:~/training_data/ \
+    ~/projects/embedded-fullstack/data/
+```
+
+### 3.2 本地训练（WSL CPU）
+```bash
+cd ~/projects/embedded-fullstack
+python3 tools/train_model.py \
+    --data ./data/training_data \
+    --out ./models \
+    --epochs 25 \
+    --batch 64 \
+    --lr 0.005
+```
+
+### 3.3 云端训练（AutoDL GPU，大数据量推荐）
+```bash
+# 上传数据+代码
+scp -i ~/.ssh/id_cloud_autodl -P 14191 -r data/ tools/train_model.py \
+    hardware_engine/cognitive/fusion_model.py \
+    root@connect.westd.seetacloud.com:/root/ironbuddy_cloud/
+
+# SSH登录训练
+ssh -i ~/.ssh/id_cloud_autodl -p 14191 root@connect.westd.seetacloud.com
+cd /root/ironbuddy_cloud
+python train_model.py --data ./data --out ./models --epochs 25
+```
+
+### 3.4 训练监控
+
+**方法一：TensorBoard（推荐）**
+```bash
+# 训练的同时开另一个终端
+tensorboard --logdir models/tb_logs
+# 浏览器访问 http://localhost:6006
+```
+
+**方法二：Streamlit 面板**
+```bash
+streamlit run tools/dashboard.py
+# → 标签页2「训练监控」
+```
+
+**看什么：**
+- Loss/train 和 Loss/val 同步下降 → 正常
+- Loss/val 开始上升而 train 继续降 → 过拟合，减少 epochs
+- val_acc > 80% → 可接受
+- Similarity 直方图三类分开 → 模型区分力强
+
+---
+
+## 阶段四：模型评估
+
+### 4.1 Streamlit 面板评估
+```bash
+streamlit run tools/dashboard.py
+# → 标签页3「模型评估」
+# 指定模型路径和数据目录，点击「开始评估」
+```
+
+**理想指标：**
+| 指标 | 目标值 |
+|------|--------|
+| 总体准确率 | > 80% |
+| 标准动作 相似度均值 | > 0.8 |
+| 错误动作 相似度均值 | < 0.3 |
+| 混淆矩阵对角线占比 | > 70% |
+
+### 4.2 不达标怎么办
+
+| 问题 | 解决方案 |
+|------|---------|
+| 准确率 < 70% | 数据量不够，每组加到90秒重采 |
+| standard 和 lazy 混淆 | lazy 的动作幅度要做得更明显 |
+| 过拟合（train高val低） | 减少 epochs 到 15，或增加数据 |
+| EMG 特征无区分度 | 检查传感器位置，或暂时忽略EMG列 |
+
+---
+
+## 阶段五：部署到板端
+
+```bash
+# 从WSL上传模型
+scp -i ~/.ssh/id_rsa_toybrick \
+    ~/projects/embedded-fullstack/models/extreme_fusion_gru.pt \
+    toybrick@10.105.245.224:~/streamer_v3/hardware_engine/cognitive/
+
+# 重启系统（模型自动加载）
+bash start_validation.sh
+```
+
+**验证部署：**
+1. 网页上出现「动作相似度」指标
+2. 做标准动作 → 相似度 > 80%
+3. 做偷懒动作 → 相似度下降到 40-60%
+4. 做错误动作 → 相似度 < 30%
+
+或用 Streamlit 面板 → 标签页4「实时推理」实时查看。
+
+---
+
+## 阶段六：迭代优化
+
+```
+数据不够 → 重复阶段一（采集更多数据）
+         ↓
+模型效果差 → 重复阶段三（调参重训）
+         ↓
+新的运动类型 → 扩展 collect_training_data.py 的 --exercise 选项
+         ↓
+真实传感器 → 替换模拟数据，重采一轮，重训
+```
+
+---
+
+## 关键文件索引
+
+| 文件 | 作用 | 修改频率 |
+|------|------|---------|
+| `tools/collect_training_data.py` | 板端数据采集 | 低 |
+| `tools/batch_collect.sh` | 批量采集脚本 | 低 |
+| `tools/validate_data.py` | 数据质量检查 | 低 |
+| `tools/train_model.py` | GRU训练 (+TensorBoard) | 调参时 |
+| `tools/dashboard.py` | Streamlit 可视化面板 | 低 |
+| `hardware_engine/cognitive/fusion_model.py` | GRU模型定义 | 改架构时 |
+| `hardware_engine/main_claw_loop.py` | 板端主循环(加载模型推理) | 低 |
+| `hardware_engine/sensor/udp_emg_server.py` | EMG接收(UDP:8080) | 传感器对接时 |
+
+---
+
+## 时间规划
+
+| 阶段 | 耗时 | 依赖 |
+|------|------|------|
+| 模拟数据采集(6组) | 约15分钟 | 板端系统运行 |
+| 数据验证 | 5分钟 | 面板或命令行 |
+| WSL本地训练 | 2-5分钟 | CPU够用 |
+| AutoDL训练 | 1-2分钟 | GPU |
+| 模型评估 | 2分钟 | 面板 |
+| 部署+验证 | 5分钟 | 板端重启 |
+| 真实传感器重采+重训 | 20分钟 | 队友在场 |
+| **总计** | **约50分钟** | |
diff --git a/.agent_memory/index.md b/.agent_memory/index.md
index 046a3ba..bb39dad 100644
--- a/.agent_memory/index.md
+++ b/.agent_memory/index.md
@@ -1,38 +1,58 @@
 # Agent Local Memory: IronBuddy (Embedded-Fullstack)
 
-> Last updated: 2026-04-11 (V3.0 sprint complete)
+> Last updated: 2026-04-12 (GRU训练闭环+可视化面板)
 
 ## Quick Reference
-读取 `_entity_graph.md` 获取完整代码拓扑和架构图。
+- 读取 `_entity_graph.md` 获取完整代码拓扑和架构图
+- 读取 `GRU训练闭环指导手册.md` 获取完整的数据采集→训练→部署流程
 
-## Architecture (2026-04-11)
-- **视觉推理**: Cloud RTMPose-m ONNX on RTX 5090 via **direct** SSH tunnel (Board→Cloud, ~100ms RTT)
+## Architecture (2026-04-12)
+- **视觉推理**: Cloud RTMPose-m ONNX on RTX 5090 via direct HTTPS (~100ms RTT)
 - **板端 NPU**: RKNN 量化模型精度不可用(conf<0.3)，已弃用
 - **通信**: /dev/shm 共享内存 IPC (atomic rename)
 - **LLM**: DeepSeek via OpenClaw WebSocket (Board→WSL:18789)
-- **EMG**: 模拟数据由视觉管线同步生成（角度驱动），真实传感器通过 emg_heartbeat 标记接管
+- **EMG**: 模拟数据由视觉管线自动生成；真实传感器通过 UDP:8080 + emg_heartbeat 标记接管
+- **GRU训练**: 7D特征 → 滑动窗口(30帧) → CompensationGRU → 相似度+分类+阶段
+- **可视化**: TensorBoard (训练曲线) + Streamlit (数据探索/评估/实时推理)
 
 ## Sprint Status
 - [x] Cloud RTMPose 部署 + SSH 直连隧道
 - [x] FSM 状态机 (深蹲+弯举)
 - [x] EMG 模拟同步（与骨架角度联动）
-- [x] 语音守护 (mono fix, Google ASR)
-- [x] DeepSeek 对话 + 教练点评
-- [x] 疲劳 1500 自动重置 + 自动触发 API
-- [x] 鬼影过滤（真实置信分数 > 0.15）
-- [x] 小人发力闪烁 + 疲劳渐变渲染
-- [ ] **GRU 神经网络训练** (代码就绪，需采集数据)
-- [ ] 传感器实物连接测试
+- [x] 语音守护 (TTS缓存 + Google ASR + retry)
+- [x] DeepSeek 对话 + 教练点评 (`<think>` 剥离)
+- [x] 疲劳 1500 自动重置
+- [x] 前端标签动态切换 + CSS骨架 10Hz 同步
+- [x] 数据采集工具 (collect_training_data.py, --auto 模式, bicep_curl 支持)
+- [x] 批量采集脚本 (batch_collect.sh, 6组一键)
+- [x] 数据验证工具 (validate_data.py)
+- [x] 训练脚本集成 TensorBoard (train_model.py)
+- [x] Streamlit 可视化面板 (dashboard.py, 4标签页)
+- [ ] **GRU 实际训练** (工具就绪，等采集真实数据)
+- [ ] 传感器实物对接 (ESP32 → UDP:8080)
 
 ## Critical Config
 - Board: `toybrick@10.105.245.224` key: `~/.ssh/id_rsa_toybrick`
 - Cloud: `root@connect.westd.seetacloud.com:14191` key: `~/.ssh/id_cloud_autodl`
-- Cloud model: `/root/ironbuddy_cloud/rtmpose_m.onnx`
-- Webcam mic: `hw:Webcam,0` **mono** (CHANNELS=1)
-- Start: `tclsh ~/projects/embedded-fullstack/start_validation.tcl`
-- Stop: `tclsh ~/projects/embedded-fullstack/stop_validation.tcl`
+- Start: `bash start_validation.sh` (从WSL执行)
+- Dashboard: `streamlit run tools/dashboard.py`
+- GitHub: `git@github.com:qqyyqq812/IronBuddy.git`
+
+## 数据流 (采集→训练→推理)
+```
+摄像头 → cloud_rtmpose → pose_data.json ─┐
+ESP32  → udp_emg_server → muscle_activation.json ─┤
+                                                    ↓
+                              collect_training_data.py → CSV
+                                                    ↓
+                              validate_data.py → 质量检查
+                                                    ↓
+                              train_model.py → extreme_fusion_gru.pt
+                                                    ↓
+                              main_claw_loop.py → 实时推理 → 前端
+```
 
 ## Dev Hints
-- 修改代码后需重读 `_entity_graph.md` 更新拓扑
 - Board Python 3.7: 不支持 `X | None` 语法, 无 pandas
-- 云端 ONNX 模型 54MB, GPU 推理 ~10ms
+- edge-tts 在板端不稳定 → 使用 ~/tts_cache/ 预缓存MP3
+- 采集工具的 --auto 模式可在非交互终端(SSH)运行
diff --git "a/docs/\345\217\257\350\247\206\345\214\226\351\235\242\346\235\277\344\275\277\347\224\250\346\214\207\345\215\227.md" "b/docs/\345\217\257\350\247\206\345\214\226\351\235\242\346\235\277\344\275\277\347\224\250\346\214\207\345\215\227.md"
new file mode 100644
index 0000000..a54869e
--- /dev/null
+++ "b/docs/\345\217\257\350\247\206\345\214\226\351\235\242\346\235\277\344\275\277\347\224\250\346\214\207\345\215\227.md"
@@ -0,0 +1,142 @@
+# IronBuddy 可视化面板使用指南
+
+## 架构总览
+
+```
+采集 CSV ──> Streamlit Data Explorer ──> 看数据质量
+                                            |
+训练模型 ──> TensorBoard ──────────────> 看 loss/acc 曲线
+                |
+训练完成 ──> Streamlit Model Evaluator -> 混淆矩阵+分类报告
+                                            |
+板端推理 ──> Streamlit Live Inference ──> 实时角度+相似度曲线
+```
+
+---
+
+## 一、安装
+
+```bash
+# WSL 端一次性安装
+pip install streamlit plotly scikit-learn tensorboard matplotlib
+```
+
+---
+
+## 二、启动面板
+
+```bash
+cd ~/projects/embedded-fullstack
+streamlit run tools/dashboard.py
+```
+
+浏览器自动打开 `http://localhost:8501`，4个标签页。
+
+---
+
+## 三、Tab 1: Data Explorer（数据探索）
+
+**用途**：采集完数据后，第一时间检查质量。
+
+操作：
+1. 输入数据目录（如 `~/projects/embedded-fullstack/data`）
+2. 用标签筛选器选择 golden / lazy / bad
+3. 查看：
+   - **每个文件的帧数和角度范围** — 帧太少或角度范围太小说明采集有问题
+   - **特征分布直方图** — 对比不同标签的 Angle/EMG 分布是否有区分度
+   - **时间序列曲线** — 逐帧查看角度、速度变化是否合理
+   - **特征相关性热力图** — 看 Angle 和 Target_RMS 是否正相关
+
+**判断标准**：
+- 三种标签的 Angle 分布应该明显不同（golden 范围最大、bad 偏移最大）
+- EMG 非全零
+- 每个文件至少 600 帧
+
+---
+
+## 四、Tab 2: Training Monitor（训练监控）
+
+**用途**：训练过程中实时看 loss 曲线。
+
+方法一（推荐）：训练时同时开 TensorBoard
+```bash
+# 终端 1: 训练
+python tools/train_model.py --data ./data --out ./models --epochs 25
+
+# 终端 2: TensorBoard
+tensorboard --logdir ./models/tb_logs
+# 浏览器访问 http://localhost:6006
+```
+
+方法二：通过 Streamlit 面板直接读取 TensorBoard 日志
+- 在 Tab 2 输入 `tb_logs` 目录路径
+- 选择 run 和 metric 查看曲线
+
+**看什么**：
+- **Loss/train vs Loss/val** — 两条线应该同步下降。val 上升 = 过拟合
+- **Accuracy** — 应该稳步上升，最终 val_acc > 80%
+- **LR** — 余弦退火，从 0.005 降到 ~0.00025
+- **Similarity 直方图** — standard 应聚集在 0.8-1.0，non_standard 在 0.1-0.3
+
+---
+
+## 五、Tab 3: Model Evaluator（模型评估）
+
+**用途**：训练完成后，用全部数据评估模型效果。
+
+操作：
+1. 指定模型文件路径（如 `./models/extreme_fusion_gru.pt`）
+2. 指定评估数据目录
+3. 点击 "Run Evaluation"
+
+**输出**：
+- **混淆矩阵** — 对角线数字应该最大
+- **分类报告** — precision/recall/f1 每类都应 > 0.7
+- **相似度分布** — 三种颜色的直方图应该分离
+- **统计表** — 每类的 mean/std/min/max
+
+**理想结果**：
+| 指标 | 目标 |
+|------|------|
+| Overall accuracy | > 80% |
+| standard 的 similarity mean | > 0.8 |
+| non_standard 的 similarity mean | < 0.3 |
+| 三类之间的直方图重叠 | 越少越好 |
+
+---
+
+## 六、Tab 4: Live Inference（实时推理）
+
+**用途**：板端运行时，实时看模型输出。
+
+操作：
+1. 确保板端系统在运行（`http://10.105.245.224:5000` 可访问）
+2. 输入板端 IP
+3. 点击 "Start Monitoring"
+4. 做动作，观察实时曲线
+
+**看什么**：
+- **Angle 曲线** — 应该跟你的动作幅度一致
+- **Similarity 曲线** — 标准动作时应接近 1.0，偷懒时下降
+
+---
+
+## 七、完整工作流
+
+```
+1. 采集数据        bash tools/batch_collect.sh
+                         |
+2. 传回 WSL        scp -r toybrick@板IP:~/training_data ./data/
+                         |
+3. 检查数据        streamlit run tools/dashboard.py  → Tab 1
+   (不合格则重采)         |
+4. 训练模型        python tools/train_model.py --data ./data --out ./models
+                         |
+5. 看训练曲线      tensorboard --logdir ./models/tb_logs  (或 Tab 2)
+                         |
+6. 评估模型        streamlit run tools/dashboard.py  → Tab 3
+   (不合格则调参重训)     |
+7. 部署到板端      scp models/extreme_fusion_gru.pt toybrick@板IP:~/...
+                         |
+8. 实时验证        streamlit run tools/dashboard.py  → Tab 4
+```
diff --git "a/docs/\346\225\260\346\215\256\351\207\207\351\233\206\344\270\216\350\256\255\347\273\203\346\214\207\345\215\227.md" "b/docs/\346\225\260\346\215\256\351\207\207\351\233\206\344\270\216\350\256\255\347\273\203\346\214\207\345\215\227.md"
new file mode 100644
index 0000000..5062c7a
--- /dev/null
+++ "b/docs/\346\225\260\346\215\256\351\207\207\351\233\206\344\270\216\350\256\255\347\273\203\346\214\207\345\215\227.md"
@@ -0,0 +1,249 @@
+# IronBuddy 数据采集与训练指南
+
+> 目标: 传感器对接 → 数据采集 → 验证 → 云端训练 → 部署测试
+
+---
+
+## 一、传感器对接（ESP32 → 板端）
+
+### 现状
+- 板端 `udp_emg_server.py` 已监听 UDP :8080
+- ESP32 固件以 1000Hz 发送 ADC 原始值（ASCII float）
+- 无传感器时：视觉管线自动生成模拟 EMG（从骨架角度推算）
+- 有传感器时：`/dev/shm/emg_heartbeat` 文件存在 → 模拟数据自动让位
+
+### 你需要做的
+1. ESP32 固件中设置目标 IP 为板端 `10.105.245.224`，端口 `8080`
+2. 确保 ESP32 和板端在同一局域网（或可达）
+3. **零代码修改** — 启动系统即可自动切换
+
+### 验证传感器连接
+```bash
+# SSH 登录板端
+ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.105.245.224
+
+# 检查 emg_heartbeat 是否存在（存在 = 真实传感器在工作）
+ls -la /dev/shm/emg_heartbeat
+
+# 实时查看 EMG 数据
+watch -n 0.3 cat /dev/shm/muscle_activation.json
+
+# 检查 UDP 是否有数据到达
+sudo tcpdump -i any udp port 8080 -c 10
+```
+
+### 不接传感器也能采集
+模拟 EMG 会根据骨架角度自动生成，数据格式完全一致。区别在于：
+- 模拟数据 JSON 中有 `"simulated": true` 字段
+- 真实数据来自 DSP 处理后的肌电信号
+- **两者输出格式相同**，训练代码无需区分
+
+---
+
+## 二、数据采集
+
+### 方法A：一键批处理（推荐）
+
+```bash
+# 在板端执行
+cd ~/tools
+bash batch_collect.sh
+```
+
+按顺序采集 6 组：深蹲×3 + 弯举×3。每组之间会暂停等你准备。
+
+每组采集流程：
+1. 按回车开始
+2. 按 `s` 开始录制
+3. 做动作（每组建议 30-60 秒，约 600-1200 帧）
+4. 按 `q` 结束保存
+5. 自动进入下一组
+
+### 方法B：单独采集
+
+```bash
+# 深蹲 - 标准
+python3 ~/tools/collect_training_data.py --exercise squat --mode golden --out ~/training_data
+
+# 深蹲 - 偷懒
+python3 ~/tools/collect_training_data.py --exercise squat --mode lazy --out ~/training_data
+
+# 深蹲 - 错误
+python3 ~/tools/collect_training_data.py --exercise squat --mode bad --out ~/training_data
+
+# 弯举 - 标准
+python3 ~/tools/collect_training_data.py --exercise bicep_curl --mode golden --out ~/training_data
+
+# 弯举 - 偷懒
+python3 ~/tools/collect_training_data.py --exercise bicep_curl --mode lazy --out ~/training_data
+
+# 弯举 - 错误
+python3 ~/tools/collect_training_data.py --exercise bicep_curl --mode bad --out ~/training_data
+```
+
+### 采集前提
+系统必须正在运行（摄像头推流 + 云端 RTMPose）：
+```bash
+tclsh ~/projects/embedded-fullstack/start_validation.tcl
+```
+
+### 6 种数据集说明
+
+| # | 运动 | 模式 | 你要做什么 |
+|---|------|------|-----------|
+| 1 | squat / golden | 标准深蹲 | 全幅深蹲，膝盖不超过脚尖，下到90° |
+| 2 | squat / lazy | 偷懒深蹲 | 蹲到一半就起来，幅度明显不够 |
+| 3 | squat / bad | 错误深蹲 | 膝盖内扣、重心偏移、身体前倾 |
+| 4 | bicep_curl / golden | 标准弯举 | 大臂不动，小臂完整弯举到顶 |
+| 5 | bicep_curl / lazy | 偷懒弯举 | 弯举幅度不够，不到顶就放下 |
+| 6 | bicep_curl / bad | 错误弯举 | 借力耸肩、身体大幅晃动 |
+
+### 采集量建议
+- 每组最少 30 秒（600帧），建议 60 秒（1200帧）
+- 做 10-20 个完整动作/组
+- 有队友配合时：一人做动作，一人看终端确认数据在录入
+
+---
+
+## 三、验证数据质量
+
+```bash
+# 在板端或 WSL 端运行
+python3 ~/tools/validate_data.py ~/training_data/20260411
+```
+
+输出示例：
+```
+文件                                              帧数  状态
+---------------------------------------------------------------------------
+train_squat_golden_20260411_143022.csv              892  ✅
+train_squat_lazy_20260411_143156.csv                743  ✅
+train_bicep_curl_golden_20260411_144012.csv         102  ⚠ 太少: 102 帧 (最少 60)
+```
+
+**检查项**：
+- 帧数 ≥ 60（否则重新采集）
+- 角度范围 ≥ 15°（否则动作幅度太小）
+- EMG 不全为零（否则传感器断了）
+
+---
+
+## 四、传输数据到 WSL
+
+```bash
+# 在 WSL 端执行
+mkdir -p ~/projects/embedded-fullstack/data
+scp -i ~/.ssh/id_rsa_toybrick -r toybrick@10.105.245.224:~/training_data/ \
+    ~/projects/embedded-fullstack/data/
+```
+
+---
+
+## 五、云端训练
+
+### 方案 A：WSL 本地训练（CPU，小数据量够用）
+```bash
+cd ~/projects/embedded-fullstack
+python3 tools/train_model.py --data ./data/training_data --out ./models --epochs 25
+```
+
+### 方案 B：AutoDL 云端 GPU 训练
+
+```bash
+# 1. 上传数据到云端
+scp -i ~/.ssh/id_cloud_autodl -P 14191 -r ~/projects/embedded-fullstack/data/ \
+    root@connect.westd.seetacloud.com:/root/ironbuddy_cloud/data/
+
+# 2. 上传训练代码
+scp -i ~/.ssh/id_cloud_autodl -P 14191 \
+    ~/projects/embedded-fullstack/tools/train_model.py \
+    ~/projects/embedded-fullstack/hardware_engine/cognitive/fusion_model.py \
+    root@connect.westd.seetacloud.com:/root/ironbuddy_cloud/
+
+# 3. SSH 登录云端
+ssh -i ~/.ssh/id_cloud_autodl -p 14191 root@connect.westd.seetacloud.com
+
+# 4. 训练
+cd /root/ironbuddy_cloud
+python train_model.py --data ./data --out ./models --epochs 25
+
+# 5. 下载模型（在 WSL 执行）
+scp -i ~/.ssh/id_cloud_autodl -P 14191 \
+    root@connect.westd.seetacloud.com:/root/ironbuddy_cloud/models/extreme_fusion_gru.pt \
+    ~/projects/embedded-fullstack/models/
+```
+
+---
+
+## 六、部署模型到板端
+
+```bash
+# 上传模型
+scp -i ~/.ssh/id_rsa_toybrick \
+    ~/projects/embedded-fullstack/models/extreme_fusion_gru.pt \
+    toybrick@10.105.245.224:~/hardware_engine/cognitive/
+
+# 重启系统测试
+ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.105.245.224 \
+    "tclsh ~/projects/embedded-fullstack/stop_validation.tcl; sleep 2; tclsh ~/projects/embedded-fullstack/start_validation.tcl"
+```
+
+模型加载是自动的：`main_claw_loop.py` 启动时检查 `extreme_fusion_gru.pt` 是否存在。
+
+---
+
+## 七、验证模型效果
+
+网页上会出现新的指标：
+- **动作相似度** — 越接近 100% 越标准
+- **分类** — standard / compensating / non_standard
+- **置信度** — 分类的可信程度
+
+验证检查：
+1. 做标准动作 → 相似度应 > 80%
+2. 做偷懒动作 → 分类应为 compensating
+3. 做错误动作 → 分类应为 non_standard
+
+---
+
+## 附录：CCS (Claude Code Desktop) 与 WSL API 密钥统一配置
+
+> 以下操作需要你手动执行，不涉及代码修改。
+
+### 问题诊断
+
+当前 WSL 中有环境变量覆盖了 CCS 的配置：
+
+```
+ANTHROPIC_BASE_URL=https://dk.claudecode.love
+ANTHROPIC_AUTH_TOKEN=sk-983c...
+```
+
+这些变量在 WSL shell 中设置（可能在 `~/.bashrc` 中），会覆盖 CCS 下发的 JSON 配置。
+
+### 解决步骤
+
+**步骤 1：找到并删除 WSL 中的环境变量**
+```bash
+# 在 WSL 终端中执行，查找在哪里设置的
+grep -rn "ANTHROPIC" ~/.bashrc ~/.profile ~/.bash_profile ~/.zshrc ~/.zshenv /etc/environment 2>/dev/null
+```
+
+找到后，编辑对应文件，删除或注释掉包含 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN` 的行。
+
+**步骤 2：重载 shell**
+```bash
+source ~/.bashrc   # 或对应的 shell 配置文件
+```
+
+**步骤 3：验证清除成功**
+```bash
+echo $ANTHROPIC_BASE_URL    # 应该为空
+echo $ANTHROPIC_AUTH_TOKEN   # 应该为空
+```
+
+**步骤 4：确认 CCS 可以接管**
+
+重启 VSCode 中的 Claude Code 扩展（Ctrl+Shift+P → "Claude Code: Restart"）。CCS 会通过自己的 JSON 配置 (`~/.claude/settings.json` 或扩展内置) 管理 API 连接，不再被环境变量覆盖。
+
+**注意**：`~/.bashrc` 中还有一行 `export OPENAI_API_KEY="sk-3ad67..."` — 这个是 OpenAI 的，不影响 Claude，但如果你不需要也可以一并清理。
\ No newline at end of file
diff --git a/hardware_engine/main_claw_loop.py b/hardware_engine/main_claw_loop.py
index bdd66cc..52efe1c 100644
--- a/hardware_engine/main_claw_loop.py
+++ b/hardware_engine/main_claw_loop.py
@@ -416,6 +416,9 @@ async def _deepseek_fire_and_forget(bridge, prompt, good_count, failed_count):
                     await asyncio.sleep(3)
                     continue
 
+            # Strip <think>...</think> reasoning block (same as chat path)
+            if "</think>" in reply:
+                reply = reply.split("</think>")[-1].strip()
             logging.info(f"💡 [后台] DeepSeek 响应 ({elapsed:.2f}s): {reply}")
 
             try:
diff --git a/hardware_engine/voice_daemon.py b/hardware_engine/voice_daemon.py
index f4d3c07..24dc0f5 100644
--- a/hardware_engine/voice_daemon.py
+++ b/hardware_engine/voice_daemon.py
@@ -42,9 +42,15 @@ TTS_REPLY = "我在，请说"
 DEVICE_SPK = "plughw:0,0"
 EDGE_TTS = "/home/toybrick/.local/bin/edge-tts"
 TTS_VOICE = "zh-CN-YunxiNeural"
+TTS_CACHE_DIR = os.path.expanduser("~/tts_cache")
+TTS_CACHE_MAP = {
+    "我在，请说": "wake_reply.mp3",
+    "抱歉，我没听清。": "not_heard.mp3",
+    "好的，收到。": "acknowledged.mp3",
+}
 CHAT_INPUT_FILE = "/dev/shm/chat_input.txt"
-STARTUP_DELAY = 15
-SPEAKER_VOLUME = int(os.environ.get("IRONBUDDY_SPEAKER_VOLUME", "80"))  # 0-100, configurable
+STARTUP_DELAY = 5
+SPEAKER_VOLUME = int(os.environ.get("IRONBUDDY_SPEAKER_VOLUME", "95"))  # 0-100, configurable
 
 # 初始化 ASR (SpeechRecognition 兜底 Vosk_ABI_Crash)
 try:
@@ -57,15 +63,25 @@ except ImportError:
     logging.error("SpeechRecognition 未安装")
 
 def process_asr(audio_obj, energy):
-    try:
-        text = global_recognizer.recognize_google(audio_obj, language="zh-CN")
-        logging.info(f"🧠 Google ASR截流: {text}")
-        return text, energy
-    except sr.UnknownValueError:
-        return "", energy
-    except Exception as e:
-        logging.error(f"Google API 报错: {e}")
-        return "", energy
+    """Google ASR with retry on network errors."""
+    for attempt in range(2):
+        try:
+            text = global_recognizer.recognize_google(audio_obj, language="zh-CN")
+            logging.info(f"🧠 Google ASR截流: {text}")
+            return text, energy
+        except sr.UnknownValueError:
+            return "", energy
+        except sr.RequestError as e:
+            logging.error(f"Google API 网络错误 (尝试 {attempt+1}/2): {e}")
+            if attempt == 0:
+                import time as _t; _t.sleep(0.5)
+                continue
+            logging.error("Google ASR 连续失败，请检查板端网络连接")
+            return "", energy
+        except Exception as e:
+            logging.error(f"Google API 报错: {e}")
+            return "", energy
+    return "", energy
 
 # TTS 播放竞态锁
 _playback_lock = threading.Lock()
@@ -97,24 +113,42 @@ def async_speak_tts(text):
         global _current_tts_process
         with _playback_lock:
             tmp_mp3 = "/tmp/voice_tts.mp3"
-            fallback_wav = "/home/toybrick/hardware_engine/fallback_reply.wav"
-            
-            try:
-                # 尝试 Edge-TTS，设定严格的 timeout，模拟联网死锁防护
-                subprocess.run(
-                    [EDGE_TTS, "--text", text, "--voice", TTS_VOICE, "--write-media", tmp_mp3],
-                    timeout=5, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
-                )
-                media_path = tmp_mp3
-                # 去除限幅阉割，满压输出，恢复物理扬声器应有的音浪
-                player_cmd = ["mpg123", "-a", DEVICE_SPK, "-q", media_path]
-            except Exception as e:
-                logging.warning(f"TTS 生成异常/超时，可能遭遇断网，启动本地降级: {e}")
-                if os.path.exists(fallback_wav):
-                    media_path = fallback_wav
-                    player_cmd = ["aplay", "-D", DEVICE_SPK, "-q", media_path]
+            media_path = None
+            player_cmd = None
+
+            # 1. Check pre-cached TTS first (instant, no network)
+            cached_file = TTS_CACHE_MAP.get(text)
+            if cached_file:
+                cached_path = os.path.join(TTS_CACHE_DIR, cached_file)
+                if os.path.exists(cached_path) and os.path.getsize(cached_path) > 100:
+                    media_path = cached_path
+                    player_cmd = ["mpg123", "-a", DEVICE_SPK, "-q", media_path]
+                    logging.info(f"🔊 使用缓存 TTS: {cached_file}")
+
+            # 2. Try edge-tts for dynamic text
+            if not media_path:
+                try:
+                    subprocess.run(
+                        [EDGE_TTS, "--text", text, "--voice", TTS_VOICE, "--write-media", tmp_mp3],
+                        timeout=8, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
+                    )
+                    if os.path.getsize(tmp_mp3) > 100:
+                        media_path = tmp_mp3
+                        player_cmd = ["mpg123", "-a", DEVICE_SPK, "-q", media_path]
+                    else:
+                        raise RuntimeError("edge-tts 输出为空")
+                except Exception as e:
+                    logging.warning(f"TTS 生成失败: {e}")
+
+            # 3. Fallback: use wake_reply as generic acknowledgement
+            if not media_path:
+                fallback = os.path.join(TTS_CACHE_DIR, "acknowledged.mp3")
+                if os.path.exists(fallback):
+                    media_path = fallback
+                    player_cmd = ["mpg123", "-a", DEVICE_SPK, "-q", media_path]
+                    logging.warning("使用缓存兜底音频")
                 else:
-                    logging.error("无可用的兜底交互音频。强制静默。")
+                    logging.error("无可用音频，静默跳过")
                     return
             
             # 安全切入音箱通道，并设置可配置的音量
@@ -171,7 +205,23 @@ def output_voice_status(listening, energy):
     except Exception:
         pass
 
+def _boost_mic_gain():
+    """Maximize Webcam microphone capture gain via ALSA mixer."""
+    try:
+        result = subprocess.run(
+            ["amixer", "-c", "Webcam", "set", "Mic", "15"],
+            capture_output=True, text=True, timeout=5
+        )
+        if result.returncode == 0:
+            logging.info("🔊 Webcam 麦克风增益已调至最大 (100%)")
+        else:
+            logging.warning(f"amixer 调整失败: {result.stderr.strip()}")
+    except Exception as e:
+        logging.warning(f"麦克风增益调整跳过: {e}")
+
+
 def main():
+    _boost_mic_gain()
     logging.info(f"等待 {STARTUP_DELAY}s 让中心进程组初始化...")
     time.sleep(STARTUP_DELAY)
 
@@ -298,6 +348,9 @@ def main():
                     # 处于发声期
                     silence_count = 0
                     audio_buffer.extend(data)
+                    if chunk_count % 5 == 0:
+                        output_debug(energy, "[录音中...]")
+                        output_voice_status(conversation_mode, energy)
             
             # 回收检查 ASR 结果 (非阻塞轮询)
             done_futures = [f for f in asr_futures if f.done()]
diff --git a/templates/index.html b/templates/index.html
index 1f2aba0..1369e60 100644
--- a/templates/index.html
+++ b/templates/index.html
@@ -488,7 +488,7 @@
                     <div class="hud-state" id="hudState" style="color: var(--accent-blue);">等待中</div>
                 </div>
                 <div class="hud-card">
-                    <div class="hud-label">膝盖角度</div>
+                    <div class="hud-label" id="hudAngleLabel">膝盖角度</div>
                     <div class="hud-value" id="hudAngle" style="color: var(--text-primary);">—</div>
                 </div>
                 <div class="hud-card">
@@ -521,11 +521,11 @@
 
                 <div class="stats-grid">
                     <div class="stat-item">
-                        <div class="stat-label">标准深蹲</div>
+                        <div class="stat-label" id="statGoodLabel">标准深蹲</div>
                         <div class="stat-value green" id="statGood">0</div>
                     </div>
                     <div class="stat-item">
-                        <div class="stat-label">违规半蹲</div>
+                        <div class="stat-label" id="statFailedLabel">违规半蹲</div>
                         <div class="stat-value red" id="statFailed">0</div>
                     </div>
                     <div class="stat-item">
@@ -708,7 +708,7 @@
                         } catch(err) {
                             self.postMessage({ type: 'ERROR' });
                         }
-                    }, 500);
+                    }, 100);
                 } else if (e.data === 'FORCE') {
                     // 主线程切回前台时，强行抓取一次平滑过渡
                     (async () => {
@@ -740,6 +740,15 @@
                 document.getElementById('hudState').textContent = stateInfo.text;
                 document.getElementById('hudState').style.color = stateInfo.color;
 
+                // Dynamic labels based on exercise mode
+                const isCurl = d.exercise === 'bicep_curl';
+                document.getElementById('statGoodLabel').textContent = isCurl ? '标准弯举' : '标准深蹲';
+                document.getElementById('statFailedLabel').textContent = isCurl ? '违规弯举' : '违规半蹲';
+                document.getElementById('hudAngleLabel').textContent = isCurl ? '肘关节角度' : '膝盖角度';
+                // Sync quick-select dropdown
+                const qSel = document.getElementById('quickExercise');
+                if (qSel && qSel.value !== d.exercise) qSel.value = d.exercise;
+
                 document.getElementById('statGood').textContent = d.good;
                 document.getElementById('statFailed').textContent = d.failed;
                 document.getElementById('hudAngle').textContent = d.angle > 0 ? d.angle + '°' : '—';
@@ -1197,7 +1206,7 @@
             _lastGood: 0,
             _lastFailed: 0,
             updatePose: function(angle, exerciseMode, isCheating, fatigue, goodCount, failedCount) {
-                if (typeof angle !== 'number' || angle <= 0) return;
+                if (typeof angle !== 'number' || angle < 0) return;
 
                 const body = document.getElementById('rig-body');
                 const thigh = document.getElementById('rig-thigh');
diff --git a/tools/batch_collect.sh b/tools/batch_collect.sh
new file mode 100755
index 0000000..1871aaf
--- /dev/null
+++ b/tools/batch_collect.sh
@@ -0,0 +1,68 @@
+#!/bin/bash
+# ============================================================
+# IronBuddy 数据采集批处理脚本
+# 6组数据: 2运动 × 3质量等级
+# 板端运行: bash ~/tools/batch_collect.sh
+# ============================================================
+set -e
+
+DATA_DIR="${HOME}/training_data/$(date +%Y%m%d)"
+TOOL="${HOME}/tools/collect_training_data.py"
+PYTHON="python3"
+
+mkdir -p "$DATA_DIR"
+
+echo "============================================"
+echo "  IronBuddy 数据采集 — $(date +%Y-%m-%d)"
+echo "  输出目录: $DATA_DIR"
+echo "============================================"
+echo ""
+
+# 采集列表: exercise mode 中文提示
+TASKS=(
+    "squat golden 深蹲-标准动作"
+    "squat lazy 深蹲-偷懒动作(幅度不够)"
+    "squat bad 深蹲-错误动作(膝盖内扣/重心偏移)"
+    "bicep_curl golden 弯举-标准动作"
+    "bicep_curl lazy 弯举-偷懒动作(幅度不够)"
+    "bicep_curl bad 弯举-错误动作(借力耸肩/身体晃动)"
+)
+
+TOTAL=${#TASKS[@]}
+CURRENT=0
+
+for task in "${TASKS[@]}"; do
+    read -r exercise mode desc <<< "$task"
+    CURRENT=$((CURRENT + 1))
+
+    echo ""
+    echo "========================================"
+    echo "  [$CURRENT/$TOTAL] $desc"
+    echo "  exercise=$exercise  mode=$mode"
+    echo "========================================"
+    echo ""
+    echo "  准备好后按回车开始采集..."
+    echo "  (采集中: [s]开始 [p]暂停 [q]结束保存)"
+    read -r
+
+    $PYTHON "$TOOL" --exercise "$exercise" --mode "$mode" --out "$DATA_DIR"
+
+    echo ""
+    echo "  ✅ $desc 采集完成"
+    echo ""
+done
+
+# 统计
+echo ""
+echo "============================================"
+echo "  全部采集完成！"
+echo "============================================"
+echo ""
+echo "文件列表:"
+ls -lh "$DATA_DIR"/train_*.csv 2>/dev/null || echo "  (无文件)"
+echo ""
+echo "每个文件行数:"
+wc -l "$DATA_DIR"/train_*.csv 2>/dev/null || echo "  (无文件)"
+echo ""
+echo "下一步: 将 $DATA_DIR 传回 WSL 进行训练"
+echo "  scp -r toybrick@10.105.245.224:$DATA_DIR ~/projects/embedded-fullstack/data/"
diff --git a/tools/collect_training_data.py b/tools/collect_training_data.py
index acc55c8..68d618d 100644
--- a/tools/collect_training_data.py
+++ b/tools/collect_training_data.py
@@ -116,10 +116,10 @@ def _symmetry_score(kpts: list) -> float:
     return 1.0 - abs(left_conf - right_conf) / total
 
 
-def _extract_angle_and_pose_score(pose_data: dict):
+def _extract_angle_and_pose_score(pose_data: dict, exercise: str = "squat"):
     """
-    Returns (angle_deg, pose_score) or (None, 0.0) if person not detected.
-    Mirrors the logic in SquatStateMachine.update().
+    Returns (angle_deg, pose_score, symmetry) or (None, 0.0) if person not detected.
+    Supports squat (knee angle) and bicep_curl (elbow angle).
     """
     objects = pose_data.get("objects", [])
     if not objects:
@@ -134,22 +134,33 @@ def _extract_angle_and_pose_score(pose_data: dict):
     if len(kpts) < 17:
         return None, score
 
-    l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
-    r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
-
-    if l_score > r_score:
-        hip, knee, ankle = kpts[11], kpts[13], kpts[15]
+    if exercise == "bicep_curl":
+        # Elbow angle: shoulder(5/6) - elbow(7/8) - wrist(9/10)
+        l_score = kpts[5][2] + kpts[7][2] + kpts[9][2]
+        r_score = kpts[6][2] + kpts[8][2] + kpts[10][2]
+        if l_score > r_score:
+            a, b, c = kpts[5], kpts[7], kpts[9]
+        else:
+            a, b, c = kpts[6], kpts[8], kpts[10]
     else:
-        hip, knee, ankle = kpts[12], kpts[14], kpts[16]
-
-    angle = _angle_3pts(hip[:2], knee[:2], ankle[:2])
+        # Knee angle: hip(11/12) - knee(13/14) - ankle(15/16)
+        l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
+        r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
+        if l_score > r_score:
+            a, b, c = kpts[11], kpts[13], kpts[15]
+        else:
+            a, b, c = kpts[12], kpts[14], kpts[16]
+
+    angle = _angle_3pts(a[:2], b[:2], c[:2])
     sym   = _symmetry_score(kpts)
     return angle, score, sym
 
 
-def _extract_emg(emg_data: dict):
-    """Returns (target_rms, comp_rms)."""
+def _extract_emg(emg_data: dict, exercise: str = "squat"):
+    """Returns (target_rms, comp_rms). Target muscle depends on exercise."""
     acts = emg_data.get("activations", {})
+    if exercise == "bicep_curl":
+        return acts.get("biceps", 0.0), acts.get("glutes", 0.0)
     return acts.get("glutes", 0.0), acts.get("biceps", 0.0)
 
 
@@ -173,13 +184,14 @@ def _amplitude_ok(angle_history: list) -> bool:
 # ---------------------------------------------------------------------------
 
 class DataCollector:
-    def __init__(self, mode: str, out_dir: str):
-        self.mode    = mode
-        self.out_dir = Path(out_dir)
+    def __init__(self, mode: str, out_dir: str, exercise: str = "squat"):
+        self.mode     = mode
+        self.exercise = exercise
+        self.out_dir  = Path(out_dir)
         self.out_dir.mkdir(parents=True, exist_ok=True)
 
         ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
-        self.out_path = self.out_dir / f"train_squat_{mode}_{ts_str}.csv"
+        self.out_path = self.out_dir / f"train_{exercise}_{mode}_{ts_str}.csv"
 
         self.rows: list[list] = []
 
@@ -254,8 +266,9 @@ class DataCollector:
     # ------------------------------------------------------------------
     def run(self):
         print(f"\nIronBuddy Data Collector")
-        print(f"  Mode   : {self.mode}")
-        print(f"  Output : {self.out_path}")
+        print(f"  Exercise: {self.exercise}")
+        print(f"  Mode    : {self.mode}")
+        print(f"  Output  : {self.out_path}")
         print(f"\nControls:  [s] start/resume   [p] pause   [q] quit & save\n")
 
         self._clear_shm_mode()
@@ -299,7 +312,7 @@ class DataCollector:
                         continue
 
                     # Person detection & angle
-                    result = _extract_angle_and_pose_score(pose_data)
+                    result = _extract_angle_and_pose_score(pose_data, self.exercise)
                     if result[0] is None:
                         score = result[1]
                         validity_msg = f"SKIP: no_person (score={score:.2f})"
@@ -326,7 +339,7 @@ class DataCollector:
                         continue
 
                     # --- feature computation ---
-                    target_rms, comp_rms = _extract_emg(emg_data)
+                    target_rms, comp_rms = _extract_emg(emg_data, self.exercise)
 
                     ang_vel   = angle - self._prev_angle
                     ang_accel = ang_vel - self._prev_ang_vel
@@ -381,6 +394,85 @@ class DataCollector:
 
         print(f"[OK] Saved {len(self.rows)} frames to {self.out_path}")
 
+    # ------------------------------------------------------------------
+    def run_auto(self, duration_sec):
+        """Non-interactive auto-record mode. No TTY required."""
+        print(f"\nIronBuddy Data Collector (AUTO MODE)")
+        print(f"  Exercise : {self.exercise}")
+        print(f"  Mode     : {self.mode}")
+        print(f"  Duration : {duration_sec}s")
+        print(f"  Output   : {self.out_path}")
+
+        self._clear_shm_mode()
+        self.recording = True
+        self._set_shm_mode()
+
+        t_start = time.monotonic()
+        print(f"\n[AUTO] Recording started...")
+
+        try:
+            while time.monotonic() - t_start < duration_sec:
+                try:
+                    pose_data, emg_data, pose_ts, emg_ts = self._read_shm()
+                except RuntimeError:
+                    time.sleep(POLL_INTERVAL)
+                    continue
+
+                if not _temporal_coherent(pose_ts, emg_ts):
+                    self.dropped += 1
+                    time.sleep(POLL_INTERVAL)
+                    continue
+
+                result = _extract_angle_and_pose_score(pose_data, self.exercise)
+                if result[0] is None:
+                    self.dropped += 1
+                    time.sleep(POLL_INTERVAL)
+                    continue
+
+                angle, pose_score, sym = result
+                self._angle_history.append(angle)
+                if len(self._angle_history) > 120:
+                    self._angle_history.pop(0)
+
+                target_rms, comp_rms = _extract_emg(emg_data, self.exercise)
+
+                ang_vel   = angle - self._prev_angle
+                ang_accel = ang_vel - self._prev_ang_vel
+                self._prev_ang_vel = ang_vel
+                self._prev_angle   = angle
+
+                phase_prog = self._compute_phase_progress(angle)
+                now = time.time()
+
+                row = [
+                    f"{now:.3f}",
+                    f"{ang_vel:.4f}",
+                    f"{angle:.4f}",
+                    f"{ang_accel:.4f}",
+                    f"{target_rms:.4f}",
+                    f"{comp_rms:.4f}",
+                    f"{sym:.4f}",
+                    f"{phase_prog:.4f}",
+                    f"{pose_score:.4f}",
+                    self.mode,
+                ]
+                self.rows.append(row)
+                self.accepted += 1
+
+                elapsed = time.monotonic() - t_start
+                remaining = duration_sec - elapsed
+                print(
+                    f"\r[AUTO] {self.accepted} frames  dropped={self.dropped}  "
+                    f"remaining={remaining:.0f}s  angle={angle:.0f}°",
+                    end="", flush=True,
+                )
+                time.sleep(POLL_INTERVAL)
+        finally:
+            self._clear_shm_mode()
+
+        print(f"\n[AUTO] Recording finished.")
+        self._save()
+
 
 # ---------------------------------------------------------------------------
 # Entry point
@@ -397,15 +489,31 @@ def _parse_args() -> argparse.Namespace:
         required=True,
         help="Label for the collected data",
     )
+    p.add_argument(
+        "--exercise",
+        choices=["squat", "bicep_curl"],
+        default="squat",
+        help="Exercise type (default: squat)",
+    )
     p.add_argument(
         "--out",
         default=".",
         help="Output directory for the CSV file (default: current dir)",
     )
+    p.add_argument(
+        "--auto",
+        type=int,
+        default=0,
+        metavar="SECONDS",
+        help="Auto-record for N seconds then save (no TTY needed)",
+    )
     return p.parse_args()
 
 
 if __name__ == "__main__":
     args = _parse_args()
-    collector = DataCollector(mode=args.mode, out_dir=args.out)
-    collector.run()
+    collector = DataCollector(mode=args.mode, out_dir=args.out, exercise=args.exercise)
+    if args.auto > 0:
+        collector.run_auto(args.auto)
+    else:
+        collector.run()
diff --git a/tools/dashboard.py b/tools/dashboard.py
new file mode 100644
index 0000000..aa403fc
--- /dev/null
+++ b/tools/dashboard.py
@@ -0,0 +1,363 @@
+#!/usr/bin/env python3
+"""
+IronBuddy 训练可视化面板 — Streamlit
+=====================================
+4 个标签页:
+  1. 数据探索   — 浏览CSV, 按标签筛选, 对比分布
+  2. 训练监控   — 读取TensorBoard日志, loss/acc曲线
+  3. 模型评估   — 加载模型, 混淆矩阵 + 分类报告
+  4. 实时推理   — 连接板端, 实时预测可视化
+
+启动:
+    streamlit run tools/dashboard.py
+"""
+import os
+import sys
+import glob
+import json
+import time
+from pathlib import Path
+
+import numpy as np
+import pandas as pd
+import streamlit as st
+import plotly.express as px
+import plotly.graph_objects as go
+
+_TOOLS_DIR  = Path(__file__).resolve().parent
+_ENGINE_DIR = _TOOLS_DIR.parent / "hardware_engine"
+_PROJECT    = _TOOLS_DIR.parent
+if str(_ENGINE_DIR) not in sys.path:
+    sys.path.insert(0, str(_ENGINE_DIR))
+
+# ---------------------------------------------------------------------------
+st.set_page_config(page_title="IronBuddy 训练面板", layout="wide")
+st.title("IronBuddy GRU 训练可视化面板")
+
+tab1, tab2, tab3, tab4 = st.tabs([
+    "1. 数据探索",
+    "2. 训练监控",
+    "3. 模型评估",
+    "4. 实时推理",
+])
+
+FEATURE_COLS = ["Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
+                "Symmetry_Score", "Phase_Progress"]
+FEAT_CN = {
+    "Ang_Vel": "角速度", "Angle": "关节角度", "Ang_Accel": "角加速度",
+    "Target_RMS": "目标肌肉EMG", "Comp_RMS": "代偿肌肉EMG",
+    "Symmetry_Score": "对称性", "Phase_Progress": "动作阶段",
+}
+CLASS_NAMES  = ["standard", "compensating", "non_standard"]
+CLASS_CN     = {"standard": "标准", "compensating": "代偿/偷懒", "non_standard": "错误"}
+LABEL_CN     = {"golden": "标准(golden)", "lazy": "偷懒(lazy)", "bad": "错误(bad)"}
+COLORS       = {"golden": "#22c55e", "lazy": "#f59e0b", "bad": "#ef4444",
+                "standard": "#22c55e", "compensating": "#f59e0b", "non_standard": "#ef4444"}
+
+
+def _load_csvs(data_dir):
+    pattern = os.path.join(data_dir, "**", "train_*_*.csv")
+    paths = sorted(glob.glob(pattern, recursive=True))
+    if not paths:
+        pattern = os.path.join(data_dir, "**", "*.csv")
+        paths = sorted(glob.glob(pattern, recursive=True))
+    frames = []
+    for p in paths:
+        try:
+            df = pd.read_csv(p)
+            df["_file"] = Path(p).name
+            frames.append(df)
+        except Exception:
+            pass
+    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
+
+
+# =========================================================================
+# 标签页 1: 数据探索
+# =========================================================================
+with tab1:
+    st.header("数据探索")
+    st.caption("采集完数据后，在这里检查质量、对比不同动作标签的分布差异")
+
+    data_dir = st.text_input(
+        "数据目录",
+        value=str(_PROJECT / "data"),
+        help="包含 train_*.csv 文件的目录"
+    )
+
+    if os.path.isdir(data_dir):
+        df = _load_csvs(data_dir)
+        if df.empty:
+            st.warning(f"在 {data_dir} 中未找到 CSV 文件")
+        else:
+            st.success(f"已加载 {len(df)} 帧数据，来自 {df['_file'].nunique()} 个文件")
+
+            labels = sorted(df["label"].dropna().unique())
+            label_opts = [LABEL_CN.get(l, l) for l in labels]
+            selected_display = st.multiselect("按标签筛选", label_opts, default=label_opts)
+            rev_map = {v: k for k, v in LABEL_CN.items()}
+            selected = [rev_map.get(s, s) for s in selected_display]
+            dff = df[df["label"].isin(selected)]
+
+            col1, col2, col3 = st.columns(3)
+            col1.metric("总帧数", f"{len(dff):,}")
+            col2.metric("文件数", dff["_file"].nunique())
+            col3.metric("标签", ", ".join(selected_display))
+
+            # 每个文件的摘要
+            st.subheader("各文件摘要")
+            summary = dff.groupby(["_file", "label"]).agg(
+                帧数=("Angle", "count"),
+                角度最小=("Angle", "min"),
+                角度最大=("Angle", "max"),
+                角度范围=("Angle", lambda x: x.max() - x.min()),
+            ).reset_index()
+            summary["label"] = summary["label"].map(LABEL_CN).fillna(summary["label"])
+            summary = summary.rename(columns={"_file": "文件名", "label": "标签"})
+            st.dataframe(summary, use_container_width=True)
+
+            # 特征分布
+            st.subheader("特征分布对比")
+            st.caption("观察不同标签的特征分布是否有明显差异 — 差异越大，模型越容易学到")
+            feat_options = [c for c in FEATURE_COLS if c in dff.columns]
+            feat = st.selectbox("选择特征", feat_options,
+                                format_func=lambda x: f"{FEAT_CN.get(x, x)} ({x})")
+            fig = px.histogram(dff, x=feat, color="label", barmode="overlay",
+                               color_discrete_map=COLORS, nbins=50, opacity=0.7,
+                               labels={"label": "标签", feat: FEAT_CN.get(feat, feat)})
+            st.plotly_chart(fig, use_container_width=True)
+
+            # 时间序列
+            st.subheader("时间序列波形")
+            st.caption("逐帧查看角度、速度等变化，确认数据是否包含完整的运动周期")
+            files = sorted(dff["_file"].unique())
+            sel_file = st.selectbox("选择文件", files)
+            df_file = dff[dff["_file"] == sel_file].reset_index(drop=True)
+
+            feats_to_plot = st.multiselect(
+                "要绘制的列",
+                [c for c in FEATURE_COLS if c in df_file.columns],
+                default=["Angle", "Ang_Vel"],
+                format_func=lambda x: f"{FEAT_CN.get(x, x)}"
+            )
+            if feats_to_plot:
+                fig2 = go.Figure()
+                for f in feats_to_plot:
+                    fig2.add_trace(go.Scatter(y=df_file[f], name=FEAT_CN.get(f, f), mode="lines"))
+                fig2.update_layout(xaxis_title="帧", yaxis_title="数值", height=400)
+                st.plotly_chart(fig2, use_container_width=True)
+
+            # 相关性热力图
+            st.subheader("特征相关性")
+            st.caption("看哪些特征高度相关（冗余）或独立（互补信息）")
+            numeric_cols = [c for c in FEATURE_COLS if c in dff.columns]
+            corr = dff[numeric_cols].corr()
+            corr_display = corr.rename(index=FEAT_CN, columns=FEAT_CN)
+            fig3 = px.imshow(corr_display, text_auto=".2f", color_continuous_scale="RdBu_r",
+                             zmin=-1, zmax=1)
+            st.plotly_chart(fig3, use_container_width=True)
+    else:
+        st.info(f"目录不存在: {data_dir}。请先采集数据。")
+
+
+# =========================================================================
+# 标签页 2: 训练监控
+# =========================================================================
+with tab2:
+    st.header("训练监控")
+    st.caption("训练时自动记录到 TensorBoard，在这里或独立终端查看")
+    st.code("tensorboard --logdir models/tb_logs", language="bash")
+
+    tb_dir = st.text_input("TensorBoard 日志目录", value=str(_PROJECT / "models" / "tb_logs"))
+
+    if os.path.isdir(tb_dir):
+        try:
+            from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
+            runs = sorted(glob.glob(os.path.join(tb_dir, "*")))
+            if runs:
+                sel_run = st.selectbox("训练轮次", [Path(r).name for r in runs])
+                ea = EventAccumulator(os.path.join(tb_dir, sel_run))
+                ea.Reload()
+
+                tags = ea.Tags().get("scalars", [])
+                if tags:
+                    tag = st.selectbox("指标", tags)
+                    events = ea.Scalars(tag)
+                    df_tb = pd.DataFrame([(e.step, e.value) for e in events],
+                                         columns=["epoch", "value"])
+                    fig = px.line(df_tb, x="epoch", y="value", title=tag,
+                                  labels={"epoch": "轮次", "value": "数值"})
+                    st.plotly_chart(fig, use_container_width=True)
+                else:
+                    st.warning("该轮次中无标量数据")
+            else:
+                st.warning("未找到训练记录")
+        except ImportError:
+            st.warning("需要安装 tensorboard: `pip install tensorboard`")
+    else:
+        st.info("尚无 TensorBoard 日志。请先训练模型。")
+
+
+# =========================================================================
+# 标签页 3: 模型评估
+# =========================================================================
+with tab3:
+    st.header("模型评估")
+    st.caption("加载训练好的模型，在全部数据上运行，生成混淆矩阵和分类报告")
+
+    model_path = st.text_input(
+        "模型文件 (.pt)",
+        value=str(_PROJECT / "models" / "extreme_fusion_gru.pt")
+    )
+    eval_data_dir = st.text_input(
+        "评估数据目录",
+        value=str(_PROJECT / "data"),
+        key="eval_data"
+    )
+
+    if st.button("开始评估") and os.path.isfile(model_path) and os.path.isdir(eval_data_dir):
+        with st.spinner("正在加载模型并运行评估..."):
+            try:
+                import torch
+                from cognitive.fusion_model import CompensationGRU, CLASS_NAMES as CN
+
+                model = CompensationGRU(input_size=7)
+                model.load_state_dict(torch.load(model_path, map_location="cpu"))
+                model.eval()
+
+                df_eval = _load_csvs(eval_data_dir)
+                if df_eval.empty:
+                    st.error("未找到数据")
+                else:
+                    all_preds, all_labels, all_sims = [], [], []
+
+                    for _, group in df_eval.groupby("_file"):
+                        if len(group) < 31:
+                            continue
+                        label_str = group["label"].iloc[0]
+                        label_idx = {"golden": 0, "lazy": 1, "bad": 2}.get(label_str)
+                        if label_idx is None:
+                            continue
+
+                        feats = group[FEATURE_COLS].values.astype(np.float32)
+                        feats[:, 1] /= 180.0
+                        feats[:, 2] = np.clip(feats[:, 2] / 10.0, -1, 1)
+                        feats[:, 3] /= 100.0
+                        feats[:, 4] /= 100.0
+
+                        for i in range(len(feats) - 30):
+                            window = feats[i:i+30]
+                            x = torch.tensor(window, dtype=torch.float32).unsqueeze(0)
+                            with torch.no_grad():
+                                sim, cls, _ = model(x)
+                            all_preds.append(int(cls.argmax(dim=1).item()))
+                            all_labels.append(label_idx)
+                            all_sims.append(float(sim[0, 0].item()))
+
+                    if not all_preds:
+                        st.error("数据不足，无法评估")
+                    else:
+                        from sklearn.metrics import confusion_matrix, classification_report
+                        import matplotlib.pyplot as plt
+
+                        cn_labels = [CLASS_CN.get(c, c) for c in CN]
+
+                        cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2])
+                        fig, ax = plt.subplots(figsize=(5, 4))
+                        ax.imshow(cm, cmap="Blues")
+                        for i in range(3):
+                            for j in range(3):
+                                ax.text(j, i, str(cm[i][j]), ha="center", va="center",
+                                        color="white" if cm[i][j] > cm.max()/2 else "black",
+                                        fontsize=14)
+                        ax.set_xticks([0, 1, 2]); ax.set_yticks([0, 1, 2])
+                        ax.set_xticklabels(cn_labels); ax.set_yticklabels(cn_labels)
+                        ax.set_xlabel("预测"); ax.set_ylabel("真实")
+                        ax.set_title("混淆矩阵")
+                        plt.tight_layout()
+                        st.pyplot(fig)
+
+                        report = classification_report(all_labels, all_preds, target_names=cn_labels)
+                        st.code(report)
+
+                        st.subheader("相似度分布")
+                        st.caption("三种标签的相似度直方图应尽量分开 — 重叠越少说明模型区分能力越强")
+                        sim_df = pd.DataFrame({
+                            "相似度": all_sims,
+                            "真实类别": [CLASS_CN.get(CN[l], CN[l]) for l in all_labels],
+                        })
+                        fig2 = px.histogram(sim_df, x="相似度", color="真实类别",
+                                            barmode="overlay", nbins=40, opacity=0.7,
+                                            color_discrete_map={CLASS_CN[k]: v for k, v in COLORS.items() if k in CLASS_CN})
+                        fig2.update_layout(xaxis_range=[0, 1])
+                        st.plotly_chart(fig2, use_container_width=True)
+
+                        st.subheader("各类别相似度统计")
+                        stats = sim_df.groupby("真实类别")["相似度"].describe()
+                        st.dataframe(stats)
+
+            except Exception as e:
+                st.error(f"评估失败: {e}")
+                import traceback
+                st.code(traceback.format_exc())
+    elif not os.path.isfile(model_path):
+        st.info("未找到训练模型。请先完成训练。")
+
+
+# =========================================================================
+# 标签页 4: 实时推理
+# =========================================================================
+with tab4:
+    st.header("实时推理监控")
+    st.caption("连接到板端运行中的系统，实时查看角度和模型推理结果")
+
+    board_ip = st.text_input("板端 IP", value="10.105.245.224")
+    board_port = st.number_input("端口", value=5000)
+    duration = st.slider("监控时长 (秒)", 10, 120, 30)
+
+    if st.button("开始监控"):
+        import requests
+        placeholder = st.empty()
+        chart_data = {"角度": [], "相似度x180": [], "帧": []}
+        frame_idx = 0
+
+        for _ in range(duration * 10):
+            try:
+                r = requests.get(f"http://{board_ip}:{board_port}/state_feed", timeout=1)
+                d = r.json()
+                frame_idx += 1
+                chart_data["帧"].append(frame_idx)
+                chart_data["角度"].append(d.get("angle", 0))
+                chart_data["相似度x180"].append(d.get("similarity", 0) * 180)
+
+                with placeholder.container():
+                    c1, c2, c3, c4, c5 = st.columns(5)
+                    ex = d.get("exercise", "?")
+                    c1.metric("运动", "深蹲" if ex == "squat" else "弯举" if ex == "bicep_curl" else ex)
+                    state_cn = {"STAND": "站立", "DESCENDING": "下蹲中", "BOTTOM": "蹲到底",
+                                "ASCENDING": "起身中", "NO_PERSON": "无人",
+                                "CURLING": "弯举中", "EXTENDING": "下放中", "TOP": "顶峰"}
+                    c2.metric("状态", state_cn.get(d.get("state", ""), d.get("state", "?")))
+                    c3.metric("角度", f"{d.get('angle', 0):.0f}")
+                    c4.metric("疲劳", f"{d.get('fatigue', 0):.0f}/1500")
+                    sim_val = d.get("similarity", None)
+                    c5.metric("相似度", f"{sim_val:.0%}" if sim_val else "无模型")
+
+                    df_live = pd.DataFrame(chart_data)
+                    if len(df_live) > 2:
+                        fig = go.Figure()
+                        fig.add_trace(go.Scatter(x=df_live["帧"], y=df_live["角度"],
+                                                 name="关节角度", line=dict(color="#3b82f6", width=2)))
+                        fig.add_trace(go.Scatter(x=df_live["帧"], y=df_live["相似度x180"],
+                                                 name="相似度(x180)", line=dict(color="#22c55e", width=2, dash="dot")))
+                        fig.update_layout(height=350, xaxis_title="帧",
+                                          yaxis_title="角度/相似度", legend=dict(orientation="h"))
+                        st.plotly_chart(fig, use_container_width=True)
+
+                time.sleep(0.1)
+            except Exception:
+                time.sleep(0.5)
+
+        st.success(f"监控结束 ({duration}秒)")
+    else:
+        st.info("点击「开始监控」连接板端实时数据流")
diff --git a/tools/train_model.py b/tools/train_model.py
index 7553517..9d29cba 100644
--- a/tools/train_model.py
+++ b/tools/train_model.py
@@ -34,6 +34,12 @@ import torch
 import torch.nn.functional as F
 from torch.utils.data import DataLoader, random_split
 
+try:
+    from torch.utils.tensorboard import SummaryWriter
+    HAS_TB = True
+except ImportError:
+    HAS_TB = False
+
 # ---------------------------------------------------------------------------
 # Locate the cognitive package relative to this script
 # ---------------------------------------------------------------------------
@@ -74,7 +80,7 @@ LABEL_GLOB_MAP = {
 # ---------------------------------------------------------------------------
 
 def _detect_label(csv_path: str) -> int | None:
-    """Infer label from filename convention: train_squat_<label>*.csv"""
+    """Infer label from filename convention: train_<exercise>_<label>*.csv"""
     name = Path(csv_path).name.lower()
     for keyword, label in LABEL_GLOB_MAP.items():
         if keyword in name:
@@ -84,14 +90,13 @@ def _detect_label(csv_path: str) -> int | None:
 
 def load_all_csvs(data_dir: str) -> list[tuple[pd.DataFrame, int]]:
     """
-    Scans data_dir recursively for CSV files matching train_squat_*.csv
-    and returns [(df, label), ...].
+    Scans data_dir recursively for CSV files matching train_*_*.csv
+    (supports both train_squat_*.csv and train_bicep_curl_*.csv).
     """
-    pattern = os.path.join(data_dir, "**", "train_squat_*.csv")
+    pattern = os.path.join(data_dir, "**", "train_*_*.csv")
     paths   = glob.glob(pattern, recursive=True)
 
     if not paths:
-        # also accept collect_training_data output names
         pattern2 = os.path.join(data_dir, "**", "*.csv")
         paths    = glob.glob(pattern2, recursive=True)
 
@@ -190,6 +195,12 @@ def train(
     out_path = Path(out_dir) / "extreme_fusion_gru.pt"
     Path(out_dir).mkdir(parents=True, exist_ok=True)
 
+    # TensorBoard
+    tb_dir = os.path.join(out_dir, "tb_logs", time.strftime("%Y%m%d_%H%M%S"))
+    writer = SummaryWriter(tb_dir) if HAS_TB else None
+    if writer:
+        print(f"TensorBoard: tensorboard --logdir {os.path.dirname(tb_dir)}")
+
     print(f"\nTraining {epochs} epochs...")
     print(f"{'Epoch':>5}  {'Train Loss':>10}  {'Train Acc':>9}  {'Val Loss':>8}  {'Val Acc':>7}  LR")
     print("-" * 70)
@@ -261,6 +272,14 @@ def train(
             f"{val_loss:8.4f}  {val_acc*100:6.1f}%  {cur_lr:.5f}"
         )
 
+        if writer:
+            writer.add_scalars("Loss", {"train": train_loss, "val": val_loss}, epoch)
+            writer.add_scalars("Accuracy", {"train": train_acc, "val": val_acc}, epoch)
+            writer.add_scalar("LR", cur_lr, epoch)
+            for cls_name, sim_vals in sim_sums.items():
+                if sim_vals:
+                    writer.add_histogram(f"Similarity/{cls_name}", np.array(sim_vals), epoch)
+
         if val_acc >= best_val_acc:
             best_val_acc   = val_acc
             best_val_epoch = epoch
@@ -288,6 +307,57 @@ def train(
             print(f"  {name:15s}: mean={arr.mean():.3f}  std={arr.std():.3f}  "
                   f"min={arr.min():.3f}  max={arr.max():.3f}")
 
+    # Confusion matrix + final TensorBoard logging
+    if writer:
+        try:
+            from sklearn.metrics import confusion_matrix, classification_report
+            import matplotlib
+            matplotlib.use("Agg")
+            import matplotlib.pyplot as plt
+
+            all_preds, all_labels = [], []
+            with torch.no_grad():
+                for x, y_cls in val_loader:
+                    x = x.to(device)
+                    _, cls_logits, _ = model(x)
+                    all_preds.extend(cls_logits.argmax(dim=1).cpu().numpy())
+                    all_labels.extend(y_cls.numpy())
+
+            cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2])
+            fig, ax = plt.subplots(figsize=(5, 4))
+            ax.imshow(cm, cmap="Blues")
+            for i in range(3):
+                for j in range(3):
+                    ax.text(j, i, str(cm[i][j]), ha="center", va="center",
+                            color="white" if cm[i][j] > cm.max()/2 else "black")
+            ax.set_xticks([0, 1, 2]); ax.set_yticks([0, 1, 2])
+            ax.set_xticklabels(CLASS_NAMES); ax.set_yticklabels(CLASS_NAMES)
+            ax.set_xlabel("Predicted"); ax.set_ylabel("True")
+            ax.set_title("Confusion Matrix")
+            plt.tight_layout()
+            writer.add_figure("ConfusionMatrix", fig, epochs)
+            plt.close(fig)
+
+            # Similarity distribution histograms
+            fig2, axes = plt.subplots(1, 3, figsize=(12, 3))
+            for idx, name in enumerate(CLASS_NAMES):
+                sims = all_sims.get(name, [])
+                if sims:
+                    axes[idx].hist(sims, bins=20, alpha=0.7, color=["#22c55e", "#f59e0b", "#ef4444"][idx])
+                axes[idx].set_title(name); axes[idx].set_xlim(0, 1)
+                axes[idx].set_xlabel("Similarity")
+            plt.tight_layout()
+            writer.add_figure("SimilarityDistribution", fig2, epochs)
+            plt.close(fig2)
+
+            report = classification_report(all_labels, all_preds, target_names=CLASS_NAMES)
+            writer.add_text("ClassificationReport", f"```\n{report}\n```", epochs)
+            print(f"\n{report}")
+        except ImportError:
+            print("[INFO] Install sklearn + matplotlib for confusion matrix in TensorBoard")
+
+        writer.close()
+
     size_kb = os.path.getsize(out_path) / 1024
     print(f"\nSaved: {out_path}  ({size_kb:.1f} KB)")
     if size_kb > 100:
@@ -304,7 +374,7 @@ def _parse_args() -> argparse.Namespace:
         description="IronBuddy GRU model training script",
         formatter_class=argparse.RawDescriptionHelpFormatter,
     )
-    p.add_argument("--data",   default=".", help="Directory containing train_squat_*.csv files")
+    p.add_argument("--data",   default=".", help="Directory containing train_*_*.csv files")
     p.add_argument("--out",    default=".", help="Output directory for the trained model")
     p.add_argument("--epochs", type=int,   default=DEFAULT_EPOCHS)
     p.add_argument("--batch",  type=int,   default=DEFAULT_BATCH)
diff --git a/tools/validate_data.py b/tools/validate_data.py
new file mode 100644
index 0000000..ef7cd67
--- /dev/null
+++ b/tools/validate_data.py
@@ -0,0 +1,77 @@
+#!/usr/bin/env python3
+"""
+Quick data quality checker — run after collection to verify datasets.
+Usage: python validate_data.py /path/to/training_data/
+"""
+import csv
+import sys
+import os
+from pathlib import Path
+
+MIN_FRAMES = 60    # 3 seconds at 20Hz
+MIN_ANGLE_RANGE = 15.0
+
+def check_file(path):
+    issues = []
+    with open(path) as f:
+        reader = csv.DictReader(f)
+        rows = list(reader)
+
+    n = len(rows)
+    if n < MIN_FRAMES:
+        issues.append(f"太少: {n} 帧 (最少 {MIN_FRAMES})")
+
+    if n == 0:
+        return n, issues
+
+    angles = [float(r["Angle"]) for r in rows if r.get("Angle")]
+    if angles:
+        rng = max(angles) - min(angles)
+        if rng < MIN_ANGLE_RANGE:
+            issues.append(f"角度范围仅 {rng:.1f}° (最少 {MIN_ANGLE_RANGE}°)")
+
+    emg_vals = [float(r["Target_RMS"]) for r in rows if r.get("Target_RMS")]
+    if emg_vals and max(emg_vals) < 1.0:
+        issues.append("EMG 全零 — 传感器可能未连接")
+
+    labels = set(r.get("label", "") for r in rows)
+    if len(labels) > 1:
+        issues.append(f"混合标签: {labels}")
+
+    return n, issues
+
+
+def main():
+    data_dir = sys.argv[1] if len(sys.argv) > 1 else "."
+    csvs = sorted(Path(data_dir).glob("train_*.csv"))
+
+    if not csvs:
+        print(f"在 {data_dir} 中未找到 train_*.csv 文件")
+        sys.exit(1)
+
+    print(f"\n{'文件':<50} {'帧数':>6}  状态")
+    print("-" * 75)
+
+    all_ok = True
+    total_frames = 0
+    for p in csvs:
+        n, issues = check_file(p)
+        total_frames += n
+        name = p.name
+        if issues:
+            all_ok = False
+            print(f"{name:<50} {n:>6}  ⚠ {'; '.join(issues)}")
+        else:
+            print(f"{name:<50} {n:>6}  ✅")
+
+    print("-" * 75)
+    print(f"{'合计':<50} {total_frames:>6}  {'全部通过 ✅' if all_ok else '有问题 ⚠'}")
+    print()
+
+    if all_ok:
+        print("可以开始训练:")
+        print(f"  python train_model.py --data {data_dir} --out ./models --epochs 25")
+
+
+if __name__ == "__main__":
+    main()
```
