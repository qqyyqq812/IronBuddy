"""Tests for hardware_engine.voice.recorder."""
import os
import sys
import types

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.voice.recorder import ArecordGate, VADConfig


def test_vad_config_defaults_match_current_board_contract(monkeypatch):
    monkeypatch.delenv("VOICE_SILENCE_LIMIT", raising=False)
    cfg = VADConfig()
    assert cfg.silence_end == 2.0
    assert cfg.hard_cap == 6.0
    assert cfg.active_speech_cap == 5.0
    assert cfg.pre_roll == 0.3


def test_vad_config_can_read_silence_limit_env(monkeypatch):
    monkeypatch.setenv("VOICE_SILENCE_LIMIT", "1.7")
    cfg = VADConfig()
    assert cfg.silence_end == 1.7


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
    assert fake_module.SILENCE_LIMIT == 2.0
    assert not hasattr(fake_module, "ACTIVE_SPEECH_CAP")


def test_arecord_gate_initial_not_suspended():
    gate = ArecordGate()
    assert not gate.suspended


def test_arecord_gate_idempotent_double_suspend(monkeypatch):
    calls = []

    def fake_signal(self, sig, process_names):
        calls.append((sig, tuple(process_names)))
        return True

    monkeypatch.setattr(ArecordGate, "_signal", fake_signal)
    gate = ArecordGate()
    gate.suspend()
    gate.suspend()
    assert calls == [("-SIGSTOP", ("arecord",))]


def test_arecord_gate_resume_after_suspend(monkeypatch):
    calls = []

    def fake_signal(self, sig, process_names):
        calls.append((sig, tuple(process_names)))
        return True

    monkeypatch.setattr(ArecordGate, "_signal", fake_signal)
    gate = ArecordGate()
    gate.suspend()
    gate.resume()
    assert ("-SIGSTOP", ("arecord",)) in calls
    assert ("-SIGCONT", ("arecord", "sudo")) in calls
    assert not gate.suspended


def test_arecord_gate_resume_without_suspend_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(ArecordGate, "_signal",
                        lambda self, sig, names: calls.append((sig, names)) or True)
    gate = ArecordGate()
    gate.resume()
    assert calls == []
