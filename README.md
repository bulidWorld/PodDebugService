# Pod Debug Service

单副本 Kubernetes MCP 服务，在集群中按需创建并复用网络诊断 Pod。Agent 只需调用 `exec_debug_command`，支持 `http`、`dns`、`tcp` 三种操作。

## 运行

```bash
python -m venv .venv
. .venv/bin/activate
pip install .
PODDEBUG_ALLOWED_NAMESPACES=network-probe,monitoring pod-debug-service
```

服务通过 MCP Streamable HTTP 传输，默认监听 `0.0.0.0:8000/mcp`。请求需携带 `Authorization: Bearer <token>`；令牌及调用方工具授权从 `MCP_CALLERS_JSON` 读取。Kubernetes 部署使用 Secret 注入此变量，集群内使用 ServiceAccount 凭据访问 API；本地调试时读取 kubeconfig。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PODDEBUG_DEFAULT_NAMESPACE` | `network-probe` | 缺省 namespace |
| `PODDEBUG_ALLOWED_NAMESPACES` | 缺省 namespace | 逗号分隔白名单 |
| `PODDEBUG_ALLOWED_NODES` | 空 | 逗号分隔节点白名单；空表示不额外限制 |
| `PODDEBUG_IMAGE` | `nicolaka/netshoot:v0.13` | 含 curl、dig、nc 的诊断镜像 |
| `PODDEBUG_MAX_EXEC_PER_POD` | `5` | 单 Pod 并发 Exec 上限 |
| `PODDEBUG_IDLE_TTL_SECONDS` | `1200` | 空闲回收 TTL |
| `PODDEBUG_CLEANUP_INTERVAL_SECONDS` | `60` | 清理扫描间隔 |
| `PODDEBUG_POD_START_TIMEOUT_SECONDS` | `120` | Pod Ready 等待上限 |
| `PODDEBUG_MAX_PODS_GLOBAL` | `30` | 本实例管理的全局 Pod 上限 |
| `PODDEBUG_MAX_PODS_PER_NAMESPACE` | `5` | 每个 namespace 的 Pod 上限 |
| `PODDEBUG_MAX_OUTPUT_BYTES` | `65536` | stdout/stderr 各自上限 |
| `PODDEBUG_CPU_REQUEST` / `PODDEBUG_CPU_LIMIT` | `50m` / `500m` | 容器 CPU 配额 |
| `PODDEBUG_MEMORY_REQUEST` / `PODDEBUG_MEMORY_LIMIT` | `64Mi` / `256Mi` | 容器内存配额 |

工具参数 `timeout` 仅允许 1–60 秒。HTTP 请求返回 `httpStatusCode`，状态码大于等于 400 时 `success` 为 false。

## 安全与部署

Pod 使用非 root UID、只读根文件系统、drop 全部 Linux capabilities、禁用提权和 ServiceAccount token 自动挂载。服务只把固定参数传给 `curl`、`dig`、`nc`，不接受 Shell 命令。namespace 默认拒绝，必须显式加入白名单。目标 URL 仍应通过集群网络策略控制其可访问范围。

服务按单副本设计；进程重启后恢复 Pod 元数据，但将恢复的 Pod 标记为状态未知，在确认前不复用也不回收，以防旧 Exec 仍在运行。跨副本部署需要额外的 Lease/执行租约协调。

部署 ServiceAccount 至少需要在允许的 namespace 中 `get/list/watch/create/delete/patch` Pods 和 `create` `pods/exec` 子资源；指定节点时还需要 `get` Nodes。建议按 namespace 创建 Role/RoleBinding，并单独授予必要的 Node 只读权限。

## 工具示例

```json
{"operation":"http","target":"https://example.com/health","namespace":"network-probe","timeout":15}
```

```json
{"operation":"dns","target":"example.com"}
```

```json
{"operation":"tcp","target":"db.internal:5432","node_name":"worker-01"}
```
