"""Static Bearer token verification for configured MCP callers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from mcp.server.auth.provider import AccessToken

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Caller:
    """保存一个获准调用方的身份、令牌和工具权限。"""

    #: 调用方标识，用作访问令牌的客户端身份与主体。
    caller_id: str
    #: 调用方持有的不透明 Bearer 令牌，不写入日志。
    token: str
    #: 允许调用的工具名称；ALL 表示允许所有工具。
    tools: tuple[str, ...]


class CallerTokenVerifier:
    """根据 MCP_CALLERS_JSON 校验不透明 Bearer 令牌，且不记录令牌内容。"""

    def __init__(self, raw_config: str):
        """解析并校验调用方配置。

        参数：
            raw_config: JSON 数组格式的调用方配置字符串。
        返回：
            无；成功时初始化令牌索引，配置错误时抛出 ValueError。
        """
        try:
            data: Any = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP_CALLERS_JSON must contain valid JSON") from exc
        if not isinstance(data, list):
            raise ValueError("MCP_CALLERS_JSON must be a JSON array")
        callers: list[Caller] = []
        seen_tokens: set[str] = set()
        seen_ids: set[str] = set()
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"MCP_CALLERS_JSON entry {index} must be an object")
            caller_id, token, tools = item.get("callerId"), item.get("token"), item.get("tools")
            if not isinstance(caller_id, str) or not caller_id.strip():
                raise ValueError(f"MCP_CALLERS_JSON entry {index} needs callerId")
            if not isinstance(token, str) or not token:
                raise ValueError(f"MCP_CALLERS_JSON entry {index} needs a non-empty token")
            if not isinstance(tools, list) or not tools or not all(isinstance(t, str) for t in tools):
                raise ValueError(f"MCP_CALLERS_JSON entry {index} needs a non-empty tools array")
            if caller_id in seen_ids:
                raise ValueError(f"MCP_CALLERS_JSON contains duplicate callerId {caller_id!r}")
            if token in seen_tokens:
                raise ValueError("MCP_CALLERS_JSON contains duplicate tokens")
            seen_ids.add(caller_id)
            seen_tokens.add(token)
            callers.append(Caller(caller_id, token, tuple(tools)))
        if not callers:
            raise ValueError("MCP_CALLERS_JSON must configure at least one caller")
        self._by_token = {caller.token: caller for caller in callers}

    async def verify_token(self, token: str) -> AccessToken | None:
        """将令牌解析为 MCP 访问令牌。

        参数：
            token: 请求提供的 Bearer 令牌。
        返回：
            匹配时返回包含调用方身份和工具权限的访问令牌，否则返回 None。
        """
        caller = self._by_token.get(token)
        if caller is None:
            return None
        return AccessToken(
            token=token,
            client_id=caller.caller_id,
            scopes=list(caller.tools),
            subject=caller.caller_id,
        )

