#!/bin/bash
# ========================================================
# 软件定义电源：全引脚蜂鸣器底层驱动 (三管齐下版)
# [说明] 利用普通 GPIO 向外供电，彻底摆脱主板原生 VCC/GND 口的束缚。
# ========================================================

# 【引脚配置区】(请根据 RK3399ProX 的 40Pin 选择任意三个空闲的 GPIO 引脚号)
VCC_GPIO=154   # 充当纯净 3.3V 正极（恒出 1）
GND_GPIO=42    # 充当电路负极接地（恒出 0）
IO_GPIO=153    # 充当信号控制端（动态 1/0）

echo "[INFO] 准备夺取系统底层硬件资源..."

# 1. 向内核注册所有引脚 (Export)
for PIN in $VCC_GPIO $GND_GPIO $IO_GPIO; do
    if [ ! -d "/sys/class/gpio/gpio${PIN}" ]; then
        echo $PIN > /sys/class/gpio/export 2>/dev/null
    fi
    # 2. 全部锁定为输出模式 (Direction)
    echo out > /sys/class/gpio/gpio${PIN}/direction
done

echo "[INFO] 资源强占完毕，正在建立软件虚拟电源树..."

# 3. 供电通道初始化 (建立电势差)
echo 1 > /sys/class/gpio/gpio${VCC_GPIO}/value  # 此时 VCC_GPIO 提供最高约 20mA 的 3.3V 涓流
echo 0 > /sys/class/gpio/gpio${GND_GPIO}/value  # 将此引脚拉至 0V
echo 1 > /sys/class/gpio/gpio${IO_GPIO}/value   # 高电平=静音（有源低电平触发模块）

echo "==================================="
echo "   ⚡ 虚拟电场已就绪，准备点火！"
echo "==================================="

sleep 1

# 4. 蜂鸣器主控回路发声（低电平触发）
echo "[Buzzer ON] 低电平触发发声..."
echo 0 > /sys/class/gpio/gpio${IO_GPIO}/value
sleep 0.8
echo "[Buzzer OFF] 高电平截止..."
echo 1 > /sys/class/gpio/gpio${IO_GPIO}/value
sleep 0.4
echo "[Buzzer ON] 警告..."
echo 0 > /sys/class/gpio/gpio${IO_GPIO}/value
sleep 0.8
echo "[Buzzer OFF] 测试结束。"
echo 1 > /sys/class/gpio/gpio${IO_GPIO}/value

# 5. 回收内核资源 (Unexport)
echo "[INFO] 切断虚拟电源，交还内核控制权。"
for PIN in $VCC_GPIO $GND_GPIO $IO_GPIO; do
    echo $PIN > /sys/class/gpio/unexport 2>/dev/null
done
