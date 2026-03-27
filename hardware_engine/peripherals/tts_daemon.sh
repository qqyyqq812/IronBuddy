#!/bin/bash
# =============================================================
# Agent1：TTS 语音播报守护进程
# 同时监听训练回复和对话回复，有新内容就用 edge-tts 播报
# =============================================================

DEVICE="plughw:0,0"
EDGE_TTS="/home/toybrick/.local/bin/edge-tts"
VOICE="zh-CN-YunxiNeural"   # 年轻男性教练声线
TTS_TMP="/tmp/tts_output.mp3"

# 监听两个文件
TRAIN_FILE="/dev/shm/llm_reply.txt"
CHAT_FILE="/dev/shm/chat_reply.txt"

TRAIN_MOD=0
CHAT_MOD=0

# 确保路由到 SPK
amixer -c 0 sset 'Playback Path' SPK > /dev/null 2>&1

echo "[tts_daemon] 启动语音播报守护 ($(date))"
echo "[tts_daemon] 引擎: edge-tts, 声线: $VOICE"

speak() {
    local text="$1"
    # 截取前 100 字避免过长
    text=$(echo "$text" | head -c 300)
    if [ -z "$text" ]; then return; fi

    echo "[tts_daemon] $(date) 开始播报: ${text:0:60}..."

    if [ -x "$EDGE_TTS" ]; then
        "$EDGE_TTS" --text "$text" --voice "$VOICE" --write-media "$TTS_TMP" 2>/dev/null
        if [ -f "$TTS_TMP" ]; then    # 5. 用 mpg123 播放 (强制 16000Hz 重采样匹配 I2S 时钟避免变调)
            mpg123 -a "$DEVICE" -r 16000 -f 8000 -q "$TTS_TMP" >/dev/null 2>&1
            rm -f "$TTS_TMP"
        fi
    else
        # 无 TTS 引擎，播放警报音代替
        aplay -D "$DEVICE" -q /home/toybrick/hardware_engine/peripherals/sounds/alert_warning.wav 2>/dev/null
    fi

    echo "[tts_daemon] 播报完毕"
}

while true; do
    # 监听训练回复
    if [ -f "$TRAIN_FILE" ]; then
        MOD=$(stat -c %Y "$TRAIN_FILE" 2>/dev/null || echo 0)
        if [ "$MOD" != "$TRAIN_MOD" ] && [ "$MOD" != "0" ]; then
            TRAIN_MOD=$MOD
            TEXT=$(cat "$TRAIN_FILE" 2>/dev/null)
            if [ -n "$TEXT" ]; then
                speak "$TEXT"
            fi
        fi
    fi

    # 监听对话回复
    if [ -f "$CHAT_FILE" ]; then
        MOD=$(stat -c %Y "$CHAT_FILE" 2>/dev/null || echo 0)
        if [ "$MOD" != "$CHAT_MOD" ] && [ "$MOD" != "0" ]; then
            CHAT_MOD=$MOD
            TEXT=$(cat "$CHAT_FILE" 2>/dev/null)
            if [ -n "$TEXT" ]; then
                speak "$TEXT"
            fi
        fi
    fi

    sleep 0.5
done
