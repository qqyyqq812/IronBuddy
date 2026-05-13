#!/bin/bash
# rag_tunnel.sh — persistent SSH tunnel for cloud vector RAG services.
# Maps board localhost:6333 -> cloud Qdrant-compatible store
# and board localhost:8008 -> cloud embedding service.

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/../.api_config.json"

if [ ! -f "$CONFIG" ]; then
    echo "[rag_tunnel] config not found: $CONFIG" >&2
    exit 1
fi

read_json() {
    python3 -c "import json; print(json.load(open('$CONFIG')).get('$1',''))"
}

HOST=$(read_json CLOUD_SSH_HOST)
PORT=$(read_json CLOUD_SSH_PORT)
USER=$(read_json CLOUD_SSH_USER)
PASS=$(read_json CLOUD_SSH_PASSWORD)
VECTOR_LPORT=$(read_json RAG_VECTOR_LOCAL_PORT)
EMBED_LPORT=$(read_json RAG_EMBEDDING_LOCAL_PORT)
VECTOR_LPORT=${VECTOR_LPORT:-6333}
EMBED_LPORT=${EMBED_LPORT:-8008}

if [ -z "$HOST" ] || [ -z "$PASS" ]; then
    echo "[rag_tunnel] missing SSH credentials in .api_config.json" >&2
    exit 1
fi

if pgrep -f "[s]sh.*-L.*${VECTOR_LPORT}:127.0.0.1:6333.*-L.*${EMBED_LPORT}:127.0.0.1:8008" > /dev/null; then
    echo "[rag_tunnel] tunnel already running"
    exit 0
fi

LOG=/tmp/rag_tunnel.log
echo "[rag_tunnel] $(date) starting tunnel to ${USER}@${HOST}:${PORT}" >> "$LOG"

if command -v expect > /dev/null 2>&1; then
    EXPECT_FILE="/tmp/rag_tunnel.exp"
    cat > "$EXPECT_FILE" <<EXPECTEOF
#!/usr/bin/expect -f
set timeout 25
spawn ssh -N -L ${VECTOR_LPORT}:127.0.0.1:6333 -L ${EMBED_LPORT}:127.0.0.1:8008 -p ${PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ControlMaster=no -o ControlPath=none -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes ${USER}@${HOST}
expect {
    -re "yes/no" { send "yes\r"; exp_continue }
    -re "(P|p)assword:" { send "${PASS}\r" }
    timeout { puts "No password prompt; assuming public-key tunnel established" }
    eof { puts "EOF before password prompt"; exit 3 }
}
set timeout -1
expect eof
EXPECTEOF
    chmod +x "$EXPECT_FILE"
    nohup expect -f "$EXPECT_FILE" >> "$LOG" 2>&1 < /dev/null &
    disown
    echo "[rag_tunnel] spawned via expect, PID=$!" | tee -a "$LOG"
elif command -v python3 > /dev/null 2>&1 && python3 -c "import pexpect" 2>/dev/null; then
    nohup python3 "$SCRIPT_DIR/rag_tunnel.py" >> "$LOG" 2>&1 < /dev/null &
    disown
    echo "[rag_tunnel] spawned via pexpect, PID=$!" | tee -a "$LOG"
else
    echo "[rag_tunnel] ERROR: neither 'expect' nor 'python3-pexpect' available" | tee -a "$LOG" >&2
    exit 1
fi

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 1
    if pgrep -f "[s]sh.*-L.*${VECTOR_LPORT}:127.0.0.1:6333.*-L.*${EMBED_LPORT}:127.0.0.1:8008" > /dev/null; then
        echo "[rag_tunnel] tunnel established on 127.0.0.1:${VECTOR_LPORT},127.0.0.1:${EMBED_LPORT}"
        exit 0
    fi
done

echo "[rag_tunnel] tunnel failed to start; check $LOG" >&2
tail -30 "$LOG"
exit 1
