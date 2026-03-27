#!/bin/bash
# =============================================================
# Agent1：蜂鸣器/音箱统一警报入口
# Agent2 FSM 调用路径：/home/toybrick/hardware_engine/peripherals/buzzer_alert.sh
# 实际通过音箱播放预录警报音（延迟 < 50ms）
# =============================================================

SOUNDS_DIR="/home/toybrick/hardware_engine/peripherals/sounds"
DEVICE="hw:0,0"

# 确保路由到 SPK（card 0 是 rk809-codec 板载音箱）
amixer -c 0 sset 'Playback Path' SPK > /dev/null 2>&1

# 后台播放警报音（不阻塞 FSM）
aplay -D "$DEVICE" -q "$SOUNDS_DIR/alert_beep.wav" 2>/dev/null &
