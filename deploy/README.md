# Kubernetes manifests

`deployment-plan.xml` 是符合 `context/开发/deployment-plan.xsd` 的执行计划，按下述顺序使用 `apply` 部署全部 YAML。计划中的路径相对于仓库根目录解析，执行器的工作目录应设置为仓库根目录。

按以下顺序创建资源：

```bash
kubectl apply -f deploy/0010-namespace.yaml
kubectl apply -f deploy/0020-account-role.yaml
kubectl apply -f deploy/0030-configmap.yaml
kubectl apply -f deploy/0035-callers-secret.yaml
kubectl apply -f deploy/0040-kubernetes.yaml
```

`api/mcp.yaml` 是服务启动时读取的 MCP 工具目录源文件，定义 Streamable HTTP endpoint、`exec_debug_command` 的入参/结果 schema，以及发布到 MCP `list tools` 的 MTAP 元数据。工具元数据须符合 `context/mtap-tool.schema.yaml`；当前 `approvalMode: none` 表示无需人工审批，但仍通过调用令牌鉴权。它不是 Kubernetes 资源，不能用 `kubectl apply`。服务会校验 MTAP Schema 及工具输入输出字段，不匹配时拒绝启动。调用方令牌仍从 Secret 注入的 `MCP_CALLERS_JSON` 读取。生产环境请将 `0035-callers-secret.yaml` 中的示例 token 替换为长随机值，避免将生产 token 提交到版本库。集群外访问时应配置 TLS Ingress/Gateway，并把 `PODDEBUG_PUBLIC_URL` 与 `api/mcp.yaml` 中的 URL 改成外部 HTTPS 地址。

MCP endpoint 为 `http://pod-debug-service.pod-debug.svc.cluster.local:8000/api/v1/mcp`。集群内客户端使用 `Authorization: Bearer <token>` 请求。

`0040-kubernetes.yaml` 包含单副本 Deployment 和 ClusterIP Service。将其中的镜像占位符替换为已推送的服务镜像。

`account-role.yaml` 为服务账号授予 `pod-debug` 中 Pod 的查询、创建、更新、删除和 Exec 权限，并授予读取 Node 的权限。若在 `PODDEBUG_ALLOWED_NAMESPACES` 中增加其他 Namespace，必须在 `account-role.yaml` 中为该 Namespace 增加一组 Role 和 RoleBinding。

服务和诊断 Pod 统一使用 `pod-debug` Namespace。
