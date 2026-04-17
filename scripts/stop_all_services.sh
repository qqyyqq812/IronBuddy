#!/bin/bash
# IronBuddy V3 graceful stop: SIGTERM -> 0.8s -> SIGKILL, then blanket pkill
set -u
LOG="/tmp/ironbuddy_startup.log"
STAMP="$(date '+%F %T')"
echo "[$STAMP] ===== IronBuddy stop =====" >> "$LOG"

declare -A PAT=( [vision]="cloud_rtmpose_client" [streamer]="streamer_app" \
                 [mainloop]="main_claw_loop"   [emg]="udp_emg_server" [voice]="voice_daemon" )

for name in vision streamer mainloop emg voice; do
    PIDFILE="/tmp/ironbuddy_${name}.pid"
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE" 2>/dev/null || echo "")
        if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
            kill -TERM "$PID" 2>/dev/null || true
            sleep 0.8
            kill -KILL "$PID" 2>/dev/null || true
            echo "[$STAMP] stopped $name pid=$PID" >> "$LOG"
        fi
    fi
    # Fallback blanket kill by pattern (bracket trick)
    first="${PAT[$name]:0:1}"
    rest="${PAT[$name]:1}"
    pkill -TERM -f "[${first}]${rest}" 2>/dev/null || true
done
sleep 0.8
for name in vision streamer mainloop emg voice; do
    first="${PAT[$name]:0:1}"
    rest="${PAT[$name]:1}"
    pkill -KILL -f "[${first}]${rest}" 2>/dev/null || true
done

rm -f /tmp/ironbuddy_*.pid
echo "[$STAMP] ===== stop done =====" >> "$LOG"
exit 0
