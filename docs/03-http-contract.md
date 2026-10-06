# 03 接口契约

机器可读的契约在 `openapi.yaml`，数据结构在 `schemas/*.json`。
本文补充那些 JSON 表达不了的东西：语义、时序、以及每个决定背后的理由。

**契约文件是唯一事实来源。** 参考实现是"符合契约的一个实现"，不是契约本身。

---

## 端点总览

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/tasks` | 提交任务。写操作意图必须带 `Idempotency-Key` |
| `GET` | `/v1/tasks` | 列出任务（游标分页） |
| `GET` | `/v1/tasks/{id}` | 任务快照 |
| `GET` | `/v1/tasks/{id}/events` | SSE 事件流，支持 `Last-Event-ID` 重放 |
| `GET` | `/v1/tasks/{id}/result` | 终态产物 |
| `POST` | `/v1/tasks/{id}/clarify` | 回答澄清问题，恢复任务 |
| `POST` | `/v1/tasks/{id}/cancel` | 取消（幂等） |
| `POST` | `/v1/tasks/{id}/feedback` | 提交人工质量信号 |
| `POST` | `/v1/media` | 上传媒体 |
| `GET` | `/v1/media/{id}` | 取回媒体 |
| `GET` | `/v1/capabilities` | 已注册 handler 与工具的自省 |
| `GET` | `/v1/policy` | 当前生效策略 |
| `GET` | `/v1/policy/versions` | 版本历史 |
| `POST` | `/v1/policy/rollback` | 回滚到指定版本 |
| `GET` | `/v1/evolution/suggestions` | 列出改进建议 |
| `POST` | `/v1/evolution/suggestions/{id}/approve` | **使用者本人**批准 |
| `POST` | `/v1/evolution/suggestions/{id}/reject` | 拒绝（理由本身是信号） |
| `POST` | `/v1/evolution/analyze` | 手动触发一次分析 |
| `GET` | `/v1/usage` | 成本与用量 |
| `GET` | `/v1/health` | 健康与各档位供应商状态 |

关于 `/v1/evolution/*`：它看起来像管理接口，但契约里**不建模 admin 角色**。
它建模的是"策略范围的所有者"——单用户场景就是使用者本人，多人场景是租户所有者。
理由见 [06-self-evolve.md](06-self-evolve.md)。

---

## 同步与异步

**异步是基底，同步是优化。** 同一条流水线只实现一次。

```
mode_preference: auto | sync | async
```

| 情形 | 响应 |
|---|---|
| `async` | `202` + `task_id` + `events_url` + `result_url` + 预估成本与耗时 |
| `sync`，投影耗时在预算内 | `200`，内联 `profile` / `decision` / `result` |
| `sync`，但投影超 `limits.sync_timeout_ms` | `202`，`mode_changed: true`，`mode_change_reason: projected_exceeds_sync_budget`，附 `projected_wall_ms` 与 `sync_budget_ms` |
| `sync`，但执行中需要澄清 | `200`，`status: awaiting_clarification` + 问题本身 + `task_id`。答复后以异步完成 |

为什么同步响应要内联 `profile` / `decision`（而不只是 `result`）：同步调用方付了一次
往返，就应该一次拿全，而不是再查三次。异步调用方则按需查——它本来就在轮询或订阅。

**同步在人机交互边界上退化为异步**，因为同步请求不能阻塞等人。这是诚实的降级，
不是缺陷。

---

## 幂等

```
Idempotency-Key: bookkeeping:u_123:sha256-3f9a1c7e
```

| 情形 | 行为 |
|---|---|
| 同 key + 同请求体哈希 | 返回既有任务的当前状态，**不重跑** |
| 同 key + 不同请求体哈希 | `409 idempotency_conflict` |
| 缺 key，且路由可能命中 `write` 工具 | `400 invalid_request` |

**必填的判定在路由之后**：请求先被评估与路由，若 `tool_set` 中含 `side_effects: write`
的工具而没有幂等键，则拒绝。这样既不必让调用方为只读请求也背一个键，也不会漏掉写操作。

键的作用域是 `(key, tenant_id)`，TTL 由配置指定。

### 写操作的重试安全

handler 不需要自己实现"重试不重复写入"。调度层按 `(task_id, subtask_id)` 派生
**稳定的** `idempotency_token` 注入 `DispatchContext`，重试时拿到同一个 token，
由 handler 侧以它作为幂等依据。这是 handler 契约里的一条保证（见
[07-handler-seam.md](07-handler-seam.md)）。

---

## 事件流（SSE）

```
event: subtask.completed
id: 7
data: {"subtask_id":"extract","output":{...},"cost":{"amount":0.0078,"currency":"CNY"}}

```

- `id:` 是事件日志的单调 `seq`，**它同时是重放游标**。
- 最后一个数据行后有一个空行（SSE 规范）。
- 事件类型与载荷见 `schemas/event.json`。

### 重放

客户端重连时带 `Last-Event-ID: <seq>`（或 `?since=<seq>`），服务端先重放
`seq+1` 起的全部事件，再切到实时推送。两者同时提供时以 `Last-Event-ID` 为准。

**这是移动端必需的能力。** 应用退到后台、网络切换、锁屏都会断连，而任务仍在服务端
继续执行。因此事件日志必须**持久**，不能只喂活连接——现有 `duowei-ai` 的 SSE 就是
即发即弃的，断线即丢失整轮运行，这里不沿用。

`heartbeat` 事件按 `limits.sse_heartbeat_ms` 发送，用于穿透 NAT 保持连接。

### 事件类型

| 事件 | 时机 |
|---|---|
| `task.created` | 受理时 |
| `profile.ready` | 01 完成（含 `degraded` 标记） |
| `route.decided` | 02 完成（含 `guard.applied`） |
| `plan.ready` | 拆解完成 |
| `subtask.started` / `.progress` / `.completed` / `.retrying` / `.failed` | 逐节点状态 |
| `agent.round` | agent 节点每完成一轮循环（含轮数、本轮工具、停止原因） |
| `agent.review` | 独立复核的进展与结论（仅在节点声明了 `verification` 时） |
| `task.escalated` | 档位升级 |
| `token` | 流式部分文本 |
| `budget.warning` / `budget.exceeded` | 成本告警 |
| `clarification.needed` | 任务停住等人 |
| `task.completed` / `.failed` / `.cancelled` | 终态 |
| `error` | 非终态错误（结构化的 `Problem`） |
| `heartbeat` | 保活 |

事件命名刻意呼应 `duowei-ai` 的 `agent_status` / `progress` / `done`，
使已有前端有熟悉的语义；但信封更丰富：带 `id` 可重放、载荷结构化、
失败有类型。

---

## 错误

错误体是 RFC 9457 形状的 `Problem`（`schemas/problem.json`）。

### 契约硬性要求：错误必须是有类型的

**不得**把上游异常吞成字符串再拼进正文。契约的反例 fixture
`fixtures/problem.invalid.json` 里就是这个具体写法：

```json
{ "code": "internal_server_error", "detail": "[DeepSeek错误] HTTP 500" }
```

它同时违反两件事：`code` 不在封闭词表里，且 `retryable` 缺失——调用方无法判断
该不该重试。参照 `duowei-ai/backend/app/core/llm_client.py::llm_chat()`，
它正是这么做的。**不要复制这个做法。**

| code | HTTP | 可重试 | 含义 |
|---|---|---|---|
| `invalid_request` | 400 | 否 | 不符合 schema |
| `unsupported_media` | 415 | 否 | MIME 不被接受（在任何模型开销前拒绝） |
| `media_too_large` | 413 | 否 | 超出大小上限 |
| `idempotency_conflict` | 409 | 否 | 同 key 不同体 |
| `no_capability_match` | 422 | 否 | 无 handler 满足 `required_capabilities` |
| `budget_exceeded` | 402 | 否 | 超出上限（仅 `hard` 模式会出现） |
| `policy_violation` | 422 | 否 | 守卫无法产出合法决策 |
| `handler_error` | 502 | 视情况 | handler 抛出；`retryable` 反映 `node.retry` |
| `upstream_llm_error` | 502 | 是 | 供应商失败，会反馈给熔断器 |
| `timeout` | 504 | 是 | 墙钟超时 |
| `cancelled` | 499 | 否 | 客户端取消 |
| `rate_limited` | 429 | 是 | 带 `Retry-After` |
| `not_found` | 404 | 否 | 资源不存在 |
| `result_not_ready` | 409 | 是 | 任务未到终态 |

### 重试

客户端只重试 `retryable: true` 的，指数退避加抖动，有上限。

内部重试分三类，各有硬界：节点级（`node.retry`）、档位升级（`escalation_rule`，
受 `max_escalations` 约束）、重规划（`max_replans`）。**每一次重试都发事件**，
使重试可见而不是神秘。

---

## 取消

`POST /tasks/{id}/cancel` 幂等：已处于终态时仍返回 `204`。

取消传播到执行中的节点。已完成节点的产物**保留并如实上报**——一张已抽取好字段的
票据截图，即使最终没有入账，它的抽取结果也是有价值的。

---

## 人工质量信号

`POST /tasks/{id}/feedback`：

```json
{
  "verdict": "edited",
  "edits": [{"field": "category", "from": "其他", "to": "餐饮"}],
  "reason": "分类不对"
}
```

**这是 04 自进化最重的输入，也是最难自动化获取的一类数据。** 记账场景的自然采集点
就是确认/修改页：用户把分类从「其他」改成「餐饮」，等于给出了一条**带真值的标注**——
它同时指出错在哪（`field`）和对的是什么（`to`）。

没有这个端点，04 只能靠延迟与成本反推质量，效果差一个量级。

---

## 版本兼容

- `TaskProfile` / `RouteDecision` / `ExecutionPlan` 各带自己的 `*_version`。
- 消费端契约（`TaskSnapshot`）的兼容策略：**只做加法**（新增可选字段），
  并在握手时用 `client.min_supported_client_version` 声明客户端下限。
  手机应用无法强制升级，这是必须遵守的约束。

---

## 相关文档

- 数据结构 → `schemas/*.json`、`openapi.yaml`
- 状态机 → [04-state-model.md](04-state-model.md)
- 媒体 → [05-media.md](05-media.md)
- 验收清单 → [10-conformance-checklist.md](10-conformance-checklist.md)
