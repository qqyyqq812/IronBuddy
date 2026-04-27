#!/usr/bin/env bash
# V7.30 Phase 3: prepare environment for subvideo 1 (squat takes).
#
# Pre-flight: services should already be running (start_validation.sh).
# This script seeds the demo state so a single take can roll cleanly.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHM=/dev/shm

bash "$SCRIPT_DIR/reset_demo_state.sh"

echo "[squat-prep] setting exercise=squat, vision=pure_vision..."
cat > "$SHM/exercise_mode.json" <<EOF
{"mode": "squat", "ts": $(date +%s.%N), "src": "subvideo_squat"}
EOF
cat > "$SHM/inference_mode.json" <<EOF
{"mode": "pure_vision", "ts": $(date +%s.%N), "src": "subvideo_squat"}
EOF
cat > "$SHM/user_profile.json" <<EOF
{"exercise": "squat", "ts": $(date +%s.%N)}
EOF

echo "[squat-prep] showing current FSM state target:"
echo "  exercise: squat"
echo "  vision:   pure_vision"
echo "  fatigue:  0 / 1500 (reset)"
echo ""
echo "[squat-prep] checklist before camera roll:"
echo "  [ ] FSM running          (pgrep -f '[m]ain_claw_loop')"
echo "  [ ] streamer running     (pgrep -f '[s]treamer_app')"
echo "  [ ] voice_daemon running (pgrep -f '[v]oice_daemon')"
echo "  [ ] HDMI monitor live    (xset q | grep Monitor)"
echo "  [ ] OBS / camera armed"
echo ""
echo "[squat-prep] ready. Counter at 0, take 1 may start."
