#!/usr/bin/env bash
# Install/upgrade IronBuddy OpenClaw systemd service on Toybrick board.
#
# Run on the board itself (or via SSH that has interactive sudo):
#     sudo bash systemd/install_openclaw_systemd.sh
#
# Idempotent: safe to re-run after every redeploy.
set -euo pipefail

UNIT_NAME="ironbuddy-openclaw.service"
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/${UNIT_NAME}"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}"

if [ ! -f "${UNIT_SRC}" ]; then
    echo "[install_openclaw_systemd] unit file not found: ${UNIT_SRC}" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "[install_openclaw_systemd] please run with sudo (need to write /etc/systemd)" >&2
    exit 2
fi

echo "[install_openclaw_systemd] copying unit file…"
install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"

echo "[install_openclaw_systemd] daemon-reload + enable + restart…"
systemctl daemon-reload
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

sleep 1
systemctl --no-pager --lines=15 status "${UNIT_NAME}" || true

echo ""
echo "[install_openclaw_systemd] done."
echo "Logs:  journalctl -u ${UNIT_NAME} -f"
echo "Stop:  sudo systemctl stop ${UNIT_NAME}"
