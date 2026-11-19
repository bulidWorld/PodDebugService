"""In-memory coordination and lifecycle management for the debug Pod pool."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.rest import ApiException

from .config import Settings
from .logging_utils import log_event

LOG = logging.getLogger(__name__)
MANAGED_BY = "poddebug.openai.com/managed-by"
PROFILE = "poddebug.openai.com/profile"
LAST_IDLE = "poddebug.openai.com/last-idle-at"
OWNER = "poddebug.openai.com/owner-instance"
STATE = "poddebug.openai.com/state"
MANAGER = "pod-debug-service"


class PoolError(RuntimeError):
    """表示诊断 Pod 池无法分配、创建或管理 Pod 的异常。"""

    pass


@dataclass(slots=True)
class PodRecord:
    """记录本服务管理的单个诊断 Pod 的调度和生命周期状态。"""

    #: Pod 所属 Kubernetes 命名空间。
    namespace: str
    #: Pod 名称。
    name: str
    #: Pod 当前所在节点；尚未调度时为 None。
    node_name: str | None
    #: 当前分配给该 Pod 的活跃 Exec 数量。
    active: int = 0
    #: 本地生命周期状态，例如 IDLE、CREATING 或 DELETING。
    state: str = "IDLE"
    #: 最近一次进入空闲状态的 Unix 时间戳。
    last_idle: float = field(default_factory=time.time)
    #: 重启恢复后置为 True，避免未核实的旧 Exec Pod 被复用或回收。
    uncertain: bool = False
    #: 预留给记录级同步的线程锁。
    lock: threading.Lock = field(default_factory=threading.Lock)


class DebugPodPool:
    """管理诊断 Pod 的发现、复用、并发占用和空闲回收。"""

    def __init__(self, core_api: Any, settings: Settings):
        """创建 Pod 池协调器。

        参数：
            core_api: Kubernetes CoreV1Api 客户端。
            settings: 服务资源限制和 Pod 配置。
        返回：
            无；初始化空的进程内记录表和清理任务状态。
        """
        self.api = core_api
        self.settings = settings
        self.records: dict[tuple[str, str], PodRecord] = {}
        self._lock = asyncio.Lock()
        self._cleaner_task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        """恢复受管 Pod，并启动空闲清理循环。

        参数：
            无。
        返回：
            无；无法确认旧 Exec 状态的恢复 Pod 会保持 uncertain 状态。
        """
        # 逐命名空间查询本服务和当前配置档案创建的 Pod，避免接管其他资源。
        for namespace in sorted(self.settings.allowed_namespaces):
            try:
                pods = await asyncio.to_thread(
                    self.api.list_namespaced_pod,
                    namespace,
                    label_selector=f"{MANAGED_BY}={MANAGER},{PROFILE}={self.settings.profile}",
                )
            except ApiException as exc:
                if exc.status == 404:
                    continue
                log_event(LOG, logging.ERROR, "exception", "恢复诊断 Pod 失败", exc_info=True, namespace=namespace)
                continue
            except Exception:
                log_event(LOG, logging.ERROR, "exception", "恢复诊断 Pod 失败", exc_info=True, namespace=namespace)
                continue
            for pod in pods.items:
                phase = pod.status.phase if pod.status else None
                if phase != "Running" or not self._ready(pod):
                    continue
                annotations = pod.metadata.annotations or {}
                try:
                    last_idle = datetime.fromisoformat(annotations[LAST_IDLE].replace("Z", "+00:00")).timestamp()
                except (KeyError, ValueError):
                    last_idle = time.time()
                rec = PodRecord(namespace, pod.metadata.name, pod.spec.node_name,
                                last_idle=last_idle, uncertain=True)
                self.records[(namespace, rec.name)] = rec
                log_event(LOG, logging.INFO, "business", "发现并保护待确认的诊断 Pod",
                          namespace=namespace, pod_name=rec.name, node_name=rec.node_name)
        self._cleaner_task = asyncio.create_task(self._cleanup_loop(), name="pod-cleaner")

    async def close(self) -> None:
        """停止后台清理任务并等待其取消。

        参数：
            无。
        返回：
            无。
        """
        self._stopping = True
        if self._cleaner_task:
            self._cleaner_task.cancel()
            try:
                await self._cleaner_task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _ready(pod: Any) -> bool:
        """判断 Kubernetes Pod 是否报告 Ready 条件。

        参数：
            pod: Kubernetes Pod API 对象。
        返回：
            Ready 条件状态为 True 时返回 True。
        """
        return any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))

    def _matches(self, rec: PodRecord, namespace: str, node_name: str | None) -> bool:
        """检查记录是否满足命名空间、节点及可复用状态要求。

        参数：
            rec: 待检查的 Pod 记录。
            namespace: 请求指定的命名空间。
            node_name: 可选的目标节点。
        返回：
            记录可供该请求复用时返回 True。
        """
        return (rec.namespace == namespace and rec.state == "IDLE" and not rec.uncertain
                and (node_name is None or rec.node_name == node_name))

    async def acquire(self, namespace: str, node_name: str | None) -> tuple[PodRecord, bool]:
        """分配可用 Pod，或在容量允许时创建并等待新 Pod。

        参数：
            namespace: 诊断 Pod 所在命名空间。
            node_name: 可选的目标节点名称。
        返回：
            (Pod 记录, 是否复用了现有 Pod)。
        异常：
            PoolError: 节点配置、池容量或 Pod 创建过程失败时抛出。
        """
        if node_name and not self.settings.allowed_nodes:
            raise PoolError("node_name requires an explicit PODDEBUG_ALLOWED_NODES allowlist")
        async with self._lock:
            # 优先选择活跃任务最少的兼容 Pod，降低单个 Pod 的并发压力。
            candidates = [r for r in self.records.values() if self._matches(r, namespace, node_name)
                          and r.active < self.settings.max_exec_per_pod]
            candidates.sort(key=lambda r: (r.active, r.name))
            if candidates:
                rec = candidates[0]
                rec.active += 1
                return rec, True
            total = len([r for r in self.records.values() if r.state != "DELETED"])
            per_ns = len([r for r in self.records.values()
                          if r.namespace == namespace and r.state != "DELETED"])
            if total >= self.settings.max_pods_global or per_ns >= self.settings.max_pods_per_namespace:
                recoverable = any(r.uncertain and r.namespace == namespace and
                                  (node_name is None or r.node_name == node_name)
                                  and r.active < self.settings.max_exec_per_pod for r in self.records.values())
                detail = "; recovered Pods are awaiting safe revalidation" if recoverable else ""
                raise PoolError("debug Pod capacity reached; retry after a Pod becomes available" + detail)
            # Reserve creation while holding the lock so simultaneous requests do not overshoot limits.
            name = "debug-net-" + uuid.uuid4().hex[:10]
            rec = PodRecord(namespace, name, None, active=1, state="CREATING")
            self.records[(namespace, name)] = rec
        try:
            await self._create_and_wait(rec, node_name)
            async with self._lock:
                rec.state = "IDLE"
                rec.uncertain = False
            log_event(LOG, logging.INFO, "business", "诊断 Pod 创建并就绪",
                      namespace=namespace, pod_name=rec.name, node_name=rec.node_name)
            return rec, False
        except Exception:
            async with self._lock:
                rec.state = "DELETED"
                self.records.pop((namespace, name), None)
            raise

    async def release(self, rec: PodRecord) -> None:
        """释放一次 Exec 占用，并在最后一个任务结束时更新时间戳。

        参数：
            rec: 本次请求占用的 Pod 记录。
        返回：
            无；空闲时间戳同步失败时记录异常但不影响请求结果。
        """
        async with self._lock:
            if rec.active > 0:
                rec.active -= 1
            if rec.active == 0 and rec.state == "IDLE":
                rec.last_idle = time.time()
                try:
                    await asyncio.to_thread(self._patch_idle, rec)
                except Exception:
                    log_event(LOG, logging.ERROR, "exception", "更新诊断 Pod 空闲时间失败",
                              exc_info=True, namespace=rec.namespace, pod_name=rec.name)

    def _manifest(self, rec: PodRecord, node_name: str | None) -> dict[str, Any]:
        """构造受管诊断 Pod 的 Kubernetes Manifest。

        参数：
            rec: 新 Pod 的名称和命名空间记录。
            node_name: 可选的节点亲和性目标。
        返回：
            可提交给 Kubernetes API 的 Pod 资源字典。
        """
        labels = {MANAGED_BY: MANAGER, PROFILE: self.settings.profile}
        annotations = {LAST_IDLE: datetime.now(timezone.utc).isoformat(),
                       OWNER: self.settings.owner_instance, STATE: "IDLE"}
        container = {
            "name": "diagnostics", "image": self.settings.image, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", "while true; do sleep 3600; done"],
            "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True,
                                "runAsUser": 1000, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": self.settings.cpu_request,
                                         "memory": self.settings.memory_request},
                          "limits": {"cpu": self.settings.cpu_limit,
                                     "memory": self.settings.memory_limit}},
        }
        spec: dict[str, Any] = {"containers": [container], "restartPolicy": "Always",
                                "automountServiceAccountToken": False,
                                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}}}
        if node_name:
            spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In",
                                                          "values": [node_name]}]}]}}}
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": rec.name, "namespace": rec.namespace, "labels": labels,
                             "annotations": annotations}, "spec": spec}

    async def _create_and_wait(self, rec: PodRecord, node_name: str | None) -> None:
        """校验节点、创建 Pod，并在超时前轮询至 Ready。

        参数：
            rec: 已预留名称和并发槽位的 Pod 记录。
            node_name: 可选的目标节点。
        返回：
            无；成功后会更新记录中的实际节点名称。
        异常：
            PoolError: 节点不可用、权限不足、调度失败或等待超时时抛出。
        """
        # 先校验目标节点存在且可用，再提交带硬性节点亲和性的 Pod。
        if node_name:
            try:
                node = await asyncio.to_thread(self.api.read_node, node_name)
            except ApiException as exc:
                if exc.status == 404:
                    raise PoolError(f"node {node_name!r} does not exist") from exc
                raise PoolError(f"could not validate node {node_name!r}: {exc.reason}") from exc
            if self.settings.allowed_nodes and node_name not in self.settings.allowed_nodes:
                raise PoolError(f"node {node_name!r} is not in the allowed node list")
            ready = any(c.type == "Ready" and c.status == "True" for c in (node.status.conditions or []))
            if not ready:
                raise PoolError(f"node {node_name!r} is not Ready")
        try:
            await asyncio.to_thread(self.api.create_namespaced_pod, rec.namespace,
                                    self._manifest(rec, node_name))
        except ApiException as exc:
            if exc.status == 403:
                raise PoolError(f"not authorized to create debug Pods in namespace {rec.namespace!r}") from exc
            raise PoolError(f"could not create debug Pod: {exc.reason}") from exc
        deadline = time.monotonic() + self.settings.pod_start_timeout_seconds
        # 轮询 Kubernetes 状态直到 Ready、明确失败或等待时限结束。
        while time.monotonic() < deadline:
            try:
                pod = await asyncio.to_thread(self.api.read_namespaced_pod, rec.name, rec.namespace)
            except ApiException as exc:
                if exc.status == 404:
                    await asyncio.sleep(1)
                    continue
                raise PoolError(f"could not inspect debug Pod: {exc.reason}") from exc
            phase = pod.status.phase if pod.status else "Unknown"
            rec.node_name = pod.spec.node_name if pod.spec else None
            if phase == "Running" and self._ready(pod):
                if node_name and rec.node_name != node_name:
                    raise PoolError(f"debug Pod scheduled on {rec.node_name!r}, expected {node_name!r}")
                return
            if phase == "Failed":
                message = getattr(pod.status, "message", None) or "Pod entered Failed phase"
                raise PoolError(f"debug Pod scheduling failed: {message}")
            await asyncio.sleep(1)
        try:
            pod = await asyncio.to_thread(self.api.read_namespaced_pod, rec.name, rec.namespace)
            reason = getattr(pod.status, "reason", None) or getattr(pod.status, "message", None)
            if not reason and pod.status and pod.status.conditions:
                reason = "; ".join(c.message for c in pod.status.conditions if c.message)
        except Exception:
            reason = None
        raise PoolError(f"timed out waiting for debug Pod to become Ready" + (f": {reason}" if reason else ""))

    def _patch_idle(self, rec: PodRecord) -> None:
        """将最近空闲时间和 IDLE 状态写入 Pod Annotation。

        参数：
            rec: 已进入空闲状态的 Pod 记录。
        返回：
            无；Kubernetes API 错误由调用方处理。
        """
        annotations = {LAST_IDLE: datetime.fromtimestamp(rec.last_idle, timezone.utc).isoformat(), STATE: "IDLE"}
        self.api.patch_namespaced_pod(rec.name, rec.namespace,
                                      {"metadata": {"annotations": annotations}})

    async def _cleanup_loop(self) -> None:
        """按配置周期扫描过期 Pod，并逐个执行删除。

        参数：
            无。
        返回：
            无；收到取消信号后由 close 方法等待退出。
        """
        while not self._stopping:
            await asyncio.sleep(self.settings.cleanup_interval_seconds)
            # 单个 Pod 删除失败只恢复该记录状态，继续处理本轮其余记录。
            for key, rec in list(self.records.items()):
                if await self._try_mark_deleting(key, rec):
                    try:
                        await asyncio.to_thread(self.api.delete_namespaced_pod, rec.name, rec.namespace)
                    except ApiException as exc:
                        if exc.status != 404:
                            log_event(LOG, logging.WARNING, "exception", "删除空闲诊断 Pod 失败",
                                      namespace=rec.namespace, pod_name=rec.name, error=str(exc))
                            async with self._lock:
                                if rec.state == "DELETING":
                                    rec.state = "IDLE"
                            continue
                    except Exception:
                        log_event(LOG, logging.ERROR, "exception", "删除空闲诊断 Pod 异常",
                                  exc_info=True, namespace=rec.namespace, pod_name=rec.name)
                        async with self._lock:
                            if rec.state == "DELETING":
                                rec.state = "IDLE"
                        continue
                    async with self._lock:
                        rec.state = "DELETED"
                        self.records.pop(key, None)

    async def _try_mark_deleting(self, key: tuple[str, str], rec: PodRecord) -> bool:
        """原子检查空闲条件并将 Pod 标记为删除中。

        参数：
            key: 记录表中的 (命名空间, Pod 名称) 键。
            rec: 待检查的 Pod 记录。
        返回：
            条件满足且 Kubernetes Annotation 更新成功时返回 True。
        """
        async with self._lock:
            if rec.state != "IDLE" or rec.active or rec.uncertain:
                return False
            if time.time() - rec.last_idle < self.settings.idle_ttl_seconds:
                return False
            rec.state = "DELETING"
            try:
                await asyncio.to_thread(self.api.patch_namespaced_pod, rec.name, rec.namespace,
                                        {"metadata": {"annotations": {STATE: "DELETING"}}})
            except Exception:
                log_event(LOG, logging.ERROR, "exception", "标记诊断 Pod 删除状态失败",
                          exc_info=True, namespace=key[0], pod_name=key[1])
                rec.state = "IDLE"
                return False
            return True
