# 02 四个阶段

本文按阶段展开。每节的结构固定：**职责 → 输入 → 判断与确定性代码的分工 → 输出 →
配置位置 → 失败模式**。重复这个结构是为了让"某个决策该由谁做"永远能一眼查到。

贯穿全文的一条纪律：**能确定就别用模型。** 每次调用 LLM 都是一次花钱、一次引入
不确定性、一次让链路更难复现。判断力是稀缺资源，只花在真正需要判断的地方。

---

# 阶段 01 — TaskEvaluator

## 职责

把一个不透明的请求变成结构化、可路由的画像。

**不做**：不选路由、不执行任何事、不调用 handler 的工具。

## 输入

`TaskEnvelope`（见 `schemas/task_envelope.json`）。要点：

- `input.text` 与 `input.media[]` 至少有一个。媒体是**引用**不是字节
  （v1 的调度层自带上传端点，见 [05-media.md](05-media.md)）。
- `declared.authoritative` 是唯一被认可的快捷路径。它**不是关键词匹配**，而是调用方
  的结构化断言"我已经知道这是什么"。`authoritative: false` 时上面的 `intent` /
  `capability` 只是**待验证的提示**，不是事实。
  **实测修正（2026-10-06）**：评估器**每个请求都跑**，没有任何一条路径会跳过它——
  `routes[].evaluate` 这个字段目前没有任何代码读它（全仓仅剩声明与注释）。
  `authoritative: true` 现在真正起作用的地方是**评估失败时的兜底**：见下面
  "评估器什么时候跑"那节的降级说明。
- `constraints.data_sensitivity` 影响可选路由（金融数据不得选择会把原图外发的路由）。

## 分工

| 做判断的 | 机制 |
|---|---|
| 任务类型、意图、复杂度、紧急度 | **LLM** |
| 能力候选、期望抽取字段、是否需要澄清 | **LLM** |
| 媒体 MIME / 大小校验、哈希 | 确定性（在任何模型开销之前） |
| 量级桶 → 金额 | 确定性（乘 `config/pricing.yaml`） |
| 是否跳过评估 | 确定性（**当前恒为"不跳过"**；`routes[].evaluate` 尚未接线） |
| 复杂度分数 → 档位 | 确定性（查 `thresholds.complexity_band_cutoffs`） |

注意 **LLM 只给量级桶，不做算术**。它擅长判断"这活看着不小"，不擅长算
`1420 × 0.004 / 1000`。

## 评估器什么时候跑

**每个请求都跑**，但跑在最便宜的档位上。理由是行为一致性比省那点钱重要：
如果某些请求跳过评估，路由质量就会随入口而变，04 也拿不到可比的样本。

在此之上加两条优化：

1. **双档升级**：先用极小档自判"信息够不够做决策"，不够才升到标准档重评。
   升级次数有硬上限（`evaluator.escalation.max_escalations`），防止反复重评。
2. **画像缓存**：相似请求复用上次画像。注意**相似度判定由 LLM 完成**，
   不能用写死的字符串比较——那会变成关键词匹配的变体。

## 输出

`TaskProfile`（`schemas/task_profile.json`）。两个字段值得特别说明。

### `needs_clarification` 是一等输出

这是记账场景最重要的一条设计。当关键信息缺失、且**猜错会造成实际损害**时，
必须置 `true` 并给出问题，而不是猜一个往下走：

```json
{
  "needs_clarification": true,
  "clarification": {
    "question": "这张截图是支出还是收入？",
    "options": [ {"id":"expense","label":"支出"}, {"id":"income","label":"收入"} ],
    "blocking": true,
    "partial_profile": { "candidate_capabilities": ["bookkeeping.expense.record","bookkeeping.income.record"] }
  }
}
```

分界线是：**错了以后用户能不能自己发现并改正。**

- 商户名不规范 → 能改（确认页），不必问。
- 分类不确定 → 能改，不必问。
- 支出还是收入 → 写进账本后用户不会发现，**必须问**。

问的成本是一次往返；猜错了写进账本，用户可能几个月后才发现。

`partial_profile` 让答复后不必从头重问。

### `degraded`

评估器未能正常完成（超时 / JSON 修复失败）时，加载 `fallback.profile_id` 的兜底画像，
置 `degraded: true`，**继续**任务而不是失败。降级率是一个被 04 监控的指标。

**兜底画像优先沿用调用方的权威声明。** 若 `declared.authoritative: true` 且其 `intent`
在词表内，兜底画像就用它作为 `task_type`（敏感级取自词表，候选能力取自该类型
`domain` 对应 handler 自己的声明），而不是无条件落到 `generic.unknown`。理由是
**评估失败恰恰是最需要这条信息的时刻**：空画像会让下游的流程模板匹配不上、
自由拆解拿到空输入，最终产出空计划（P0-1c）。声明的 `intent` 不在词表内时不认——
声明是断言，不是特权，封闭词表不能被调用方绕过。

## 配置位置

| 内容 | 位置 |
|---|---|
| 任务类型词表 | `config/taxonomy.yaml` |
| 成本量级桶 → token 数 | `config/pricing.yaml` |
| 档位、超时、修复次数、升降级、缓存 | `config/routing.policy.yaml` 的 `evaluator` 段 |
| 系统提示词 | `prompts/evaluator.md` |

## 失败模式

| 失败 | 检测 | 处置 |
|---|---|---|
| 超时 | `evaluator.timeout_ms` | 用兜底画像，置 `degraded`，继续；发 `profile.ready` |
| JSON 非法 | schema 校验 | 修复重试至 `max_repair_attempts`，再不行走兜底画像 |
| 类型不在词表 | 按 taxonomy 校验 | 回落到 `taxonomy.fallback_type`，记 `taxonomy_miss` 指标（该指标升高是 04 提出"词表缺项"的依据） |
| 能力候选无人满足 | 注册表查找 | 照常往下走；路由器会落到兜底路由并发 `no_capability_match` |
| 图像不可读 / 超大 | 确定性前置校验 | `415` / `413`，**在任何模型开销之前**拒绝 |
| 输入歧义 | `confidence < thresholds.clarify_below` | 置 `needs_clarification`，任务停住，发 `clarification.needed` |

---

# 阶段 02 — Router

## 职责

把 `TaskProfile` 映射为 `RouteDecision`。决策表就在这里。

## 分工

**LLM 从菜单里选。** 菜单由 `render_policy_for_llm(policy, profile)` 生成，形如：

```
【请求画像】
<TaskProfile JSON>

【可选路由】（只能从下列 id 中选择）
- direct_answer         路径=direct_llm        允许档位=[cheap, standard]  成本上限=0.003  模式=sync
    适用：<该路由的 when 散文>
- single_tool_action    路径=single_step_tool  允许档位=[cheap, standard]  成本上限=0.020  模式=sync
    适用：...

【本请求可用工具】（来自已注册 handler 的声明，只能从中选择）
bookkeeping: extract_receipt_fields(none,vision.extract)  normalize_merchant(read)  dedupe_check(read)  build_ledger_entry(write)
calendar:    ...

【硬约束】
- 用户数据敏感级别 = financial；不得选择会把原始图片外发的路由
- 模型档位只能从 cheap / standard / strong 中选

【输出】
只输出 JSON：{route_id, model_tier, handler, tool_set[], execution_mode, decompose,
             budget{...}, escalation_rule{...}, rationale(≤120字), confidence(0-1)}
```

`when:` 是散文——因为读它的是 LLM，而散文正是 LLM 最擅长的接口。它是 YAML，
所以可编辑；它在文件里，所以有版本。**新增一条路由 = 追加一段 YAML。**

**代码做校验。** 确定性步骤按固定顺序：

1. **幂等 / 缓存命中** —— 同 key + 同请求体哈希 → 返回既有决策与任务（重放，不重跑）。
2. **预算预留** —— `BudgetLedger.reserve(user, tenant, decision.budget.max_cost)`。
   注意当前默认 `advisory` 模式：只记账与告警，不中断。
3. **供应商健康门** —— 某档位的供应商熔断打开（`health.state`）时，
   确定性地降到 `allowed_tiers` 中最近的健康档位。
4. **`PolicyGuard`** —— 集合校验：

   ```
   route_id ∈ policy.routes.ids                      否则 → fallback.route_id
   tier     ∈ routes[route_id].allowed_tiers         否则 → default_tier
   tool_set ⊆ handler.tool_names                     否则 → 取交集（空则回落）
   handler  ∈ registry.ids                           否则 → 兜底路由
   budget.max_cost ≤ min(路由上限, 请求上限)          否则 → 截断
   ```
5. **模式仲裁** —— 优先级与配置分层一致：**请求 > 路由配置 > 模型建议**。

   | 情形 | 结果 |
   |---|---|
   | 路径是 `decompose` | async（结构上不可能同步） |
   | 调用方明确要 `async` | async |
   | 调用方明确要 `sync` | sync —— 路由配置与评估器的建议**都只是建议**，不能推翻调用方的明确主张 |
   | 调用方写 `auto` | 路由配置或评估器建议任一倾向 async 即 async |

   最后一条里 async 是安全方向：它只是慢一点，而错误地选 sync 会中途超时。
   第 3 行被结构性否定时（要拆解却要 sync），提升为 async 并置
   `mode_changed: true` 与 `mode_change_reason: route_requires_decomposition`。

## 输出

`RouteDecision`（`schemas/route_decision.json`）。两个字段值得说明。

### `guard.applied` 由代码写入，不是 LLM 输出

它记录每一次确定性修正。于是"LLM 反复被纠正"从一个隐性问题变成一个可观测信号：
某条路由的守卫推翻率持续偏高，几乎总意味着它的 `when:` 散文与实际流量已经不符。
这正是 04 提出 `route_guidance_patch` 的依据。

### `escalation_rule.max_escalations`

硬上限，由代码执行，不受 LLM 影响。否则升级会成环。

## 配置位置

`config/routing.policy.yaml`：`model_tiers` / `routes` / `thresholds` / `budget` /
`fallback` / `router`。提示词在 `prompts/router.md`。

`locked_paths` 说明哪些内容 04 永远不能改。

## 失败模式

| 失败 | 处置 |
|---|---|
| 路由器超时 | 兜底路由，`degraded: true`，`fallback_used: true` |
| 路由 id 不在策略中 | `PolicyGuard` → 兜底路由 |
| 档位不在 `allowed_tiers` | 降到 `default_tier`，记入 `guard.applied` |
| 工具集含 handler 未声明的工具 | 取交集；交集为空 → 兜底路由 + `tool_set_violation` 指标 |
| LLM 每次都选最强档（档位通胀） | 由 04 的确定性指标 `mean_tier_index` 发现 → 建议收紧 `allowed_tiers` 或改写 `when:`。**不是代码修复** |
| `confidence < escalate_below_confidence` | 确定性地升到 `escalation_tiers[tier]`，受 `max_escalations` 约束 |

---

# 阶段 03 — Decomposer + DagRunner

## 职责

把"要拆解"的决策变成一张 DAG，然后执行它，全程上报状态。

## 拆解：两条来源，都是配置驱动

1. **`flow_templates/*.yaml`** —— 具名、参数化、预先校验过的 DAG。
   LLM 选一个模板并绑定参数。
2. **自由拆解** —— LLM 在 `tool_set` 与 `required_capabilities` 约束下自行生成。

模板存在的理由：票据入账的形状是确定的，不该每次请求都让 LLM 重新拆一遍——那既费
token，又会在同一个形状上反复产生小幅幻觉（顺序变化、多加一个节点）。模板是数据不是
代码，新增一个模板 = 加一个 YAML 文件。

**模板未命中率**是一个被监控的指标：某类任务反复走自由拆解，说明该补一个模板。

## 节点的两种执行者

每个节点声明 `executor`：

- **`tool`** —— 单次工具调用。输入确定则输出唯一。例：按 media_id 查重、按已定字段写库。
- **`agent`** —— 带角色、带工具白名单、有界多轮循环的自主执行者。用于需要"先看到中间
  结果再决定下一步"的步骤。例：图糊了要先放大局部重看；商户名不规范要结合用户历史习惯判断。

判错的代价不对称：本该用 `tool` 的写成 `agent`，浪费成本，有硬界兜底；本该用 `agent`
的写成 `tool`，根本产不出结果。所以拿不准时可偏向 `agent`，但不可滥用——每一步都变 agent
会让成本和延迟同时失控。

> **多 Agent 协作的完整设计见 [11-multi-agent.md](11-multi-agent.md)。**

## 节点超时 `timeout_ms`：是"每次调用"，不是"每个节点"

**实测修正（2026-10-06）。** 模板里的 `timeout_ms` 读起来像节点级上限，实际语义是
**单次 LLM 调用**的超时。证据链：

- 接线点只有一处——`NodeExecutor._run_agent` 在**每一轮**循环里
  `ctx.llm_json(..., timeout_ms=node.timeout_ms)`；
- 该值传到 `OpenAICompatibleLLM` 后变成 httpx 单次 POST 的 `timeout=`；
- `DagRunner` 里**没有任何节点级墙钟**（没有 `wait_for`，也不对 `timeout_ms` 求和）。

于是节点总耗时上限 ≈ `timeout_ms × max_rounds × (1 + retry.max)`。`receipt_to_entry`
的 `extract`（`max_rounds: 4`、`retry.max: 1`、`timeout_ms: 20000`）最坏可跑到
160 秒——**实跑 54.2 秒仍成功，是符合预算的，不是超时失效**。`normalize`
（`max_rounds: 3`、8s）同理，14.7 秒在预算内。

**已知缺口**：`executor: tool` 的节点上 `timeout_ms` **完全不参与**——`_run_tool`
不传它，工具内部自调的 `ctx.llm(...)` 也不带超时，于是退回全局默认
（`settings.llm_request_timeout_s`）。对 `dedupe` / `write` 这类不碰模型的确定性工具
无害；对**会调模型**的 tool 节点（如 `extract_receipt_fields`）则意味着节点声明的
超时被绕过。`tests/test_node_timeout.py` 把这个现状钉住了。

## 执行前的确定性校验

**这一层是幻觉的拦截网**，且全是泛化的、无语义的检查：

- **计划至少有一个节点**（空计划单独判、最早判，并在空集上短路——其余检查在空集上恒真）
- DAG 无环
- 所有 `depends_on` 目标存在
- 所有 `tool` ∈ `handler.tool_names`
- 所有 `role` ∈ `agents.yaml` 的 `roles[].id`
- `tool_whitelist` ⊆ 角色 `allowed_tools` 且 ⊆ 决策 `tool_set`
- `join` 集合与入边集合完全一致
- 每个 `output_schema_ref` 可解析
- 节点预算之和 ≤ 决策预算
- `agent` 节点数与总轮数不超 `limits.max_agent_nodes_per_plan` / `max_total_rounds_per_task`

任一违规 → 有界重规划（`decomposer.max_replans`）→ 再不行 `plan_invalid` 失败。

**注意这些检查里没有一条是关于领域的。** LLM 幻觉出一个不存在的工具名，
是被集合成员判定拒绝的，不是被某条规则拒绝的。

## 执行：DagRunner

把 `duowei-ai` 的 `asyncio.gather` + `Semaphore` 波次模式泛化成真正的动态 DAG：

```
ready = { n | n.depends_on ⊆ succeeded }
while ready:
    sem = Semaphore(plan.max_parallelism)          # 来自计划，不是模块常量
    wave = ready 按关键路径长度降序
    results = await TaskGroup(run_node(n) for n in wave)
    按各节点的 on_failure 处置
    ready = 重新计算就绪集
```

相对 `duowei-ai` 现有实现的每一处改进，都对应一个具体缺陷：

| 改进 | 现有代码的缺陷 |
|---|---|
| 动态就绪集，而非硬编码波次 | `wave1` / `wave2` 是 Python 字面量 |
| 并发度来自 `plan.max_parallelism` | `MAX_CONCURRENT_AGENTS = 9` 是模块常量 |
| 取消传播（`TaskGroup` / 显式 task 跟踪） | 完全没有；客户端断连后 gather 仍在跑 |
| 每个失败产出有类型的 `NodeFailure` | `return_exceptions=True` 后把异常用 `"; "` 拼成一个字符串，结构被破坏 |
| 每节点后 `BudgetLedger.charge()` | 两个代码库里都没有飞行中的成本记账 |
| 事件日志持久 + `Last-Event-ID` 重放 | SSE 即发即弃，断线丢失整轮运行 |

## 节点间数据流

`$ref` 解析到受限上下文：`envelope.*`、`task.artifacts.*`、`<subtask_id>.<field>`。
解析失败是**确定性错误**，与 LLM 无关。

## handler 看到的世界

节点执行时，handler 只拿到 `DispatchContext`：

```
DispatchContext {
  task_id, subtask_id, tenant_id, user_id, trace_id
  media: MediaResolver          # media_id → 字节/流，MIME 与大小已校验
  llm(messages, requires=[], tier_override=None)   # 按能力解析，不按模型名
  config: dict                  # 该用户的 handler 配置，已按其 schema 校验
  store: StateStorePort         # 按 handler 命名空间隔离
  emit(event)                   # 进度 / 部分结果 → SSE
  budget: BudgetHandle          # charge(amount), remaining()
  idempotency_token: str        # 按 (task_id, subtask_id) 派生，稳定
  cancellation: CancellationToken
  clock: Clock                  # 可测时间
  logger
}
```

**`llm()` 的签名收的是 `requires`（能力），不是模型名。** 这是"禁止硬编码模型"在结构上
的强制：handler 连模型名字符串都拿不到。

## 同步与异步：一套基底

- **异步**：`POST /tasks` 返回 `202` + `task_id`，DAG 后台执行，状态走
  `GET /tasks/{id}` 或事件流。
- **同步**：API 层内联跑同一条流水线，返回 `200` 与最终结果。
  硬界 `limits.sync_timeout_ms`；超界则**提升为异步**，返回 `202` 并说明原因，
  客户端转为订阅事件流。

同步是优化，异步是基底——执行逻辑不实现两遍。

**同步模式遇到澄清请求时**：不能阻塞等人。返回 `200` 且 `status: awaiting_clarification`
与问题本身，客户端答复后任务以异步完成。同步模式在人机交互边界上退化为异步，
这是诚实的行为。

## 状态可见性

每次状态转移都调用 `emit()`：（a）通过 `StateStorePort` 追加到事件日志，
（b）扇出给活跃的 SSE 订阅者。

因为事件日志是持久的、带单调 `seq` 的，手机端退到后台再回来时可以用 `Last-Event-ID`
补齐缺失事件。**两套参照代码都没有这个能力**，而对一个手机记账应用它是必需的。

## 失败模式

| 失败 | 处置 |
|---|---|
| 工具抛瞬时异常 | 按 `node.retry` 退避重试，发 `subtask.retrying` |
| 工具抛永久异常 | 按 `node.on_failure` 处置：`fail_task` / `skip` / `continue_with_default` |
| 节点输出不满足 `output_schema_ref` | 按节点失败处理；可先按 `escalation_rule` 升档重试 |
| Agent 轮数耗尽 | 按 `on_round_limit`：`fail_task` / `accept_partial` / `escalate_and_retry` |
| 供应商熔断 | 若 `allowed_tiers` 允许则降档；否则 `upstream_unavailable` |
| 计划非法 | 有界重规划，受 `max_replans` 约束 |
| 成本超上限 | 当前 `advisory` 模式：告警并继续。`hard` 模式：取消剩余节点，置 `budget_exceeded`，保留并上报部分产物 |
| 客户端断连 | 异步任务**继续执行**（只是订阅者离开）。同步模式可取消 |
| 死锁（join 永不可满足） | 墙钟超时；DAG 校验本应拦住 |
| 执行中需要澄清 | handler 返回 `NeedsConfirmation`；任务转 `awaiting_clarification`，发 `clarification.needed`，由 `POST /tasks/{id}/clarify` 恢复 |

---

# 阶段 04 — Self-Evolve

完整的日志 schema、检测器、建议格式、审批与金丝雀流程见
[06-self-evolve.md](06-self-evolve.md)。这里只讲它在流水线里的位置。

04 不在关键路径上。它的输入是 03 产出并落库的 `RunLog`，输出是**待审批的建议**。
它**从不自动应用任何改动**。

两段式（沿用 `quality_service.py` 的确定性 + LLM 双层写法）：

1. **确定性检测器** —— 便宜、常跑、无 LLM。阈值全在 `config/evolution/detectors.yaml`，
   代码里零字面量。
2. **LLM 在证据包上归因** —— 它不计算指标（指标已经算好了），只在证据之上提出
   假设与建议。

每条建议过确定性校验后才入库：目标路径存在、不在 `locked_paths`、类型一致、
幅度 ≤ `evolution.max_delta_ratio`、样本量达标、影响面可解析。不过关的静默丢弃并计数——
丢弃率升高本身指向提示词问题。

---

# 一条请求走完全程

以"上传餐饮支付截图 + 文字 '中午吃饭花了 38'"为例：

| 步骤 | 产物 | 关键点 |
|---|---|---|
| 客户端上传 | `media_id` + `sha256` | 哈希用于跨设备去重，避免同一张截图重复入账 |
| `POST /tasks` | `202` + `task_id` | 幂等键命中含写操作的路由，故必填 |
| 01 评估 | `TaskProfile` | `task_type=bookkeeping.capture_from_receipt`，`vision.required=true`，`complexity=0.42/medium`，`confidence=0.86`，成本区间 `[0.004, 0.021]` |
| 02 路由 | `RouteDecision` | LLM 选 `vision_extract_then_write`，但它提的成本上限高于请求约束 → `PolicyGuard` 截断到更严者，记入 `guard.applied: [budget_clamped]`。模式 `async` |
| 03 拆解 | `ExecutionPlan` | 命中 `flow_template:receipt_to_entry`。`extract` 与 `normalize` 是 agent 节点，`dedupe` 与 `write` 是 tool 节点。并发度 3 |
| 03 执行 | 事件序列 | `task.created → profile.ready → route.decided → plan.ready → subtask.started(extract) → agent.round ×2 → subtask.completed(extract) → …` |
| 确认 | `clarification.needed` | 若 `extract.confidence < 0.75`，在 `write` 前插入人工确认。用户改分类 → 记为 `human_signal.edits` |
| 03 完成 | `task.completed` | 产物：`LedgerEntry` |
| 04 | `RunLog` | 含 profile / decision / plan / nodes / outcome / human_signal / redaction |
| 04 分析 | `Suggestion` | 例：`human_edit_rate` 偏高且集中在 `category` 字段 → 提 `prompt_patch` 改 `receipt_extractor` 的分类指引 |
| 使用者审批 | `PolicyPatch` → 新 `PolicyVersion` | 金丝雀一天；护栏指标劣化超 `rollback_margin` 则自动回滚 |

完整可校验的实例数据见 `fixtures/`——那些文件描述的就是这一条链路，
且它们互相之间是一致的（同一个 `task_id`、同一组成本、同一组时间戳）。

## 相关文档

- 接口与事件 → [03-http-contract.md](03-http-contract.md)
- 状态机与持久化 → [04-state-model.md](04-state-model.md)
- 多 Agent → [11-multi-agent.md](11-multi-agent.md)
- 自进化 → [06-self-evolve.md](06-self-evolve.md)
