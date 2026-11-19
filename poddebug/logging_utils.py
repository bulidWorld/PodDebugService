"""提供符合服务规范的 JSON 结构化日志支持。"""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any


class JsonLogFormatter(logging.Formatter):
    """将日志记录转换为包含时间、级别和类型的单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """格式化单条日志记录。

        参数：
            record: Python logging 创建的日志记录。
        返回：
            包含规范基础字段及附加结构化字段的 JSON 字符串。
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "log_type": getattr(record, "log_type", "business"),
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "structured_fields", {}).items():
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exceptionInfo"] = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: int) -> None:
    """配置进程根日志记录器输出 JSON 到标准错误。

    参数：
        level: logging 数值级别。
    返回：
        无；根日志记录器将使用 JSON 格式化器。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    level: int,
    log_type: str,
    message: str,
    *,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """以统一字段向指定日志记录器写入结构化事件。

    参数：
        logger: 负责写入事件的日志记录器。
        level: 日志级别。
        log_type: 日志类型，例如 business、http、access 或 exception。
        message: 人类可读的事件说明。
        exc_info: 是否附带当前异常堆栈。
        **fields: 事件的额外结构化字段。
    返回：
        无；事件交由配置好的日志处理器输出。
    """
    logger.log(level, message, extra={"log_type": log_type, "structured_fields": fields}, exc_info=exc_info)
