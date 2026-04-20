"""IronBuddy 外部平台集成层。

当前提供：
  - feishu_client.FeishuClient : 飞书开放平台客户端（支持 dry-run）
"""

from .feishu_client import FeishuClient  # noqa: F401

__all__ = ["FeishuClient"]
