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

## 三、离线网线抢修 SOP（板端断网恢复）

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

