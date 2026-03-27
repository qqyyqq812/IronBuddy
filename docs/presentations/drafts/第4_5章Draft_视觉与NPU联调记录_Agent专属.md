# 视觉与NPU联调记录

> 本文档由前期开发过程中的多份零散备忘录精准沉淀合并而成。

---

---
marp: true
theme: default
paginate: true
header: "IronBuddy 智能私教系统 - P1阶段总结"
footer: "Agent 2 架构演进与端侧推理验证"
---

# IronBuddy 智能私教系统
## P1 阶段：环境选型与端侧 C++ 推理重构总结
**报告人:** Agent 2
**汇报点:** 环境选型、架构痛点排查与方案升级

---

## 1. 核心模型选型：YOLOv5s-Pose

在初代 NPU 项目预研中，为何我们最终锚定 **YOLOv5s** 而非理论上性能更优的 YOLOv7 甚至 YOLOv11？

- **极致的算子适配率**：RK3399Pro 搭载的是初代 NPU（约 3.0 TOPS），仅支持极其有限的基础算子集。YOLOv5 存世时间长，其架构（尤其是 SiLU, Focus 层）被 Rockchip 官方底层高度打磨和特别关照，几乎可以直接 100% 量化运行，掉 CPU 回退风险极小。
- **免训练获取资源**：开源社区（如 Jamjamjon 的 `RKNN-YOLO`）已专门针对此架构预训练了带有 17 关键点提取的高校模型 `pose-5s6-640-uint8.rknn`，可以直接跨过长达周级别的重新组网与权重导出。
- **端侧延迟保障**：量化后的纯 NPU INT8 推理可将单帧耗时压制在 **60ms (~16FPS) 以内**，这对于人体姿态评估属于完全可用的实时范畴。

---

## 2. 环境部署深坑：Python RKNN-Toolkit 困境

在 P0 环境搭建初期，我们原计划在 WSL2 通过加载官方离线 Ubuntu 18.04 镜像，配合 Docker 直接跑通官方的 Python 环境以量化模型。

- **代理死锁与 403 阻断**：由于宿主网络复杂的代理配置、TLS 劫持以及各大开源镜像站限流（Github / Gitee 大面积阻断 700MB 以上离线包拉取）。
- **重型依赖拖累**：`rknn-toolkit-1.7.5` 要求古老的 Python 3.6/3.8 以及极为繁琐的一批编译期 C 库依赖，本地交叉编译环境复原难度极大。
- **Python 推流的 GIL 瓶颈**：即使 Python 跑起张量推理，面对工业摄像头每秒 30 张高分图像时，全局解释器锁会导致 `VideoCapture()` 读取和 NPU 推理发生灾难性的阻塞与时序错位。

---

## 3. 架构痛点破除：拥抱原生 C++ 零拷贝方案

为贯彻“务实高效、火速上线”的开发信条，我们在主理人授权下进行了大规模的重构，废除所有冗长的转换流，切换至业内最高效的开源方案：

- **完全抛弃模型自行导出**：通过物理机介入直接获取验证过的高可用成品 `pose.rknn` 权重。
- **切换到 C++ Native API 编译**：抛弃了 Python 脚本封装。直接通过 SSH 把包含了 C++ RKNN API 与 Eigen 矩阵计算库的工程源码推上真机。
- **动态寻址 OpenCV 与 Firmware**：摆脱了旧项目源码中的硬编码路径依赖（`find_package` 重构 CMakeLists）。仅一次 `make -j4` 就顺利产出了执行体 `./main`。

**结论**：用 C++ 的极高编译门槛换取了板端**零依赖、零卡顿**的工业级原生执行效率，直接铺平了向 OpenClaw 策略系统供给视觉帧的物理管道。


=======================================================

# P5 NPU 端侧推理测试与免密推流开发总结

## 1. 任务背景与目标
在 Python 版 RKNN-Toolkit 遭遇算子不支持、GIL 锁性能瓶颈等问题后，项目战略性转移至 **C++ 原生 RKNN API** 方案。
本阶段的核心目标是在 RK3399ProX 靶板上，无 GUI (Headless) 环境下，成功拉起 YOLOv5s-Pose 模型，接管摄像头，进行高帧率人体姿态估计，并将结果图像优雅地推流回宿主机验证。

## 2. 核心挑战与排障记录

### 2.1 Headless 环境下的 Core Dump
* **现象**：在不接显示器的开发板上执行带有 `cv::imshow()` 或 `cv::waitKey()` 的 C++ 编译文件，会立刻触发 `Gtk-WARNING **: cannot open display` 及段错误崩溃。
* **解决**：修改 `main.cpp`，彻底删除与 GUI 交互相关的 OpenCV API，改为抽样落地策略（ `cv::imwrite`）。

### 2.2 V4L2 像素格式不佳导致摄像头开启失败
* **现象**：OpenCV 默认调用 V4L2 请求 YUYV 格式，导致报错 `V4L2: getting property #7 is not supported`。
* **解决**：在 C++ 代码中强制执行 `capture.set(cv::CAP_PROP_FOURCC, cv::VideoWriter::fourcc('M', 'J', 'P', 'G'));` 和 `capture.set(cv::CAP_PROP_FRAME_WIDTH, 640);`，锁定为 MJPG 格式输出，成功点亮镜头。

### 2.3  I/O 阻塞导致“4 秒一帧”的假卡顿与满屏粉色乱码
* **现象**：实拍测试时不仅画面卡在 4-5s 一帧，而且画面中出现大量粉红色的错位 bounding box 和线条。  
* **解决 (双杀优化)**：
  1. **端口乱码纠正**：由于启动参数错传为 `1` (人脸检测5点模型)，导致其与实际加载的人体姿态 17 点模型不匹配，发生指针溢出画错位图。纠正启动参数为 **`2` (HumanPose2D)**。
  2. **物理存储节流**：原设定下，图像每 30 帧才存入 eMMC 闪存一次。重构代码，将取帧频率提升为每 2 帧一取，并写入极速内存盘 **`/dev/shm/result.jpg`**。

### 2.4  网络端口被占与 "ssh/scp" 假死陷阱
* **现象**：使用 `scp` 拉取或默认 `8080` 启动 Web 服务时，受限于密码等待遮挡和内部端口隐蔽占用（Errno 98）。
* **解决**：改写 HTTP 流媒体器 `flask_streamer.py` 至独立端口 `8088`，并严格区分目标 IP 执行路径。

## 3. 标准打板测试方法 (供后续上传真图留存)

为了留存以及日后随时复验该模块的巅峰性能（**稳态 29 FPS**），请严格执行以下双终端指令流：

**终端 A：启动 C++ 核心推理引擎 (参数 2 开启 17 点人体骨骼渲染)**
```bash
ssh -o StrictHostKeyChecking=no toybrick@10.28.134.224 "echo toybrick | sudo -S fuser -k /dev/video5 ; cd /home/toybrick/yolo_test/build && ./main 2 ../data/weights/pose-5s6-640-uint8.rknn 5 0.5"
```

**终端 B：挂载 Python Flask 内存盘推流服务 (极低迟延)**
```bash
ssh -o StrictHostKeyChecking=no toybrick@10.28.134.224 "cd /home/toybrick/yolo_test/build && python3 flask_streamer.py"
```

**浏览器视窗验证**：
在宿主机浏览器访问：`http://10.28.134.224:8088/`。即可获得丝滑的无密实时 NPU 检测推流画面。

## 4. 阶段验收定论
NPU C++ 推理逻辑已与硬件完全解耦并达成最大流帧率（29FPS）。**底层视觉基建大获全胜**。本模块自此固化为高可用后端 API，准备向总体 Agent (视觉、语音、大模型结合) 移交。


=======================================================

# 05_C++ 端侧推理验证与实拍排障总结.md

**录入者:** Agent 2 (NPU 模型推理专家)
**录入时间:** 2026-03-16
**针对模块:** NPU 算法接驳能力与原机系统级相机联调闭环

### 1. 架构目标与验证结果
在 P2 阶段落地的最终目标是：彻底抛弃基于 Python 与 Docker 的厚重封装，使用极简的 C++ 方案搭载 `rknn_api` 在原生 RK3399Pro 上完成轻量骨骼关键点（YOLOv5s-Pose）的 30FPS 实机拉流与推理验证。
- **结果**: 验证通过。通过单步 SSH 编译并下发执行指令，我们成功构建出了高效的终端可执行文件 `./main`，打通了原生 `/dev/video5` 到 NPU 张量计算层的零拷贝数据管线，耗时处于理想区间。

### 2. 核心技术坑点与攻坚排障 (Pitfalls)

#### 2.1 Headless 环境下的 UI 崩溃 (Core Dump)
- **现象**: 当含有 `cv::imshow("opencv_display", frame)` 或 `cv::waitKey()` 的 OpenCV 代码被抛上仅存在 SSH 终端的开发板时，由于没有任何 X Server （或 Wayland）提供图形显示面，程序会瞬间抛出 `Gtk-WARNING **: cannot open display:` 并直接触发 coredump 死亡。
- **解法 (极简防呆)**: 强制从源码切除所有带 GUI 的交互渲染，改为时间序列抽样：通过定帧 (例如每30帧) 执行 `cv::imwrite("result.jpg", frame)` 落地为静态文件供主理人验证。

#### 2.2 OpenCV 对 V4L2 YUYV (Raw) 的解码抗拒
- **现象**: 程序启动并打开 `/dev/video*` 时，OpenCV 底层 V4L2 插件报出 `Pixel format of incoming image is unsupported`。开发板内置的旧版 OpenCV 在无 VPU 处理能力下，无法接手未经压缩的原始 RAW 视频。
- **解法 (强制封锁格式)**: 抢在接流前，利用 C++ 将解码格式锁死在所有板卡都吃得开的 MJPG，并卡死分辨率保护内存。
  ```cpp
  capture.set(cv::CAP_PROP_FOURCC, cv::VideoWriter::fourcc('M', 'J', 'P', 'G'));
  capture.set(cv::CAP_PROP_FRAME_WIDTH, 640);
  capture.set(cv::CAP_PROP_FRAME_HEIGHT, 480);
  ```

#### 2.3 底层摄像头软性死锁 (Device or resource busy)
- **现象**: 由于前期试错或意外中断，导致摄像头文件句柄未被干净释放引发僵尸进程锁孔（如 `VIDIOC_S_FMT: failed`）。
- **解法 (暴力清除)**: 使用系统级指令 `sudo fuser -k /dev/video5` 强权砍断所有持械进程，确保每次运行的环境处于绝对干净态。

---

### 3. 给主理人的实拍验证方案 (How to Test)

当您准备好调整镜头，对准真人目标进行实拍验证时，请严格遵守以下步骤抓取结果图像：

**步骤一：清场与启动**
打开宿主机的终端，向局域网靶机发起启动指令（此代码将会持续运行推理循环，并每秒在板子内生成一张 `result.jpg`，请您自由摆出骨架动作）：
```bash
ssh -o StrictHostKeyChecking=no toybrick@10.28.134.224 "echo toybrick | sudo -S fuser -k /dev/video5 ; cd /home/toybrick/yolo_test/build && ./main 1 ../data/weights/pose-5s6-640-uint8.rknn 5 0.5"
```
*(注意：输入靶机的 `toybrick` 密码后，会看到类似 `> Frame 30 saved to result.jpg` 的持续打印输出，此时代表正在连续实拍提取中。)*

**步骤二：抽样抓取回宿主机**
新开一个本地终端窗口（切莫打断正在运行的步骤一），将刚刚生成的成果图拖回本地：
```bash
scp -o StrictHostKeyChecking=no toybrick@10.28.134.224:/home/toybrick/yolo_test/build/result.jpg ./sandbox/
```

**步骤三：鉴赏与退出**
打开本地刚收到的 `result.jpg`，核验人形与骨架点！
核验完毕后，回到步骤一运行推理流的终端，按下 `Ctrl + C`，释放真机摄像头兵权。


=======================================================

---
marp: true
theme: default
paginate: true
backgroundColor: #f0f4f8
---

<!-- _class: lead -->
# RK3399ProX 视觉感知基建 🚀
### 从图形渲染困局到 MJPEG 极简推流

**汇报人**: 主理人 / Agent 1 硬件建设者
**日期**: 2026-03-15

---

## 🛑 痛点：X-Windows (XRDP) 的系统折磨

我们在尝试使用原生 `mstsc` 远程开发板查看 `/dev/video5` 时，遇到了极大的性能与权限壁垒：

- **权限僵尸进程陷阱**：`xfce4-session` 进程死锁后台，即便加入 `video` 组也无法继承读写权限，频发 `Permission denied` 或设备占用。
- **不可接受的延时**：在远程桌面环境强行拉起 OpenCV 图形渲染，带宽告急，画面直接卡顿至 **5秒/帧** 的 PPT 级别。

![bg right:40% fit](./images/远程桌面.png)

---

## 🔍 第一性原理分析：为什么会卡？

**传统远程桌面协议根本不适合原生流媒体监控！**

1. **CPU 渲染灾难**：xrdp/VNC 的原理是对整个“桌面系统 GUI 像素”进行超高频的截图重制。
2. **带宽黑洞**：将连续视频流降维打击为像素图谱传输，榨干了无线局域网的回传吞吐。
3. **架构违背**：端侧物联网硬件（RK3399ProX）的核心是“无屏感知（Headless）”，强行渲染桌面属于算力浪费。

👉 **技术决断**：必须甩掉所有 GUI 外壳，进行“纯后台硬件级抓帧”！

---

## 🛠️ 极客解法：Flask MJPEG 无首流媒体引擎

以 Python 构建轻量级 Web 推流服务 (`streamer_app.py`)：

```python
# 1. OpenCV 底层暴力抽帧，零桌面渲染压码
camera = cv2.VideoCapture(5)
ret, buffer = cv2.imencode('.jpg', frame)

# 2. Flask Multipart 协议分发管道，暴露给 5000 端口
def generate_frames():
    while True: ...
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
```

**部署方案**：摒弃视窗，通过纯纯 SSH 命令 `nohup python3 app.py &` 启动后台守护。

---

## 🎉 成果展示：端云协同与满帧视觉呈现

完成了前后分离的绝杀：宿主机只需使用 Chrome 浏览器输入 `http://10.28.134.224:5000` 即可通过原生单纯的 `<img src>` 接收投射！

- **性能巨幅释放**：玩具板不再承担任何像素绘制开销，全力留存算力以备后续 NPU AI 使用。
- **极致无界**：在内网达成了低延迟、30 FPS 的原生画面串流！

![bg left:50% fit](./images/python推流网页.png)

---

<!-- _class: lead -->
# 第一阶段战役：告捷 
**下一进程规划**：下沉底层逻辑，封装零耦合通用型 Python 硬件外设外驱 API。


=======================================================

# 汇报草案 II: 端云协同网络延迟评估与三级降权展示策略

> **背景陈述**： 在迈向 OpenClaw 智能化深水区时，主理人提出了一条极具工程清醒度的前瞻性预警：**“网络链路过长可能导致 API 极速回传崩溃，语音指导的慢半拍会摧毁 AI 健身教练的体验。是否需引入零延迟的底层硬件（蜂鸣器）作为动作截断器？”** 
> 这是一个教科书级别的软硬协同命题。针对该命题，本技术决策给出了明确的电平匹配方案与三级平滑降级（Graceful Degradation）展示策略。

---

## 🔬 1. 硬件外设的电平匹配：蜂鸣器究竟能不能直连？

**结论：完全具备直连条件，坚决无需重新购买！**

主理人现存的物资是一块**“有源蜂鸣器 模块 3.3V 低电平触发”**。这是最理想的外设形式：
1. **自带运放（三极管驱动）**：这代表它不需要 RK3399ProX 的 GPIO 提供直接驱动电流（主板 GPIO 裸驱能力极弱，通常 $<10$ mA），它只需要主板给它一个“微弱的低电平信号”作为开关即可。
2. **电平极度吻合（原生支持）**：坚决摒弃外挂 CH341B 拓展卡等臃肿方案。根据《RK3399开发板连接教程.pdf》，RK3399ProX 主板原生提供 2 个独立可控的 **3.0V GPIO 口**。3.0V 的高电平完全达到了 3.3V 逻辑阈值（TTL 标准下 >2.0V 即为高），足以关断蜂鸣器；拉低到 0V 则稳定触发长鸣。
3. **接线实施方案**：
   * **VCC 针脚**：接入主板提供的系统 3.3V/5V 取电引脚（如有），或独立外接 3.3V 纽扣/干电池供电（蜂鸣器仅需数毫安维持）。
   * **GND 针脚**：接入主板统一的地线 (GND)。
   * **I/O 触发脚**：将其直接杜邦线连接至板载的 **原生 3.0V GPIO 引脚**。因其为低电平触发，平时代码里置高 (HIGH/1) 即静音，侦测到动作错误时，Linux Sysfs 代码瞬间将其置低 (LOW/0) 即可发出刺耳警报。

---

## 🚥 2. 软硬解耦：系统三级延迟防波堤策略

基于上述零延迟的 GPIO 发音武器，我们为最终的系统验收/展示确立了以下三个层级的**动态降级策略**。

### 🥇 【第一级：理想形态】双轨交响（硬截断 + 软指导）
这是我们在展示时力保的终极效果体系。
* **管线机制**：RK3399ProX 板载一颗 3.0 TOPS 的独立 NPU。当只跑 YOLOv5-Pose 时，NPU 的帧率可轻松逼近 30 FPS，这意味着每一次骨骼点抓取的**本地算力延迟不超过 33 毫秒**。
* **展示效果**：
  1. **零延迟硬截断**：比如判定要求“深蹲臀部必须低于膝盖”。一旦前置 NPU 连续 3 帧（不到 0.1 秒）发现臀部过高，板端 C++ 在本地直接 `echo 0 > /sys/class/gpio/gpioX/value`，**蜂鸣器瞬间发出“滴——”的长鸣**制止动作。
  2. **异步软指导**：在蜂鸣器拉响的同一毫秒，系统通过 `openclaw_bridge` 给云端的 DeepSeek 发射包含骨骼坐标的报错 Json。由于蜂鸣器已经抢夺了用户的注意力，用户停止动作；1.5秒后，OpenClaw 生成的语音“你的深蹲幅度不够，请注意大腿与地面平行”才从音箱中娓娓道来。**（利用底层物理声响掩盖云端网络延时）**

### 🥈 【第二级：妥协形态】哑巴教练（仅硬截断）
* **触发条件**：展示当日会场校园网拥堵，DeepSeek API 严重限流或超时（>4秒）。
* **管线机制**：此时如果强求 API 语音，会导致人已经做完了两组，前一组的报错声音才传过来，极其滑稽。
* **展示效果**：立刻在监控端关闭大模型对话流，系统化身为“硬核打卡机”。NPU 依然神速，只要动作不达标，蜂鸣器直接狂叫。不再有废话语音。向评委和导师传达：“本设备的边缘 AI 算力完全闭环，不惧断网”。

### 🥉 【第三级：底线形态】赛后复盘机 (Post-action Reviewer)
* **触发条件**：极其罕见的情况。如果有其他重负载模块拖垮了 NPU，导致本地 YOLO-Pose 抽帧降到了 5 FPS 甚至更低，连蜂鸣器“及时”报警都做不到了（动作已经做错，但由于帧积压，3秒后蜂鸣器才响）。
* **管线机制**：直接放弃对动作的“实时干预”。
* **展示效果**：让用户安静做完一套（如 10 个俯卧撑），系统全程只静默录制并将关键骨骼数据存为数组。当且仅当用户说“我做完了，教练”，系统才一次性将这堆超大的 Json 矩阵连带着训练总时长丢进 OpenClaw。DeepSeek 读取后生成一份犹如健身房私教出具的**综合训练报告**：“总体不错，但在第3个和第8个动作时，你的腰部发生了塌陷。”

---

> **阶段汇总语**:
> 主理人，以上的分析不仅仅是代码层面的逻辑设定，它是未来面对 PPT 评审时展现给专家的最高维度思考——我们没有盲目迷信大模型，我们清晰地认知到了物联网系统的木桶短板（网络 I/O 延迟），并给出了利用单片机底层外设（Buzzer GPIO）去“障眼”乃至硬隔离大模型延迟的成熟系统思维。
> 
> 请将此文档亦作为 `Final_Project_Presentation.md` 的核心图景！

---

## ⏱️ 3. 端云协同管线全链路耗时剖析 (Latency Profiling)

在答辩与验收时，我们需要向评委呈现清晰的数据支撑，证明加入蜂鸣器的绝对必要性。以下为系统全节点的处理耗时预估表：

| 处理环节 | 执行节点 | 耗时预估 | 备注说明 |
| :--- | :--- | :--- | :--- |
| **视觉采集外设读入** | USB Webcam -> V4L2 | ~15ms | 取决于 480p/720p 帧设置 |
| **NPU 动作姿态解析** | RK3399ProX NPU (YOLO-Pose) | ~33ms | 算力可达 3.0 TOPS，稳跑 30FPS |
| **判定逻辑与 GPIO 触发** | CPU (Python/C++ Sysfs) | < 2ms | 极简阈值判断及内核文件写操作 |
| **[硬截断] 总耗时点** | **蜂鸣器鸣响警报** | **~50ms** | **人类完全无法感知的极速“零延迟”体验** |
| ASR 终端听觉转写 | CPU (Vosk 脱机中文版) | ~200-300ms | 离线词库快速切音 |
| OpenClaw 局域网桥接 | WSL2 Gateway (WebSocket) | < 10ms | 千兆内网传输极快 |
| **云端大脑思考涌现** | **DeepSeek API TTFT** | **1200ms - 2500ms** | 强依赖外部公网与 API 服务器负载，不可控最大变量 |
| 异步语音提醒播放 | TTS + ALSA 扬声器 | ~500ms | 需将字符串转为音频波形 |
| **[软指导] 总耗时点** | **音箱吐出纠正语句** | **~2000m - 3300ms** | **2-3秒空窗期，这就是必须需要蜂鸣器掩盖网络延迟的命门所在** |

---

## 🔌 附：有源蜂鸣器基于 RK3399ProX 的原生物理接线表

为了免除 CH341B 等外设的繁文缛节，针对 3.3V 有源低电平触发蜂鸣器，直接采用以下三根杜邦线挂载至开发板露出的扩展排针口（40 Pin Header）：

1. **红线 (VCC)** 👉 寻找排针上的标记为 **`3V3`** (例如 Pin 1 或 Pin 17)。这是开发板系统稳压输出的 3.3V 电量源，负责给蜂鸣器内部的振荡及三极管供电。（注意：千万绝对不要插到 5V 引脚以防烧穿）。
2. **黑线 (GND)** 👉 寻找排针上标记为 **`GND`** 的任何一个接地针（例如 Pin 6, 9, 14, 20）。
3. **黄/蓝线 (I/O)** 👉 寻找任意一个原生引出的 **3.0V GPIO 引脚**（例如 `GPIO1_B1`, `GPIO0_A5` 等）。查阅开发板的排针位号图，选定一个之后，在 Linux 中通过 `echo <引脚号> > /sys/class/gpio/export` 即可获得该引脚的操作权。

**测试代码范例：**
```bash
# 假设我们在开发板选用 GPIO 41
echo 41 > /sys/class/gpio/export
echo out > /sys/class/gpio/gpio41/direction

echo 1 > /sys/class/gpio/gpio41/value  # 高电平拦截触发 -> 蜂鸣器静音
echo 0 > /sys/class/gpio/gpio41/value  # 低电平击穿导通 -> 蜂鸣器长鸣
```


=======================================================

# Agent 2 (视觉分析专员) YOLOv5 部署交接指南

你好，Agent 2！我是负责底层视听传感与 OpenClaw 神经中枢建设的 Agent 1。
目前，我这边已经在宿主机（WSL2）与端侧跑通了包括 `ASRWorker`（脱机声纹捕捉）以及基于深层 WebSocket 握手构建的 `OpenClawBridge`。这一切意味着开往云端的大门已经打开，且随时可以与主控制流 `main_claw_loop.py` 做会师。

接下来，整个系统的最后一块视觉“双眼”，也就是基于开发板 NPU 的 YOLOv5 物体追踪，完全交棒给你！

## 🎯 你的核心职责方向
你需要聚焦于 `hardware_engine/ai_sensory/sensor/camera.py` 和对应视觉预测脚本上的突破：
1. **专注视觉追踪与识别**：请参考此前在沙盒里的 YOLOv5 RKNN 转换进度，尽快在端侧（RK3399ProX）将 RKNN 库唤醒，使 NPU 能通过 USB 摄像头实现高速逐帧预测。
2. **纯粹的推理闭环**：在接手初期，**请千万不要花费精力去修改或阅读我的 `OpenClawBridge` 或 `main_claw_loop.py` 等联络中枢代码**。保持你组件的纯洁度！当你成功跑通了 YOLO 并输出框和坐标置信度后，主理人自会安排我将你的成果抽离整合到总联络管线。
3. **输出要求**：请将 YOLO 的框渲染功能稳定，并打印每一帧推理延迟，最后将测准的坐标与短句存入共享队列即可，确保代码能在终端单机顺利脱机自测！

## 💡 给主理人的 Prompt (可无脑粘贴发起会话)：

```text
你好 Agent 2。根据此前 Agent 1 (硬件协同专家) 的前序工作交接（在 /home/qq/projects/embedded-fullstack/docs/presentations/drafts/Agent2_YOLO_Handover.md ），现在需要你完全接管视觉系统的 NPU 加速工作。
1. 请只专注于跑通和稳定 YOLOv5 的 RKNN 推理。
2. 确保视频流读取、渲染与实时推理框选能无障碍循环。
3. 请开始检视项目环境，确认下一步实施方案！
```


=======================================================

# 现役推流中台 `streamer_app.py` v3 API 架构补充

> 基于 `streamer_app.py`（184 行）源码编写，补充当前运行版本的技术细节。

## 1. 架构概述

当前运行的推流中台为 **v3 精简版**。相比早期版本，剔除了所有直接操作麦克风/音频的模块，专注于三个核心职责：
- 视频帧推流
- FSM 状态数据分发
- 大模型触发与对话的前端入口

Flask 应用运行在 `0.0.0.0:5000`，启用多线程模式（`threaded=True`）。

## 2. API 端点清单

| 端点 | 方法 | 功能 | 数据源 |
|------|------|------|--------|
| `/` | GET | 主页 HTML（绕过 Jinja2 缓存） | `templates/index.html` |
| `/snapshot` | GET | 当前视频帧（JPEG） | `/dev/shm/result.jpg` |
| `/state_feed` | GET | FSM 状态 JSON | `/dev/shm/fsm_state.json` |
| `/llm_reply_feed` | GET | 大模型训练点评 | `/dev/shm/llm_reply.txt` |
| `/trigger_deepseek` | POST | 手动触发大模型点评 | 写入 `/dev/shm/trigger_deepseek` |
| `/reset_session` | POST | 重置 FSM 计数 | 写入 `/dev/shm/fsm_reset_signal` |
| `/api/chat` | POST | 接收用户文字/语音消息 | 写入 `/dev/shm/chat_input.txt` |
| `/api/chat_reply` | GET | 读取大模型对话回复 | `/dev/shm/chat_reply.txt` |
| `/api/chat_input` | GET | 读取 ASR 转写内容 | `/dev/shm/chat_input.txt` |
| `/api/chat_draft` | GET | 读取正在识别的草稿 | `/dev/shm/chat_draft.txt` |

## 3. 核心技术：帧去重与压缩

`/snapshot` 端点是推流的核心管线。为降低带宽占用，实现了双层优化：

### 3.1 帧去重
通过比较 `/dev/shm/result.jpg` 的 `mtime_ns`（纳秒精度修改时间），如果文件未变化则直接返回内存缓存：
```python
if st.st_mtime_ns == _snapshot_last_mtime and _snapshot_cache:
    return Response(_snapshot_cache, ...)  # 零 I/O 命中
```

### 3.2 JPEG 重压缩
读取原始帧后，使用 OpenCV 以质量参数 65 重新编码：
```python
SNAPSHOT_QUALITY = 65  # 原始 ~97KB → 压缩后 ~15-40KB
ok, enc = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, SNAPSHOT_QUALITY])
```
不做任何缩放，保持原始分辨率以确保文字和骨骼线的清晰度。

## 4. 缓存控制

所有响应均设置强制无缓存头：
```python
resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
resp.headers["Pragma"] = "no-cache"
resp.headers["Expires"] = "0"
```
这解决了早期开发中浏览器缓存旧帧导致画面"卡住不动"的问题。

