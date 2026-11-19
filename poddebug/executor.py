"""Safe diagnostic command construction and Kubernetes exec handling."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import time
from dataclasses import dataclass
from typing import Any

from kubernetes.stream import stream

from .logging_utils import log_event

LOG = logging.getLogger(__name__)


class ValidationError(ValueError):
    """表示诊断工具参数不符合允许规则的异常。"""

    pass


@dataclass(slots=True)
class ExecResult:
    """封装一次 Kubernetes Exec 的结果及执行状态。"""

    #: 命令是否成功，HTTP 操作还要求状态码小于 400。
    success: bool
    #: 命令退出码；超时时使用 124。
    exit_code: int
    #: 截断到配置上限后的标准输出。
    stdout: str
    #: 截断到配置上限后的标准错误。
    stderr: str
    #: 命令是否超过时限。
    timed_out: bool
    #: 命令耗时，单位为毫秒。
    duration_ms: int
    #: HTTP 响应状态码；非 HTTP 操作或未解析到时为 None。
    http_status_code: int | None = None


def build_command(operation: str, target: str) -> list[str]:
    """构造允许执行的诊断命令，用户输入始终作为独立参数传递。

    参数：
        operation: 诊断类型，只允许 http、dns、tcp。
        target: URL、主机名或 host:port 形式的诊断目标。
    返回：
        可直接交给 Kubernetes Exec 的参数列表。
    异常：
        ValidationError: 类型不支持或目标格式不合法时抛出。
    """
    if not isinstance(target, str) or not target.strip() or len(target) > 2048:
        raise ValidationError("target must be a non-empty string (maximum 2048 characters)")
    target = target.strip()
    if any(ord(char) < 32 for char in target):
        raise ValidationError("target contains control characters")
    if operation == "http":
        if not target.startswith(("http://", "https://")):
            raise ValidationError("http target must begin with http:// or https://")
        parsed = urllib.parse.urlsplit(target)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValidationError("http target must contain a hostname and cannot include credentials")
        if any(char.isspace() for char in target) or any(char in target for char in "'\"`;<>\\"):
            raise ValidationError("http target contains invalid URL characters")
        return ["curl", "--silent", "--show-error", "--location", "--max-redirs", "5",
                "--connect-timeout", "10", "--output", "/dev/null", "--write-out",
                "__PODDEBUG_HTTP_STATUS__%{http_code}", "--", target]
    if operation == "dns":
        if len(target) > 253 or not all(
            label and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
            and all(c.isalnum() or c == "-" for c in label)
            for label in target.rstrip(".").split(".")
        ):
            raise ValidationError("dns target must be a valid hostname")
        return ["dig", "+time=3", "+tries=1", target]
    if operation == "tcp":
        host, sep, port_text = target.rpartition(":")
        if not sep or not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValidationError("tcp target must use host:port with a port from 1 to 65535")
        if any(char.isspace() for char in host) or host.startswith("-"):
            raise ValidationError("tcp target contains an invalid host")
        return ["nc", "-vz", "-w", "5", host, str(int(port_text))]
    raise ValidationError("operation must be one of: http, dns, tcp")


def _decode(data: bytes, limit: int) -> str:
    """按 UTF-8 解码输出字节，并在超过限制时附加截断标记。

    参数：
        data: 命令输出字节。
        limit: 最多保留的原始字节数。
    返回：
        可展示的 UTF-8 字符串。
    """
    if len(data) > limit:
        return data[:limit].decode("utf-8", errors="replace") + "\n[output truncated]"
    return data.decode("utf-8", errors="replace")


def _execute_blocking(core_api: Any, namespace: str, pod_name: str, command: list[str],
                      timeout: int, output_limit: int) -> ExecResult:
    """在线程中同步运行 Exec 并持续读取受限的命令输出。

    参数：
        core_api: Kubernetes CoreV1Api 客户端。
        namespace: 目标 Pod 所在命名空间。
        pod_name: 目标诊断 Pod 名称。
        command: 已校验的可执行文件及参数。
        timeout: 命令最大执行秒数。
        output_limit: 标准输出和标准错误各自的字节上限。
    返回：
        命令输出、退出状态、耗时和可选 HTTP 状态码。
    """
    started = time.monotonic()
    response = None
    try:
        response = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
            _request_timeout=timeout,
        )
        deadline = started + timeout
        stdout = bytearray()
        stderr = bytearray()
        # 分批读取 websocket，避免长命令阻塞，并将内存占用限制在输出上限内。
        while response.is_open() and time.monotonic() < deadline:
            response.update(timeout=0.2)
            if response.peek_stdout():
                stdout.extend(response.read_stdout().encode("utf-8", errors="replace"))
            if response.peek_stderr():
                stderr.extend(response.read_stderr().encode("utf-8", errors="replace"))
            if len(stdout) > output_limit:
                del stdout[output_limit:]
            if len(stderr) > output_limit:
                del stderr[output_limit:]
        timed_out = response.is_open()
        if timed_out:
            # Close the websocket to terminate the remote exec session before releasing the lease.
            response.close()
            stderr.extend(b"\ncommand timed out")
        else:
            response.update(timeout=0)
            if response.peek_stdout():
                stdout.extend(response.read_stdout().encode("utf-8", errors="replace"))
            if response.peek_stderr():
                stderr.extend(response.read_stderr().encode("utf-8", errors="replace"))
        exit_code = 124 if timed_out else (response.returncode if response.returncode is not None else 1)
        out_text = _decode(bytes(stdout), output_limit)
        http_status = None
        if "__PODDEBUG_HTTP_STATUS__" in out_text:
            out_text, status_text = out_text.rsplit("__PODDEBUG_HTTP_STATUS__", 1)
            if status_text[:3].isdigit():
                http_status = int(status_text[:3])
        return ExecResult(
            success=not timed_out and exit_code == 0 and (http_status is None or http_status < 400),
            exit_code=int(exit_code), stdout=out_text, stderr=_decode(bytes(stderr), output_limit),
            timed_out=timed_out, duration_ms=int((time.monotonic() - started) * 1000),
            http_status_code=http_status,
        )
    except Exception as exc:
        log_event(
            LOG,
            logging.ERROR,
            "exception",
            "Kubernetes Exec 调用失败",
            exc_info=True,
            method="EXEC",
            path="/pods/exec",
            duration_ms=int((time.monotonic() - started) * 1000),
            status=500,
            namespace=namespace,
            pod_name=pod_name,
        )
        return ExecResult(False, 1, "", str(exc)[:output_limit], False,
                          int((time.monotonic() - started) * 1000))
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                log_event(LOG, logging.DEBUG, "exception", "关闭 Exec WebSocket 失败", exc_info=True)


async def execute(core_api: Any, namespace: str, pod_name: str, command: list[str],
                  timeout: int, output_limit: int) -> ExecResult:
    """将阻塞式 Kubernetes Exec 调度到工作线程执行。

    参数：
        core_api: Kubernetes CoreV1Api 客户端。
        namespace: 目标 Pod 所在命名空间。
        pod_name: 目标诊断 Pod 名称。
        command: 已校验的命令参数列表。
        timeout: 命令最大执行秒数。
        output_limit: 单路输出最多保留的字节数。
    返回：
        异步等待得到的命令执行结果。
    """
    return await asyncio.to_thread(
        _execute_blocking, core_api, namespace, pod_name, command, timeout, output_limit
    )
