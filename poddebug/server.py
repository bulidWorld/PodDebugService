"""MCP entry point for Kubernetes network diagnostics."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from importlib.resources import files
from typing import Any

from kubernetes import client, config
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, create_model
import yaml

from .auth import CallerTokenVerifier
from .config import Settings
from .executor import ValidationError, build_command, execute
from .pool import DebugPodPool

LOG = logging.getLogger("poddebug")
settings = Settings.from_env()
pool: DebugPodPool | None = None
core_api: Any = None
token_verifier = CallerTokenVerifier(settings.mcp_callers_json)


class ExecCommandOutput(BaseModel):
    """MCP 工具对外返回的结构化诊断结果。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    #: 命令执行及 HTTP 状态检查是否成功。
    success: bool
    #: 命令进程退出码。
    exit_code: int = Field(alias="exitCode")
    #: 命令标准输出。
    stdout: str
    #: 命令标准错误。
    stderr: str
    #: 命令是否超时。
    timed_out: bool = Field(alias="timedOut")
    #: 实际执行命令的 Pod 名称；未分配 Pod 时为 None。
    pod_name: str | None = Field(alias="podName")
    #: 实际执行的命名空间。
    namespace: str
    #: Pod 实际所在节点；未调度或未分配时为 None。
    node_name: str | None = Field(alias="nodeName")
    #: 是否复用了池中已有 Pod。
    reused: bool
    #: 命令耗时，单位为毫秒。
    duration_ms: int = Field(alias="durationMs")
    #: HTTP 操作响应状态码；其他操作时为 None。
    http_status_code: int | None = Field(default=None, alias="httpStatusCode")


def _load_tool_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    """加载并校验 MCP YAML 契约与服务实现的字段一致性。

    参数：
        无；可通过 PODDEBUG_MCP_CONFIG 指定契约文件路径。
    返回：
        (完整契约, 唯一工具定义)。
    异常：
        RuntimeError: 文件无法读取或 YAML 无法解析时抛出。
        ValueError: 工具定义、安全元数据或输入输出结构不匹配时抛出。
    """
    configured_path = os.getenv("PODDEBUG_MCP_CONFIG")
    path = Path(configured_path) if configured_path else None
    try:
        contract_text = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("poddebug").joinpath("mcp.yaml").read_text(encoding="utf-8")
        )
        contract = yaml.safe_load(contract_text)
    except (OSError, yaml.YAMLError) as exc:
        source = str(path) if path is not None else "poddebug/mcp.yaml"
        raise RuntimeError(f"could not load MCP contract from {source}: {exc}") from exc
    if not isinstance(contract, dict) or not isinstance(contract.get("tools"), list):
        raise ValueError("MCP contract must define a tools array")
    tools = contract["tools"]
    if len(tools) != 1 or not isinstance(tools[0], dict):
        raise ValueError("MCP contract must define exactly one tool")
    tool = tools[0]
    input_schema = tool.get("inputSchema")
    input_properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    if (
        not isinstance(tool.get("name"), str)
        or not tool["name"]
        or not isinstance(input_schema, dict)
        or not isinstance(input_properties, dict)
        or not isinstance(input_schema.get("required", []), list)
        or not set(input_schema.get("required", [])) <= set(input_properties)
    ):
        raise ValueError(
            "MCP contract must define a named tool and a valid input schema"
        )
    if set(input_properties) != {"operation", "target", "namespace", "node_name", "timeout"}:
        raise ValueError("MCP contract inputs do not match exec_debug_command implementation")
    # 参数的类型和执行边界必须同时由 MCP schema 与运行时校验实现。
    expected_inputs = {
        "operation": {"type": "string"},
        "target": {"type": "string"},
        "namespace": {"type": "string"},
        "node_name": {"type": ["string", "null"]},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 60},
    }
    for field_name, constraints in expected_inputs.items():
        field = input_properties[field_name]
        if not isinstance(field, dict) or any(
            field.get(key) != value for key, value in constraints.items()
        ):
            raise ValueError(f"MCP contract input field {field_name!r} does not match implementation")
    if set(input_properties["operation"].get("enum", [])) != {"http", "dns", "tcp"}:
        raise ValueError("MCP contract operation enum does not match implementation")
    if set(input_schema.get("required", [])) != {"operation", "target"}:
        raise ValueError("MCP contract required inputs do not match implementation")
    metadata = tool.get("_meta")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("security"), dict):
        raise ValueError("MCP contract tool must define _meta.security")
    security = metadata["security"]
    # 启动时拒绝不完整或实现不支持的审批描述，避免运行时暴露错误契约。
    if security.get("riskLevel") not in {"low", "medium", "high", "critical"}:
        raise ValueError("MCP tool security.riskLevel is invalid")
    if security.get("approvalMode") not in {"none", "required", "conditional"}:
        raise ValueError("MCP tool security.approvalMode is invalid")
    if security.get("defaultAction") not in {"allow", "deny", "require_approval"}:
        raise ValueError("MCP tool security.defaultAction is invalid")
    if security["approvalMode"] == "none" and "approval" in metadata:
        raise ValueError("MCP tool with approvalMode=none must not define approval inputs")
    if security["approvalMode"] != "none":
        approval = metadata.get("approval")
        if (
            not isinstance(security.get("approvalRole"), str)
            or not security["approvalRole"].strip()
        ):
            raise ValueError("MCP tool requiring approval must define security.approvalRole")
        if (
            not isinstance(approval, dict)
            or approval.get("schema_version") != 1
            or approval.get("inputPolicy") not in {"fixed", "model_assisted"}
            or not isinstance(approval.get("inputs"), list)
        ):
            raise ValueError("MCP tool requiring approval must define approval metadata and inputs")
        valid_types = {"confirm", "single_select", "multi_select", "text"}
        allowed_types = approval.get("allowedInputTypes", [])
        if approval["inputPolicy"] == "model_assisted" and (
            not isinstance(allowed_types, list)
            or not all(isinstance(item, str) for item in allowed_types)
            or not set(allowed_types) <= valid_types
        ):
            raise ValueError("approval.allowedInputTypes contains an unsupported input type")
        seen_keys: set[str] = set()
        for approval_input in approval["inputs"]:
            if not isinstance(approval_input, dict):
                raise ValueError("approval.inputs entries must be objects")
            key, input_type = approval_input.get("key"), approval_input.get("type")
            if (
                not isinstance(key, str)
                or not key
                or key in seen_keys
                or not isinstance(approval_input.get("label"), str)
                or not isinstance(approval_input.get("required"), bool)
                or input_type not in valid_types
            ):
                raise ValueError("approval.inputs entry has invalid key, label, required, or type")
            seen_keys.add(key)
            if input_type in {"single_select", "multi_select"}:
                options = approval_input.get("options")
                if (
                    not isinstance(options, list)
                    or not options
                    or any(
                        not isinstance(option, dict)
                        or not isinstance(option.get("value"), str)
                        or not isinstance(option.get("label"), str)
                        for option in options
                    )
                ):
                    raise ValueError(f"approval input {key!r} requires labeled options")
            if input_type == "text" and "max_length" in approval_input:
                if (
                    not isinstance(approval_input["max_length"], int)
                    or approval_input["max_length"] < 1
                ):
                    raise ValueError(f"approval text input {key!r} has invalid max_length")
    output_schema = tool.get("outputSchema")
    if not isinstance(output_schema, dict) or output_schema.get("type") != "object":
        raise ValueError("MCP contract tool must define an object outputSchema")
    output_fields = {
        "success",
        "exitCode",
        "stdout",
        "stderr",
        "timedOut",
        "podName",
        "namespace",
        "nodeName",
        "reused",
        "durationMs",
        "httpStatusCode",
    }
    output_properties = output_schema.get("properties", {})
    if not isinstance(output_properties, dict) or set(output_properties) != output_fields or not output_fields - {
        "httpStatusCode"
    } <= set(output_schema.get("required", [])):
        raise ValueError("MCP contract output schema does not match exec_debug_command output")
    expected_outputs = {
        "success": "boolean", "exitCode": "integer", "stdout": "string", "stderr": "string",
        "timedOut": "boolean", "podName": ["string", "null"], "namespace": "string",
        "nodeName": ["string", "null"], "reused": "boolean", "durationMs": "integer",
        "httpStatusCode": ["integer", "null"],
    }
    if any(output_properties[key].get("type") != value for key, value in expected_outputs.items()):
        raise ValueError("MCP contract output types do not match exec_debug_command implementation")
    return contract, tool


MCP_CONTRACT, TOOL_CONTRACT = _load_tool_contract()


@asynccontextmanager
async def _lifespan(_: FastMCP) -> AsyncIterator[None]:
    """管理 MCP 服务启动和退出期间的 Kubernetes 资源生命周期。

    参数：
        _: FastMCP 服务实例，由生命周期管理器传入。
    返回：
        异步上下文迭代器；启动 Pod 池后让服务运行，退出时关闭清理器。
    """
    _init_kubernetes()
    assert pool is not None
    await pool.start()
    try:
        yield
    finally:
        await pool.close()


mcp = FastMCP(
    "kubernetes-network-diagnostics",
    host=settings.http_host,
    port=settings.http_port,
    streamable_http_path="/mcp",
    token_verifier=token_verifier,
    auth=AuthSettings(
        issuer_url=settings.public_url,
        resource_server_url=settings.public_url,
        validate_token_resource=False,
    ),
    lifespan=_lifespan,
)


def _valid_k8s_name(value: str, field: str) -> str:
    """校验命名空间或节点名称是否符合 Kubernetes 名称格式。

    参数：
        value: 待校验的名称。
        field: 用于错误信息的参数名称。
    返回：
        校验通过的原始名称。
    异常：
        ValidationError: 名称为空、过长或格式不合法时抛出。
    """
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", value)
        or len(value) > 253
    ):
        raise ValidationError(f"{field} must be a valid Kubernetes name")
    return value


def _init_kubernetes() -> None:
    """加载集群内或本地 Kubernetes 配置并初始化 Pod 池。

    参数：
        无；本地配置路径取自 Settings.kubeconfig。
    返回：
        无；设置模块级 CoreV1Api 客户端和 DebugPodPool。
    """
    global core_api, pool
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config(config_file=settings.kubeconfig)
    core_api = client.CoreV1Api()
    pool = DebugPodPool(core_api, settings)


async def exec_debug_command(
    operation: str,
    target: str,
    namespace: str | None = None,
    node_name: str | None = None,
    timeout: int = 30,
) -> ExecCommandOutput:
    """在 Kubernetes 诊断 Pod 内执行受限的 HTTP、DNS 或 TCP 连通性检查。

    参数：
        operation: 操作类型，支持 http、dns、tcp。
        target: 对应操作的 URL、主机名或 host:port。
        namespace: 可选命名空间，缺省时使用服务默认值。
        node_name: 可选的工作节点名称。
        timeout: 命令时限，必须为 1 到 60 秒的整数。
    返回：
        包含命令输出、退出状态和 Pod 执行环境的结构化结果。
    """
    selected_namespace = namespace or settings.default_namespace
    # 先检查调用身份与工具权限，再校验所有用户可控的执行参数。
    access_token = get_access_token()
    if access_token is None:
        # HTTP middleware normally rejects missing/invalid credentials before dispatch.
        return ExecCommandOutput(
            success=False,
            exitCode=1,
            stdout="",
            stderr="Bearer token required",
            timedOut=False,
            podName=None,
            namespace=selected_namespace,
            nodeName=node_name,
            reused=False,
            durationMs=0,
        )
    if "ALL" not in access_token.scopes and "exec_debug_command" not in access_token.scopes:
        return ExecCommandOutput(
            success=False,
            exitCode=1,
            stdout="",
            stderr="caller is not authorized for exec_debug_command",
            timedOut=False,
            podName=None,
            namespace=selected_namespace,
            nodeName=node_name,
            reused=False,
            durationMs=0,
        )
    try:
        _valid_k8s_name(selected_namespace, "namespace")
        if selected_namespace not in settings.allowed_namespaces:
            raise ValidationError(f"namespace {selected_namespace!r} is not allowed")
        if node_name is not None:
            _valid_k8s_name(node_name, "node_name")
            if settings.allowed_nodes and node_name not in settings.allowed_nodes:
                raise ValidationError(f"node {node_name!r} is not allowed")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ValidationError("timeout must be an integer from 1 to 60 seconds")
        command = build_command(operation, target)
    except ValidationError as exc:
        return ExecCommandOutput(
            success=False,
            exitCode=2,
            stdout="",
            stderr=str(exc),
            timedOut=False,
            podName=None,
            namespace=selected_namespace,
            nodeName=node_name,
            reused=False,
            durationMs=0,
        )
    if pool is None:
        return ExecCommandOutput(
            success=False,
            exitCode=1,
            stdout="",
            stderr="Kubernetes client is unavailable",
            timedOut=False,
            podName=None,
            namespace=selected_namespace,
            nodeName=node_name,
            reused=False,
            durationMs=0,
        )
    record = None
    reused = False
    try:
        # 获取 Pod 后始终通过 finally 释放占用，覆盖执行成功和异常路径。
        record, reused = await pool.acquire(selected_namespace, node_name)
        result = await execute(
            core_api, selected_namespace, record.name, command, timeout, settings.max_output_bytes
        )
        response: dict[str, Any] = {
            "success": result.success,
            "exitCode": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timedOut": result.timed_out,
            "podName": record.name,
            "namespace": selected_namespace,
            "nodeName": record.node_name,
            "reused": reused,
            "durationMs": result.duration_ms,
        }
        if operation == "http":
            response["httpStatusCode"] = result.http_status_code
        return ExecCommandOutput.model_validate(response)
    except Exception as exc:
        LOG.exception("Diagnostic request failed")
        return ExecCommandOutput(
            success=False,
            exitCode=1,
            stdout="",
            stderr=str(exc)[: settings.max_output_bytes],
            timedOut=False,
            podName=record.name if record else None,
            namespace=selected_namespace,
            nodeName=record.node_name if record else node_name,
            reused=reused,
            durationMs=0,
        )
    finally:
        if record is not None:
            await pool.release(record)


def _input_model_from_schema(schema: dict[str, Any]) -> type[BaseModel]:
    """根據 MCP 輸入 schema 建立 FastMCP 可識別的參數模型。

    参数：
        schema: 工具定义中的 JSON Schema 对象。
    返回：
        描述工具输入字段、默认值及限制的 Pydantic 模型。
    """
    annotations: dict[str, Any] = {"http": str, "dns": str, "tcp": str}
    fields: dict[str, Any] = {}
    required = set(schema.get("required", []))
    for name, definition in schema["properties"].items():
        field_type = definition["type"]
        if field_type == "string":
            python_type: Any = str
        elif field_type == ["string", "null"]:
            python_type = str | None
        elif field_type == "integer":
            python_type = int
        else:
            raise ValueError(f"unsupported input schema type for {name!r}: {field_type!r}")
        default = ... if name in required else definition.get("default", None)
        constraints: dict[str, Any] = {}
        if "enum" in definition:
            constraints["enum"] = definition["enum"]
        if "minimum" in definition:
            constraints["ge"] = definition["minimum"]
        if "maximum" in definition:
            constraints["le"] = definition["maximum"]
        fields[name] = (python_type, Field(default, description=definition.get("description"), **constraints))
    return create_model(
        "ExecDebugCommandInput",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


registered_tool = mcp.add_tool(
    exec_debug_command,
    name=TOOL_CONTRACT["name"],
    description=TOOL_CONTRACT["description"],
    meta=TOOL_CONTRACT["_meta"],
    structured_output=True,
)
registered_info = next(
    info for info in mcp._tool_manager.list_tools() if info.name == TOOL_CONTRACT["name"]
)
registered_info.parameters = _input_model_from_schema(TOOL_CONTRACT["inputSchema"]).model_json_schema(
    mode="validation", by_alias=True
)
registered_info.fn_metadata.output_schema = TOOL_CONTRACT["outputSchema"]


def main() -> None:
    """配置标准错误日志并以 Streamable HTTP 方式运行 MCP 服务。

    参数：
        无。
    返回：
        无；服务运行至进程中断。
    """
    logging.basicConfig(
        stream=sys.stderr, level=os_level(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass


def os_level() -> int:
    """读取日志级别环境变量并回退到 INFO。

    参数：
        无。
    返回：
        logging 模块对应的数值级别。
    """
    import os

    return getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)


if __name__ == "__main__":
    main()
