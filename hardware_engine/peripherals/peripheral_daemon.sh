#!/bin/bash
# =============================================================
# Agent1 外设旁路监听守护精灵（音箱版）
# 监听 /dev/shm/llm_reply.txt，检测纠错关键字后播放警报音
# =============================================================

WATCH_FILE="/dev/shm/llm_reply.txt"
ALERT_SCRIPT="/home/toybrick/hardware_engine/peripherals/speaker_alert.sh"
LAST_MOD=0

echo "[peripheral_daemon] 启动外设监听守护精灵 ($(date))"

while true; do
    if [ -f "$WATCH_FILE" ]; then
        MOD=$(stat -c %Y "$WATCH_FILE" 2>/dev/null || echo 0)
        if [ "$MOD" != "$LAST_MOD" ]; then
            LAST_MOD=$MOD
            if grep -qE '错误|不正确|违规|纠正|警告|太浅|内扣' "$WATCH_FILE" 2>/dev/null; then
                echo "[peripheral_daemon] $(date) 检测到纠错关键字，触发音箱警报！"
                bash "$ALERT_SCRIPT" &
            fi
        fi
    fi
    sleep 0.5
done
