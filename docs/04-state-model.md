# 04 状态模型与持久化契约

---

## 任务状态机

```
received
  → evaluating
  → routing
  → planning                    （仅当 route.decompose）
  → running
      ⇄ awaiting_clarification  （停住等人；可恢复）
      ⇄ escalated               （档位提升；有硬上限）
  → succeeded | failed | cancelled | budget_exceeded | rejected
```

### 两个需要解释的状态

**`awaiting_clarification` 是正常状态，不是异常路径。** 记账场景中「这是支出还是收入」
必须问清楚——猜错的代价是账目被静默污染，且用户往往几个月后才发现。把它建模成
一等状态（而不是某种错误重试），才能在界面上正确呈现、在指标上正确统计、
在恢复时正确续跑。

**`rejected` 与 `failed` 不同**：`rejected` 表示守卫无法产出合法决策
（例如无 handler 满足 `required_capabilities` 且没有可用的兜底），**什么都没有跑**。
`failed` 表示跑了但失败了。这个区分对成本统计很重要——`rejected` 的任务不应
计入失败率，它属于配置问题。

---

## 子任务状态机

```
pending → ready → running → (retrying) → succeeded
                     ↓
          failed | skipped | cancelled | defaulted
```

`succeeded` / `failed` / `skipped` / `cancelled` / `defaulted` 都是**终态**。

```
progress = 终态节点数 / 总节点数
```

这个公式与 `ai-workmate/app/lib/data/task_repo.dart` 完全一致，因此 Flutter 侧
已有的进度控件不需要改动就能显示调度层的任务。

`defaulted` 对应 `on_failure: continue_with_default` —— 节点失败了，但按预置默认值
继续。它与 `skipped` 的区别是：`skipped` 什么都没有，`defaulted` 有一个明确的替代值。

---

## 转移不变式

这几条是状态机的正确性条件，实现与测试都应据其校验：

1. 任务为 `succeeded` **当且仅当**所有非 `optional` 节点为 `succeeded` 或 `defaulted`。
2. `awaiting_clarification` 只能从 `running` 进入，答复后回到 `running`。
3. `budget_exceeded` 只能从 `running` / `planning` 进入，不能从终态进入。
4. **终态不可变。** 重跑产生**新任务**并置 `parent_task_id`，不改写原任务。
   这对审计很重要，也是 04 检测"返工"的依据。
5. 每次转移都追加一个 `seq` 单调递增的事件。

---

## 事件日志是状态的真源

任务的快照可以由事件日志折叠得出。这不是理论上的洁癖，它带来两个实际能力：

- **断线重放**：手机端退到后台再回来，用 `Last-Event-ID` 补齐缺失事件即可
  重建完整视图，不需要重新拉全量。
- **存储可替换**：已有事件管道的消费方可以完全不落任务表，只写事件流
  （`EventLogOnlyStore`）。

因此 `seq` 必须是**任务内单调**的，由存储层保证（SQLite 用自增列，
Postgres 用 per-task sequence 或 `bigserial`）。

---

## 持久化契约

```
StateStorePort {
  create_task(TaskRecord)
  get_task(task_id) -> TaskRecord | null
  update_task(task_id, patch, expected_revision) -> TaskRecord   # 乐观并发
  list_tasks(tenant_id, filter, cursor, limit) -> Page[TaskRecord]
  append_event(task_id, Event) -> seq                            # 单调
  read_events(task_id, since_seq, limit) -> [Event]              # 重放用
  prune_events(before_ts, keep_terminal_days) -> int
  get_idempotency(key, tenant) -> IdempotencyRecord | null
  put_idempotency(key, tenant, body_hash, task_id, ttl)
}

MediaStorePort {
  put(bytes, mime, ttl) -> MediaRecord
  get(media_id) -> stream | null
  stat(media_id) -> MediaMeta
  delete(media_id)
  sweep_expired(now) -> int
}

ConfigProvider {
  get_policy(scope) -> Policy
  put_policy_version(version, scope)
  resolve_secret(ref) -> str        # secret://llm/standard_model → 实际密钥
  get_user_config(user_id, handler_id) -> dict
  put_user_config(user_id, handler_id, dict)
}
```

### 明确不假定什么

这几条是硬约束，因为消费端包含一部手机：

- **不假定 Postgres。** 核心层没有 SQL，编排层的函数签名里没有 `AsyncSession`。
  对照：`ai-workmate` 的 `GraphState` 节点函数直接带 `db: AsyncSession` 与
  `user_id: UUID`，这类耦合不能复制。
- **不假定分布式部署。** 因此读路径不依赖咨询锁。`scheduler_lock` 只在 04 的定时
  分析任务里复用，且仅当配置的存储是 Postgres 时。
- **不假定事件可按 JSON 路径查询。** 事件只按 `seq` 顺序读。
- **不假定有对象存储。** 媒体存储是端口，手机端可以是本地文件。

### 提供的实现

| 实现 | 用途 | 说明 |
|---|---|---|
| `InMemoryStateStore` | 测试、开发、单进程 | 受 `limits.memory_events` 约束 |
| `SqliteStateStore` | **手机记账应用的本地模式**、嵌入式服务 | 单文件；自增 `seq`；支持跨应用重启的 `Last-Event-ID` 重放——这是移动端体验成立的前提 |
| `PostgresStateStore` | 服务端部署 | JSONB 载荷；`seq` 来自 per-task sequence |
| `EventLogOnlyStore` | Serverless / 已有事件管道 | 只写事件；快照由折叠事件重建 |

`EventLogOnlyStore` 的存在本身就是存储抽象成立的证明：任务快照确实可以从事件流
推导出来，所以已经有事件管道的消费方可以完全不要任务表。

---

## 关于成本状态

**当前默认 `advisory` 模式。** 完整记账、超过 `warn_at_ratio` 时告警，
但**永不在执行中打断任务**。

这是明确的取舍：先在真实使用里攒出成本数据，让 04 依据数据提出阈值建议，
而不是一开始就用一个拍脑袋的数字硬拦——那样拦错了既没有数据支撑，
用户也只会觉得"它莫名其妙不给我干"。

需要硬拦时把 `budget.enforcement.mode` 改成 `hard` 即可，无需改代码。
`hard` 模式下超出上限会取消剩余节点、置 `budget_exceeded`，
并**保留与上报部分产物**。

因此 `budget_exceeded` 这个任务状态在当前默认配置下不会出现——
它属于 `hard` 模式。契约保留它，是为了让这个模式切换不需要改契约。

---

## 相关文档

- 接口与 SSE → [03-http-contract.md](03-http-contract.md)
- 媒体生命周期 → [05-media.md](05-media.md)
- handler 拿到的上下文 → [07-handler-seam.md](07-handler-seam.md)
