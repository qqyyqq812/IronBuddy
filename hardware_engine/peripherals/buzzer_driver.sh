#!/bin/bash
# Agent 1 专供: RK3399ProX 纯 User-Space (Sysfs) 极简硬件驱动
# 有源蜂鸣器模块：低电平触发（I/O=0 鸣叫，I/O=1 静音）
# 遵循老师的建议，避开 C 语言内核模块，纯粹使用 echo 映射底层寄存器

# 硬件定义: 物理 40Pin 左列向下第4根 (GPIO4_D1_3V0)
GPIO_PIN=153
GPIO_PATH="/sys/class/gpio/gpio${GPIO_PIN}"

echo "[Agent 1] 🚀 开始挂载并强制夺取底层 GPIO ${GPIO_PIN} (Buzzer Sysfs Driver) ..."

# 1. 向内核请求暴露出数字引脚的控制权
if [ ! -d "$GPIO_PATH" ]; then
    echo "[->] 向 /sys/class/gpio/export 注入针号 ${GPIO_PIN}"
    echo $GPIO_PIN > /sys/class/gpio/export || exit 1
else
    echo "[!] 针号 ${GPIO_PIN} 已经被内核暴露，跳过 export."
fi

# 2. 将引脚方向锁定为 "输出 (out)" 模式，只有这样才能通电
echo "[->] 切换为 Output 发射模式"
echo out > ${GPIO_PATH}/direction || exit 1

echo "==================================="
echo "🔔 开始实弹警报演练 (低电平触发)"
echo "==================================="

# 3. 核心驱动动作：拉低电平，触发有源蜂鸣器 (低电平触发)
echo "[Buzzer ON] 拉响！蹲得太浅了！ (低电平触发)"
echo 0 > ${GPIO_PATH}/value
sleep 1

# 4. 核心驱动动作：拉高电平，截止蜂鸣器 (高电平=静音)
echo "[Buzzer OFF] 恢复静默。 (高电平截止)"
echo 1 > ${GPIO_PATH}/value
sleep 0.5

echo "[Buzzer ON] 再次警告！"
echo 0 > ${GPIO_PATH}/value
sleep 0.5

echo "[Buzzer OFF] 结束测试。"
echo 1 > ${GPIO_PATH}/value

# 5. 防死锁清理：向内核交还兵权
echo "[->] 释放内核资源 (unexport)"
echo $GPIO_PIN > /sys/class/gpio/unexport

echo "✅ 纯 echo 方案驱动级测试完美落幕！"
