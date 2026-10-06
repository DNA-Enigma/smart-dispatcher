# 07 Handler 接缝

怎么给调度层接一个新的领域（记账、日程、学习……），以及这条接缝的验收标准。

---

## 界线

> **调度层拥有决策与编排；handler 拥有领域事实与领域动作。**

调度层永远不知道"票据""科目""账本"是什么。handler 永远不命名模型、
不读全局配置、不自定并发、不假设自己会被恰好调用一次。

### 验收标准

> **接入一个新的领域 handler，需要改动 `dispatcher/` 下的文件数为 0。**

这是本文最要紧的一句话。如果接入记账时发现"得给调度层加一个判断"，
那说明调度层里混进了领域知识——回炉改设计，而不是就地打补丁。

---

## 声明式清单

handler 是一个模块，暴露一份声明 + 一个执行函数。

```
CapabilityHandler {
  # ── 声明（静态；注册时校验，fail-fast）──
  handler_id: str                    # "bookkeeping"
  version: str
  capabilities: [str]                # "bookkeeping.expense.record", ...
  tools: [ToolSpec]
  flows: [FlowTemplate]              # 见 config/flow_templates/
  config_schema: JSONSchema          # 该用户的配置形状
  required_ports: [str]              # ["media", "llm", "store"]

  # ── 生命周期（全部可选）──
  on_register(registry)              # fail-fast 校验
  on_task_start(ctx)
  on_task_end(ctx, outcome)
  health() -> HealthStatus

  # ── 唯一可执行面 ──
  execute_tool(tool_name: str, args: dict, ctx: DispatchContext) -> ToolResult
}
```

```
ToolSpec {
  name: str
  description: str
  input_schema: JSONSchema
  output_schema_ref: str
  side_effects: "none" | "read" | "write"
  requires_capabilities: [str]       # "vision.extract"、"llm.strong" —— 永不出现模型名
  idempotent: bool                   # true 时调度层注入稳定的 idempotency_token
  cost_hint: { llm_calls: int, est_tokens: str }
}
```

### 注册时的 fail-fast 校验

沿用 `ai-workmate/server/app/agent/base.py::validate_tools()` 的做法——
**启动期报错，而不是运行期惊喜**：

- `handler_id` 重复
- 工具的 `output_schema_ref` 不可解析
- 工具声明的 `requires_capabilities` 没有任何已配置档位能满足
- `flows` 引用了未声明的工具
- `config_schema` 无法编译

---

## 调度层给 handler 的保证

这八条是 handler 可以依赖的契约。逐条说明为什么重要。

### 1. 归一化的多模态输入

`ctx.media.resolve(media_ref)` 给出字节或流，MIME 与大小已校验，
生命周期由任务范围界定。保留了多久、什么时候删，由调度层按用户配置处理，
**不是 handler 的责任**。

### 2. 按能力解析的模型访问

```
ctx.llm(messages, requires=["vision.extract"])
```

**handler 连模型名字符串都拿不到**——`llm()` 的签名里没有那个参数。
它说"我需要视觉能力"，调度层把它映射到档位，再把档位映射到实际模型。

这是"禁止硬编码模型"在结构上的强制，而不是靠约定。密钥与限流也在调度层。

### 3. 异步执行与实时状态

handler 可以慢。状态机、SSE、断线重放都是调度层的事。
handler 只需要在它能报进度的时候调用 `ctx.emit()`。

### 4. 按用户隔离且已校验的配置

`ctx.config` 是该用户在这个 handler 上的配置切片，
在 handler 运行**之前**已按其 `config_schema` 校验过。
handler 不需要写防御式校验代码。

### 5. 成本归属

每次 `ctx.llm()` 的费用、每个工具声明的 `cost_hint`，
都按 `(tenant, user, task, subtask)` 归属到 `BudgetLedger`。
handler 不需要自己算钱。

### 6. 写操作的幂等

`ctx.idempotency_token` 按 `(task_id, subtask_id)` 派生，**稳定**。
重试时拿到同一个 token，handler 以它作为幂等依据即可，
不必自己实现"重试不重复写入"。

### 7. 重试、取消、超时

handler 只实现"一次 `execute_tool` 调用该做的工作"。
退避重试、取消传播、超时、档位升级都在外层。

### 8. 提示注入围栏

handler 返回的数据在进入 LLM 提示词时经 `data_block()` 包裹；
系统提示词槽位由 `guard_system()` 独占。见 [05-media.md](05-media.md)。

---

## handler 不得做的事

| 禁止 | 为什么 |
|---|---|
| 读全局配置 | 配置按用户隔离，全局读会串号 |
| 命名模型 | 结构上做不到；若发现能绕过，是契约被破坏了 |
| 自行开无界并发 | 并发度是计划级的资源约束，由 `DagRunner` 统一管 |
| 修改调度层状态 | 状态机是调度层的，handler 只能 `emit` |
| 依赖"恰好被调用一次" | 会重试、会被取消、会升档重跑 |
| 假设存在 Postgres | 消费端包含一部手机 |
| 假设媒体会长期存在 | 默认任务结束即删原图 |

---

## 记账 handler 的具体形状

以下是设计接缝时的验证用例，**不是本轮要实现的代码**。
它存在的意义是证明接缝够宽——如果接账时需要改调度层，这里就会露馅。

### 能力

```
bookkeeping.expense.record
bookkeeping.income.record
bookkeeping.ledger.query
bookkeeping.report.monthly
bookkeeping.investment.position
bookkeeping.plan.advice
```

### 工具

| 工具 | 副作用 | 所需能力 | 幂等 |
|---|---|---|---|
| `extract_receipt_fields` | none | `vision.extract` | 是 |
| `crop_and_zoom` | none | `vision.extract` | 是 |
| `read_media_region` | none | `vision.extract` | 是 |
| `normalize_merchant` | read | `text` | 是 |
| `lookup_merchant` | read | — | 是 |
| `list_categories` | read | — | 是 |
| `dedupe_check` | read | — | 是 |
| `build_ledger_entry` | write | — | 是 |
| `query_ledger` | read | — | 是 |
| `compare_entries` | read | — | 是 |
| `compute_portfolio` | none | **（空）** | 是 |
| `generate_financial_plan` | read | `reasoning.strong` | 否 |

两个细节值得注意：

- **`compute_portfolio` 声明 `requires_capabilities: []`** —— 它是纯算术，
  必须**不**经过 LLM。"该确定性就确定性"的纪律在 handler 层同样适用，
  而且声明为空是有约束力的：调度层不会给它配模型。
- **整张表里没有一处模型名。** 有的是能力名。

### 流程模板

`receipt_to_entry`（见 `config/flow_templates/receipt_to_entry.yaml`）。
节点混用两种执行者：`extract` 与 `normalize` 是 agent（需要多轮与判断），
`dedupe` 与 `write` 是 tool（输入确定则输出唯一）。

### 配置 schema

```
config_schema:
  default_currency: str
  categories: [str]                    # 用户自己的分类体系
  confirmation:
    auto_confirm_above_confidence: number
    require_confirm_on_amount_above: number
  privacy:
    retain_receipt_images_days: integer
  investment:
    tracking_enabled: boolean
    benchmark: str
```

注意 `confirmation.*` 在 handler 配置里，但**路由级的确认阈值在策略里**
（`routes[vision_extract_then_write].requires_confirmation_below_confidence`）。
这个分工是有意的：前者是用户的个人偏好，后者是全局行为策略，且后者可被 04
用数据提出修改建议。

---

## 走一遍，证明接缝成立

以"上传餐饮支付截图 + 文字"为例，逐项检查是否真的不需要改调度层：

| 需求 | 落在哪里 | 需要改 `dispatcher/` 吗 |
|---|---|---|
| 从图里读金额 | `extract_receipt_fields` 工具 + `receipt_extractor` 角色 | 否 |
| 图糊了要放大再看 | 角色声明的 `crop_and_zoom` 在 `tool_whitelist` 里，agent 循环自然支持 | 否 |
| 商户归类 | `merchant_classifier` 角色 + `query_ledger` 查历史习惯 | 否 |
| 查重 | `dedupe_check` 工具，`optional: true` + `continue_with_default` | 否 |
| 入账 | `build_ledger_entry` 工具，`idempotent: true` | 否 |
| 抽取不确定时问用户 | 路由的 `requires_confirmation_below_confidence` + 模板的 `confirmation` 段 | 否 |
| 流程形状固定 | `flow_templates/receipt_to_entry.yaml` | 否 |
| 金额单位是元还是分 | handler 的 `config_schema` | 否 |
| 保留原图几天 | `ctx.config.privacy` + `MediaStorePort` | 否 |
| 分类体系是用户自定义的 | `ctx.config.categories` | 否 |

**十项需求，零处改动调度层。** 接缝成立。

### 第二个与第三个验证

计划里排在 M4/M6，列出是为了说明这条接缝要被反复验证：

- **日程 handler** —— 大部分是确定性的（"三点加个提醒"不需要多轮判断），
  它会验证"不需要 LLM 的路径"是否同样顺畅，以及 `executor: tool` 是否好用。
- **现有学习平台** —— 用它替换 `duowei-ai/backend/app/agents/router.py::_match_group()`
  的关键词匹配路由。这是最强的验证：如果一套已经跑起来的真实产品能被接缝吸收
  而不需要改调度层核心，那这条界线就是对的。

---

## 给你自己的应用接入（M4 完成后的实际做法）

接缝现在**是被机械验证过的**，不是一句承诺。`tests/test_handler_seam.py` 会：

1. 算出 `dispatcher/` 下所有文件的哈希；
2. 在 `dispatcher/` **之外**造一个全新的领域（新目录：声明 + 实现）；
3. 经配置装进来，**真的跑一个任务**让它被调用；
4. 再算一次哈希，断言一模一样。

它还会在有人把领域名写进调度层可执行代码时报错（这条我故意破坏过，确认它会红）。

### 目录形状

```
handlers/                          ← 注意：在 dispatcher/ 外面
└── <你的领域>/
    ├── handler.yaml               声明：能力、工具、流程、config_schema
    ├── handler.py                 实现：execute_tool
    └── ports.py                   领域端口（你的数据库接口），只属于这个领域
```

### 三步接入

**第一步：写声明与实现。** `handler.yaml` 里列能力、工具、`input_schema`；
`handler.py` 里实现 `execute_tool(tool_name, args, ctx)`。契约见本文开头。

`input_schema` **一定要写**。参数抽取环节靠它决定产出的形状；不写的后果不是报错，
而是"写进去一条空记录却报成功"——这是真跑出来的教训。

**第二步：注册。** 编辑 `config/handlers.yaml` 加一条：

```yaml
handlers:
  - manifest: handlers/你的领域/handler.yaml
    impl: handlers.你的领域.handler:你的类
```

`impl` 留空 = 只有声明没有实现。这不是半成品，而是一种有用的状态：
评估器能看见它的能力（于是能正确判断请求该由谁承接），执行会明确报
`handler_not_implemented`。**缺实现时报错，比假装能做更好。**

**第三步（只在需要接自己数据库时）：注入。** 如果你的领域要用自己的数据库，
在代码里构造好实例再传进去——调度层不参与那件事，它不知道你连的是什么库：

```python
from dispatcher.pipeline import Dispatcher, DispatcherConfig

class MyLedger:                      # 实现你领域自己的端口
    async def append(self, entry, *, idempotency_token): ...
    async def find_by_token(self, token): ...
    async def recent(self, *, limit): ...

d = Dispatcher.build(DispatcherConfig(handlers=[MyHandler(manifest, ledger=MyLedger())]))
```

### 领域端口住在你的领域里

账本存储这类接口属于领域，**不属于 `dispatcher/ports/`**。把它放进调度层，
等于让调度层知道了"账本"这个概念，接缝就漏了。参考实现的端口在
`handlers/bookkeeping/ports.py`，可以直接照抄形状。

### 你的应用能拿到什么

调度层保证的东西（详见本文开头的八条）里，对你最要紧的三条：

- **`ctx.llm(messages, requires=["vision.extract"])`** —— 你说能力，不说模型。
  你的代码里不可能出现模型标识字符串，因为签名里没有那个参数。
- **`ctx.idempotency_token`** —— 按 `(task_id, subtask_id)` 派生且稳定。
  把它存进你的数据库并加唯一约束，"重试不会重复入账"就是免费的。
- **`ctx.config`** —— 该用户的配置，已按你的 `config_schema` 校验过。
  用户的分类词表、币种、确认阈值都放这里，不要写死在代码里。

## 相关文档

- 上下文里的每个字段 → [02-stages.md](02-stages.md) 的"handler 看到的世界"
- Agent 角色 → [11-multi-agent.md](11-multi-agent.md)
- 流程模板 → `config/flow_templates/`
- 能力自省端点 → `openapi.yaml` 的 `/capabilities`
