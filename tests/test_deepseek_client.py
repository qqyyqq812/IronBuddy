"""Tests for hardware_engine.cognitive.deepseek_client.

All HTTP calls are mocked; no real DeepSeek requests fire.
"""
import io
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.cognitive.deepseek_client import (
    DeepSeekClient, DeepSeekConfig, ToolResponse,
)


def _fake_response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def test_config_defaults():
    cfg = DeepSeekConfig(api_key="sk-test")
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.timeout == 8.0
    assert cfg.model == "deepseek-chat"


def test_chat_returns_none_without_api_key():
    client = DeepSeekClient(DeepSeekConfig(api_key=""))
    assert client.chat("sys", "user") is None


def test_chat_with_tools_returns_empty_response_without_api_key():
    client = DeepSeekClient(DeepSeekConfig(api_key=""))
    out = client.chat_with_tools("sys", "user", tools=[])
    assert isinstance(out, ToolResponse)
    assert out.content == ""
    assert out.tool_calls == []
    assert not out.has_tool_call


def test_chat_returns_message_content():
    cfg = DeepSeekConfig(api_key="sk-test")
    client = DeepSeekClient(cfg)
    payload = {"choices": [{"message": {"content": "hello"}}]}
    with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        out = client.chat("sys", "user")
    assert out == "hello"


def test_chat_swallows_network_error():
    import urllib.error as ue
    cfg = DeepSeekConfig(api_key="sk-test")
    client = DeepSeekClient(cfg)
    with mock.patch("urllib.request.urlopen", side_effect=ue.URLError("boom")):
        out = client.chat("sys", "user")
    assert out is None


def test_chat_with_tools_extracts_tool_calls():
    cfg = DeepSeekConfig(api_key="sk-test")
    client = DeepSeekClient(cfg)
    payload = {
        "choices": [
            {
                "message": {
                    "content": "ok",
                    "tool_calls": [
                        {"id": "x1", "function": {"name": "switch_exercise",
                                                    "arguments": '{"exercise":"squat"}'}},
                    ],
                }
            }
        ]
    }
    with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
        out = client.chat_with_tools("sys", "user", tools=[{"type": "function"}])
    assert out.has_tool_call
    assert out.tool_calls[0]["function"]["name"] == "switch_exercise"
    assert out.content == "ok"


def test_chat_with_tools_swallows_malformed_response():
    cfg = DeepSeekConfig(api_key="sk-test")
    client = DeepSeekClient(cfg)
    with mock.patch("urllib.request.urlopen", return_value=_fake_response({"oops": True})):
        out = client.chat_with_tools("sys", "user", tools=[])
    assert isinstance(out, ToolResponse)
    assert out.content == ""
    assert not out.has_tool_call


def test_from_config_reads_env_var(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    client = DeepSeekClient.from_config()
    assert client.config.api_key == "sk-from-env"


def test_from_config_falls_back_to_api_config_json(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    cfg_path = tmp_path / ".api_config.json"
    cfg_path.write_text(json.dumps({"DEEPSEEK_API_KEY": "sk-from-file"}))
    real_join = os.path.join

    def fake_join(*parts):
        if any(".api_config.json" in str(p) for p in parts):
            return str(cfg_path)
        return real_join(*parts)

    monkeypatch.setattr(os.path, "join", fake_join)
    client = DeepSeekClient.from_config()
    assert client.config.api_key == "sk-from-file"


def test_from_config_returns_empty_when_no_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("builtins.open", mock.MagicMock(side_effect=OSError("no file")))
    client = DeepSeekClient.from_config()
    assert client.config.api_key == ""
