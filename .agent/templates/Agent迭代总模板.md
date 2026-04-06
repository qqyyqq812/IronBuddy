# Agent 迭代总模板

> 每一代 Agent 切换时，必须阅读本模板。板块 A 为永久约束，禁止修改；板块 B 由当代 Agent 填写。

---

## 板块 A：用户永久偏好（🔒 每代必须遵守，禁止修改）

### 1. 沟通方式
- 使用 **Implementation Plan** 与用户沟通所有非平凡修改
- Plan 必须列出涉及的文件、修改内容、验证方法

### 2. 修改质量
- **一次修改必须改全所有相关代码**，不丢三落四，不留编译/运行时 bug
- 修改前完整理解调用链，确保上下游一致

### 2.5 板端 IP 变更检查清单
板端 IP 由手机热点 DHCP 分配，**每次变更时必须逐一更新以下文件**：

| 文件 | 位置 | 说明 |
|------|------|------|
| `tests/start_validation.sh` | `TARGET=` 行 | 启动脚本 |
| `tests/stop_validation.sh` | `TARGET=` 行 | 停止脚本 |
| `tests/host_stream_viewer.py` | 默认 IP 行 | 主机推流测试 |
| `README.md` | 项目根目录 | 架构图和访问地址 |
| `docs/presentations/User用户手册_项目控制与运行图鉴.md` | SSH 命令和访问地址 | 用户手册 |
| `docs/technical/Agent协作与握手区/Agent2完整上下文恢复文档.md` | 板端 IP 和 SSH 命令 | 状态黑板 |

> ⚠️ **代码文件无需改**：`streamer_app.py` 绑定 `0.0.0.0`，`openclaw_bridge.py` 使用 `127.0.0.1`（通过 SSH 隧道），均与板端 IP 无关。

### 2.6 板端连接法定通道
**唯一合法连接方式**：通过 PowerShell 使用密钥 SSH 连接，**禁止乱尝试其他方式**。
```
ssh -i C:\temp\id_rsa -R 18789:127.0.0.1:18789 -o StrictHostKeyChecking=no toybrick@<板端IP>
```
- `-R 18789` 反向隧道用于 DeepSeek/OpenClaw 通信，**必须带上**
- ICMP ping 被手机热点屏蔽，**ping 超时不代表板子离线**，直接 SSH 即可
- 密钥路径固定：Windows `C:\temp\id_rsa`，WSL `~/.ssh/id_rsa_toybrick`

### 3. 定时总结
- 每完成一个工作阶段，在 `docs/presentations/drafts/` 对应章节 draft 中同步更新
- 如果对应章节 draft 不存在，先创建再更新

### 4. 文件夹规则
- 严格遵守 `docs/presentations/README.md` 中定义的所有规则
- draft 文件夹是各模块的权威草稿，内容必须基于具体代码，不凭空捏造
- 所有 `.md` 文件使用中文命名

### 5. 使用 Skills
- 写作使用 `academic-writing-cs` / `docs-writer` skill
- 计划使用 `writing-plans` skill
- 创意工作使用 `brainstorming` skill

### 6. 目的导向
- 最终目标是**展示**（Marp 汇报），所有工作以此为导向
- 完成代码修改后，必须告知用户：
  - 测试流程（具体命令）
  - 期待结果（具体现象）
  - Bug 反馈模板（见下方）

### 7. Bug 反馈模板
当告知用户测试时，附上以下模板供用户反馈：
```
**问题描述**：（一句话说明现象）
**复现步骤**：
1. ...
2. ...
**终端输出**：（粘贴关键错误行）
**截图**：（如有）
```

---

## 板块 B：当代 Agent 填写区（⚡ 每次换代更新此区域）

| 字段 | 值 |
|------|-----|
| **当前版本** | V2.1 |
| **接手时间** | 2026-03-24 10:49 |
| **Agent 编号** | Agent 2（第二代） |
| **接手来源** | `Agent2完整上下文恢复文档.md` |

### 当前遗留问题（继承）

| 优先级 | 问题 | 状态 |
|--------|------|------|
| P0 | `voice_daemon.py` 唤醒词不触发 | ✅ 已修复（移除失效代理 + 阈值调优） |
| P0+ | 语音 ASR 依赖外网（架构性短板） | ✅ V2.2 已替换为 Vosk 离线 ASR |
| P1 | 累积速率调优（primary 15→8） | ✅ 已修改并上板验证 |
| P2 | Marp 汇报 §8 更新 | ✅ 已撰写 3 页 |
| P3 | 深蹲判定精度（2D 投影失真） | ✅ V2.2 已用 3D 角度反馈 FSM |

### 本代已完成工作

**Phase 1: 基础治理**
- [x] Technical 目录治理：清理散落文件、标注活跃区/冻结区
- [x] 创建 Agent 迭代总模板（本文件）
- [x] 更新项目宏观任务看板（V2 Phase 1-3 标记完成）
- [x] 全项目 IP 统一：`172.19.98.224` → `10.28.134.224`
- [x] 添加 IP 变更检查清单和连接法定通道

**Phase 2: V2 核心修复（P0-P2）**
- [x] P0 语音守护修复：移除失效代理 + 阈值 300→150 + ASR 5s 超时
- [x] P1 累积速率调优：`muscle_model.py` primary 15→8
- [x] P2 Marp §8 撰写：3 页 V2 热力图内容
- [x] V2 全链路上板测试：18 标准 + 11 违规，NPU 37 FPS
- [x] 补齐全 8 章 Draft（新建第 1、6 章）
- [x] 项目深度反思与多阶段迭代规划

**Phase 3: V2.2 迭代升级**
- [x] T1: Vosk 离线 ASR 替换 Google ASR（重写 `voice_daemon.py`）
- [x] T2: 3D 角度反馈 FSM（`main_claw_loop.py` +12 行）
- [x] T3: MJPEG 流替换 JPEG 轮询（`streamer_app.py` +34 行 / `index.html` -44+13 行）
- [x] T4: 富 Prompt + 历史注入 + 肌肉激活数据（+37 行）
- [x] T5: Performance benchmark 脚本（`tests/benchmark.py`）
- [x] Vosk 模型板端部署脚本（`scripts/deploy_vosk.sh`）

### 下一步计划（V2.5 阶段）

~~1. 上板部署 V2.2 全部改动 + Vosk 模型~~
~~2. 运行 benchmark 收集第一份性能基线数据~~
~~3. 更新 Marp 展示（加入 V2.2 Vosk+3D 技术亮点）~~
~~4. 更新 §6 Draft（Vosk 离线 ASR 实现细节）~~
~~5. supervisord 进程守护配置~~

**Phase 4: V2.5 迭代（已完成）**
- [x] T1: 训练历史页面（`history.html` + Chart.js + `/api/training_log`）
- [x] T2: 教练人格 SOUL.md + prompt 注入
- [x] T3: supervisord 进程守护配置
- [x] T4: 用户手册更新（V2.2/V2.5 新功能入口）
- [x] 更新 Marp 展示 V2.2 亮点页
- [x] 更新 §6 Draft Vosk 实现细节

### 下一步计划（V3.0 阶段，待用户上板验证后启动）

1. 上板部署并实测 V2.2+V2.5 全部改动
2. 运行 `benchmark.py` 收集性能数据
3. 动作插件化架构（支持俯卧撑/仰卧起坐）
4. WebSocket 替换 HTTP 轮询（state_feed / muscle_activation）
5. SVG 人体轮廓升级（解剖学风格）
