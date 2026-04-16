# Toybrick 板端环境与守护进程红线

当你为 IronBuddy 编写或修改代码时，严格遵守以下红线：

1. **Python 语法限制**
   - 板端环境仅支持 **Python 3.7**。
   - **绝对禁止使用** `X | None` 类型注解语法、`match/case` 以及 `:=` 海象运算符。
   - **绝对禁止引入** `pandas` 库，板端无此依赖。
   - 捕获子进程输出使用 `capture_output=True` 即可。

2. **进程管理与 Shell 陷阱**
   - 不允许直接使用 `nohup cd dir && cmd`，`nohup` 无法包裹复合命令，必须写成临时 shell 脚本执行。
   - 调用 `pgrep -f` 匹配进程时，必须使用正则括号陷阱（bracket trick），例如 `pgrep -f "[c]loud_rtm"`，防止匹配到 bash cmdline 本身。
   - 停止服务体系必须实行双重判定：先通过 `SIGTERM` 缓冲 0.8s，再 `kill -9` 强杀残留进程树。

3. **硬件与系统调用避坑**
   - **HDMI 屏幕**：必须赋予特权环境 `startx -- -nocursor`, `xhost +local:`。并手动拉取 Xauthority，由 Display 控制器直写 Framebuffer 零延迟。
   - **音频重置魔咒**：由于板端掉电特性，开机 Playback Path 必定被置回 OFF。每次重置必须前置运行 `SPK_HP (numid=1 val=6)` 等 amixer 回拨逻辑。
   - 推理中的 person_score (NPU量化板) 最高通常在 0.1~0.2 之间，相关置信度阈值判定 `MIN_KPT_CONF` 必须适配，不能原搬云端浮点模型的 >0.5。
