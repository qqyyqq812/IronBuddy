#!/bin/bash
# cloud_gpu_bootstrap.sh — start cloned IronBuddy GPU services via SSH.
#
# Reads .api_config.json and starts the cloud services expected by the board:
#   6006: RTMPose HTTP server
#   6333: Qdrant-compatible vector store
#   8008: embedding HTTP server
#
# The SSH password is never printed.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/../.api_config.json"
LOG=/tmp/cloud_gpu_bootstrap.log

if [ ! -f "$CONFIG" ]; then
    echo "[cloud_gpu_bootstrap] config not found: $CONFIG" >&2
    exit 1
fi

read_json() {
    python3 -c "import json; print(json.load(open('$CONFIG')).get('$1',''))"
}

HOST=$(read_json CLOUD_SSH_HOST)
PORT=$(read_json CLOUD_SSH_PORT)
USER=$(read_json CLOUD_SSH_USER)
PASS=$(read_json CLOUD_SSH_PASSWORD)
PORT=${PORT:-22}
USER=${USER:-root}

if [ -z "$HOST" ] || [ -z "$PASS" ]; then
    echo "[cloud_gpu_bootstrap] missing credentials" >&2
    exit 1
fi

if ! command -v expect >/dev/null 2>&1; then
    echo "[cloud_gpu_bootstrap] expect not installed" >&2
    exit 1
fi

REMOTE_FILE=/tmp/cloud_gpu_bootstrap_remote.sh
EXPECT_FILE=/tmp/cloud_gpu_bootstrap.exp
REMOTE_RUN=/tmp/ironbuddy_cloud_gpu_bootstrap.sh

cleanup() {
    rm -f "$REMOTE_FILE" "$EXPECT_FILE"
}
trap cleanup EXIT

cat > "$REMOTE_FILE" <<'REMOTE_EOF'
set -eu
export PATH=/root/miniconda3/bin:$PATH

if ! curl -fsS -m 3 http://127.0.0.1:6006/health >/dev/null 2>&1; then
  pkill -f '[r]tmpose_http_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_cloud
  nohup python rtmpose_http_server.py >> server.log 2>&1 < /dev/null &
  echo $! > server.pid
fi

if ! curl -fsS -m 3 http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
  pkill -f '[q]drant --http-port 6333|[q]drant_compat_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_rag
  nohup bash start_qdrant.sh > qdrant.log 2>&1 < /dev/null &
  echo $! > qdrant.pid
fi

if ! curl -fsS -m 3 http://127.0.0.1:8008/health >/dev/null 2>&1; then
  pkill -f '[e]mbedding_server.py' >/dev/null 2>&1 || true
  cd /root/ironbuddy_rag
  nohup bash start_embedding.sh > embedding.log 2>&1 < /dev/null &
  echo $! > embedding.pid
fi

sleep 2
printf 'rtmpose='
curl -fsS -m 5 http://127.0.0.1:6006/health >/dev/null 2>&1 && echo ready || echo starting
printf 'qdrant='
curl -fsS -m 5 http://127.0.0.1:6333/healthz >/dev/null 2>&1 && echo ready || echo starting
printf 'embedding='
curl -fsS -m 5 http://127.0.0.1:8008/health >/dev/null 2>&1 && echo ready || echo starting
REMOTE_EOF

REMOTE_B64=$(base64 "$REMOTE_FILE" | tr -d '\n')

cat > "$EXPECT_FILE" <<EXPECT_EOF
#!/usr/bin/expect -f
set timeout 90
log_user 0
spawn ssh -p ${PORT} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 ${USER}@${HOST} "printf %s '${REMOTE_B64}' | base64 -d > '${REMOTE_RUN}' && bash '${REMOTE_RUN}'; rm -f '${REMOTE_RUN}'"
expect {
    -re "yes/no" { send "yes\r"; exp_continue }
    -re "(P|p)assword:" { send "${PASS}\r" }
    timeout { puts "[cloud_gpu_bootstrap] timeout waiting for password prompt"; exit 2 }
    eof { puts "[cloud_gpu_bootstrap] eof before password prompt"; exit 3 }
}
log_user 1
expect eof
catch wait result
set exit_status [lindex \$result 3]
exit \$exit_status
EXPECT_EOF

chmod +x "$EXPECT_FILE"
echo "[cloud_gpu_bootstrap] $(date) starting ${USER}@${HOST}:${PORT}" >> "$LOG"
expect -f "$EXPECT_FILE" 2>&1 | sed -e "s/${PASS}/PASSWORD_REDACTED/g" | tee -a "$LOG"
rc=${PIPESTATUS[0]}
if [ "$rc" -eq 0 ]; then
    echo "[cloud_gpu_bootstrap] done"
else
    echo "[cloud_gpu_bootstrap] failed rc=$rc" >&2
fi
exit "$rc"
