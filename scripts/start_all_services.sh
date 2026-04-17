#!/bin/bash
# IronBuddy V3 board-side boot: 5 services + APIs + audio reset
# Sudoers template (run `sudo visudo` once on board):
#   toybrick ALL=(ALL) NOPASSWD: /usr/bin/amixer, /usr/bin/killall, /bin/rm /dev/shm/*
set -u
ROOT="/home/toybrick/streamer_v3"
LOG="/tmp/ironbuddy_startup.log"
STAMP="$(date '+%F %T')"
echo "[$STAMP] ===== IronBuddy start =====" > "$LOG"
cd "$ROOT" || { echo "[$STAMP] ERROR: no $ROOT" >> "$LOG"; exit 1; }

# 1. Kill any stale instances (bracket trick to skip self)
for PAT in "[c]loud_rtmpose_client" "[s]treamer_app" "[m]ain_claw_loop" "[u]dp_emg_server" "[v]oice_daemon"; do
    pkill -f "$PAT" 2>/dev/null || true
done
sleep 1
rm -f /tmp/ironbuddy_*.pid

# 2. Fresh slate in /dev/shm
sudo -n rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null || \
    rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null || true
echo '{"mode":"pure_vision"}' > /dev/shm/inference_mode.json

# 3. Load API keys
if [ -f "$ROOT/.api_config.json" ]; then
    eval "$(python3 -c "import json;c=json.load(open('$ROOT/.api_config.json'));[print(f'export {k}={v!r}') for k,v in c.items() if isinstance(v,str)]")"
fi
export LLM_BACKEND=direct

# 4. Audio reset (板厂商标准: Main Mic 输入 + SPK 输出)
# 关键: Capture MIC Path=1 (Main Mic板载) 否则 RMS=0 无法唤醒
sudo -n amixer -c 0 cset numid=2,iface=MIXER,name='Capture MIC Path' 1 >/dev/null 2>&1 || true
# Playback Path=2 (SPK=PH2.0 板载扬声器)
sudo -n amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 2 >/dev/null 2>&1 || true

# 5. Launch helper: setsid+nohup+disown = true detach
launch() {
    local name=$1; shift
    setsid nohup "$@" > "/tmp/${name}.log" 2>&1 < /dev/null &
    local pid=$!
    disown 2>/dev/null || true
    echo "$pid" > "/tmp/ironbuddy_${name}.pid"
    echo "[$STAMP] launched $name pid=$pid" >> "$LOG"
}

launch vision   python3 -u hardware_engine/ai_sensory/cloud_rtmpose_client.py
sleep 3
launch streamer python3 -u streamer_app.py
sleep 1
launch mainloop python3 -u hardware_engine/main_claw_loop.py
launch emg      python3 -u hardware_engine/sensor/udp_emg_server.py
launch voice    bash "$ROOT/scripts/start_voice_with_env.sh"

# 6. Verify each service survived
sleep 4
RC=0
declare -A PAT=( [vision]="[c]loud_rtmpose_client" [streamer]="[s]treamer_app" \
                 [mainloop]="[m]ain_claw_loop" [emg]="[u]dp_emg_server" [voice]="[v]oice_daemon" )
for name in vision streamer mainloop emg voice; do
    if ! pgrep -f "${PAT[$name]}" >/dev/null 2>&1; then
        echo "[$STAMP] ERROR: $name NOT running (see /tmp/${name}.log)" >> "$LOG"
        RC=1
    else
        echo "[$STAMP] OK: $name alive" >> "$LOG"
    fi
done
echo "[$STAMP] ===== exit=$RC =====" >> "$LOG"
exit $RC
