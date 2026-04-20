"""FeishuClient - IronBuddy 飞书开放平台客户端。

设计原则：
  - **dry_run 安全默认**：未显式打开 IRONBUDDY_FEISHU_DRY_RUN=0 时，绝不发起真实
    HTTP 请求，仅打日志。适合演示前 / CI / 本机调试。
  - **无第三方依赖**：仅用 urllib（Toybrick Python 3.7 兼容，与 streamer_app.py
    风格一致，禁用 pandas）。
  - **凭据多级回退**：构造参数 → 环境变量 → ~/.ironbuddy/secrets.env → 项目
    .api_config.json。真实密钥永远不出现在仓库里。
  - **异常不抛出**：所有公开方法返回 {"ok": bool, ...}，保障调用方（daemon、Flask
    端点）不会因网络抖动崩溃。
  - **token 缓存**：tenant_access_token 默认 2h，过期前 120s 自动续签。

Python 3.7 兼容：不使用 `X | None`、`match/case`、`:=`、f-string 自带 `=`。
"""

from __future__ import absolute_import

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

_LOG = logging.getLogger("ironbuddy.feishu")
if not _LOG.handlers:
    # 库级 logger：不配置 handler（交给宿主），但保证 INFO 及以上可见。
    _LOG.setLevel(logging.INFO)

# ---- 文件路径（可在调用方覆盖）---------------------------------------------
_USER_SECRETS_PATH = os.path.expanduser("~/.ironbuddy/secrets.env")

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_PROJECT_API_CONFIG = os.path.join(_PROJECT_ROOT, ".api_config.json")

# ---- 常量 ------------------------------------------------------------------
_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"

# 默认 token 有效期（秒）与续签 buffer
_TOKEN_TTL_FALLBACK = 7200
_TOKEN_RENEW_BUFFER = 120


def _parse_env_file(path):
    """解析 KEY=VALUE 格式的 secrets 文件。容忍注释、空行、引号。"""
    result = {}  # type: Dict[str, str]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
    except Exception as exc:
        _LOG.debug("parse env file failed: %s path=%s", exc, path)
    return result


def _load_api_config_snapshot():
    """只读快照：项目 .api_config.json（与 streamer_app 的行为保持一致）。"""
    try:
        with open(_PROJECT_API_CONFIG, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


class FeishuClient(object):
    """飞书 IM 最小客户端。

    用法::

        c = FeishuClient()                    # dry_run 默认开
        c.send_text("hello")                  # 只打 log
        c.send_card(FeishuClient.build_morning_card(...))

        # 生产环境开关
        #   export IRONBUDDY_FEISHU_DRY_RUN=0
    """

    TOKEN_URL = _TOKEN_URL
    MSG_URL = _MSG_URL

    def __init__(
        self,
        app_id=None,        # type: Optional[str]
        app_secret=None,    # type: Optional[str]
        chat_id=None,       # type: Optional[str]
        dry_run=None,       # type: Optional[bool]
        timeout=15,         # type: int
    ):
        self.app_id = app_id or self._resolve("FEISHU_APP_ID")
        self.app_secret = app_secret or self._resolve("FEISHU_APP_SECRET")
        self.chat_id = chat_id or self._resolve("FEISHU_CHAT_ID")

        if dry_run is None:
            # 默认 True = 安全默认；只有明确写 0 / false / no 才真发。
            env_val = os.environ.get("IRONBUDDY_FEISHU_DRY_RUN", "1").strip().lower()
            dry_run = env_val not in ("0", "false", "no", "off")
        self.dry_run = bool(dry_run)

        self.timeout = int(timeout)
        self._token = None                # type: Optional[str]
        self._token_expires = 0.0         # type: float

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _resolve(key):
        """凭据多级回退：env → secrets.env → .api_config.json。

        .api_config.json 兼容大小写两种键名（与 streamer_app._pick 同步）。
        """
        value = os.environ.get(key)
        if value:
            return value

        if os.path.exists(_USER_SECRETS_PATH):
            secrets = _parse_env_file(_USER_SECRETS_PATH)
            if secrets.get(key):
                return secrets[key]

        cfg = _load_api_config_snapshot()
        if cfg:
            if cfg.get(key):
                return cfg[key]
            lower = key.lower()
            if cfg.get(lower):
                return cfg[lower]
        return ""

    def _ssl_context(self):
        # 与 streamer_app 保持一致（板端偶发证书问题时降级）
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # ----------------------------------------------------------------- token
    def _get_token(self, force=False):
        """2h 缓存的 tenant_access_token；dry_run 下返回固定假 token。"""
        if self.dry_run:
            return "DRYRUN_FAKE_TOKEN"

        now = time.time()
        if (not force) and self._token and now < self._token_expires:
            return self._token

        if not self.app_id or not self.app_secret:
            raise RuntimeError(
                "FeishuClient 缺少 app_id 或 app_secret（请配置环境变量或 "
                "~/.ironbuddy/secrets.env）"
            )

        payload = json.dumps(
            {"app_id": self.app_id, "app_secret": self.app_secret}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context())
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") not in (0, None):
            raise RuntimeError("获取 tenant_access_token 失败: " + str(data))

        self._token = data.get("tenant_access_token", "")
        expires_in = int(data.get("expire", _TOKEN_TTL_FALLBACK) or _TOKEN_TTL_FALLBACK)
        self._token_expires = now + max(60, expires_in - _TOKEN_RENEW_BUFFER)
        _LOG.info(
            "[FeishuClient] token 刷新成功，%ss 后过期（含 %ss buffer）",
            expires_in - _TOKEN_RENEW_BUFFER, _TOKEN_RENEW_BUFFER,
        )
        return self._token

    # --------------------------------------------------------------- 发送接口
    def _send_message(self, msg_type, content_obj, chat_id=None):
        """统一发送入口。content_obj 由上层按 feishu 协议组装。"""
        target = chat_id or self.chat_id
        if not target:
            return {"ok": False, "error": "chat_id 未配置"}

        if self.dry_run:
            preview = json.dumps(content_obj, ensure_ascii=False)
            if len(preview) > 160:
                preview = preview[:160] + "..."
            _LOG.info(
                "[FeishuClient DRY-RUN] would send %s to %s: %s",
                msg_type, target, preview,
            )
            return {"ok": True, "dry_run": True, "msg_type": msg_type, "chat_id": target}

        try:
            token = self._get_token()
            body = json.dumps({
                "receive_id": target,
                "msg_type": msg_type,
                "content": json.dumps(content_obj, ensure_ascii=False),
            }).encode("utf-8")
            req = urllib.request.Request(
                self.MSG_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                },
            )
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context())
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0:
                return {"ok": True, "msg_type": msg_type, "msg_id": data.get("data", {}).get("message_id", "")}
            # token 过期兜底重试一次
            if data.get("code") in (99991663, 99991664):
                _LOG.warning("[FeishuClient] token 失效，强制刷新后重试")
                self._get_token(force=True)
                return self._send_message(msg_type, content_obj, chat_id)
            return {"ok": False, "error": "send failed", "detail": data}
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": "HTTPError " + str(exc.code), "detail": str(exc)}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": "URLError", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 全兜底，不抛给调用方
            return {"ok": False, "error": "exception", "detail": str(exc)}

    def send_text(self, text, chat_id=None):
        """发送纯文本。text 会被 feishu 包成 {"text": ...}。"""
        if not text:
            return {"ok": False, "error": "empty text"}
        return self._send_message("text", {"text": str(text)}, chat_id=chat_id)

    def send_card(self, card, chat_id=None):
        """发送 interactive 卡片。card 为 dict，按飞书 message card 协议。"""
        if not isinstance(card, dict):
            return {"ok": False, "error": "card must be dict"}
        return self._send_message("interactive", card, chat_id=chat_id)

    # ----------------------------------------------------------------- 卡片
    @staticmethod
    def _header(title, template="blue"):
        return {
            "template": template,
            "title": {"tag": "plain_text", "content": str(title)},
        }

    @staticmethod
    def _md(content):
        return {"tag": "markdown", "content": str(content)}

    @staticmethod
    def _hr():
        return {"tag": "hr"}

    @staticmethod
    def build_morning_card(date_str, stats_text, today_plan_text):
        """早报卡片：昨日战绩 + 今日建议。

        参数全部为字符串；调用方自行拼 markdown。
        """
        elements = [
            FeishuClient._md("**昨日战报**\n" + str(stats_text or "（暂无数据）")),
            FeishuClient._hr(),
            FeishuClient._md("**今日建议**\n" + str(today_plan_text or "（待生成）")),
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "IronBuddy · " + str(date_str)}
                ],
            },
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": FeishuClient._header("IronBuddy 训练早报 · " + str(date_str), "turquoise"),
            "elements": elements,
        }

    @staticmethod
    def build_weekly_card(week_label, summary_lines, highlights):
        """周报卡片：周标题 + 汇总 bullet list + 高光。

        summary_lines: 可迭代字符串；每行一个 bullet。
        highlights: dict 或 list，关键战绩（总时长/总 reps/最佳命中率）。
        """
        lines = list(summary_lines or [])
        if not lines:
            lines = ["（本周暂无训练记录，快去练一组吧）"]

        summary_md = "\n".join("- " + str(x) for x in lines)

        if isinstance(highlights, dict):
            hl_lines = []
            for k, v in highlights.items():
                hl_lines.append("**" + str(k) + "**：" + str(v))
            highlight_md = "\n".join(hl_lines) if hl_lines else "（无高光）"
        elif isinstance(highlights, (list, tuple)):
            highlight_md = "\n".join(str(x) for x in highlights) or "（无高光）"
        else:
            highlight_md = str(highlights or "（无高光）")

        elements = [
            FeishuClient._md("**本周训练概览**\n" + summary_md),
            FeishuClient._hr(),
            FeishuClient._md("**高光时刻**\n" + highlight_md),
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "IronBuddy · " + str(week_label)}
                ],
            },
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": FeishuClient._header("IronBuddy 训练周报 · " + str(week_label), "violet"),
            "elements": elements,
        }
