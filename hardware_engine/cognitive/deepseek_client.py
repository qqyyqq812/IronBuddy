"""V7.30 unified DeepSeek client.

Replaces the four scattered call sites:
    voice_daemon.py        — _try_deepseek_chat
    cognitive/deepseek_direct.py — direct urlopen
    streamer_app.py        — DeepSeek chat preview
    main_claw_loop.py      — fatigue summary trigger

All four hit the same /v1/chat/completions endpoint with slightly different
parameters. This module standardises:
    - api key loading (env var → .api_config.json fallback)
    - timeout + retry policy
    - error logging
    - tool calling envelope (Phase 2 router)

Python 3.7 compatible.
"""
import os
import json
import logging
import urllib.request
import urllib.error


class DeepSeekConfig(object):
    __slots__ = ("api_key", "base_url", "model", "timeout", "max_retries")

    def __init__(self, api_key, base_url="https://api.deepseek.com/v1",
                 model="deepseek-chat", timeout=8.0, max_retries=1):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries


class ToolResponse(object):
    __slots__ = ("content", "tool_calls", "raw")

    def __init__(self, content="", tool_calls=None, raw=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.raw = raw

    @property
    def has_tool_call(self):
        return bool(self.tool_calls)


class DeepSeekClient(object):
    def __init__(self, config):
        self.config = config

    def _post(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.api_key,
            },
        )
        return urllib.request.urlopen(req, timeout=self.config.timeout)

    def chat(self, system, user, max_tokens=200, temperature=0.7):
        if not self.config.api_key:
            return None
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            resp = self._post(payload)
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            logging.warning(u"DeepSeek chat failed: %s", e)
            return None

    def chat_with_tools(self, system, user, tools, max_tokens=400, temperature=0.3):
        if not self.config.api_key:
            return ToolResponse()
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            resp = self._post(payload)
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            return ToolResponse(
                content=msg.get("content", "") or "",
                tool_calls=msg.get("tool_calls", []) or [],
                raw=data,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            logging.warning(u"DeepSeek chat_with_tools failed: %s", e)
            return ToolResponse()

    @classmethod
    def from_config(cls):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            cfg_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "..", ".api_config.json",
            )
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    api_key = json.load(f).get("DEEPSEEK_API_KEY", "")
            except (OSError, ValueError):
                pass
        return cls(DeepSeekConfig(api_key=api_key))
