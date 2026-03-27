# IronBuddy V3.x 硬件传感器融合扩展计划 (Implementation Plan)

## 🎯 [Goal Description]
当前 IronBuddy 高度依赖纯视觉（RGB 摄像头 + NPU 2D提取 + 拟合 3D），在复杂遮挡、动作绝对深度和**真实肌肉发力判断**上存在物理上限。本计划采用 **极客开源流（方案A）**，引入超低成本的**穿戴式贴片传感器（IMU 惯性 + sEMG 表面肌电）**，构建真正的**视-感融合（Sensor-Fusion）生物力学引擎**。

---

## 🔬 一、 技术解析：视觉与贴片的互补矩阵

| 维度 | 视觉方案 (当前) | 极客贴片方案 (新增) | 融合后收益 |
|------|-----------------|---------------------|------------|
| **Z轴位移** | 弱（基于2D投影推算） | 强（9轴 IMU 绝对倾角与加速度） | 彻底解决下蹲视角导致的**深度判定不准**问题 |
| **肌肉激活** | 伪推算（根据关节角度猜测） | 真测量（sEMG 捕捉肌肉放电信号） | 从“看起来在发力”升级为**“真正检测到肌肉泵血”** |
| **高频震颤** | 无（帧率上限 15-30fps） | 有（IMU 采样率可达 100-200Hz） | 检测力竭边缘的**肌肉颤抖（疲劳特征）**，提前预警 |
| **遮挡抗性** | 弱（转身或器械遮挡即跟丢） | 强（穿透物理遮挡） | 允许背对屏幕、使用复杂器械进行训练 |

---

## 🛒 二、 详细 BOM (物料清单) 与采购搜索词

秉承成本极小化与可控性最大化的原则，将硬件贴片分为两个独立节点：**左腿姿态节点**（纯 IMU，重防遮挡）和 **右臂肌电节点**（EMG+IMU，重发力检测）。

### 2.1 姿态节点 (纯 IMU，用于代替被挡住的深蹲判定)
> **目标**：不焊线，开箱即用，通过蓝牙直接输出 Pitch/Roll 倾角和加速度。

- **推荐硬件 A1 (成品模块)**：维特智能 (WitMotion) 蓝牙 5.0 姿态传感器。
  - **淘宝搜索词**：`维特智能 蓝牙5.0 加速度计 陀螺仪 BWT61CL` 或 `WT901BLE`。
  - **参考成本**：¥60 - ¥120 / 个（带内置锂电池和极小外壳）。
  - **优势**：官方有开源的 Python HEX 协议解析库，免折腾底层卡尔曼滤波，输出自带去噪。

- **推荐硬件 A2 (硬核自制)**：合宙 ESP32-C3 极简版 + MPU6050 模块 + 超小软包锂电（需自行焊接）。
  - **淘宝搜索词**：`合宙 esp32c3 经典版`、`MPU6050 陀螺仪模块`、`3.7V 软包锂电池 200mah`。
  - **参考成本**：约 ¥30 / 个。
  - **优势**：成本白菜价，代码完全自我掌控（Arduino 写 GATT Server）。

### 2.2 肌肉节点 (sEMG + IMU，用于真实肌电捕捉)
> **目标**：能捕获真实的 EMG 毫伏放电，并同时反馈手臂/肢体运动。

- **推荐硬件 A3 (开源神器)**：uMyo 表面肌电贴片传感器（内置无线和 6 轴）。
  - **淘宝/原站搜索关键词**：国内平台可搜 `开源 OYMotion 肌电`（傲意提供的开源件），或检索 `uMyo EMG 蓝牙 肌肉传感器 开发板`。如果有渠道，可以直接在 Tindie 上购入原版 uMyo。
  - **备选自制 (要求略高)**：`ESP32` + `Muse/MyoWare 肌电采集模块` (搜索：`MyoWare 肌电传感器 AT-04`)。
  - **参考成本**：¥150 - ¥300。
  - **优势**：完美获取原始 ADC 值，直观显示目标肌肉有没有充血。

---

## ⚙️ 三、 软件架构融合 (Proposed Architecture)

基于 RK3399ProX 开发板的 Linux 原生蓝牙栈（BlueZ），打通新旁路。

### 1. 采集守护进程 (Bluetooth Worker)
#### [NEW] `hardware_engine/sensor/ble_wearable.py`
使用 Python 的 `bleak` 库，独立于 FSM 运行：
```python
# 伪代码：蓝牙低功耗并行抓取节点
async def connect_sensor(mac, char_uuid):
    async with BleakClient(mac) as client:
        await client.start_notify(char_uuid, notification_handler)

def notification_handler(sender, data):
    # 将收到的 16 进制协议字节转为物理量
    # { "acc": [x, y, z], "gyro": [p, r, y], "emg": 1024, "ts": ... }
    write_to_dev_shm("/dev/shm/sensor_data.json", data)
```

### 2. 生物力学引擎升级
#### [MODIFY] `hardware_engine/biomechanics/muscle_model.py`
将之前的线性增加（做一次增加固定的10%）替换为：
**最终激活度 (0-100%) = (视觉评估标准分 [0.4]) + (IMU离心向心速度比得分 [0.3]) + (肌电真实 mV 强度分 [0.3])**，实现三合一精准打分。

### 3. FSM 引入卡尔曼滤波信任仲裁
#### [MODIFY] `hardware_engine/main_claw_loop.py`
在 `update()` 中加入**遮挡兜底逻辑**：
```python
# 当画面中人被哑铃挡住，或侧身只剩一条腿时：
if kpts[KNEE].confidence < 0.5:
    # 启用蓝牙传感器兜底
    knee_angle = get_from_shm("/dev/shm/sensor_data.json")["pitch"]
    # 甚至检测到 gyro 超高频抖动，输入 LLM “正在力竭打颤”
```

---

## 🧪 四、 实施进度与研发路径 (Verification Plan)

**Phase 1: 硬件采购与盲测 (3天)**
- [ ] 采购一块成品蓝牙 IMU（WitMotion 或自配 ESP32+MPU6050），加一块绑带。
- [ ] 编写并运行 `tests/ble_scanner.py`，确认 RK3399ProX 开发板可通过 `bleak` 稳定扫描到该 MAC 并建立 GATT 连接。

**Phase 2: 数据对齐与解析 (4天)**
- [ ] 编写 `ble_wearable.py` 解析 20Hz~50Hz 的 HEX 字节包，转为标准俯仰角 (Pitch)。
- [ ] 开发板实时将时间戳写入 `/dev/shm/`。

**Phase 3: 视觉与传感器融合重构 (5天)**
- [ ] 修改 FSM：当 NPU 提取的膝盖置信度 `< 0.7` 或深度特征失效时，让 `SquatStateMachine` 无缝采纳大腿贴片的 IMU Pitch 角判定蹲下趋势。
- [ ] 当检测到 IMU 中的 Gyro 震颤特征时，向 FSM 塞入 `fsm.muscle_fatigue = True`，交由 DeepSeek 做感性点评。
