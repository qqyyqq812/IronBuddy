"""Tests for hardware_engine.voice.turn — Turn id + atomic JSON writer."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.voice.turn import Turn, TurnWriter


def test_new_turn_has_8_hex_id():
    t = Turn.new()
    assert len(t.turn_id) == 8
    assert all(c in "0123456789abcdef" for c in t.turn_id)


def test_new_turn_has_recent_started_ts():
    t = Turn.new()
    assert abs(time.time() - t.started_ts) < 1.0


def test_two_turns_have_distinct_ids():
    a = Turn.new()
    b = Turn.new()
    assert a.turn_id != b.turn_id


def test_writer_emits_json_with_required_fields(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="wake")
    data = json.loads(p.read_text())
    assert data["turn_id"] == t.turn_id
    assert data["stage"] == "wake"
    assert "started_ts" in data
    assert "ts" in data


def test_writer_includes_text_when_provided(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="user_input", text="切到深蹲")
    data = json.loads(p.read_text())
    assert data["text"] == "切到深蹲"
    assert data["stage"] == "user_input"


def test_writer_atomic_via_tmp_file(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="wake")
    assert p.exists()
    assert not (tmp_path / "voice_turn.json.tmp").exists()


def test_writer_rejects_unknown_stage(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    with pytest.raises(ValueError):
        w.write(t, stage="bogus")


def test_writer_extra_fields_merged(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="auto", extra={"trigger_source": "fatigue_max"})
    data = json.loads(p.read_text())
    assert data["trigger_source"] == "fatigue_max"


def test_same_turn_multiple_stages_keep_id(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="wake")
    w.write(t, stage="user_input", text="hi")
    w.write(t, stage="assistant_reply", text="ok")
    final = json.loads(p.read_text())
    assert final["turn_id"] == t.turn_id
    assert final["stage"] == "assistant_reply"
