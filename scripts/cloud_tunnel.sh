#!/bin/bash
# cloud_tunnel.sh — persistent SSH tunnel from localhost:6006 → cloud:6006 for RTMPose inference
# Reads credentials from ../.api_config.json. Idempotent: no-op if tunnel already running.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/../.api_config.json"

if [ ! -f "$CONFIG" ]; then
    echo "[cloud_tunnel] config not found: $CONFIG" >&2
    exit 1
fi

# Parse JSON with python3 (available on both WSL and board)
read_json() {
    python3 -c "import json,sys; print(json.load(open('$CONFIG')).get('$1',''))"
}

HOST=$(read_json CLOUD_SSH_HOST)
PORT=$(read_json CLOUD_SSH_PORT)
USER=$(read_json CLOUD_SSH_USER)
PASS=$(read_json CLOUD_SSH_PASSWORD)
LPORT=$(read_json CLOUD_LOCAL_TUNNEL_PORT)
LPORT=${LPORT:-6006}

if [ -z "$HOST" ] || [ -z "$PASS" ]; then
    echo "[cloud_tunnel] missing SSH credentials in .api_config.json" >&2
    exit 1
fi

# Bracket trick to avoid matching the pgrep itself
if pgrep -f "[s]sh.*-L.*${LPORT}:127.0.0.1:6006" > /dev/null; then
    echo "[cloud_tunnel] tunnel already running"
    exit 0
fi

LOG=/tmp/cloud_tunnel.log
echo "[cloud_tunnel] $(date) starting tunnel to ${USER}@${HOST}:${PORT}" >> "$LOG"

# Prefer expect if available
if command -v expect > /dev/null 2>&1; then
    # Keep ssh in foreground under expect; nohup detaches the whole expect process.
    # expect holds ssh's stdin open so the tunnel stays alive. If ssh dies, expect exits.
    EXPECT_FILE="/tmp/cloud_tunnel.exp"
    cat > "$EXPECT_FILE" <<EXPECTEOF
#!/usr/bin/expect -f
set timeout 25
spawn ssh -N -L ${LPORT}:127.0.0.1:6006 -p ${PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes ${USER}@${HOST}
expect {
    -re "yes/no" { send "yes\r"; exp_continue }
    -re "(P|p)assword:" { send "${PASS}\r" }
    timeout { puts "TIMEOUT waiting for password prompt"; exit 2 }
    eof { puts "EOF before password prompt"; exit 3 }
}
# After password, ssh -N runs indefinitely. Wait on eof.
set timeout -1
expect eof
EXPECTEOF
    chmod +x "$EXPECT_FILE"
    nohup expect -f "$EXPECT_FILE" >> "$LOG" 2>&1 < /dev/null &
    disown
    echo "[cloud_tunnel] spawned via expect (pidfile), PID=$!" | tee -a "$LOG"
elif command -v python3 > /dev/null 2>&1 && python3 -c "import pexpect" 2>/dev/null; then
    nohup python3 "$SCRIPT_DIR/cloud_tunnel.py" >> "$LOG" 2>&1 < /dev/null &
    disown
    echo "[cloud_tunnel] spawned via pexpect, PID=$!" | tee -a "$LOG"
else
    echo "[cloud_tunnel] ERROR: neither 'expect' nor 'python3-pexpect' available" | tee -a "$LOG" >&2
    exit 1
fi

# Poll up to 15s for tunnel to come up (cloud SSH auth over WAN can be slow)
TUNNEL_UP=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 1
    if pgrep -f "[s]sh.*-L.*${LPORT}:127.0.0.1:6006" > /dev/null; then
        TUNNEL_UP="yes"
        break
    fi
done

if [ -n "$TUNNEL_UP" ]; then
    echo "[cloud_tunnel] ✓ tunnel established on 127.0.0.1:${LPORT} (after ${i}s)"
    # Quick health probe
    if command -v curl > /dev/null 2>&1; then
        if curl -s -m 5 "http://127.0.0.1:${LPORT}/health" > /dev/null 2>&1; then
            echo "[cloud_tunnel] ✓ cloud /health responded"
        else
            echo "[cloud_tunnel] ⚠ tunnel up but /health not reachable yet (may warm up)"
        fi
    fi
    exit 0
else
    echo "[cloud_tunnel] ✗ tunnel failed to start within 15s, check $LOG"
    tail -30 "$LOG"
    exit 1
fi
