#!/usr/bin/env bash
# V7.30 Phase 3: reset all demo-time state files so a clean take starts from zero.
#
# Use: bash scripts/reset_demo_state.sh
#   - Wipes /dev/shm/* signal files (FSM stats, voice turn, auto trigger, etc).
#   - Resets fatigue_limit display to 1500.
#   - Stops all arecord/aplay leftovers.
#   - Does NOT kill running services (FSM, voice, streamer) — they reload state on next cycle.
#
# Safe to re-run. Idempotent.

set -u

SHM=/dev/shm

echo "[reset] killing zombie audio processes..."
sudo killall -9 arecord aplay 2>/dev/null || true

echo "[reset] removing transient signal files..."
for f in \
    "$SHM/voice_turn.json" \
    "$SHM/voice_turn.json.tmp" \
    "$SHM/voice_interrupt" \
    "$SHM/chat_active" \
    "$SHM/voice_speaking" \
    "$SHM/auto_trigger.json" \
    "$SHM/auto_mvc.json" \
    "$SHM/llm_inflight" \
    "$SHM/llm_reply.txt" \
    "$SHM/chat_input.txt" \
    "$SHM/chat_reply.txt" \
    "$SHM/chat_input.txt.seq" \
    "$SHM/chat_reply.txt.seq" \
    "$SHM/llm_reply.txt.seq" \
    "$SHM/violation_alert.txt" \
    "$SHM/voice_debug.json" \
    "$SHM/intent_exercise_mode.json" \
    "$SHM/intent_inference_mode.json" \
    "$SHM/intent_fatigue_limit.json"; do
    [ -f "$f" ] && rm -f "$f" && echo "  rm $f"
done

echo "[reset] resetting fatigue_limit display to 1500..."
cat > "$SHM/ui_fatigue_limit.json" <<EOF
{"limit": 1500, "ts": $(date +%s.%N), "src": "reset"}
EOF
cat > "$SHM/fatigue_limit.json" <<EOF
{"limit": 1500, "ts": $(date +%s.%N), "src": "reset"}
EOF

echo "[reset] resetting mute to off..."
cat > "$SHM/mute_signal.json" <<EOF
{"muted": false, "ts": $(date +%s.%N)}
EOF

echo "[reset] done. State ready for a fresh take."
