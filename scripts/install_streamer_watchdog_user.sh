#!/bin/bash
# Install a user-level streamer watchdog with a crontab fallback.
set -eu

ROOT="${IRONBUDDY_ROOT:-/home/toybrick/streamer_v3}"
PY_BIN="${PYTHON:-python3}"
if command -v "$PY_BIN" >/dev/null 2>&1; then
  PY_BIN="$(command -v "$PY_BIN")"
fi

SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/ironbuddy-streamer-watchdog.service"
LOG_FILE="/tmp/ironbuddy_streamer_watchdog.log"
STATUS_FILE="/tmp/ironbuddy_streamer_watchdog_status.json"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=IronBuddy streamer watchdog
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$PY_BIN -u $ROOT/scripts/streamer_watchdog.py --loop --root $ROOT --port 5000 --interval 5 --max-failures 3 --status $STATUS_FILE
Restart=always
RestartSec=3
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE

[Install]
WantedBy=default.target
EOF

SYSTEMD_OK=0
if command -v systemctl >/dev/null 2>&1; then
  if systemctl --user daemon-reload >/dev/null 2>&1 && \
     systemctl --user enable ironbuddy-streamer-watchdog.service >/dev/null 2>&1 && \
     systemctl --user restart ironbuddy-streamer-watchdog.service >/dev/null 2>&1; then
    SYSTEMD_OK=1
  fi
fi

CRON_LINE="@reboot cd $ROOT && $PY_BIN -u $ROOT/scripts/streamer_watchdog.py --loop --root $ROOT --port 5000 --interval 5 --max-failures 3 --status $STATUS_FILE >> $LOG_FILE 2>&1"
if command -v crontab >/dev/null 2>&1; then
  (crontab -l 2>/dev/null | grep -v 'streamer_watchdog.py --loop --root'; echo "$CRON_LINE") | crontab -
fi

if [ "$SYSTEMD_OK" -eq 0 ]; then
  if ! pgrep -af 'streamer_watchdog.py --loop --root' >/dev/null 2>&1; then
    nohup "$PY_BIN" -u "$ROOT/scripts/streamer_watchdog.py" --loop --root "$ROOT" --port 5000 --interval 5 --max-failures 3 --status "$STATUS_FILE" >> "$LOG_FILE" 2>&1 &
  fi
fi

"$PY_BIN" -u "$ROOT/scripts/streamer_watchdog.py" --root "$ROOT" --port 5000 --status "$STATUS_FILE" || true

echo "systemd_user=$SYSTEMD_OK"
echo "service_file=$SERVICE_FILE"
echo "status_file=$STATUS_FILE"
echo "log_file=$LOG_FILE"
