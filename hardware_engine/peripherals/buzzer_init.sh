#!/bin/bash
# Agent1：开机 GPIO 蜂鸣器静默初始化
GPIO=153
P="/sys/class/gpio/gpio${GPIO}"

_w() { echo toybrick | sudo -S sh -c "echo $2 > $1" 2>/dev/null; }

[ ! -d "$P" ] && _w /sys/class/gpio/export $GPIO && sleep 0.05
_w $P/direction out
_w $P/value 1   # 立即拉高→截止蜂鸣器
echo "[buzzer_init] GPIO ${GPIO} → 高电平（静音）"
