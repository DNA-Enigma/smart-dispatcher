# 01 架构与分层

## 这份文档要回答的问题

调度层与应用之间、调度层与领域逻辑之间的界线画在哪里，以及为什么画在那里。
界线画错的典型症状是：加一个新领域需要改调度层的代码。

---

## 一句话界线

> **调度层拥有决策与编排；handler 拥有领域事实与领域动作。**

调度层永远不知道"票据"是什么、"科目"是什么、"账本"是什么。它只知道：有一个请求，
它有某些能力和工具可选，它需要被评估、被路由、被拆解、被执行、被记录。

handler 永远不命名模型、不读全局配置、不自定并发、不假设自己会被恰好调用一次。

这条界线是否成立，有一个可执行的验收标准：

> **接入一个新的领域 handler，需要改动 `dispatcher/` 下的文件数为 0。**

## 分层

```
┌────────────────────────────────────────────────────────────────┐
│ 消费端：记账 APP（手机） │ 日程/待办 │ 学习平台 │ …
│ 各自拥有 UI、认证、领域数据库、产品逻辑                          │
└───────────────┬────────────────────────────────────────────────┘
                │  HTTP/JSON + SSE   ← 唯一的耦合面（见 03）
┌───────────────▼────────────────────────────────────────────────┐
│ 调度层                                                          │
│                                                                 │
│  ┌── 接口层 ───────────────────────────────────────────────┐   │
│  │ TaskAPI   EventStream(SSE)   AdminAPI(policy/evolution)  │   │
│  │ RequestValidator   ProblemJSON   IdempotencyStore        │   │
│  └───────────────────────┬─────────────────────────────────┘   │
│  ┌── 编排层 ─────────────▼─────────────────────────────────┐   │
│  │ Pipeline: Evaluator → Router → Decomposer → DagRunner    │   │
│  │           → RunLogCollector → SelfEvolve                  │   │
│  │ PolicyStore · PolicyGuard · BudgetLedger · Escalator      │   │
│  └───────────────────────┬─────────────────────────────────┘   │
│  ┌── 端口（接口；适配器在层外） ────▼──────────────────────┐   │
│  │ LLMPort · StateStorePort · MediaStorePort · ConfigProvider│   │
│  │ Clock · EventSink · HandlerRegistry · TelemetrySink       │   │
│  └───────────────────────┬─────────────────────────────────┘   │
└──────────────────────────┼─────────────────────────────────────┘
                           │ v1：进程内注册表
                           │ v2：HandlerPort over HTTP
┌──────────────────────────▼─────────────────────────────────────┐
│ 能力 handler：bookkeeping │ calendar │ learning │ …
│ 声明 tools / flows / config_schema；实现 execute_tool()
└────────────────────────────────────────────────────────────────┘
```

## 核心抽象

| 抽象 | 所在层 | 职责 |
|---|---|---|
| `TaskEnvelope` | 接口 | 规范化的入站请求：输入（文本 + 媒体引用）、声明、约束、身份、幂等键 |
| `TaskProfile` | 01 输出 | 请求的结构化画像 |
| `RouteDecision` | 02 输出 | 选定的路由 / 档位 / handler / 工具集 / 模式 / 预算 |
| `ExecutionPlan` | 03 输出 | 子任务节点与边的 DAG |
| `CapabilityHandler` | 插件 | 领域边界。声明 `capabilities` / `tools` / `flows` / `config_schema`；实现 `execute_tool()` |
| `HandlerRegistry` | 核心 | 注册、按能力索引查找、冲突裁决、工具白名单校验 |
| `ToolSpec` | 核心 | 工具声明：输入输出 schema、副作用类型、所需能力、是否幂等 |
| `AgentRole` | 核心（数据） | Agent 角色：提示词、可用工具、所需能力、轮数上限 |
| `Policy` | 核心（数据） | 版本化的决策基底：路由、档位、阈值、预算、模板、禁区 |
| `PolicyGuard` | 核心（代码） | **无语义**的校验器：枚举成员、子集、截断、兜底 |
| `LLMPort` | 端口 | `complete()` / `stream()` / `generate_json()`，**按档位名或能力解析，永不按模型名** |
| `StateStorePort` | 端口 | 任务快照读写、事件追加与重放。不假定 Postgres |
| `MediaStorePort` | 端口 | 媒体字节的存取与生命周期 |
| `BudgetLedger` | 核心 | 确定性的成本累加与告警 |
| `RunLog` | 04 输入 | 每次运行的结构化遥测 |
| `Suggestion` / `PolicyPatch` | 04 输出 | 建议改动 + 证据 + 回滚凭据 |
| `PolicyVersion` | 核心 | 策略的不可变快照，父子可追溯，可金丝雀，可回滚 |

## 三条强制规则（可 lint 化）

这三条不是倡议，是构建时必须通过的检查。它们存在的意义是让"不写死规则"这件事
有牙齿——否则它会在第一次赶工期时就被忘掉。

### R1 — 只有两处允许出现关于路由的 `if`

`HandlerRegistry` 与 `PolicyGuard`。而且其中的每一个 `if` 都必须是集合代数
（`in` / `issubset` / `min` / `clamp`），不能是领域谓词。

```python
# 允许 —— 集合成员判定
if decision.route_id not in policy.routes.ids:
    ...

# 禁止 —— 领域谓词
if profile.task_type == "bookkeeping.capture_from_receipt":
    ...
```

### R2 — 接口层之外不得导入任何传输类型

编排层里不出现 `fastapi`、不出现 `Request`、不出现 `AsyncSession`。
核心是"在数据类之上运行的纯异步函数"。

对照：`ai-workmate` 的 `GraphState` 节点函数签名里直接带 `db: AsyncSession` 与
`user_id: UUID`，这类耦合正是不能复制的。

### R3 — 端口层之外不得导入任何具体存储或 LLM 供应商

编排层通过 `StateStorePort` 等接口工作。具体实现（SQLite / Postgres / 内存）在层外。

## 配套的 CI 检查

| 检查 | 拦截什么 |
|---|---|
| `no-literal-policy` | `dispatcher/core/**` 与 `dispatcher/stages/**` 中的数值字面量、模型名字符串、阈值比较 |
| `no-orphan-config` | 代码引用了但默认配置里不存在的键；默认配置里存在但从不被引用的键（两个方向都查，这也让"无死代码"有了牙齿） |
| `config-key-typed` | 配置键的类型与代码读取时的期望一致 |

## 决策表作为「LLM 消费的策略」

这是整套设计里最新、也最需要讲清楚的一点，因此单独成节。详见
[02-stages.md](02-stages.md) 与 [08-config-model.md](08-config-model.md)。

三层分离：

| 层 | 是什么 | 谁改 |
|---|---|---|
| **策略基底** | `config/*.yaml` + `prompts/*.md`。人可读、可改、带版本号 | 人或 04（经人批准） |
| **判断** | LLM 读策略渲染出的菜单，在封闭集合里做选择，给出理由与置信度 | LLM |
| **守卫** | `PolicyGuard` 做集合校验与截断 | 确定性代码 |

三条不变式：

- **I1 — 守卫永不检视语义。** 它只问"X 是否在允许集合里"。所有含义存在于
  `when:` 散文与人的头脑中。没有 `if task_type == ...`。**加一个领域概念不会加一个分支。**
- **I2 — 跨信任边界的每个 LLM 输出都回落到配置派生的枚举再校验一次。**
  这不是新发明，是把 `duowei-ai` 的 `profile_evolution.py::_extract_changes`
  （`ENUM_FIELDS` + `MAX_CHANGES` + 定长文本）已有且经过验证的写法推广到路由层。
- **I3 — `rationale` 与 `policy_version` 一并持久化。** 于是漂移可审计，
  04 也能分析"理由的质量"，而不只是看结果。

安全性质一句话：

> **LLM 永远被配置定义的集合所限，而配置经人批准。**
> 注入评估器最多只能选到另一条**合法**路由，无法发明非法路由。
> 只有"04 + 人批准"能改变合法集合，而那是可回滚的版本化产物。

## 确定性代码与 LLM 的分工

这张表是判断"某个决策该由谁做"的依据。写新功能时先查表。

| 决策 | 机制 | 为什么 |
|---|---|---|
| 读取阈值 / 预算 / 档位映射 | 确定性（读配置） | 这是数据 |
| 意图与任务类型分类 | **LLM** | 开放语义、多模态 |
| 复杂度与紧急度打分 | **LLM**（出 0-1 分 + 理由） | 判断题 |
| 复杂度档 → 默认档位 | 确定性（查表） | 对 LLM 产出的档做纯查表 |
| 选择路由 | **LLM**（从菜单选） | 策略之下的判断 |
| 校验路由 / 档位 / 工具集合法性 | 确定性 `PolicyGuard` | 集合代数 |
| 判断某步骤用 tool 还是 agent | **LLM**（拆解器） | 判断题，受硬界约束 |
| Agent 循环何时停止 | **LLM** 判断"产出是否满足 schema"，**代码**判轮数上限 | 前者是判断，后者是安全 |
| 成本估算 | **混合**：LLM 出量级桶，代码乘价格表 | LLM 不擅算术，擅"这活看着不小" |
| 成本记账与告警 | 确定性 `BudgetLedger` | 记账不能靠提示词 |
| 低置信度升级 | 确定性（读 `escalation_rule`），有硬上限 | 有界重试，不能成环 |
| 拆解子任务 | **LLM** 或选 `flow_template` | 判断题 |
| DAG 拓扑、join 解析、并发度 | 确定性 | 图算法 |
| 失败归因与建议起草 | **LLM** 在确定性聚合指标之上 | 对证据做判断 |
| 建议是否采纳 | **人（使用者本人）** | 见 [06](06-self-evolve.md) |

## 从现有代码里复用与不沿用的东西

已实读以下文件，设计直接沿用其形状。

### 复用

| 来源 | 复用点 |
|---|---|
| `ai-workmate/server/app/agent/toolkit.py` | `@tool` 注册表 + 由签名推导 schema + `dispatch()` → 泛化为 `HandlerRegistry` / `ToolSpec` |
| `ai-workmate/server/app/agent/base.py` | `validate_tools()` 启动期 fail-fast；工具白名单按 Agent 隔离 → 按路由 / 节点 / 角色三级隔离 |
| `ai-workmate/server/app/services/usage_service.py` | 原子 `INSERT ... ON CONFLICT DO UPDATE` 预留 + 汇总 → `BudgetLedger` |
| `ai-workmate/server/app/services/scheduler_lock.py` | `pg_advisory_lock` → 04 分析任务的单实例保证 |
| `ai-workmate/server/app/services/report_time.py` | 按用户时区的日 / 周窗口 → 04 分析窗口与用户侧"今天"语义 |
| `ai-workmate/server/app/services/ai_service.py` | `data_block()` 注入围栏；多模态调用（`guard_system()` **未移植**：它在那里是"拼产品身份 + 防注入前言"的变换，本仓的反注入条款写在各提示词文件自己头上，见 05-media.md） |
| `ai-workmate/app/lib/data/task_repo.dart` | 任务状态与进度词汇 → `TaskSnapshot` 刻意对齐，Flutter 侧 1:1 映射 |
| `duowei-ai/backend/app/core/graph.py` | `asyncio.gather` + `Semaphore` 波次并行 → `DagRunner` |
| `duowei-ai/backend/app/core/llm_client.py` | `QPSLimiter` 令牌桶 + 兼容 OpenAI 的 stream / non-stream + `llm_generate_json()` → `LLMPort` 参考适配 |
| `duowei-ai/backend/app/core/llm_router.py` | 目前是空壳透传 —— 正是接入档位路由的位置 |
| `duowei-ai/backend/app/services/profile_evolution.py` | 04 的近乎逐行模板：LLM 提改动 → 白名单校验 → 变更数上限 → 审计 → 静默失败不阻塞主链路 |
| `duowei-ai/backend/app/services/quality_service.py` | 两层评分（便宜的确定性 + 可选 LLM）→ 04 的"检测器 → 分析"两段式 |
| `duowei-ai/backend/app/routers/learning.py` | `sse_starlette` 事件命名与断连处理 → 扩展出 `Last-Event-ID` 重放 |

### 不沿用（都是需要主动避免的反模式）

| 反模式 | 位置 | 问题 |
|---|---|---|
| 关键词匹配式路由 | `duowei-ai/.../agents/router.py::_match_group()` 的 `for kw in tpl["keywords"]: if kw in haystack` | 文件名叫 router，实为学习路由。加一个概念加一个关键词。这正是"不写死规则"要禁的东西 |
| 拓扑与提示词硬编码 | 同文件的 `GROUP_TEMPLATES` | 把 `"plan": [...]` 与 prompt 字符串写在 Python 里。应当是 `config/flow_templates/*.yaml` |
| 模块级并发常量 | `duowei-ai/.../core/graph.py` 的 `MAX_CONCURRENT_AGENTS = 9` 与写死的 `N1→N2→batch→batch2` | 并发度应当来自计划本身，拓扑应当动态生成 |
| 阈值字面量 | `duowei-ai/.../services/quality_service.py` 的 `if char_count > 2000: return 0.9` | 每个字面量都该是一个配置键 |
| 字符串哨兵错误 | `duowei-ai/.../core/llm_client.py::llm_chat()` 把异常吞成 `"[DeepSeek错误] HTTP 500"` 拼进正文 | 破坏错误契约。错误必须是有类型的、可传播的（见 `schemas/problem.json` 的反例 fixture） |
| 异常拼接 | `duowei-ai/.../core/graph.py` 用 `"; "` 拼接多个异常 | 丢失结构。每个失败必须是有类型的 `NodeFailure` |
| 即发即弃的 SSE | `duowei-ai/.../routers/learning.py` | 只喂活连接，断线即丢失整轮运行。移动端必须有持久事件日志 + 重放 |
| 固定 Agent 拓扑 | `duowei-ai` 的 LangGraph 9-agent 图 | 见 [11-multi-agent.md](11-multi-agent.md)：角色进配置，拓扑动态生成 |

## 相关文档

- 四阶段详设 → [02-stages.md](02-stages.md)
- 多 Agent 协作 → [11-multi-agent.md](11-multi-agent.md)
- 配置分层与 CI 检查 → [08-config-model.md](08-config-model.md)
- handler 接缝 → [07-handler-seam.md](07-handler-seam.md)
