#!/bin/bash
# =============================================================
# Agent1：音箱短促警报（替代蜂鸣器，延迟 < 50ms）
# 被 Agent2 FSM 调用，也可手动执行
# =============================================================

SOUNDS_DIR="/home/toybrick/hardware_engine/peripherals/sounds"
DEVICE="plughw:0,0"

# 确保路由到 SPK（card 0 是 rk809-codec 板载音箱）
amixer -c 0 sset 'Playback Path' SPK > /dev/null 2>&1

# 后台播放警报音（不阻塞调用方）
aplay -D "$DEVICE" -q "$SOUNDS_DIR/alert_beep.wav" 2>/dev/null &
