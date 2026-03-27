# 网络与通信排障说明

> 本文档由前期开发过程中的多份零散备忘录精准沉淀合并而成。

---

# 🚨 Agent 与主理人互通交接: 开发板网络连通 SOP 规范 (终局版)

> **警告：所有未来介入的 Agent 或开发者，在触碰 RK3399ProX 靶机前，必须熟读此文档。违者将陷入无限的“No route to host”死锁迷宫。**

## 🛑 现状定调与死亡禁区
在主理人环境 (`Windows 11` + `WSL2` + `移动热点分布`) 下，由于 WSL2 底层的 Hyper-V 虚拟交换机 (`vEthernet`) 在与 Windows 的“移动热点”适配时存在物理性的 NAT（网络地址转换）漏包与路由黑洞：

**绝对禁止的行为：**
- ❌ **禁止在 WSL 终端内直接 `ping 10.28.134.224`**：即使网络连着，也永远会遭遇 `Destination Host Unreachable`。 
- ❌ **禁止在 WSL 终端内直接 `ssh toybrick@10.28.134.224`**：永远会返回 `No route to host`。

不要去重置 WSL，不要去修改路由表！这些操作治标不治本！

## ✅ 唯一法定连接路径：Windows 物理终端借道 (PowerShell / CMD)

由于 Windows 宿主机本身可以完美畅通地与热点下的开发板通信，我们**必须且只能通过借用 Windows 原生终端**来进行连接和发号施令。

### 环境预置 (已由 Agent 2 永久打通)：
我的 SSH 身份私钥已经被安全转移至 Windows 层文件系统：
- 物理路径：`C:\temp\id_rsa`
- 权限：已赋予了严苛给 `%USERNAME%` 的单人独占权限 (ACL)。

### 标准连接与执令规范 (SOP)：

所有的后续 Agent，在需要登录靶机或批量执行命令时，**必须在您的 Bash/Tool call 中套用如下 PowerShell 命令格式**：

```bash
# 1. 探活测试 (Ping)
powershell.exe -Command "ping -n 1 10.28.134.224"

# 2. 单次发令免密执行 (不要漏掉 -i 和对应的宿主路径)
powershell.exe -Command "ssh -i C:\temp\id_rsa -o StrictHostKeyChecking=no toybrick@10.28.134.224 '你要执行的命令放在这'"

# 举例: 唤醒开机大脚本
powershell.exe -Command "ssh -i C:\temp\id_rsa -o StrictHostKeyChecking=no toybrick@10.28.134.224 'killall -9 python3 ; /home/toybrick/boot_ironbuddy.sh > /home/toybrick/boot.log 2>&1 &'"
```

## 🔍 前置条件排查清单 (如果连 Powershell 都连不上)：
1. **主理人设备是否处于相同热点？** 请主理人确认手机热点页面，找到设备并确认它仍分到了 `10.28.134.224`。
2. **靶卡是否断电/死锁？** NPU 在执行重度推理后若遭遇强杀，可能会引起 PCIe 总线卡顿挂死网络模块，此时只能物理按键重启靶机。
3. **OpenClaw 网关是否存活？** 作为大模型中转枢纽，需要确保 `openclaw gateway` 在本地 18789 端口存活。


=======================================================

# 经验教训与端侧通信规范总结 (Agent 2)

**文档属主**: Agent 2
**时间**: 2026-03-16

在 P2 阶段的端侧推送与推理验证过程中，我们多次遭遇了终端卡死与进程挂起的问题。经过主理人指导与痛点复盘，现总结出以下铁律与经验教训，作为后续系统交互必须要遵守的开发规范。

## 一、 通信卡死深层原因剖析

1. **管道阻塞与多重密码死锁**
   此前尝试使用 `scp ... && ssh ...` 或管道符将“代码推流”与“远端编译”在同一行命令中串联。这不仅使得底层 TTY 会在后台极短的时间内连续两次索取密码，还导致大模型（Agent）因为接收输出的滞后性而错失密码输入的最佳时机，引发永久性死锁挂起。
   
2. **错误尝试“黑盒子”屏蔽**
   在遭遇死锁后，曾试图通过临时安装 `paramiko` / `pexpect` 等 Python 封装库来规避原生交互。这违背了 KISS（保持简单）原则，并因网络波动导致 `pip` 安装挂起，让主理人更无法看清真实进程。

## 二、 黄金通信规约 (Agent 1 最佳实践)

总结 Agent 1 的成功经验及指导，后续与开发板交互时需严格遵守：

1. **化整为零，单步执行**
   必须绝对分离传输动作与执行动作！推送修改（`scp`）完成后，再开新的进程进行远端编译执行（`ssh`）。
   
2. **遇到密码不要干等**
   既然已经明确交互需要 `toybrick` 密码，只要命令一拉起产生 Background 进程，就立刻或略微等待后毫不犹豫地发送 `send_command_input`。
   
3. **暴露可视化的运行进度**
   任何下载或传输（包括 `scp`），坚决不能使用静默参数（`-q`），必须保持终端实时滚动进度条，确保主理人完全掌控硬件的运行状况。

## 三、 C++ 调用 V4L2 摄像头异常排雷指南

我们在真正驱动真机摄像头 `/dev/video5` 时触发了：`V4L2: Pixel format of incoming image is unsupported by OpenCV` 的核心转储报错。
**原因**：普通全栈 USB 摄像头默认偏好抛出未经压缩的异构 Raw 数据（如 YUYV），RK3399Pro 端侧的老版本 OpenCV 在没有 VPU 硬件编解码加持时当场崩溃。

👉 **终极修复法则**：在使用 `VideoCapture` 的 `open()` 动作后，必须强制索取全天下通用性最好的 `MJPG` 动态帧，并且锁死物理分辨率以匹配 NPU。

```cpp
// 在 C++ 中追加硬件防呆设定
capture.set(cv::CAP_PROP_FOURCC, cv::VideoWriter::fourcc('M', 'J', 'P', 'G'));
capture.set(cv::CAP_PROP_FRAME_WIDTH, 640);
capture.set(cv::CAP_PROP_FRAME_HEIGHT, 480);
```

---
> 总结完毕，规范确立。接下来，系统将贯彻以上“拆分任务 + 积极供给密码”的战术，恢复 `scp` 与 `ssh` 测速跑通！


=======================================================

# 05_热点自启与网络抢险配置详解

**最后修订**: 2026-03-15
**作者**: Agent 1 (硬件系统专家)
**系统生态**: RK3399ProX / Debian 10

此文档详述了为什么开发板之前开机会有连不上的情况，以及我们目前重构后的 **终极坚固版 `rc.local` 开机抢连机制**。

---

## 🛑 开机断联原因复盘 (Root Cause)

你遇到了“重启电脑后，板子突然无法自动连热点”的致命灵异事件。经过我在板卡基层的探查，发现由两大原罪导致：

1. **热点信息过期：** 之前的 `abc.conf` 存储的一直是笔记本的 ICS 共享热点或者另一台手机的热点，当你更换手机热点“Magic5 Pro”由于未更新配置，导致板卡成了断线风筝。
2. **底层进程竞争 (Race Condition)：** 原有的 `/etc/rc.local` 虽然能用，但极为脆弱。网卡 `wlan0` 拉起后，直接无脑启动了 `wpa_supplicant -B` 放入后台，并瞬间发起 `dhclient` 抢夺 IP。然而 WiFi 握手通常需要 3 秒以上。过早发起 DHCP 请求会导致 DHCP 进程被挂起失联 (`Network is down`)。

---

## 🛠️ 重构后的极简护城河机制

我在板端强行覆写了 `/etc/rc.local`，加入了时序保护。其核心逻辑链条与代码存档如下：

### 1. 核心密码本 `/etc/wpa_supplicant/abc.conf`
这是你的无线电发射密码。今后不论你换电脑还是换手机，只要名字和密码变了，你必须以网线或盲操登录后重置此文件：
```bash
wpa_passphrase "你的热点名称" "你的热点密码" > /etc/wpa_supplicant/abc.conf
```

### 2. 终极自启动脚本 `/etc/rc.local` (当前已在板卡生效)
这是板子加电后的第一重反制盾，它强制镇压了所有系统级网络挂号向手机投诚。

```bash
#!/bin/sh -e
# RK3399ProX Auto-Hotspot Script - Official Robust Version

# 0. 【官方底层镇压法】强制等待 25 秒，确保 wlan0 网卡及底层 USB 桥接彻底枚举成功
sleep 25

# 1. 强制拉起硬件网卡，给内核 2 秒喘息准备收发器件
ip link set wlan0 up
sleep 2

# 2. 屠杀由于网络闪断遗留的旧握手协议，保证不串联
killall wpa_supplicant || true
killall dhclient || true
sleep 1

# 3. 后台加载上一步做好的 abc.conf 热点指纹，开始进行无线电波握手
wpa_supplicant -B -i wlan0 -c /etc/wpa_supplicant/abc.conf

# 【核心防滑崩机制】给 WPA 握手和密码校验至少 8 秒！绝不提前抢注 IP
sleep 8

# 4. 握手成功后，才向上级主控手机发起 IP 索要请求
dhclient wlan0

exit 0
```

> **为何有效？** 它在首部使用了官方极为稳健的 `sleep 25` 强制避开了网卡热插拔和开机初期的总线未加载崩溃点，由于自带 8 秒抗 dhclient 抢占能力，它兼顾了最极端的全盲操长尾环境。

---

## 🚀 未来防坑指北

*   只要你不换手机、不改热点密码，这个板卡只要一通电，**12~15秒后**绝对会自动出现在你手机的个人热点连接设备列表里。
*   如果你又要在某个无法连热点的环境里抢连网线，请参考 `04_网线推演.md` 的 ICS 网卡转接法建立“物理霸权”。


=======================================================

# 网线直连配置全景沙盘推演 (从失联到完整开发态)

在网线到货并实施前，本指南将为您“沙盘推演”整个连通及环境配置流程，让您对后续的工作做到心中有绝对的底。

---

## 阶段一：建立物理霸权 (强制接管底层控制)

**目标**：不依靠路由器、不依靠有问题的调试串口，仅用网线获取系统的完整 SSH 控制权。

1.  **开启电脑热点共享 (ICS)**：
    *   在 Windows 中打开 `ncpa.cpl`，右键点击已经连上外网的网卡（如 WiFi 或 USB 5G网卡），选择“属性” -> “共享”。
    *   勾选“允许其他网络用户共享”，并在下拉菜单里选中接开发板的那张“USB 转 RJ45”的有线网卡。*（此时，电脑化身为 DHCP 服务器，开始放号）*
2.  **物理连接与上电**：
    *   插好网线（一头板子，一头转接卡），给板子上电。
3.  **盲抓 IP 寻址**：
    *   等待 1 分钟后，在 Windows 黑框输入：`arp -a`。
    *   寻找 `192.168.137.x` （Windows 共享默认网段）下面的动态 IP 列表。**排除您自己的网关 IP（如 .1）和广播地址（.255），剩下的那个（或者新冒出来的）就是 RK3399proX 的 IP。**
4.  **第一条指令（SSH 破门）**：
    *   在自己熟悉的终端里输入：`ssh root@发现的IP`，或者 `ssh toybrick@发现的IP`。
    *   出现 `Welcome to Toybrick` 或密码输入提示，即**破门成功，完成物理霸权交接**。

---

## 阶段二：打通云端经脉 (板载直连无线热点)

**目标**：拔掉碍事的网线，让开发板直接连上您手机的热点，成为独立的物联网设备。

1.  **命令行连 WiFi (NMCLI 神器)**：
    *   在刚通过 SSH 登录的开发板控制台里，利用自带的网络管理器连接您的手机热点：
        ```bash
        # 扫描周围的 WiFi 信号
        nmcli dev wifi list
        
        # 让板子连接您的手机热点 (替换为您自己的名字和密码)
        nmcli dev wifi connect "My_Phone_Hotspot" password "12345678"
        ```
2.  **验证脱网生存能力**：
    *   连接成功后，输入 `ping baidu.com` 确认板板能自己上网了。
    *   输入 `ip addr show wlan0` 查看它在手机热点里的新 IP（比如 `192.168.43.15`）。
3.  **剪断脐带**：
    *   果断拔掉那根接在笔记本上的网线！
    *   让您的笔记本也连接**同一个手机热点**。
    *   此时，笔记本用全新的指令 `ssh root@192.168.43.15` 即可再次登录。**至此，板子进入彻底的无线、无约束开发状态。**

---

## 阶段三：部署 AI 兵工厂 (开发环境搭建)

**目标**：板子不仅能跑系统，还能挂载您买来的各种感觉器官，并能顺利跑我们写的代码（例如我们之前要做的语音录制或者机器视觉脚本）。

1.  **更新防伪劣弹药库 (顺带测速)**：
    *   板子连上网后，执行系统级更新（如果源在国内则很快）：
        ```bash
        sudo apt-get update && sudo apt-get upgrade -y
        ```
2.  **安装感官驱动框架 (Python/音频/视觉)**：
    *   针对您的新硬件（USB 摄像头、耳机麦克风），必须装好基础抓取框架：
        ```bash
        # 安装基础编译环境、Python3 环境 以及音频/摄像头核心库
        sudo apt-get install -y python3-pip python3-dev
        sudo apt-get install -y v4l-utils alsa-utils libasound2-dev  # V4L 控制摄像头，ALSA 控制麦克风/喇叭
        ```
3.  **感官点火测试 (声光初体验)**：
    *   **测试喇叭/耳机**：插上那个 1.25mm 的小喇叭（或塞上耳机），输入 `speaker-test -t wav -c 2`，能听到“Front Left”的低沉女声即宣告语音喉咙打通。
    *   **测试新麦克风**：用 `arecord -l` 查看录音设备列表，然后录一段 5 秒的音轨测试有无环境声波形。
    *   **测试 USB 摄像头**：插上后输入 `ls /dev/video*`，看到设备挂在这个列表上即宣告视觉神经联通。

---

## 阶段四：交给 Antigravity 接管全局

**目标**：当以上三步走通，您将不需要再记忆复杂的 Linux 命令，直接在咱们的 `embedded-fullstack` 工程空间开始全自动编程。

1.  **AI 协同代码开发**：
    *   我会为您编写一套 `hardware_init.py` 脚本，用来一键测试读摄像头、录音和蜂鸣报警逻辑。
2.  **在终端里将我召唤过去**：
    *   您将通过刚才提到的系统 SSH 登录板子，然后由 Antigravity 帮您部署执行环境。我们也可以在您的 Windows 笔记本写代码，依靠远程同步把 Python 代码传到板子上执行。直接进入高效的应用开发阶段。



=======================================================

# 🔴 2026-03-21 WiFi "连上即断"事故复盘 — NetworkManager 劫持事件

**文档属主**: Agent 1 | **时间**: 2026-03-21 | **严重等级**: P0

## 一、故障现象

板卡连接手机热点"Magic5 Pro"后，手机端显示"已连接设备"闪现后**立即消失**（约 1-2 秒内断开）。反复重启均无法恢复。

## 二、根因 —— NetworkManager 控制权劫持

```
systemctl is-active NetworkManager → active  ← 凶手确认
```

### 因果链

```
rc.local → wpa_supplicant 连上热点 → dhclient 获取 IP
  ↓ 此时 NetworkManager 检测到 wlan0 状态变化
NM 认为 wlan0 被"未授权进程"占用 → 强制 kill wpa_supplicant
  ↓
NM 尝试用自己的策略重连 → 但 NM 没有 abc.conf 配置 → 失败
  ↓ 周而复始：连上→被杀→断开
```

**本质**：`wpa_supplicant` 与 `NetworkManager` 同时争夺 `wlan0` 控制权，NM 优先级更高，把 wpa_supplicant 的连接无情拆毁。

## 三、修复措施（已执行）

```bash
# 永久禁用 NetworkManager
sudo systemctl stop NetworkManager
sudo systemctl disable NetworkManager

# rc.local 中加入防线
systemctl stop NetworkManager 2>/dev/null || true
```

## 四、IP 地址变更

| 时间 | IP | 原因 |
|------|-----|------|
| 2026-03 上旬 | `172.19.98.224` | 旧热点首次分配 |
| 2026-03-21 修复后 | `10.28.134.224` | 新热点 DHCP 分配 |
| 2026-03-24 确认 | `10.28.134.224` | Agent 2 SSH 验证通过 |

> ⚠️ IP 不固定！手机热点每次可能分配不同 IP，需在手机热点页面查看已连接设备确认。
> ⚠️ **ICMP ping 被手机热点防火墙屏蔽**，ping 超时不代表板子离线！直接 SSH 即可验证连通性。

## 五、铁律追加

> 🚨 **禁止在板端启用 NetworkManager！** 如因 `apt upgrade` 被重新拉起，必须立刻 `systemctl disable NetworkManager`。无线连接统一由 `wpa_supplicant` + `dhclient` 经 `rc.local` 管理。

## 六、网线抢修 SOP 速查

1. `Win+R` → `ncpa.cpl` → 共享上网网卡给有线网卡
2. 插网线 → 等 60 秒 → PowerShell `arp -a` 找 `192.168.137.x` 段
3. `ssh toybrick@发现的IP`（密码 `toybrick`）
4. 禁用 NM + 重建 rc.local + 手动连热点验证
5. `sudo reboot` 验证开机自动连接
