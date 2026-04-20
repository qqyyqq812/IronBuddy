# Antigravity Agent 任务简报：ESP32 + UDP 环境配置

> 本文件由上游 Claude Code 生成，为 Antigravity Agent 提供完整上下文。
> 读完本文件后，请严格按最后的「任务清单」执行，不要重复已经验证过的步骤。

---

## 一、宏观背景（30 秒看懂）

我在做一个**边缘侧 AI 健身教练**（项目名 IronBuddy），核心硬件是 **Toybrick RK3399ProX 开发板**。

**今天下午的任务**：队友把 2 个 sEMG（表面肌电）传感器 + 1 个 ESP32 模块带来。ESP32 采集肌电信号，通过 **WiFi UDP** 把 ASCII 数据发到 RK3399 板子上的 Python 进程。板子上跑 2 个 GRU 神经网络（深蹲 + 弯举），根据视觉关键点 + EMG 实时判断动作代偿类型。

```
[2 片 sEMG 贴片]
      │
      ▼
[ESP32 WiFi 模块]  ← 我要配置编译烧录这玩意
      │
      │ UDP ASCII "val1 val2\n"，1kHz
      │（走同一个手机热点 2.4GHz）
      ▼
[Toybrick 板子] - Python udp_emg_server.py (0.0.0.0:8080)
      │  ↓ DSP 滤波 + RMS + MVC 归一 + 硬件域校准
      │  ↓ 写 /dev/shm/muscle_activation.json
      ▼
[main_claw_loop.py] - 视觉 7D + EMG 拼 30 帧滑窗
      │
      ▼
[CompensationGRU] - 代偿三分类（PyTorch CPU 推理，板端本地）
```

**关键事实**：神经网络在板端跑，不在 PC 跑。所以 UDP 最终要发**板子的 IP**，不是 PC 的 IP。PC 仅用于今天下午调试期验证 ESP32 发包正常。

---

## 二、我的当前环境（硬约束）

| 项 | 值 |
|---|---|
| 操作系统 | **Windows 11**（原生，不是 WSL）|
| IDE | **Antigravity**（Google 出品，VSCode fork）|
| 项目路径 | WSL2 挂载到 Win，实际代码在 `\\wsl$\Ubuntu\home\qq\projects\embedded-fullstack\` |
| 插件市场 | **Open VSX**（Antigravity 默认，不是 Microsoft VSCode Marketplace）|
| 语言环境 | 中文系统，所有交互用中文 |
| 网络 | 中国大陆，GitHub 直连不稳，Espressif 官方 URL 有时慢 |

---

## 三、已经确认/完成的事实（别重复）

### 3.1 队友的 ESP32 固件已分析完毕

文件：`projects/embedded-fullstack/total/队友交付/代码/WiFiUDPClient.ino`

**关键引脚**：
- GPIO34、GPIO35 → 2 路传感器模拟输入（ADC 11db，0~4095）
- GPIO22、GPIO21 → 软件拉高到 3.3V，给传感器供电

**关键参数**（烧录前必须修改）：
```cpp
const char * networkName = "Magic5 Pro";    // ← 改成我手机热点名
const char * networkPswd = "12345678";       // ← 改成我热点密码
const char * udpAddress = "10.105.245.224";  // ← 改成【板子】或【PC 调试】的 IP
const int udpPort = 8080;                    // 不用改，板端监听 8080
```

**数据协议**：ASCII 文本 `"v1 v2\r\n"`，空格分隔，`delay(1)` 约 1kHz 发包。

### 3.2 板端 UDP 接收服务已存在，协议兼容

文件：`projects/embedded-fullstack/hardware_engine/sensor/udp_emg_server.py`

已验证队友的 ASCII 协议和这个 server 的解析逻辑（L133-145）**完全对得上**，不需要改协议。只要 `udpAddress` 填板子 IP，板子上跑起这个 server，数据就能流进 GRU 管线。

### 3.3 我装了一个错误的 VSCode 扩展

扩展市场搜 "Arduino" 时我点错了，装了一个**叫 Arduino 但实际是配色主题**的扩展（发行商 `lintangwisesa`，仅 9534 下载）。**不要把这个当 Arduino 开发环境**，它只能换编辑器配色。可以不卸载（不影响），但它不提供任何编译/烧录能力。

---

## 四、今天下午的具体任务

**目标**：让 Antigravity 能**编译并烧录** `WiFiUDPClient.ino` 到 ESP32 开发板，USB 接我 Windows 笔记本。

**分解**：

1. 在 Antigravity（Open VSX 市场）里**找到并安装**一个真正能开发 ESP32 的扩展，推荐优先级：
   - **① PlatformIO IDE**（`platformio.platformio-ide`）—— 首选，Open VSX 上一定有，自动管理 ESP32 工具链，无需手工装 Arduino CLI
   - **② Arduino Community Edition**（`vscode-arduino.vscode-arduino-community`）—— 备选，如果 Open VSX 上搜不到就跳过
   - ③ **如果前两个都装不了**：建议我装独立的 Arduino IDE 2.x（https://arduino.cc），Antigravity 只做编辑器
2. 装完后配置 **ESP32 Dev Module** 板型
3. 指导我装 **CP210x** 或 **CH340** USB 驱动（ESP32 USB 转串口驱动，Windows 下必装，否则看不到 COM 口）
4. 选对 **COM 口**（设备管理器 → 端口 (COM 和 LPT)，找到 ESP32）
5. 如果选了 PlatformIO：**把 `.ino` 转成 PlatformIO 项目结构**（我允许你这么做，但请在**新路径**创建项目，不要污染 `代码/` 目录，因为那是队友原件）
6. 编译 + 烧录
7. 打开**串口监视器**（波特率 9600），看到 WiFi 连上 + 滚动的 `"数字 数字"` 即成功

**今天 PC 侧的 UDP 接收用途仅为调试**：我会在 WSL 里跑一个临时 Python 脚本 `/tmp/udp_listen_test.py`（已有），先把 `udpAddress` 临时填 PC 在热点下的 IP 验证链路。验证通过后再改回板子 IP 做正式集成，这一步不需要你帮。

---

## 五、避坑清单（别浪费我时间）

| 编号 | 坑 | 规避 |
|-----|----|------|
| P1 | Open VSX ≠ VS Marketplace，微软 `vsciot-vscode.vscode-arduino` 可能搜不到 | 优先 PlatformIO，它两个市场都有 |
| P2 | Antigravity 是 VSCode fork 但不等于 VSCode，设置路径可能不同 | 有疑问直接查 Antigravity 官方文档，不要套 VSCode 路径 |
| P3 | ESP32 开发板工具链 ~300MB，直连很慢 | 国内用户建议用 PlatformIO 的默认源（已自带镜像）或 Espressif 镜像 `https://download.espressif.com/arduino-esp32/package_esp32_index.json` |
| P4 | ESP32 烧录有时要按住 BOOT 键才能进下载模式 | 如果烧录卡在 `Connecting...____` 超时，提示我按住 BOOT 再点上传 |
| P5 | `.ino` 文件必须和同名文件夹同目录（Arduino 硬规矩）| 当前 `代码/WiFiUDPClient.ino` 不满足（文件夹叫"代码"），要么新建 PlatformIO 项目，要么把文件夹重命名为 `WiFiUDPClient` |
| P6 | 烧录必选 **ESP32 Dev Module** 板型，不是 "ESP32S3" 或 "ESP32C3" 等 | 队友板子是经典 ESP32（WROOM-32 模块） |
| P7 | 串口监视器波特率必须 **9600**（对齐 ino 里 `Serial.begin(9600)`），不要用默认 115200 | 很多教程默认 115200，**不要照抄** |
| P8 | 手机开 5GHz 热点 ESP32 连不上（只支持 2.4GHz）| 让用户手机热点强制 2.4GHz |
| P9 | 不要改 `delay(1)` 的频率 | 板端 DSP 滤波器按 1kHz 设计，改慢会让滤波截止频率全部下移 |
| P10 | WiFi SSID **不能含中文**/特殊字符 | Arduino WiFi 库中文支持有 bug |

---

## 六、期望输出格式

请你给我一份**傻瓜式步骤**：

1. 每一步带具体命令/点击路径
2. 每一步后有"预期现象"——让我能判断这一步是否成功
3. 遇到岔路（比如 PlatformIO 装不上时切 Arduino IDE）要给清晰的 fallback
4. 总时长估计 + 下载包大小提示（让我知道要等多久）
5. 最后给一个「**验收检查清单**」：5-8 条，每条都是可以打勾的客观事实

**不要**：
- 空话套话（"这是一个很棒的工具！"）
- 大段理论背景（我已经懂 UDP/WiFi/Arduino 的原理，需要的是执行步骤）
- 帮我写我没要求的代码（`.ino` 是队友的，除了改 3 个配置行不要动）

---

## 七、必要的文件引用

| 路径 | 内容 |
|------|-----|
| `projects/embedded-fullstack/total/队友交付/代码/WiFiUDPClient.ino` | 队友 ESP32 固件，待烧录 |
| `projects/embedded-fullstack/hardware_engine/sensor/udp_emg_server.py` | 板端 UDP 接收器，证实协议已对齐 |
| `projects/embedded-fullstack/docs/验收表/深蹲神经网络权威指南.md` | 深蹲 GRU 部署细节（确认跑在板端）|
| `projects/embedded-fullstack/docs/验收表/弯举神经网络权威指南.md` | 弯举 GRU 部署细节（确认跑在板端）|

---

## 八、开工指令

请按第四节的任务分解**一步一步**来，每完成一个小里程碑向我汇报并等我确认再继续。**不要一口气把 8 步全推完**——硬件配置最怕一次做太多，出错难定位。

第一步先帮我**查 Antigravity 的 Open VSX 里能不能搜到 PlatformIO IDE**。如果能，直接开始装；如果不能，切到 Arduino IDE 2.x 独立安装路径。

开始吧。
