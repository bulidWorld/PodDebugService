"""Environment based service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _csv(name: str, default: str) -> frozenset[str]:
    """读取逗号分隔的环境变量并清理空白项。

    参数：
        name: 环境变量名称。
        default: 环境变量未设置时使用的字符串。
    返回：
        去除首尾空白且不含空项的不可变字符串集合。
    """
    return frozenset(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """服务运行配置；字段默认值与环境变量默认值保持一致。"""

    image: str = "nicolaka/netshoot:v0.13"
    default_namespace: str = "debug-pod"
    allowed_namespaces: frozenset[str] = frozenset({"debug-pod"})
    allowed_nodes: frozenset[str] = frozenset()
    max_exec_per_pod: int = 5
    idle_ttl_seconds: int = 1200
    cleanup_interval_seconds: int = 60
    pod_start_timeout_seconds: int = 120
    max_pods_global: int = 30
    max_pods_per_namespace: int = 5
    max_output_bytes: int = 65536
    cpu_request: str = "50m"
    cpu_limit: str = "500m"
    memory_request: str = "64Mi"
    memory_limit: str = "256Mi"
    profile: str = "network-v1"
    owner_instance: str = "pod-debug-service"
    kubeconfig: str | None = None
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    public_url: str = "http://pod-debug-service.debug-pod.svc.cluster.local:8000"
    mcp_callers_json: str = "[]"

    @classmethod
    def from_env(cls) -> "Settings":
        """从进程环境变量构造配置，并对资源上限应用最小值保护。

        参数：
            cls: 配置类本身。
        返回：
            使用当前环境变量填充的不可变服务配置。
        """
        # 先解析默认命名空间，使允许列表在未配置时默认只开放该命名空间。
        default_ns = os.getenv("PODDEBUG_DEFAULT_NAMESPACE", "debug-pod")
        return cls(
            image=os.getenv("PODDEBUG_IMAGE", "nicolaka/netshoot:v0.13"),
            default_namespace=default_ns,
            allowed_namespaces=_csv("PODDEBUG_ALLOWED_NAMESPACES", default_ns),
            allowed_nodes=_csv("PODDEBUG_ALLOWED_NODES", ""),
            max_exec_per_pod=max(1, int(os.getenv("PODDEBUG_MAX_EXEC_PER_POD", "5"))),
            idle_ttl_seconds=max(1, int(os.getenv("PODDEBUG_IDLE_TTL_SECONDS", "1200"))),
            cleanup_interval_seconds=max(1, int(os.getenv("PODDEBUG_CLEANUP_INTERVAL_SECONDS", "60"))),
            pod_start_timeout_seconds=max(1, int(os.getenv("PODDEBUG_POD_START_TIMEOUT_SECONDS", "120"))),
            max_pods_global=max(1, int(os.getenv("PODDEBUG_MAX_PODS_GLOBAL", "30"))),
            max_pods_per_namespace=max(1, int(os.getenv("PODDEBUG_MAX_PODS_PER_NAMESPACE", "5"))),
            max_output_bytes=max(1024, int(os.getenv("PODDEBUG_MAX_OUTPUT_BYTES", "65536"))),
            cpu_request=os.getenv("PODDEBUG_CPU_REQUEST", "50m"),
            cpu_limit=os.getenv("PODDEBUG_CPU_LIMIT", "500m"),
            memory_request=os.getenv("PODDEBUG_MEMORY_REQUEST", "64Mi"),
            memory_limit=os.getenv("PODDEBUG_MEMORY_LIMIT", "256Mi"),
            profile=os.getenv("PODDEBUG_PROFILE", "network-v1"),
            owner_instance=os.getenv("PODDEBUG_OWNER_INSTANCE", "pod-debug-service"),
            kubeconfig=os.getenv("KUBECONFIG"),
            http_host=os.getenv("PODDEBUG_HTTP_HOST", "0.0.0.0"),
            http_port=int(os.getenv("PODDEBUG_HTTP_PORT", "8000")),
            public_url=os.getenv("PODDEBUG_PUBLIC_URL", "http://pod-debug-service.debug-pod.svc.cluster.local:8000").rstrip("/"),
            mcp_callers_json=os.getenv("MCP_CALLERS_JSON", "[]"),
        )
