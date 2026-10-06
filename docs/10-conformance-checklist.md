# 10 一致性清单

任何实现都必须通过这份清单才能宣称"符合契约"。分四类：**契约自洽**（本仓库现在就该通过）、
**跨文件一致**、**架构不变式**（实现要过）、**行为验收**（实现要过）。

---

## 一、契约自洽（本仓库现在就该通过）

### 1.1 YAML 语法

```bash
python3 -c "import yaml,glob
fs = glob.glob('config/**/*.yaml', recursive=True) + ['openapi.yaml']
[yaml.safe_load(open(f)) for f in fs]
print(f'{len(fs)} yaml files ok')"
```

### 1.2 Schema 自身合法 + fixture 双向校验

已实现为 `tests/test_contract_fixtures.py`。它用受支持的 `referencing` 库注册
跨文件 `$ref`（**不要**用已废弃的 `RefResolver`——新版 jsonschema 里它的片段解析
行为已变，会报 `Unresolvable JSON pointer: '$defs/Money'`）。

```bash
.venv/bin/python -m pytest tests/test_contract_fixtures.py -q
```

**通过标准**：10 个 schema 全部合法；20 个 fixture 全部符合预期，**且每个反例都因
预期的那一条而失败**——不是被别的字段带崩。

各反例**应当**失败的原因（这条对应关系本身就是契约的一部分）：

| 反例 | 预期失败原因 |
|---|---|
| `task_envelope.invalid.json` | `input` 既无 `text` 也无 `media` → `anyOf` 不满足 |
| `task_profile.invalid.json` | `needs_clarification: true` 但 `clarification: null` |
| `route_decision.invalid.json` | `path: direct_llm` 却给了 `handler` 与 `tool_set` |
| `execution_plan.invalid.json` | `on_failure: "retry_forever"` 不在枚举内 |
| `task_snapshot.invalid.json` | `status` 不在枚举内 / `progress: 1.4` 越界 |
| `event.invalid.json` | `subtask.started` 的 `data` 缺 `attempt`（且 `seq: 0`） |
| `suggestion.invalid.json` | `kind: "code_refactor"` 不在封闭词表内 |
| `policy_patch.invalid.json` | `artifact` 指向代码文件；`op: "delete"` 不允许 |
| `problem.invalid.json` | `code` 不在封闭词表内、缺 `retryable`——**正是字符串哨兵反例** |
| `run_log.invalid.json` | 缺 `policy_version` 与 `redaction` |

### 1.3 OpenAPI

```bash
npx @redocly/cli lint openapi.yaml
```

本机未安装时跳过，但 `openapi.yaml` 里每个 `$ref` 都指向 `schemas/` 下的真实文件，
应人工核对一遍。

---

## 二、跨文件一致

这些用脚本能做，本仓库先人工过。**注意这里有一个刻意的"不检查项"。**

### 2.1 不应检查的：路由 id 与档位名不得出现在 schema 枚举里

这是一条**反向**要求，容易搞反：

| 词表 | 放哪 | 理由 |
|---|---|---|
| `RouteDecision.path`、`Node.executor`、任务状态、事件类型、`Problem.code`、`Suggestion.kind` | **schema 枚举**（封闭） | 它们是实现结构的一部分，改它们就是改契约 |
| 路由 id、**模型档位名**、任务类型、Agent 角色 id、能力名、工具名 | **不封闭**，由 `PolicyGuard` 按配置校验 | 它们是数据。写死在 schema 里会让"加一档模型"变成契约变更 |

所以：**如果发现 `route_decision.json` 里有 `model_tier: {enum: [cheap, standard, strong]}`，
那是错的**，要改成自由字符串 + 说明由守卫校验成员资格。

### 2.2 应检查的

| 检查 | 位置 |
|---|---|
| `flow_templates/*.yaml` 每个节点的 `executor` 与字段搭配：`tool` ↔ `tool` 字段、`agent` ↔ `role` 字段；且两者不同时出现 | `config/flow_templates/receipt_to_entry.yaml` |
| 模板引用的每个工具都在 [07-handler-seam.md](07-handler-seam.md) 的工具表里 | 同上 |
| `agents.yaml` 里每个 `system_prompt_ref` 解析到真实文件 | 7 个角色 |
| 每个角色的 `max_rounds` ≤ `limits.max_agent_rounds`；`verification.reviewers` ≤ `limits.max_reviewers` | `agents.yaml` vs `routing.policy.yaml` |
| `detectors.yaml` 每个 `source_field` 存在于 `schemas/run_log.json` | 双向：反向也查 `run_log.json` 里没有"采集了却无检测器使用"的死字段 |
| `suggestion_kinds.yaml` 的 `forbidden_target_artifacts` 覆盖 `pricing.yaml`、`limits.*`、`fallback`、`evolution.*`、`agents.*.hard_bounds` | — |
| `routing.policy.yaml` 的 `locked_paths` 与上一条一致 | — |
| `pricing.yaml` 的 `models.*` 与 `routing.policy.yaml` 的 `model_tiers.*.price_per_1k` 一致 | 不一致时以 `pricing.yaml` 为准并在启动告警 |
| `taxonomy.yaml` 每个类型的 `domain` 有对应的 `handler_id`（在 `07` 的工具表里） | — |

### 2.3 概念验证

拿一个具体请求手工走一遍 [02-stages.md](02-stages.md) 的末尾表格：
"上传餐饮支付截图 + 文字 '中午吃饭花了 38'"，
逐步填出 `TaskProfile` → `RouteDecision`（含 `guard.applied`）→ `ExecutionPlan` →
事件序列 → `RunLog`。

**通过标准：中途不需要"再加一条 if 判断"。** 若需要，说明设计漏了东西——
回炉改文档，而不是在那里记一个例外。

对应的可校验实例已经在 `fixtures/` 里：那些文件描述的就是这一条链路，
同一个 `task_id`、同一组成本、同一组时间戳，互相之间一致。

---

## 二·补、实现侧的自动化检查

M2/M3 的验收项已经写成可执行的测试，不必人工过：

| 断言 | 在哪 |
|---|---|
| 契约 fixture 双向校验、镜像与 schema 不分叉 | `tests/test_contract_fixtures.py` |
| 守卫的集合代数、输出不随领域语义变化 | `tests/test_guard.py` |
| 跨配置一致性（角色提示词、检测器字段、模板工具、禁区覆盖） | `tests/test_config_consistency.py` |
| 架构不变式的 AST 检查（R2/R3/无字面量阈值/无模型名/无领域谓词） | `tests/test_invariants.py` |
| 事件 seq 无洞、断线重放、跨重启持久化、两实现 parity | `tests/test_sqlite_state.py` |
| 并发语义：动态就绪集、并发取自计划、取消传播、重试、升级、预算、澄清暂停 | `tests/test_runner.py` |
| 端到端：模板命中、确定性路径零模型调用、成本拆解、幂等、计划校验硬界 | `tests/test_integration.py` |
| 自进化：检测器指标、建议校验的九类拒绝、策略路径、补丁原子性、金丝雀转正/回滚、反馈补挂 | `tests/test_evolution.py` |

一条命令跑全部：

```bash
.venv/bin/python -m pytest -q
```

## 三、架构不变式（实现要过）

### 3.1 R1 — 只有两处允许出现关于路由的 `if`

`HandlerRegistry` 与 `PolicyGuard`。且每个 `if` 都是集合代数
（`in` / `issubset` / `min` / `clamp`），**不是领域谓词**。

```python
# 通过
if decision.route_id not in policy.routes.ids: ...
# 不通过
if profile.task_type == "bookkeeping.capture_from_receipt": ...
```

检查方式：在 `dispatcher/core/**` 与 `dispatcher/stages/**` 里 grep
`task_type\s*==`、`route_id\s*==`、`==\s*"bookkeeping`、`==\s*"calendar`，
应当零命中。

### 3.2 R2 — 接口层之外不得导入传输类型

编排层里不出现 `fastapi`、`Request`、`AsyncSession`。

```bash
grep -rnE "(from fastapi|import fastapi|AsyncSession|starlette)" dispatcher/core dispatcher/stages
```

应零命中。对照：`ai-workmate` 的 `GraphState` 节点函数带 `db: AsyncSession`
与 `user_id: UUID`，这类耦合不能复制。

### 3.3 R3 — 端口层之外不得导入具体存储或供应商

```bash
grep -rnE "(sqlite3|asyncpg|psycopg|boto3|openai|httpx)" dispatcher/core dispatcher/stages
```

应零命中——具体实现都在适配器里。

### 3.4 `no-literal-policy`

CI 检查：`dispatcher/core/**` 与 `dispatcher/stages/**` 中不得出现

- 数值字面量（白名单：`0`、`1`、`-1` 等结构性常量）
- 模型名字符串（形如 `*-v\d`，或含 `deepseek` / `kimi` / `qwen` / `gpt` 等供应商词）
- 形如 `if x > <数字>` 的阈值比较

### 3.5 `no-orphan-config`

两个方向：代码引用但默认配置里不存在的键（拼错或漏默认值）、
默认配置里存在但从不被引用的键（**死配置**）。

第二个方向让"无死代码"这条要求也有了牙齿。

### 3.6 `config-key-typed`

配置键的类型与代码读取时的期望一致。防止把字符串读成数字静默变成 NaN。

### 3.7 handler 侧

- `handlers/**` 里 grep 模型名 → 零命中（结构上做不到，若命中说明绕过了 `LLMPath`）
- `ctx.llm()` 的调用只传 `requires`，不传模型名
- 没有 handler 自行创建并发原语（`Semaphore` / `ThreadPool` / 裸 `create_task`）

---

## 四、行为验收（实现要过）

### 4.1 契约机制（M1）

- [ ] LLM 从菜单里选，**从不发明**路由 id、档位名、工具名、角色 id
- [ ] `PolicyGuard` 拦住所有越界选择，且全程未使用领域谓词
- [ ] 改 `routing.policy.yaml` 的 `when:` 散文（**不碰代码**）能真实改变路由行为
      ——这是整套设计的核心承诺，必须实证
- [ ] `guard.applied` 由代码写入，LLM 无法伪造

### 4.2 状态与流式（M2）

- [ ] 断开 SSE 后重连，带 `Last-Event-ID` 能补齐全部缺失事件
- [ ] 事件 `seq` 任务内严格单调
- [ ] 异步任务的客户端断连**不中断执行**
- [ ] 同步请求投影超时时被提升为异步，并给出 `mode_change_reason`
- [ ] 错误全部是有类型的 `Problem`；上游异常**不**被吞成字符串拼进正文
- [ ] `advisory` 模式下成本超上限**不会**中断任务，只告警

### 4.3 拆解与执行（M3）

- [ ] 无依赖的节点真正并行，并发度等于 `plan.max_parallelism`
- [ ] 取消传播到在执行中的节点
- [ ] 每个失败产出有类型的 `NodeFailure`，不拼接字符串
- [ ] `join` 与入边不一致的计划被**校验阶段**拒绝（而非执行阶段挂住）
- [ ] 幻觉出的工具名 / 角色 id 被集合成员判定拒绝
- [ ] 重规划次数不超过 `decomposer.max_replans`

### 4.4 多 Agent（M3）

- [ ] `agent` 节点的轮数不超过 `min(role.max_rounds, limits.max_agent_rounds)`
- [ ] 单计划 agent 节点数 ≤ `limits.max_agent_nodes_per_plan`
- [ ] 整任务总轮数 ≤ `limits.max_total_rounds_per_task`
- [ ] agent 只能调用其 `tool_whitelist` 内的工具，且该集合 ⊆ 决策 `tool_set`
- [ ] 每轮都发 `agent.round`，含 `round` / `max_rounds` / `tool_calls` / `stop_reason`
- [ ] 轮数耗尽时按 `on_round_limit` 正确处置（三种取值各测一次）
- [ ] `independent_review` 的复核者**独立取数**，不复用被复核者的中间推理
- [ ] 仲裁者能输出「无法裁决」；`on_disagreement: ask_user` 时任务转为
      `awaiting_clarification`
- [ ] 一致度低时发 `agent.review` 且 `outcome: undecidable`，不强行选一个

### 4.5 接缝（M4）

- [ ] **接入 `bookkeeping` handler，改动 `dispatcher/` 的文件数为 0**
- [ ] 一张真实截图端到端：抽取 → 用户确认/修改 → 入账
- [ ] `extract.confidence < requires_confirmation_below_confidence` 时任务
      **停在确认页**而非猜一个答案入账
- [ ] 用户的修改被记为 `human_signal.edits`（含 `field` / `from` / `to`）
- [ ] 同一张截图（同 `sha256` + 同 `Idempotency-Key`）重复提交不重复入账
- [ ] `build_ledger_entry` 重试时收到**同一个** `idempotency_token`
- [ ] 默认不保留原始截图（`redaction.media_retained: false`）
- [ ] 声明 `requires_capabilities: []` 的工具**不会**被配模型

### 4.6 自进化（M5）

- [ ] 建议永不自动应用；`locked_paths` 命中的建议在**入库前**被丢弃，不呈现给使用者
- [ ] 数值变化幅度超过 `max_delta_ratio` 被拒
- [ ] 样本量不足 `min_sample_size` 被拒
- [ ] 批准后产生**新的** `PolicyVersion`，父版本可追溯；**永不原地改配置**
- [ ] 金丝雀期间护栏指标劣化超 `rollback_margin` 时**自动回滚**
- [ ] 回滚即时生效
- [ ] `prompts/evolution_analysis.md` 不可被 04 修改
- [ ] 分析输入经 `data_block` 包裹；run log 里的指令性文字不改变分析行为

### 4.7 契约演化

- [ ] `TaskSnapshot` 的变更**只做加法**（新增可选字段），不删不改已有字段
- [ ] 客户端通过 `client.min_supported_client_version` 声明下限并被尊重
- [ ] 每条 `RunLog` 都带 `policy_version`

---

## 一份最小验收命令

```bash
cd smart-dispatcher

# 1. YAML
python3 -c "import yaml,glob;fs=glob.glob('config/**/*.yaml',recursive=True)+['openapi.yaml'];[yaml.safe_load(open(f)) for f in fs];print(len(fs),'yaml ok')"

# 2. schema + fixtures（脚本见 1.2）
# 3. openapi
npx @redocly/cli lint openapi.yaml

# 4. provider 名称泄漏（对手册里的"禁止硬编码模型"做一次反向检查）
grep -rniE "deepseek|kimi|qwen|gpt-|claude-|gemini" --include=*.json --include=*.yaml . \
  | grep -vE "routing.policy.yaml|pricing.yaml|problem.json|task_profile.json|01-architecture|02-stages|06-self-evolve|08-config-model|09-roadmap|10-conformance"
```

第 4 条：模型与供应商名**只应**出现在三类位置：

1. **档位绑定** —— `routing.policy.yaml` 的 `model_tiers.*.provider`（当前 3 处）。
   这是唯一允许把供应商写进配置的地方，且模型本身仍由 `secret://` 引用间接指定。
2. **反例引用** —— 文档与 fixture 里为了说明"不要这么写"而引用的具体名称，
   例如 `fixtures/problem.invalid.json` 与 `schemas/problem.json` 描述里的
   `"[DeepSeek错误] HTTP 500"` 字符串哨兵反例。
3. **说明性文字** —— 文档里解释"handler 拿不到模型名字符串"时举的例子。

`config/pricing.yaml` **不应**出现任何供应商名（它只用档位键）。
`handlers/**` 与 `dispatcher/**` 里出现即违规。

期望的分布：`routing.policy.yaml` 3 处（provider 绑定）、`pricing.yaml` 0 处，
其余集中在文档与反例 fixture 中。

---

## 相关文档

- 分层与不变式 → [01-architecture.md](01-architecture.md)
- 四阶段 → [02-stages.md](02-stages.md)
- 多 Agent → [11-multi-agent.md](11-multi-agent.md)
- 实施顺序 → [09-roadmap.md](09-roadmap.md)
