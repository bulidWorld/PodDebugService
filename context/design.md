# **Kubernetes MCP 网络诊断服务设计方案**

基于 Kubernetes API Server、MCP、LangGraph 的动态 Debug Pod 池设计

## **1. 设计背景与目标**

在 Kubernetes 运维 Agent 场景中，需要通过 MCP 工具执行网络诊断命令，例如 `curl`、`nslookup`、`dig` 和 TCP 连通性检测。

由于不同 Namespace、不同 Worker 节点可能存在不同的网络环境，诊断命令需要能够在指定的 Kubernetes 环境中执行，而不是仅从 MCP Server 所在容器发起请求。

传统的“每次请求创建一个 Pod，执行完成后删除”方案存在以下问题：

- 每次执行命令都需要经过 Pod 创建、调度、镜像准备等流程，响应速度较慢。
- 多个并发请求容易创建大量重复的诊断 Pod。
- Agent 需要管理 Pod 创建、执行、删除等多个工具，增加编排复杂度。
- 部分网络问题需要在指定 Namespace 或 Worker 节点复现。
- Pod 生命周期管理容易与正在执行的命令发生冲突。

因此，本方案设计一个由 MCP Server 自动管理的 **Debug Pod Pool（诊断 Pod 池）**，对 Agent 隐藏 Kubernetes 资源生命周期，只暴露统一的网络诊断接口。

### **1.1 核心设计目标**

| 目标           | 设计要求                       |
| ------------ | -------------------------- |
| 简化 MCP 工具    | 对外只暴露一个网络诊断工具              |
| 动态创建         | 无可用 Pod 时自动创建              |
| 自动复用         | 多次请求复用符合条件的 Pod            |
| Namespace 指定 | 支持指定 Pod 所在 Namespace      |
| Worker 指定    | 可选指定 Pod 所在 Worker 节点      |
| 并发执行         | 多个请求可以在同一个 Pod 中独立执行       |
| 同步返回         | 命令结束后直接返回执行结果              |
| 自动回收         | 最后一次任务完成后空闲 20 分钟删除        |
| 任务保护         | 正在执行任务的 Pod 不允许主动删除        |
| 无外部数据库       | 利用内存状态和 Kubernetes 元数据管理   |
| 故障恢复         | MCP Server 重启后重新发现已创建的 Pod |

### **1.2 非目标**

本方案不负责提供通用远程 Shell 平台，不允许 Agent 任意执行系统管理命令，也不负责长期保存所有网络诊断历史。

Pod 池主要用于短时间、无状态、受限的网络诊断。

## **2. 总体架构**

**CopilotKit / AG-UI**

用户交互、网络诊断结果展示

**LangGraph Agent Server**

MultiServerMCPClient / Tool Calling

**Kubernetes MCP Server**

统一工具：exec_debug_command

**DebugPodManager**

Pod 查找、创建、匹配、复用、并发协调

**CommandExecutor**

Exec、超时、输出捕获

**PodCleaner**

空闲检测、超时回收

**Kubernetes API Server**

Pod Create / Get / List / Exec / Delete

worker-01

Debug Pod A

worker-02

Debug Pod B

worker-03

Debug Pod C

### **2.1 模块职责**

| 模块               | 职责                          |
| ---------------- | --------------------------- |
| MCP Tool         | 参数解析、合法性校验、返回结果             |
| DebugPodManager  | 管理 Pod 池、匹配条件、资源占用          |
| PodFactory       | 构造 Manifest、创建 Pod、等待就绪     |
| CommandExecutor  | 通过 Kubernetes Exec API 执行诊断 |
| PodCleaner       | 检查空闲时间，自动删除过期 Pod           |
| PodStateRegistry | 管理 Pod 活跃任务数量和生命周期状态        |

所有模块部署在 Kubernetes MCP Server 内部，不需要独立微服务。

## **3. MCP 工具接口设计**

### **3.1 单工具原则**

不再向 Agent 暴露 `create_debug_pod`、`get_command_result` 和 `delete_debug_pod`。

对外只提供：

`exec_debug_command`

工具参数：

| 参数          | 类型      | 必填 | 默认值             | 说明                 |
| ----------- | ------- | -- | --------------- | ------------------ |
| `operation` | string  | 是  | —               | `http`、`dns`、`tcp` |
| `target`    | string  | 是  | —               | 网络诊断目标             |
| `namespace` | string  | 否  | `network-probe` | 指定执行环境             |
| `node_name` | string  | 否  | `null`          | 指定 Worker 节点       |
| `timeout`   | integer | 否  | 30              | 单次命令超时秒数           |

推荐将命令超时范围限制为 1～60 秒，便于控制任务资源消耗和 Pod 回收。

### **3.2 返回结果**

统一返回：

| 字段           | 含义              |
| ------------ | --------------- |
| `success`    | 命令是否成功          |
| `exitCode`   | 命令退出码           |
| `stdout`     | 标准输出            |
| `stderr`     | 错误输出            |
| `timedOut`   | 是否超时            |
| `podName`    | 实际使用的 Pod       |
| `namespace`  | 实际执行的 Namespace |
| `nodeName`   | 实际运行节点          |
| `reused`     | 是否复用了已有 Pod     |
| `durationMs` | 实际命令执行耗时        |

对于 HTTP 检测，可额外返回 `httpStatusCode`，避免将 HTTP 404/500 错误误判为正常请求。

### **3.3 工具执行原则**

MCP Server 在一次工具调用内完成整个执行流程：

1. 校验输入。
2. 获取或创建符合要求的 Debug Pod。
3. 登记任务占用。
4. 执行命令并同步等待结果。
5. 收集输出、退出码、执行耗时。
6. 释放 Pod 占用。
7. 返回结构化结果。

Agent 不需要知道 Pod 是何时创建的，也不需要管理 Pod 清理。

## **4. Debug Pod 池设计**

### **4.1 Pod 池的管理维度**

Pod 复用不是简单地寻找任意 Running Pod，而是根据执行环境进行匹配。

建议使用以下匹配维度：

Pod 复用匹配条件

**Namespace 匹配**

必须与请求 Namespace 一致

**Node 匹配**

指定节点时，必须运行在该节点

**镜像能力匹配**

包含请求所需的网络诊断工具

**Pod 状态与容量**

Running、Ready、未删除、未达到并发上限

**安全身份匹配**

满足授权范围和网络策略要求

如果 `node_name` 未指定，则允许使用该 Namespace 下任意合规节点上的 Debug Pod。

需要注意，这意味着“未指定节点”并不是要求每次都重新随机调度，而是优先复用已有资源。

### **4.2 Pod 选择策略**

建议优先复用已存在的 Pod。

当存在多个可用 Pod 时，可以选择当前活跃任务数量最少的 Pod，避免单个 Pod 过载。

但不建议仅为了负载均衡不断创建新 Pod。只有所有合适 Pod 都达到并发上限，或者没有健康 Pod 时，才需要创建新 Pod。

初始建议：

每个 Pod 最大并发 Exec

# **5**

初始配置值，可调

空闲回收时间

# **20 min**

从最后一次任务结束开始

并发上限是应用层限制，不是 Kubernetes 对 Exec 的硬性规定。

## **5. Namespace 与 Worker 节点调度**

### **5.1 Namespace 指定**

MCP Server 支持在授权的 Namespace 中创建 Debug Pod。

例如：

- `network-probe`：通用网络诊断。
- `monitoring`：监控服务网络问题排查。
- `production`：业务应用网络环境排查。

不同 Namespace 内的 Debug Pod 不复用。

这样做的核心目的不是资源归类，而是**使诊断 Pod 尽可能处于目标业务的网络策略环境中**。

不过，仅处于同一 Namespace 也不能完全等价于目标业务 Pod，因为 NetworkPolicy 还可以匹配 Pod 标签，某些环境还存在 ServiceAccount、服务网格或节点级网络规则。

### **5.2 Worker 节点指定**

指定 `node_name` 后，MCP Server 先检查节点是否存在、是否属于允许的节点范围，再使用 Kubernetes 调度约束。

推荐使用 `nodeAffinity`，通过 `metadata.name` 匹配目标节点，让 Kubernetes Scheduler 正常参与调度。

不推荐默认直接设置 `spec.nodeName`，因为这会绕过正常的调度决策。

如果指定节点资源不足、不可调度或不满足其他约束，应返回明确的调度失败原因，不自动切换到其他节点，否则会影响诊断准确性。

### **5.3 多节点复用示例**

| 请求                   | 处理行为                          |
| -------------------- | ----------------------------- |
| monitoring，未指定节点     | 复用 monitoring 中任意合规 Debug Pod |
| monitoring，worker-01 | 仅复用 worker-01 上的 Debug Pod    |
| monitoring，worker-02 | 仅复用 worker-02 上的 Debug Pod    |
| production，worker-01 | 复用或创建 production 对应 Pod       |
| 指定节点不存在              | 返回明确错误                        |
| 指定节点调度失败             | 返回调度失败原因                      |

## **6. Pod 生命周期管理**

这是整个设计中最重要的部分。

### **6.1 空闲超时，而不是创建后固定超时**

采用 **Idle TTL** 机制。

每次有新任务进入 Pod 后，Pod 处于活跃状态；当最后一个任务执行结束时，重新开始计算 20 分钟空闲时间。

例如：

Pod 生命周期示例

1. **10:00 创建 Pod**

   Running

   等待网络诊断请求。
2. **10:05 请求 A**

   开始执行 curl，10:06 完成，空闲计时更新。
3. **10:15 请求 B**

   执行 DNS 查询，10:16 完成，空闲计时再次更新。
4. **10:25 请求 C**

   执行网络检测，持续至 10:30。期间无论空闲计时如何，都不能删除 Pod。
5. **10:50 自动回收**

   删除

   最后一次任务在 10:30 完成，之后连续空闲 20 分钟。

实际删除时间可能受清理扫描间隔影响。

### **6.2 Pod 状态机**

**CREATING**

创建并等待容器 Ready

**IDLE**

可接收 Exec，等待请求

**BUSY**

activeExecutions ＞ 0

全部任务完成 → IDLE → 空闲 20 分钟

**DELETING**

禁止接收新任务，调用删除 API

**DELETED**

系统内部需要维护明确的状态，不能仅根据 Pod 的 Kubernetes `Running` 判断是否空闲。Kubernetes 不知道 MCP Server 当前还分配了多少个 Exec 请求。

### **6.3 执行保护**

为每个 Pod 维护 `activeExecutions`。

请求开始前，在并发锁保护下增加计数；任务完成并确认远程执行结束后减少计数。

清理条件必须同时满足：

\\[ \text{activeExecutions}=0 \\]

\\[ \text{now}-\text{lastIdleAt}\geq1200s \\]

并且 Pod 未处于删除中。

检查条件和标记删除必须是原子操作，避免清理器判断空闲时，新请求恰好分配到同一个 Pod。

如果出现远程执行超时、MCP 请求取消或连接中断，不能立即推断容器中的进程已经停止。应确认进程终止，或者保守地保持 Pod 受保护状态。

## **7. Kubernetes 元数据与故障恢复**

### **7.1 无数据库的状态管理**

采用两层状态：

| 状态类型               | 保存位置                            | 目的        |
| ------------------ | ------------------------------- | --------- |
| 活跃任务数、并发占用、删除标记    | MCP Server 内存                   | 高并发、快速协调  |
| Pod 归属、配置版本、最后空闲时间 | Kubernetes Labels / Annotations | 重启恢复与资源识别 |

建议记录：

- `managed-by=k8s-mcp-server`
- `profile=network-v1`
- `last-idle-at`
- `owner-instance` 或其他管理身份信息（如需要）

不需要 PostgreSQL、Redis 或单独的任务结果存储。

### **7.2 MCP Server 重启恢复**

重启后从 Kubernetes API 查询已有的受管 Debug Pod，验证其状态、配置和运行节点，然后重建本地 Pod 池。

但这里存在一个重要边界：

**Kubernetes Pod 元数据并不能直接告诉新的 MCP Server：旧实例发起的 Exec 是否仍在运行。**

所以恢复时不能简单将所有 Pod 的 `activeExecutions` 设为零并立即清理。

建议在单实例场景采用保守策略：重启后先将已有 Pod 标记为待确认，不立即删除；只有在确认没有遗留执行任务、或通过受控执行机制确认已安全退出后，才恢复正常复用与回收。

若要满足进程崩溃、多副本切换时也严格保证“执行中绝不删除”，需要进一步采用 Kubernetes Lease 或等效的持久化执行占用机制，并配合命令超时和遗留进程处理。不能仅依靠内存计数和 Annotation。

## **8. 并发控制与部署方式**

### **8.1 推荐初期使用单副本**

单副本 MCP Server 允许使用内存锁管理 Pod 池，架构简单。

同一 Pod 可同时承载多个 Kubernetes Exec 连接，前提是每个 Exec 独立捕获输出，命令不相互依赖本地文件或环境状态。

### **8.2 后续多副本扩展**

扩容时会面临三个问题：

- 不同 MCP Server 实例可能同时发现无可用 Pod，重复创建资源。
- 不同实例无法直接知道彼此的活跃 Exec 数量。
- 清理任务可能删除其他实例正在使用的 Pod。

推荐通过 Kubernetes Lease 实现跨实例协调，或独立设置 Pod Pool Manager。

单纯使用 Lease 作为创建锁还不够，必须同时解决执行占用和清理互斥。

因此本阶段明确以**单副本 MCP Server** 为基础实现，未来再扩展多副本协调能力。

## **9. 自动清理策略**

建议清理周期为 60 秒。

每次扫描：

1. 查询 MCP Server 管理的 Debug Pod。
2. 排除正在创建、删除或状态不明确的 Pod。
3. 检查是否存在正在执行的任务。
4. 判断最后空闲时间是否超过 20 分钟。
5. 在并发控制下将符合条件的 Pod 标记为 `DELETING`。
6. 调用 Kubernetes 删除 API。
7. 确认删除结果，失败时保留可重试状态。

清理失败不应影响其他 Pod，也不应导致整个 MCP Server 停止工作。

**不使用固定的&#x20;**`activeDeadlineSeconds: 1200`，否则运行时间超过 20 分钟的 Pod 仍可能被 Kubernetes 终止，与按空闲时间保留的要求冲突。

## **10. 安全及资源控制**

考虑到 MCP 工具由 Agent 调用，建议严格限制执行能力。

| 控制项            | 设计原则                          |
| -------------- | ----------------------------- |
| Namespace      | 配置允许使用的 Namespace 白名单         |
| Worker 节点      | 限制可选节点，避免误用敏感节点               |
| 命令类型           | 只允许 HTTP、DNS、TCP 等受控诊断        |
| Shell 执行       | 不允许任意 Shell 命令                |
| 容器权限           | 非 Root、禁止提权、Drop Capabilities |
| ServiceAccount | 不自动挂载 API Token               |
| CPU / 内存       | 为诊断容器配置 Requests 和 Limits     |
| 并发数量           | 限制每个 Pod 的同时执行数               |
| 输出大小           | 对 stdout/stderr 设置最大长度        |
| 命令时间           | 单个命令必须具有超时约束                  |
| Pod 总量         | 限制每个 Namespace 及全局 Pod 数量     |

RBAC 采用最小权限原则，按允许使用的 Namespace 授予 Pod 创建、查询、删除以及 Exec 相关权限。

另外，Pod 共享机制只适合相互信任的诊断请求。如果涉及多个不可信租户，应将租户安全身份也纳入 Pod 池匹配条件，不能跨租户共享容器执行环境。

## **11. 异常场景与预期行为**

| 异常场景                 | 处理策略                |
| -------------------- | ------------------- |
| Pod 不存在              | 创建新 Pod             |
| Pod Pending 超时       | 返回调度错误及相关原因         |
| Pod CrashLoop 或异常退出  | 标记不可用，尝试创建新 Pod     |
| Worker 节点 NotReady   | 不复用该节点上的 Pod        |
| Exec 建立连接失败          | 返回错误，必要时重新评估 Pod 状态 |
| Exec 超时              | 终止或确认远程任务退出后释放占用    |
| Pod 正在删除             | 不再分配新任务             |
| 空闲到期但仍有执行            | 延迟删除                |
| MCP Server 重启        | 重建 Pod 池，保守处理未知任务   |
| Kubernetes API 暂时不可用 | 不盲目修改占用状态，重试查询      |
| 达到 Pod 数量上限          | 等待容量释放或返回资源不足错误     |

对于需要保持诊断环境准确性的请求，不应在指定节点失败后自动降级到其他节点。

## **12. 设计决策总结**

| 设计问题      | 最终选择                            | 理由                 |
| --------- | ------------------------------- | ------------------ |
| MCP 工具数量  | 1 个                             | 降低 Agent 编排复杂度     |
| Pod 创建方式  | 按需创建                            | 避免始终占用集群资源         |
| Pod 复用方式  | Namespace + 节点 + 能力 + 安全身份匹配    | 保证诊断环境合理           |
| 节点调度方式    | Node Affinity                   | 保留 Kubernetes 调度能力 |
| Exec 执行方式 | 同步等待                            | 直接返回结果，无需结果数据库     |
| Pod 并发方式  | 共享 Pod、独立 Exec                  | 减少重复创建             |
| Pod 保留时间  | 最后一次任务完成后空闲 20 分钟               | 提高资源复用率            |
| 执行任务保护    | 活跃计数 + 原子占用/删除协调                | 避免任务执行中被清理         |
| 元数据保存     | Kubernetes Labels / Annotations | 减少外部依赖             |
| 资源回收      | MCP Server 内部定时清理               | 不增加 Agent 工具       |
| 初始部署      | MCP Server 单副本                  | 控制实现复杂度            |

## **13. 推荐实施顺序**

1. **第一阶段：基础网络诊断**

   实现单一 MCP 工具、Kubernetes Exec、Namespace 和 Worker 节点选择、同步返回结果。
2. **第二阶段：Pod 池与复用**

   实现健康 Pod 查找、能力匹配、按需创建和并发容量控制。
3. **第三阶段：空闲回收与任务保护**

   实现 20 分钟 Idle TTL、任务占用计数、原子删除状态切换和清理任务。
4. **第四阶段：故障恢复与生产加固**

   实现 Annotation 恢复、异常执行确认、Pod 数量限制、RBAC、审计和可观测性。

## **最终结论**

本方案采用 **一个 MCP 网络诊断工具 + 一个内部 DebugPodManager + 一个自动清理器** 的架构。

Agent 只负责表达诊断意图；MCP Server 负责选择执行环境、创建或复用 Pod、同步获取执行结果，并在最后一次任务完成且空闲 20 分钟后自动回收。

它既保留了跨 Namespace、指定 Worker 节点的诊断能力，又避免了每次请求重复创建 Pod 的开销。
