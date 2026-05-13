"""IronBuddy voice subsystem (V7.30 refactor).

Modules:
    state    — VoiceStateMachine (3 explicit states)
    recorder — VAD config + arecord process gate
    turn     — Turn id + voice_turn.json writer (UI bubble dedupe)
    tools    — DeepSeek tool calling spec (Phase 2)
    router   — User text → tool call dispatcher (Phase 2)
"""
