#!/usr/bin/env bash
# V7.30 Phase 3: prepare environment for subvideo 2 (curl takes).
#
# Pre-flight: services should already be running (start_validation.sh).
# Difference from squat: vision_sensor mode by default (curl demo highlights EMG fusion).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHM=/dev/shm

bash "$SCRIPT_DIR/reset_demo_state.sh"

echo "[curl-prep] setting exercise=curl, vision=vision_sensor..."
cat > "$SHM/exercise_mode.json" <<EOF
{"mode": "curl", "ts": $(date +%s.%N), "src": "subvideo_curl"}
EOF
cat > "$SHM/inference_mode.json" <<EOF
{"mode": "vision_sensor", "ts": $(date +%s.%N), "src": "subvideo_curl"}
EOF
cat > "$SHM/user_profile.json" <<EOF
{"exercise": "bicep_curl", "ts": $(date +%s.%N)}
EOF

echo "[curl-prep] showing current FSM state target:"
echo "  exercise: bicep_curl"
echo "  vision:   vision_sensor (NN + EMG fusion)"
echo "  fatigue:  0 / 1500 (reset)"
echo ""
echo "[curl-prep] checklist before camera roll:"
echo "  [ ] FSM running          (pgrep -f '[m]ain_claw_loop')"
echo "  [ ] streamer running     (pgrep -f '[s]treamer_app')"
echo "  [ ] voice_daemon running (pgrep -f '[v]oice_daemon')"
echo "  [ ] EMG simulator OR ESP32 broadcasting on UDP:8080"
echo "  [ ] MVC calibration done? (cat /dev/shm/emg_calibration.json | jq .calibrated)"
echo "  [ ] OBS / camera armed"
echo ""
echo "[curl-prep] ready. Counter at 0, take 1 may start."
