"""Tests for hardware_engine.voice.state — VoiceStateMachine.

AAA pattern; describes behavior, not implementation.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.voice.state import VoiceState, VoiceStateMachine


def test_initial_state_is_listen():
    sm = VoiceStateMachine()
    assert sm.state == VoiceState.LISTEN


def test_listen_to_dialog_on_wake():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="wake_detected")
    assert sm.state == VoiceState.DIALOG


def test_dialog_to_listen_on_completion():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="wake")
    sm.transition(VoiceState.LISTEN, reason="dialog_done")
    assert sm.state == VoiceState.LISTEN


def test_busy_releases_back_to_listen():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.BUSY, reason="auto_trigger")
    sm.transition(VoiceState.LISTEN, reason="tts_done")
    assert sm.state == VoiceState.LISTEN


def test_history_records_each_transition():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="wake")
    sm.transition(VoiceState.LISTEN, reason="dialog_done")
    assert len(sm.history) == 2
    assert sm.history[0].from_state == VoiceState.LISTEN
    assert sm.history[0].to_state == VoiceState.DIALOG
    assert sm.history[0].reason == "wake"
    assert sm.history[1].from_state == VoiceState.DIALOG
    assert sm.history[1].to_state == VoiceState.LISTEN


def test_is_membership_check():
    sm = VoiceStateMachine()
    assert sm.is_(VoiceState.LISTEN)
    assert sm.is_(VoiceState.LISTEN, VoiceState.DIALOG)
    assert not sm.is_(VoiceState.BUSY)


def test_time_in_state_increases():
    sm = VoiceStateMachine()
    t0 = sm.time_in_state
    time.sleep(0.05)
    t1 = sm.time_in_state
    assert t1 > t0


def test_transition_resets_time_in_state():
    sm = VoiceStateMachine()
    time.sleep(0.05)
    before = sm.time_in_state
    sm.transition(VoiceState.DIALOG, reason="wake")
    after = sm.time_in_state
    assert after < before
