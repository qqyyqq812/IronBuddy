# 第 8 章：V2 — 肌肉激活热力图与 3D 生物力学引擎

> 本章记录 IronBuddy 从 V1（纯 2D 深蹲计数）升级到 V2（3D 姿态估计 + 生物力学建模 + 实时肌肉激活热力图）的完整技术路径。所有内容均基于实际代码和板端实测数据。

---

## 8.1 V2 升级动机与技术挑战

### V1 的局限
V1 仅利用 2D 关键点计算膝盖角度，只能判定"蹲到底"或"半蹲"。用户无法得知：
- 哪些肌肉群在训练中被激活
- 训练强度是否达标
- 是否存在肌肉代偿（如用腰代替腿部发力）

### V2 目标
引入 **视觉-生物力学混合管线**：
```
摄像头 → NPU 2D检测(YOLOv5-Pose) → 2D→3D Lifting(VideoPose3D) → 关节角度计算 → 肌肉激活估算 → 前端SVG热力图
```

### 核心挑战
1. **纯视觉方法无法获得体内生理数据**（体脂率、EMG 等），只能通过运动学+文献力矩臂数据做定性估算
2. **板端 CPU 资源有限**（RK3399ProX A72×2 @1.8GHz），VideoPose3D 推理 ~350-465ms
3. **用户参数补偿**：不同体重/身高/器材重量对肌肉负荷的影响需纳入计算

---

## 8.2 技术选型与决策

### 2D→3D Lifting 模型选择

| 候选方案 | 优劣 | 决策 |
|---------|------|------|
| **VideoPose3D** (Facebook Research) | ✅ 轻量（16.9M参数）、预训练权重公开（Human3.6M）、causal 模式支持实时 | **采用** |
| MAGNET / RT-MAGNET | ❌ 论文发表但代码未开源 | 暂不考虑 |
| MusclePose | ❌ 未找到可用预训练权重 | 暂不考虑 |

**关键决策**：保留 V1 的 **YOLOv5-Pose (NPU)** 作 2D 检测，新增 **VideoPose3D (CPU)** 做 2D→3D lifting。不冲突，不修改 NPU 管线。

### 部署策略
- **ONNX Runtime CPU 部署**：将 PyTorch 权重导出为 ONNX（opset 13），板端使用 `onnxruntime 1.14.1` 推理
- **降频调用**：每 5 帧跑一次 3D lifting（~3Hz @15fps 输入），将 ~465ms 推理延迟隐藏在帧间隔中
- **causal 模式**：使用因果卷积（非对称 padding），输入最近 243 帧的 2D 关键点，输出最新帧的 3D 坐标

---

## 8.3 VideoPose3D ONNX 导出与板端验证

### 模型架构
```
TemporalModel(
    num_joints_in=17, in_features=2,
    num_joints_out=17,
    filter_widths=[3,3,3,3,3],  # receptive field = 243 帧
    causal=True,                 # 因果模式 → 实时
    channels=1024,
    dropout=0.0                  # 推理时关闭
)
```

### PyTorch → ONNX 导出
```python
# 核心导出命令（/tmp/export_videopose3d_onnx.py）
torch.onnx.export(
    model, dummy_input, ONNX_PATH,
    opset_version=13,
    input_names=['input_2d'],     # shape: [1, 243, 17, 2]
    output_names=['output_3d'],   # shape: [1, 1, 17, 3]
    dynamo=False                  # legacy exporter 兼容 aarch64
)
```

### 验证数据

| 指标 | WSL x86_64 | 板端 RK3399ProX (aarch64) |
|------|-----------|--------------------------|
| ONNX 文件大小 | 64.6 MB | 同 |
| PyTorch vs ORT 最大差异 | 0.000003 | — |
| 推理延迟 | **23.2 ms** | **348-465 ms** |
| onnxruntime 版本 | 1.24.4 | 1.14.1 |

---

## 8.4 生物力学引擎实现

引擎由三个模块组成，全部位于 `biomechanics/` 目录：

### 8.4.1 关节角度计算器 `joint_calculator.py`

输入：17 个 3D 关键点 `(x, y, z)`

输出：8 个关节的角度（°）和角速度（°/s）

```python
# 计算的 8 个关节角度
joints = {
    'l_knee':     (hip_l, knee_l, ankle_l),
    'r_knee':     (hip_r, knee_r, ankle_r),
    'l_hip':      (shoulder_l, hip_l, knee_l),
    'r_hip':      (shoulder_r, hip_r, knee_r),
    'l_elbow':    (shoulder_l, elbow_l, wrist_l),
    'r_elbow':    (shoulder_r, elbow_r, wrist_r),
    'l_shoulder': (hip_l, shoulder_l, elbow_l),
    'r_shoulder': (hip_r, shoulder_r, elbow_r),
}
```

角速度通过帧间角度差分计算：`ω = Δθ / Δt`（fps=15）。

### 8.4.2 肌肉激活模型 `muscle_model.py`（V2.1 累积模式）

**V2.0 问题**：每帧独立计算激活值 → 数值持续跳变，无训练累积概念。

**V2.1 解决方案**：改为 **累积模式**
- FSM 每检测到完成一次动作 → 调用 `on_rep_completed(is_good)`
- 标准动作：主动肌 +8%，协同肌 +5%，稳定肌 +3%
- 违规动作：主动肌 +3%，协同肌 +2%，稳定肌 +1%
- 重置按钮：调用 `reset_set()` 归零
- 闪烁反馈：每完成一次动作，主动肌区域白色闪烁 0.8 秒

```python
# 关键累积逻辑（V2.1 调优后）
_REP_WEIGHTS_GOOD = {'primary': 8, 'synergist': 5, 'stabilizer': 3}
_REP_WEIGHTS_BAD  = {'primary': 3, 'synergist': 2, 'stabilizer': 1}

def on_rep_completed(self, is_good):
    weights = self._REP_WEIGHTS_GOOD if is_good else self._REP_WEIGHTS_BAD
    for m, info in profile['muscles'].items():
        self._cumulative[m] = min(100, self._cumulative[m] + weights[info['role']])
```

### 8.4.3 力矩臂查找表 `moment_arm_tables.json`

数据来源：
- **Buford (1997)**：上肢肌肉力矩臂
- **Dostal (1981)**：髋关节肌群力矩臂
- **Ackland (2008)**：肩关节肌群力矩臂
- **Winter 人体测量学**：身高→体节长度比例

### 8.4.4 动作-肌群映射 `exercise_profiles.json`

V2 支持两种动作：

| 动作 | 主动肌 | 协同肌 | 稳定肌 |
|------|--------|--------|--------|
| 深蹲 (squat) | 股四头肌、臀大肌 | 腘绳肌、小腿肌、竖脊肌 | 腹肌、髋内收肌 |
| 哑铃弯举 (bicep_curl) | 肱二头肌 | 肱肌、肱桡肌 | 三角肌前束、前臂屈肌 |

代偿检测：当稳定肌激活 > 主动肌 50% 时触发警告（如弯举时斜方肌过度用力）。

---

## 8.5 前端热力图渲染

### SVG 人体轮廓
使用 16 个 `<rect>` 元素组成人体正面轮廓，每个矩形对应一个肌肉区域。左右对称肌肉（股四头肌-L/R、肱二头肌-L/R 等）同步着色。

### 颜色映射（累积渐变）
```
0%  → 灰色  rgba(60,70,90,0.15)     "未激活"
15% → 钢蓝  rgba(100,120,160,0.3)   "轻微激活"
30% → 蓝色  rgba(96,165,250,0.5)    "初步激活"
50% → 绿色  rgba(52,211,153,0.6)    "中度激活"
70% → 黄色  rgba(250,204,21,0.7)    "显著激活"
85% → 橙色  rgba(251,146,60,0.8)    "高度激活"
100%→ 红色  rgba(239,68,68,0.9)     "满载"
```

### 闪烁反馈
每完成一次动作，主动肌区域短暂切为白色 `rgba(255,255,255,0.9)` 持续 0.8 秒，CSS transition 为 0.1 秒快速闪入 + 0.6 秒缓慢恢复。

---

## 8.6 板端实测数据

### 测试条件
- 板端：RK3399ProX + USB HD 720P Webcam (`/dev/video5`)
- 动作：深蹲，用户参数 175cm/70kg
- 测试次数：14 标准 + 7 违规 = 21 次

### 实测结果

| 肌群 | 21 次后激活% | 角色 | 符合预期 |
|------|-------------|------|---------|
| 股四头肌 | 100% | 主动肌 | ✅ |
| 臀大肌 | 100% | 主动肌 | ✅ |
| 腘绳肌 | 100% | 协同肌 | ✅ |
| 竖脊肌 | 100% | 协同肌 | ✅ |
| 小腿肌 | 100% | 协同肌 | ✅ |
| 腹肌 | 49% | 稳定肌 | ✅ |
| 髋内收肌 | — | 稳定肌 | ✅ |
| 上肢 (7 肌群) | 0% | 不参与 | ✅ |

### 性能指标

| 指标 | 值 |
|------|---|
| 3D Lifting 延迟 | ~465 ms (CPU, 每 5 帧一次) |
| 帧积累启动时间 | ~16 秒 (243/15fps) |
| JPEG 帧大小 | ~30 KB |
| 前端轮询间隔 | 800 ms |

---

## 8.7 已知问题与待优化

### 已知问题
1. **DeepSeek Gateway 连接**：SSH 反向隧道 (`-R 18789`) 需要保持长连接，一次性 SSH 命令结束后隧道即断。需通过 `autossh` 或 `start_validation.sh` 脚本保持持久隧道
2. **累积速度已调优**：✅ 已将 `_REP_WEIGHTS_GOOD.primary` 从 15 降至 8，标准深蹲约 13 次达 100%（原 7 次）
3. **3D 可视化**：当前 3D lifting 的坐标不在视频画面上显示（C++ NPU 引擎只画 2D 骨骼），可考虑在前端增加 Canvas 3D 骨骼线框

### 后续规划
- **NPU 量化**：将 VideoPose3D ONNX 转为 RKNN INT8 在 NPU 推理（目标 < 50ms）
- **更多动作支持**：卧推、硬拉、引体向上
- **训练总结**：多组累积后生成训练报告（总激活量、代偿次数、进步趋势）

---

## 8.8 V2 系统架构总览

```
┌─────────────── 板端 (RK3399ProX) ───────────────┐
│                                                   │
│  USB 摄像头 ──→ NPU C++ 引擎 (YOLOv5-Pose)       │
│                  ↓ /dev/shm/pose_data.json        │
│              main_claw_loop.py ←─── FSM 状态机     │
│                  ↓ 每5帧                           │
│       ┌── Lifting3D (ONNX CPU, 465ms)            │
│       │     ↓ 17×(x,y,z)                         │
│       ├── JointCalculator (8 关节角度)             │
│       │     ↓                                     │
│       └── MuscleModel (13 肌群累积激活)            │
│              ↓ /dev/shm/muscle_activation.json     │
│       streamer_app.py (Flask, port 5000)          │
│              ↓ HTTP API                           │
│       index.html (SVG 热力图 + 训练仪表盘)        │
│                                                   │
│  ←── SSH 反向隧道 18789 ──→                       │
│       OpenClaw Gateway → DeepSeek API (教练点评)   │
└───────────────────────────────────────────────────┘
```

---

## 附：关键文件清单

| 文件 | 位置 | 行数 | 描述 |
|------|------|------|------|
| `lifting_3d.py` | `biomechanics/` | 143 | VideoPose3D ONNX 推理封装（帧缓冲+延迟加载） |
| `joint_calculator.py` | `biomechanics/` | 88 | 8 关节 3D 角度 + 角速度计算 |
| `muscle_model.py` | `biomechanics/` | ~230 | V2.1 累积式 13 肌群激活估算 |
| `moment_arm_tables.json` | `biomechanics/` | 43 | 文献力矩臂数据 |
| `exercise_profiles.json` | `biomechanics/` | 27 | 动作→肌群角色映射 |
| `main_claw_loop.py` | `hardware_engine/` | ~510 | 主循环含 FSM + V2 管线集成 |
| `streamer_app.py` | 项目根目录 | 213 | Flask 推流中台（含 `/api/muscle_activation`） |
| `index.html` | `templates/` | ~875 | 前端仪表盘含 SVG 热力图 |
| `videopose3d_243f_causal.onnx` | `biomechanics/checkpoints/` | 64.6MB | 预训练权重 |
