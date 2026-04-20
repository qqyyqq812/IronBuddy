#!/bin/bash
# IronBuddy V3 板端静音启动 (图书馆 demo 专用)
# 仿 start_all_services.sh, 但 **完全跳过 voice_daemon** + 系统级静音三重保险
# 与 start_all_services.sh 完全独立, 不互相影响
#
# 三重静音保险:
#   1) 不启动 voice_daemon 进程 (无 arecord / 无 aplay 调用源头)
#   2) amixer Speaker mute + Playback 0% (硬件层面屏蔽 PCM 输出)
#   3) 写入 /dev/shm/mute_signal.json muted=true (若 voice_daemon 被手动拉起也全丢 TTS)
set -u
ROOT="/home/toybrick/streamer_v3"
LOG="/tmp/ironbuddy_silent_startup.log"
STAMP="$(date '+%F %T')"
echo "[$STAMP] ===== IronBuddy SILENT start =====" > "$LOG"
cd "$ROOT" || { echo "[$STAMP] ERROR: no $ROOT" >> "$LOG"; exit 1; }

# 1. Kill any stale instances (含 voice_daemon, 确保静音)
for PAT in "[c]loud_rtmpose_client" "[s]treamer_app" "[m]ain_claw_loop" "[u]dp_emg_server" "[v]oice_daemon"; do
    pkill -f "$PAT" 2>/dev/null || true
done
sleep 1
rm -f /tmp/ironbuddy_*.pid

# 2. Fresh slate in /dev/shm
sudo -n rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null || \
    rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null || true
echo '{"mode":"pure_vision"}' > /dev/shm/inference_mode.json

# 2b. SILENT 专属: 预置 mute_signal.json 为 muted=true
#     即使 voice_daemon 后续被手动拉起, 也会因 _is_muted[0]=True 而吞掉所有 TTS
cat > /dev/shm/mute_signal.json <<'EOF'
{"muted":true,"ts":0,"source":"silent_startup"}
EOF
echo "[$STAMP] mute_signal.json 预置 muted=true" >> "$LOG"

# 3. Load API keys (与原版一致, DeepSeek API 仍可调用, 只是回复不走 TTS)
if [ -f "$ROOT/.api_config.json" ]; then
    eval "$(python3 -c "import json;c=json.load(open('$ROOT/.api_config.json'));[print(f'export {k}={v!r}') for k,v in c.items() if isinstance(v,str)]")"
fi
export LLM_BACKEND=direct

# 4. Audio: SILENT 模式 - 系统级静音 (与原版的激活通路完全相反)
# 4a. 扬声器硬静音: Speaker control mute + 音量 0%
sudo -n amixer -c 0 sset Speaker 0% mute >/dev/null 2>&1 || \
    amixer -c 0 sset Speaker 0% mute >/dev/null 2>&1 || true
# 4b. Playback 音量归零 (不同板 codec 可能用不同 control name, 全部尝试)
for ctrl in "Speaker" "Master" "Headphone" "Playback"; do
    sudo -n amixer -c 0 sset "$ctrl" 0% >/dev/null 2>&1 || \
        amixer -c 0 sset "$ctrl" 0% >/dev/null 2>&1 || true
done
# 4c. Playback Path 保持激活(=2), 但音量已被前面清零
#     不断开 Path, 是为了 voice_daemon 若被启动时 ALSA 不报错
sudo -n amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 2 >/dev/null 2>&1 || true
# 4d. Capture MIC Path 也关闭 (图书馆模式无语音输入)
sudo -n amixer -c 0 cset numid=2,iface=MIXER,name='Capture MIC Path' 0 >/dev/null 2>&1 || true
echo "[$STAMP] amixer: Speaker muted + Playback 0% + Capture MIC Path=0" >> "$LOG"

# 5. Launch helper (同原版)
launch() {
    local name=$1; shift
    setsid nohup "$@" > "/tmp/${name}.log" 2>&1 < /dev/null &
    local pid=$!
    disown 2>/dev/null || true
    echo "$pid" > "/tmp/ironbuddy_${name}.pid"
    echo "[$STAMP] launched $name pid=$pid" >> "$LOG"
}

# 5a. 启动 4 个服务 (不含 voice!)
launch vision   python3 -u hardware_engine/ai_sensory/cloud_rtmpose_client.py
sleep 3
launch streamer python3 -u streamer_app.py
sleep 1
launch mainloop python3 -u hardware_engine/main_claw_loop.py
launch emg      python3 -u hardware_engine/sensor/udp_emg_server.py
echo "[$STAMP] voice_daemon SKIPPED (silent mode)" >> "$LOG"

# 5b. Cloud RTMPose SSH tunnel (与原版一致)
bash "$(dirname "$0")/cloud_tunnel.sh" || echo "[start_silent] cloud tunnel failed; will fallback to local NPU"

# 6. Verify 4 services survived (voice 故意排除, 不检查)
sleep 4
RC=0
declare -A PAT=( [vision]="[c]loud_rtmpose_client" [streamer]="[s]treamer_app" \
                 [mainloop]="[m]ain_claw_loop" [emg]="[u]dp_emg_server" )
for name in vision streamer mainloop emg; do
    if ! pgrep -f "${PAT[$name]}" >/dev/null 2>&1; then
        echo "[$STAMP] ERROR: $name NOT running (see /tmp/${name}.log)" >> "$LOG"
        RC=1
    else
        echo "[$STAMP] OK: $name alive" >> "$LOG"
    fi
done

# 6b. 确认 voice_daemon 真的没在跑 (反向断言)
if pgrep -f "[v]oice_daemon" >/dev/null 2>&1; then
    echo "[$STAMP] WARN: voice_daemon unexpectedly running in silent mode!" >> "$LOG"
    RC=1
else
    echo "[$STAMP] OK: voice_daemon absent (silent mode confirmed)" >> "$LOG"
fi

echo "[$STAMP] ===== SILENT exit=$RC =====" >> "$LOG"
exit $RC
