"""voice/turn — dialog Turn id + voice_turn.json writer (S1 fix prep).

S1: UI used to render a fresh chat bubble on every chat-poll update,
producing duplicate bubbles when STT mid-recognition kept coming in.
Solution: every dialog turn carries a uuid; UI dedupes by turn_id.

The writer atomic-renames over /dev/shm/voice_turn.json so the streamer
chat-poll always sees a complete JSON.
"""
import os
import json
import time
import uuid


class Turn(object):
    """Immutable turn id + start timestamp."""
    __slots__ = ("turn_id", "started_ts")

    def __init__(self, turn_id, started_ts):
        self.turn_id = turn_id
        self.started_ts = started_ts

    @classmethod
    def new(cls):
        return cls(uuid.uuid4().hex[:8], time.time())


class TurnWriter(object):
    """Atomic /dev/shm/voice_turn.json writer (rename-over).

    Stages:
        wake             — wake word detected, recording started
        user_input       — STT result available
        assistant_reply  — LLM/handler text returned, TTS queued
        closed           — turn ended (success path)
        auto             — auto-trigger (fatigue / violation), not user-driven
    """

    VALID_STAGES = ("wake", "user_input", "assistant_reply", "closed", "auto")

    def __init__(self, path="/dev/shm/voice_turn.json"):
        self.path = path

    def write(self, turn, stage, text=None, extra=None):
        if stage not in self.VALID_STAGES:
            raise ValueError("unknown stage: %s" % stage)
        payload = {
            "turn_id": turn.turn_id,
            "started_ts": turn.started_ts,
            "stage": stage,
            "ts": time.time(),
        }
        if text is not None:
            payload["text"] = text
        if extra:
            payload.update(extra)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, self.path)
