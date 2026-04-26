"""Tests for hardware_engine.voice.recorder — VADConfig + ArecordGate."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.voice.recorder import VADConfig, ArecordGate


def test_vad_config_defaults_match_design_doc():
    cfg = VADConfig()
    assert cfg.silence_end == 1.0
    assert cfg.hard_cap == 6.0
    assert cfg.active_speech_cap == 5.0
    assert cfg.pre_roll == 0.3


def test_vad_config_is_frozen_after_construction():
    cfg = VADConfig()
    with pytest.raises(AttributeError):
        cfg.silence_end = 99.0


def test_vad_config_accepts_overrides():
    cfg = VADConfig(silence_end=0.5, hard_cap=4.0)
    assert cfg.silence_end == 0.5
    assert cfg.hard_cap == 4.0


def test_apply_to_voice_daemon_sets_module_globals():
    fake_module = types.ModuleType("fake_voice_daemon")
    fake_module.VAD_TIMEOUT = 12
    fake_module.SILENCE_LIMIT = 1.2
    fake_module.ACTIVE_SPEECH_CAP = 99.0
    cfg = VADConfig(silence_end=0.8, hard_cap=5.0, active_speech_cap=4.0)
    cfg.apply_to_voice_daemon(fake_module)
    assert fake_module.VAD_TIMEOUT == 5
    assert fake_module.SILENCE_LIMIT == 0.8
    assert fake_module.ACTIVE_SPEECH_CAP == 4.0


def test_apply_to_voice_daemon_handles_missing_active_speech_cap():
    fake_module = types.ModuleType("fake_voice_daemon")
    fake_module.VAD_TIMEOUT = 12
    fake_module.SILENCE_LIMIT = 1.2
    cfg = VADConfig()
    cfg.apply_to_voice_daemon(fake_module)
    assert fake_module.VAD_TIMEOUT == 6
    assert fake_module.SILENCE_LIMIT == 1.0
    assert not hasattr(fake_module, "ACTIVE_SPEECH_CAP")


def test_arecord_gate_initial_not_suspended():
    gate = ArecordGate()
    assert not gate.suspended


def test_arecord_gate_idempotent_double_suspend(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr("subprocess.run", fake_run)
    gate = ArecordGate()
    gate.suspend()
    gate.suspend()
    assert len(calls) == 1


def test_arecord_gate_resume_after_suspend(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])

    monkeypatch.setattr("subprocess.run", fake_run)
    gate = ArecordGate()
    gate.suspend()
    gate.resume()
    assert any("-SIGSTOP" in c for c in calls)
    assert any("-SIGCONT" in c for c in calls)
    assert not gate.suspended


def test_arecord_gate_resume_without_suspend_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: calls.append(a))
    gate = ArecordGate()
    gate.resume()
    assert calls == []
