#!/usr/bin/env bash
# Recover only the IronBuddy web/control surface from WSL.
#
# Usage:
#   IRONBUDDY_BOARD_IP=10.29.10.224 bash scripts/recover_streamer.sh
#
# This deliberately does not stop training services. It only restarts the
# board-side streamer_app.py through the precise restart script.

set -euo pipefail

BOARD_IP="${IRONBUDDY_BOARD_IP:-10.29.10.224}"
BOARD_USER="${IRONBUDDY_BOARD_USER:-toybrick}"
BOARD_KEY="${IRONBUDDY_BOARD_KEY:-$HOME/.ssh/id_rsa_toybrick}"
BOARD_ROOT="${IRONBUDDY_BOARD_ROOT:-/home/toybrick/streamer_v3}"
URL="http://${BOARD_IP}:5000/api/fsm_state"

SSH_OPTS=(
  -i "$BOARD_KEY"
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=8
)

echo "[recover] board=${BOARD_USER}@${BOARD_IP}"
echo "[recover] restarting web/control surface only..."

ssh "${SSH_OPTS[@]}" "${BOARD_USER}@${BOARD_IP}" \
  "cd '$BOARD_ROOT' && bash scripts/restart_streamer.sh"

echo "[recover] waiting for ${URL} ..."
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl --noproxy '*' -fsS --max-time 3 "$URL" >/dev/null; then
    echo "[recover] ok: http://${BOARD_IP}:5000/"
    exit 0
  fi
  sleep 1
done

echo "[recover] streamer did not answer within timeout" >&2
exit 1
