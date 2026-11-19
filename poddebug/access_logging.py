"""记录 MCP Streamable HTTP 入站请求及可选的请求响应正文。"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .logging_utils import log_event

LOG = logging.getLogger(__name__)
MAX_PAYLOAD_CHARS = 5000


class AccessLogMiddleware:
    """以 ASGI 包装器记录入站 HTTP 请求，且不主动读取请求正文。"""

    def __init__(self, app: ASGIApp, payload_enabled: bool = False):
        """创建访问日志包装器。

        参数：
            app: 被包装的 ASGI 应用。
            payload_enabled: 是否记录查询参数及应用实际读取的正文。
        返回：
            无；实例保存应用和日志开关。
        """
        self.app = app
        self.payload_enabled = payload_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """记录单次 HTTP 请求的状态、耗时及可选正文。

        参数：
            scope: ASGI 请求上下文。
            receive: ASGI 请求消息接收函数。
            send: ASGI 响应消息发送函数。
        返回：
            无；请求完成后输出 access 日志。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") == "/health":
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        method = scope.get("method", "")
        status = 500
        request_chunks: list[bytes] = []
        response_chunks: list[bytes] = []
        request_truncated = False
        response_truncated = False

        async def capture_receive() -> Message:
            nonlocal request_truncated
            message = await receive()
            if self.payload_enabled and message["type"] == "http.request":
                body = message.get("body", b"")
                remaining = MAX_PAYLOAD_CHARS + 1 - sum(len(chunk) for chunk in request_chunks)
                if remaining > 0:
                    request_chunks.append(body[:remaining])
                if sum(len(chunk) for chunk in request_chunks) > MAX_PAYLOAD_CHARS or (body and len(body) > remaining):
                    request_truncated = True
            return message

        async def capture_send(message: Message) -> None:
            nonlocal status, response_truncated
            if message["type"] == "http.response.start":
                status = message["status"]
            elif self.payload_enabled and message["type"] == "http.response.body":
                body = message.get("body", b"")
                remaining = MAX_PAYLOAD_CHARS + 1 - sum(len(chunk) for chunk in response_chunks)
                if remaining > 0:
                    response_chunks.append(body[:remaining])
                if sum(len(chunk) for chunk in response_chunks) > MAX_PAYLOAD_CHARS or (body and len(body) > remaining):
                    response_truncated = True
            await send(message)

        try:
            await self.app(scope, capture_receive, capture_send)
        except Exception:
            log_event(LOG, logging.ERROR, "exception", "入站 HTTP 请求处理异常", exc_info=True,
                      method=method, path=path, status=status)
            raise
        finally:
            fields: dict[str, Any] = {
                "method": method,
                "path": path,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "status": status,
            }
            if self.payload_enabled:
                query = scope.get("query_string", b"").decode("utf-8", errors="replace")
                fields["request_query"] = [[key, value] for key, value in parse_qsl(query, keep_blank_values=True)]
                request_body = self._decode(b"".join(request_chunks))
                response_body = self._decode(b"".join(response_chunks))
                fields["request_body"] = request_body[:MAX_PAYLOAD_CHARS]
                fields["response_body"] = response_body[:MAX_PAYLOAD_CHARS]
                if request_truncated or len(request_body) > MAX_PAYLOAD_CHARS:
                    fields["request_body_truncated"] = True
                if response_truncated or len(response_body) > MAX_PAYLOAD_CHARS:
                    fields["response_body_truncated"] = True
            log_event(LOG, logging.INFO, "access", "入站 HTTP 请求", **fields)

    @staticmethod
    def _decode(data: bytes) -> str:
        """将捕获的正文按 UTF-8 解码。

        参数：
            data: 已捕获的请求或响应正文片段。
        返回：
            使用替换字符处理非法字节后的字符串。
        """
        return data.decode("utf-8", errors="replace")
