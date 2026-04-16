# User 操作速查手册

本手册为 IronBuddy 系统的操作指南。所有服务的启动/停止已封装为自动化脚本。

> ⚠️ 板子 IP 不固定（DHCP），每次启动前在手机热点页面确认！当前：`10.105.245.224`

---

## 一、自动化启停（推荐）

### 启动全面服务 (板端全链路拉起)
```bash
tclsh ~/projects/embedded-fullstack/start_validation.tcl
```
*(注意：此命令将切断旧有进程，拉起 FSM、视觉后端以及最新的 V3 肌电 UDP 接收海关。)*

### 停止全部服务
```bash
tclsh ~/projects/embedded-fullstack/stop_validation.tcl
```

### ✅ [V3专属] PC 侧局域网物理欺骗启动 (无实体传感器外接时使用)
当你身边没有队友开发的真实传感器时，可以在 WSL 中另开一个终端执行：
```bash
python3 ~/projects/embedded-fullstack/tests/mock_teammate_esp32.py
```
*(该命令将在跨网段直接向板端的 UDP 接收器倾泻逼真的深蹲肌肉包络数据以及代偿干扰流，让板端以为已外接了传感器，配合前端观察 FSM 的积分跳变)*

---

## 二、手动基础操作

### 进入 Windows PowerShell
```bash
powershell.exe
```

### 手动 SSH 登入开发板（带通信隧道）
在 PowerShell 中执行：
```powershell
ssh -i C:\temp\id_rsa -R 18789:127.0.0.1:18789 -o StrictHostKeyChecking=no toybrick@10.123.123.224
```

### 打开前端监控面板
浏览器访问：`http://10.105.245.224:5000/`

---

## 三、音频硬件与底层寄存器调控 (救急/排雷 SOP)

在板端运行过程中，如果外放声音被系统限幅抹除，或是麦克风突然失聪，可以通过以下终端救雷指令强制重置硬件增益配置：

### 🔊 扬声器 (外放) 音量拉满
如果你觉得教练的声音太小（低于 50% 甚至被改为了 1%），用这段命令直接注射鸡血：
```bash
# 强制解限 ALSA 物理主扬声器音量到 100%
amixer -c 0 sset 'Master' 100% unmute
amixer -c 0 sset 'Playback Path' 'SPK'
```

### 🎤 麦克风 (拾音) 灵敏度拉满
如果你发现你的外接 USB Webcam（硬件代号 2）收音能量低于 15，像死锁了一样，请用以下指令重启它的拾音放大器：
*(之前我在远程协助你操作的“摄像头板卡音量”就是这个指令。它的作用是让麦克风对你远距离呼喊“教练”的声音更敏感，防止拾音距离过短)*
```bash
# 锁定序号 2 的声卡（通常是拔插的 USB 摄像头），将它的麦克风收录敏锐度拉到极值
amixer -c 2 sset 'Mic' 100% unmute
```

---

## 四、离线网线抢修 SOP（板端断网恢复）

当板端无法连接手机热点时，使用以下流程通过网线恢复控制：

### 步骤 1：建立有线连接
1. `Win+R` → 输入 `ncpa.cpl` → 找到已联网的网卡（如 WiFi），右键 → 属性 → 共享
2. 勾选"允许其他网络用户通过此计算机的 Internet 连接来连接"
3. 在下拉菜单中选择连接开发板的有线网卡
4. 用网线将板端与电脑直连，给板端上电

### 步骤 2：寻找板端 IP
等待约 60 秒后，在 PowerShell 中执行：
```powershell
arp -a
```
在 `192.168.137.x` 网段下找到新出现的 IP（排除 `.1` 网关和 `.255` 广播地址）。

### 步骤 3：SSH 登入并恢复热点
```powershell
ssh -i C:\temp\id_rsa toybrick@192.168.137.x
```
登入后重新连接手机热点：
```bash
# 扫描可用 WiFi
nmcli dev wifi list

# 连接手机热点（替换为实际热点名和密码）
nmcli dev wifi connect "你的热点名称" password "你的热点密码"

# 确认连接成功并记录新 IP
ip addr show wlan0
```

### 步骤 4：切换回无线开发模式
1. 宿主机也连接同一手机热点
2. 在 PowerShell 中用新 IP 重新 SSH 登入
3. 拔掉网线，恢复无线开发状态

> 如果 `nmcli` 无效（NetworkManager 已被禁用），使用 `wpa_supplicant` 方式：
> ```bash
> wpa_passphrase "热点名称" "密码" > /etc/wpa_supplicant/abc.conf
> sudo wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/abc.conf
> sleep 8
> sudo dhclient wlan0
> ```

---

## 四、V2.2/V2.5 新功能

### 训练历史页面
浏览器访问：`http://10.105.245.224:5000/history`
- 每日标准/违规趋势折线图（最近 14 天）
- 28 天训练热力日历
- 训练记录详情列表

### 语音对话（V2.2 离线 ASR）
- 对摄像头说 **"教练"** 即可唤醒
- 首次部署需运行 Vosk 模型安装：
  ```bash
  ssh toybrick@10.105.245.224 "bash /home/toybrick/scripts/deploy_vosk.sh"
  ```
- V2.2 后**完全离线运行**，无需外网

### 视频流
- V2.2 起使用 MJPEG 流，浏览器自动解码
- 如果画面不出现，手动访问 `http://10.105.245.224:5000/video_feed` 检查

### 性能测试
```bash
ssh toybrick@10.105.245.224 "python3 /home/toybrick/tests/benchmark.py"
```

### supervisord 进程守护（V2.5）
```bash
# 一键启动（替代 start_validation.sh 的进程管理部分）
ssh toybrick@10.105.245.224 "supervisord -n -c /home/toybrick/deploy/ironbuddy-supervisor.conf &"
# 查看状态
ssh toybrick@10.105.245.224 "supervisorctl -c /home/toybrick/deploy/ironbuddy-supervisor.conf status"
```

---

## 五、V3.1 管理面板 (Admin Console)

V3.1 新增了 Web 管理面板，可以在浏览器中管理整个 IronBuddy 系统。

### 访问方式

**本地开发机（推荐）**：
```bash
cd ~/projects/embedded-fullstack
python3 streamer_app.py
# 浏览器打开 http://localhost:5000/admin
```

**板端运行时**：
```
http://10.105.245.224:5000/admin
```

### 6 个功能模块

| 模块 | 功能 | 使用场景 |
|------|------|---------|
| **Overview** | 系统总览：板子在线状态、训练数据统计、模型信息、快速启停 | 日常第一眼查看 |
| **Services** | 5个服务运行状态（Vision/Streamer/FSM/EMG/Voice），一键启动/停止 | 部署和调试 |
| **Training Data** | 按动作类型和标签（golden/lazy/bad）分类浏览所有CSV训练数据 | 数据管理 |
| **History** | 训练历史记录（每次session的标准次数、违规次数、合格率） | 训练复盘 |
| **System** | 板端CPU温度和运行时间、云端GPU状态（显存/利用率）、网络配置 | 硬件监控 |
| **Project** | Git分支/提交历史、GRU模型参数、TensorBoard训练记录 | 版本管理 |

### API 端点列表

所有管理API均以 `/api/admin/` 开头：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/admin/overview` | GET | 系统总览数据 |
| `/api/admin/services` | GET | 5个服务运行状态 |
| `/api/admin/start` | POST | 启动全部服务（等效 start_validation.sh） |
| `/api/admin/stop` | POST | 停止全部服务（等效 stop_validation.sh） |
| `/api/admin/training_data` | GET | 训练数据文件列表 |
| `/api/admin/system_info` | GET | GPU/板子/网络状态 |
| `/api/admin/project_info` | GET | Git/模型/配置信息 |

---

## 六、PPT 生成工作流 (PPTSkills)

项目集成了 Claude Code + PPTSkills 工作流，可以从 Markdown 内容一键生成可编辑的 `.pptx` 文件。

### 环境要求

| 工具 | 版本要求 | 安装命令 |
|------|---------|---------|
| Node.js | >= v18 | `nvm install --lts` |
| Claude Code | 最新 | `npm install -g @anthropic-ai/claude-code` |
| PPTSkills | 最新 | `npx skills add https://github.com/anthropics/skills --skill pptx --agent claude-code -y` |
| pptxgenjs | 最新 | `npm install -g pptxgenjs` |
| markitdown | 最新 | `pip install "markitdown[pptx]"` |
| LibreOffice | 任意 | `sudo apt install libreoffice-impress poppler-utils` |

### 生成 PPT 的步骤

**1. 准备内容文件**

PPT内容基于 Marp Markdown 文件：
```
docs/presentations/最终展示汇报_V2.md    # 最新版本
docs/presentations/最终展示汇报_Marp.md  # 原始版本
```

**2. 在 Claude Code 中生成 .pptx**

在项目根目录运行 `claude`，然后输入：

```
/pptx 在 docs/presentations/ 目录里有一个名为 最终展示汇报_V2.md 的文件，

请根据这个 Markdown 文件的内容，制作一份25张幻灯片的项目汇报 PPT，要求输出可编辑的 .pptx 文件。

演讲框架（请严格按照以下结构分配幻灯片）：

封面 (1张)：标题、汇报人、日期
目录 (1张)：列出主要章节
项目简介 (2张)：受众、痛点、核心区别
网络配置 (3张)：ICS直连、25秒延时、IP检测
硬件外设 (3张)：摄像头推流、蜂鸣器→音箱、语音系统
神经网络 (2张)：YOLOv5选型、NPU→云端RTMPose
状态机 (2张)：深蹲FSM、弯举FSM
大模型对接 (3张)：OpenClaw、语音交互、飞书推送
系统架构 (2张)：三层架构、零延迟策略
V2升级 (2张)：热力图、V2.2迭代
拓展模块 (3张)：传感器、GRU训练、管理面板
致谢 (1张)

内容要求：
- 每张幻灯片标题简洁明确，正文以要点形式呈现，每条要点不超过 20 字
- 涉及数据或比较结果时，请用表格或列表形式组织，不要大段文字
- 语言为中文，专业术语保留英文原文
- 关键数值、核心结论请加粗突出

设计风格：
- 配色方案：科技深蓝风，深蓝 (#1E2761) 搭配冰蓝 (#CADCFC)，白色背景
- 字体层次清晰：标题大、正文适中，重要数据或结论可加粗突出
- 每张幻灯片保留适当留白，避免信息过载
- 卡片式布局区分不同模块，提升视觉层次感

输出到 docs/presentations/IronBuddy_汇报.pptx
```

**3. QA 检查**

```bash
# 检查文字内容
python -m markitdown docs/presentations/IronBuddy_汇报.pptx

# 转换为图片检查排版（需要 LibreOffice + Poppler）
python ~/.claude/skills/pptx/scripts/office/soffice.py --headless --convert-to pdf docs/presentations/IronBuddy_汇报.pptx
pdftoppm -jpeg -r 150 docs/presentations/IronBuddy_汇报.pdf slide
```

**4. 后续编辑**

生成的 `.pptx` 文件可以用 PowerPoint 或 WPS 直接打开编辑：
- 修改文字内容
- 调整配色和字体
- 插入实际截图替换占位图
- 增删幻灯片

### 复用指南

以后需要生成新PPT时，只需要：
1. 准备好内容的 Markdown 文件（或PDF、Word、纯文字描述）
2. 在 Claude Code 中运行 `/pptx` 命令 + 你的要求
3. PPTSkills 会自动调用 pptxgenjs 生成 .pptx 文件
4. 用 markitdown 检查内容，用 LibreOffice 转图片检查排版

支持的输入格式：
- `.md` Markdown 文件
- `.pdf` PDF 文件
- `.docx` Word 文档
- 纯文字描述（直接在指令中写）

