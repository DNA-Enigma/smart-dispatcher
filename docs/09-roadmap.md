# 09 实施路线

分阶段实施顺序，以及每个阶段"做到什么算完成"。

---

## 排序原则

两条：

1. **契约先于实现。** M0 就是本轮，交付的是文档与契约文件。后面每个阶段都是对契约的
   一次兑现，而不是对契约的修改。
2. **风险最高的先做。** 整套设计里最新、最没被验证过的是"LLM 消费策略"这个机制——
   让 LLM 从配置渲染出的菜单里选，而不是走规则树。它成立，后面都是工程；它不成立，
   后面全白做。所以它排在 M1，在状态存储、SSE、执行器之前。

---

## M0 — 契约冻结 ✅ 本轮

**交付**：本仓库的全部文档与契约文件。

**完成标志**：

- `schemas/*.json` 全部是合法 draft 2020-12，且 20 个 fixture 全部符合预期
  （`.example` 通过、`.invalid` 被拒，且反例因预期的那一条而失败）
- 交叉引用一致（见 [10-conformance-checklist.md](10-conformance-checklist.md)）
- 概念验证走得通：拿"上传餐饮支付截图 + 文字"手工填一遍
  `TaskProfile → RouteDecision → ExecutionPlan → 事件序列 → RunLog`，
  中途不需要"再加一条 if 判断"

**不交付**：任何实现代码、SDK、参考实现、记账 handler、实现语言选型。

---

## M1 — 01 评估 + 02 路由 ✅ 已完成

**已交付**（`dispatcher/`，Python + FastAPI）：

- `core/policy.py` 策略加载与自洽性校验（`extra="forbid"`，写错键名立刻报错）
- `core/yamlio.py` 按 YAML 1.2 解析（否则 `on:` 会被当成布尔 `True`，配置静默失效）
- `core/guard.py` `PolicyGuard`：纯集合代数，无语义判断
- `core/registry.py` handler 注册表；`handlers/<领域>/handler.yaml` 声明（经 `config/handlers.yaml` 装载）
- `core/prompts.py` 提示词加载与**策略菜单渲染**（含 `data_block` 注入围栏）
- `core/pricing.py` 量级桶 → 金额的确定性算术
- `ports/` + `adapters/` LLMPort 与 OpenAI 兼容实现（含 `QPSLimiter`）
- `stages/evaluator.py` 01；`stages/router.py` 02
- `interface/app.py` 薄 FastAPI 层；`pipeline.py` 装配
- `tests/` 86 个测试：契约 fixture 双向校验、镜像与 schema 不分叉、
  守卫的集合代数（含"输出不随语义变化"的行为证明）、跨配置一致性、
  架构不变式的 AST 检查
- `scripts/experiment_when_edit.py` 下面那个关键实验

**尚未**（属 M2+）：执行层、事件流与重放、真 handler、澄清接口、自进化。

---

## M1 原定目标（保留备查）

**交付**：

- `HandlerRegistry`（泛化 `ai-workmate/server/app/agent/toolkit.py` 的注册表形状）
- `Policy` 加载与校验、`PolicyGuard`（只有集合代数）
- `LLMPort` 适配（泛化 `duowei-ai/backend/app/core/llm_client.py` 的
  `QPSLimiter` + 兼容 OpenAI 请求；替换 `core/llm_router.py` 那个空壳透传）
- `Evaluator` 与 `Router`，含提示词渲染器 `render_policy_for_llm()`

**可运行标志**：

> 一个文本请求与一个带图的请求，`POST /tasks` 都能返回 `TaskProfile` 与
> `RouteDecision`；`GET /tasks/{id}` 显示 `status: routing` 与决策内容。
> 尚无执行。

**为什么这个阶段最关键**：它是唯一能证明"决策表作为 LLM 消费的策略"成立的实验。
具体要验证三件事：

- LLM 确实能只从菜单里选，不会发明路由 id 或档位名
- `PolicyGuard` 的集合校验能拦住所有越界选择，且**一次都没用到领域谓词**
- 改 `routing.policy.yaml` 的 `when:` 散文（不碰代码）能真实改变路由行为

第三件事是整套设计的核心承诺。**在这一步就用一个改动去验证它**，而不是等到 M4。

**这一阶段的验收测试**：把 `direct_answer` 的 `when` 里"不需要任何用户私有数据"
这句话删掉，然后跑一批"我上个月花了多少"的请求，看是否如预期被吸进 `direct_answer`
（也就是复现 `fixtures/suggestion.example.json` 里那条建议所描述的现象）。
能复现，说明机制真实有效；复现不了，说明配置与行为之间没有真实耦合，回炉。

---

## M1 验收结论（2026-10-06，首次真实模型验证）

**机制成立。** `scripts/experiment_when_edit.py` 的结果：

| 轮次 | 需要私有数据的请求落到 `direct_answer` | 该直答的请求落到 `direct_answer` |
|---|---|---|
| 当前策略 | 0 / 8 | 5 / 5 |
| 把"查账"误列进 `direct_answer` 的适用示例后 | **3 / 8** | 5 / 5 |

改动**只在 YAML 里**、代码一行未动，分流结果就变了。**"决策表是配置、判断交给 LLM"
这条断言得到了实证。**

这个实验被我改了三版才有效，教训值得留着：

1. **只删最后一句 → 测不出东西。** 那句是**重复陈述**（前一句已含同样信息），
   实验区分不了"散文没用"和"散文冗余"。
2. **删掉全部相关文字 → 还是测不出。** 剩下的示例（解释/改写/闲聊/常识/翻译）
   本身就不含"查我的账"，模型靠示例就排除了。
3. **必须让改动"有诱惑力"**：把查账明确列进直答的适用示例，模型才真的被带偏。

### 首次真实运行暴露的问题（都已修）

| 问题 | 后果 | 修法 |
|---|---|---|
| `timeout_ms` 拍脑袋填了 1200/2000，比实测中位数还小 | **12/13 的"路由决策"其实是超时兜底**，外表完全正常 | 按实测重设（评估 8s / 路由 12s），并把具体超时值写进错误消息 |
| 评估器假设模型输出是正确类型 | `estimated_scale` 返回字符串时崩 | 边界处一律 `_as_dict` / `_as_list` 收口 |
| `handler` 是**可推导的冗余字段**，却要模型给 | 模型正确选了路由与工具、但漏掉 handler，触发兜底 | 守卫从 `tool_set` 推导（子集判定）；跨多 handler 才失败 |
| 拆解器用字符串哨兵 `handler=decision.handler or "none"` | 报了"handler none 没有可执行实现"，把人引向错误方向 | 去掉哨兵；路由点不出工具时**退到自由拆解**（确定性修复） |
| `build_ledger_entry` 缺必填字段照样写 | **写入一条空记录并报成功** | 示例 handler 校验金额；工具补 `input_schema` 让参数抽取有形状可依 |

### 尚未解决（诚实记录）

**"成功但结果不是用户要的"仍会发生。** 实例：*"明天下午三点开会"* 被路由到
`single_tool_action` 的 `parse_natural_time`——时间**解析正确**（`2026-10-07T15:00:00+08:00`），
但**会没建成**；任务状态是 `succeeded`。

路由本身不算错（那是它选中的工具），错在缺少"解析→建日程"这条已知形状的表达。
按设计的答案与票据入账同构：**给日程加一张流程模板**（`parse_natural_time` → `create_event`），
让拆解器命中它而不是走单步。这是 M4 之前该补的第一件事。

## M2 — 状态 + 流式 + 单步执行 ✅ 已完成

**交付**：

- `StateStorePort` + `InMemoryStateStore` + `SqliteStateStore`
- 事件日志（单调 `seq`）、SSE 端点、`Last-Event-ID` 重放
- 同步 / 异步模式仲裁
- `DispatchContext`、单步工具执行（`executor: tool`）
- `BudgetLedger`（`advisory` 模式：记账 + 告警，不中断）
- `ProblemJSON` 错误映射、幂等存储

**可运行标志**：

> 一个单工具任务端到端跑通，带实时状态；断开 SSE 再重连能补齐缺失事件；
> 各类错误返回正确的 `code`；成本被完整记账。

**这一阶段最容易做漏的一件事**：事件日志的持久性。如果为了省事只喂活连接，
M2 看起来能过，但 M4 的移动端会在第一个后台切换上暴露问题。**重放不是优化，
是移动端的必需能力**，一开始就要做对。

---

## M3 — 拆解 + 并行 + 多 Agent ✅ 已完成

**交付**：

- `Decomposer`（LLM 自由拆解 + `flow_templates` 参数化）
- `DagRunner`：动态就绪集、`Semaphore(plan.max_parallelism)`、取消传播、
  节点重试、档位升级、有界重规划
- `$ref` 解析、join 处理、`awaiting_clarification`
- **Agent 执行器**：`executor: agent` 的有界多轮循环、角色加载、
  `agent.round` 事件
- **独立复核**：`verification.independent_review` + 仲裁者、
  `agent.review` 事件（见 [11-multi-agent.md](11-multi-agent.md)）

**可运行标志**：

> 一张 4 节点的票据 DAG 跑起来，两个分支并行，一个节点重试一次，
> 一个节点走默认值，逐节点状态实时可见；
> 一个 `agent` 节点跑满多轮后按要求收敛（或按 `on_round_limit` 正确处置）；
> 一个声明了 `independent_review` 的节点给出复核结论，含一致度。

**相对现有实现的改进点**（每一条都对应 `duowei-ai` 里的一个具体缺陷）：
动态就绪集（而非硬编码波次）、并发度来自计划（而非 `MAX_CONCURRENT_AGENTS = 9`）、
取消传播（现在完全没有）、有类型的 `NodeFailure`（现在用 `"; "` 拼接异常）、
飞行中成本记账（现在没有）。

---

## M4 — 接缝验收 ✅ 已完成

**做的是接缝，不是账本。** 账本 schema 属于消费端（用户在单独开发记账软件），
所以 M4 的交付物是**让接缝可以被机械证明**：

* handler 的声明与实现从 `dispatcher/adapters/` 移到顶层 `handlers/<领域>/`
* `config/handlers.yaml` + `dispatcher/plugins.py` —— 配置驱动的插件加载，
  **加载器里不出现任何领域名**
* 领域端口（`LedgerPort`）住在 `handlers/bookkeeping/ports.py`，
  **不在 `dispatcher/ports/`** —— 账本概念不属于调度层
* 外部注入通道：`DispatcherConfig(handlers=[...])`，让应用带着自己的数据库注入
* `handlers/bookkeeping/handler.py` 的分类词表改从 `ctx.config` 读，
  幂等改由存储端口保证（原先写在 handler 里对着内存列表查，
  等于把幂等做成了"只有内存实现才有"的特性）
* `tests/test_handler_seam.py`（9 个用例）—— **哈希不变**的机械证明

**过程中的两个真实教训**：

| 问题 | 后果 |
|---|---|
| 测试 fixture 造的临时包与仓库里的 `handlers` 同名，且只清理了自己加的那几个模块 | **单独跑通过、全量跑挂**——模块缓存污染，报错出现在别的测试里 |
| `isinstance(body[0].value, str)` 少了一层 `.value`（那是个 AST 节点不是字符串） | 文档字符串排除法静默失效，返回空集，检查失去排除能力 |

后者尤其值得记：**一个静默失效的检查比没有检查更糟**——它会让人以为已经守住了。

---

## M4 原定目标（保留备查）

**M2/M3 已交付的基础设施**（使得 M4 只剩领域逻辑）：

* 事件日志 + 内存/SQLite 状态存储（跨重启重放、seq 无洞、两实现 parity 测试）
* `DispatchContext` / 预算记账（**所有阶段都记账**，含评估、路由、拆解、agent 循环、复核仲裁）
* `DagRunner`：动态就绪集、并发取自计划、取消传播、节点重试、档位升级、有界重规划
* `NodeExecutor`：工具节点 + agent 有界多轮 + 独立复核与仲裁
* `Decomposer`：模板优先 + 自由拆解 + 计划确定性校验
* 完整状态机、同步/异步分流、SSE 事件流（`Last-Event-ID` 重放）、澄清恢复、取消
* `handlers/`（**在 `dispatcher/` 之外**）——参考实现，证明接缝成立

**M4 仍要交付**：

- `bookkeeping` handler：声明式清单 + `execute_tool()`
- `receipt_to_entry` 模板落地
- `extract_receipt_fields` / `crop_and_zoom` / `read_media_region` /
  `normalize_merchant` / `dedupe_check` / `build_ledger_entry` / `query_ledger` 等工具
- `receipt_extractor` / `merchant_classifier` 角色的提示词落地
- `GET /usage`、`POST /tasks/{id}/feedback`、`RunLog` 采集、脱敏控制
- 媒体上传端点的实现

**可运行标志**：

> 一张真实截图 → 结构化字段 → 用户在确认页核对（或修改）→ 入账，
> 且**改动 `dispatcher/` 的文件数为 0**。

**这是整条接缝的验收测试。** 如果接账时发现必须改调度层核心，说明界线画错了——
回炉改设计，不要就地打补丁（否则这条界线会在每个新领域上再退一步）。

**为什么排在 M4 而不是 M1**：它是接缝的**第一个消费者**。让消费者跟着接缝一起长出来，
会让 handler 的临时需求渗进调度层——那些需求往往不是通用的。先把接缝验证到
"能用"，再接第一个领域。

---

## M5 — 自进化 ✅ 已完成

**已交付**（`dispatcher/evolution/`，约 1200 行 + `tests/test_evolution.py` 31 个用例）：

* `core/runlog.py` —— RunLog 从 TaskRecord **折叠**出来（不是另维护一份真相），
  `human_signal` 与 `redaction` 都在
* `ports/evolution.py` + `adapters/{memory,sqlite}_evolution.py` —— 冷数据单独存储。
  SQLite 版是必需的：分析窗口 7 天，内存实现在一次重启后就空了，
  于是"每周跑一次分析"永远只看到"重启以来"的几条，样本量永远不达标——
  **整套机制静默地不工作**
* `evolution/detectors.py` —— 21 个检测器，规则全读自配置；谓词与聚合是**封闭集合**，
  写错在加载期就失败。`source_field: internal` 那类**显式报告跳过**，
  而不是安静放过（安静的跳过等于一个看起来在跑、实际永不报警的监控）
* `evolution/validator.py` —— 建议的确定性校验：类型封闭、**不能指向代码**、
  路径存在、不在禁区、类型一致、幅度受限、样本量达标、影响面可解析
* `evolution/analyzer.py` —— 在证据包上归因；没有检测器触发时**不调模型**
* `evolution/policy_store.py` —— 不可变版本、父子链、金丝雀、自动回滚
* `evolution/loop.py` —— 分析 → 建议 → 审批 → 新版本 → 护栏 → 转正/回滚
* 7 个端点：建议列表 / 批准 / 拒绝 / 触发分析 / 版本列表 / 回滚 / 人工反馈
* `Dispatcher.apply_policy()` —— **热换策略**（批准后立刻生效，否则"金丝雀"
  就变成"重启一次"）

**真实模型端到端验证过**：检测器触发（`guard_overrule_rate` 0.30）→ LLM 产出
1 条通过校验的建议（正确指出 `direct_answer.when` 漏了两条排除项，反事实从真实采样读出）
→ 审批 → `pv_2026-10-01_04`（金丝雀）→ 策略热换生效 → 金丝雀在样本不足时正确
拒绝下结论 → 回滚到 `...03`。

**首次真实运行暴露并修掉的问题**：

| 问题 | 后果 | 修法 |
|---|---|---|
| 提示词要求"输出 JSON 数组"，而供应商的 JSON 模式**只保证顶层是对象** | 模型把结果包了一层，分析结果全丢 | 提示词改成 `{"suggestions": [...]}`，读取端接受常见包装键并把实际键名写进备注 |
| 提示词的字段说明是散文，模型把 `kind` 写成 `type`、把 `target.path` 写成 `target_path` | **分析内容完全正确却被整条丢弃**——丢的是格式不是判断 | 提示词给出逐字照抄的 JSON 模板；读取端归一这些**无歧义的形式偏差**（语义问题仍留给校验器） |
| `**/*.py` 被当成空前缀，匹配一切 | 每条建议都被拒，理由写着"落在禁区里"，看起来还很合理 | 区分 `dir/**`（前缀）与 `**/*.py`（后缀）。这个 bug 是"正常建议应当通过"的用例抓出来的 |
| `guards.locked_path` 是**反语义**的（true 表示非法），却和其余守卫一起进了 `all()` | 同样导致全部被拒，而且理由栏是空的 | 显式分成"必须为真"与"必须为假"两组 |
| 检测器的谓词把 `thresholds.escalate_below_confidence` 解析成 `None` | 类型错误，检测器求值失败 | 前缀要从字典路径里去掉 |
| `is non-empty` 被逐元素求值 | `guard_overrule_rate` 恒为 1.0——**一个永远触发的检测器比不触发更糟**，它会持续产出假建议 | 空值判断针对整个集合，不做展开 |
| `human_signal.edits[].field` 遇到 `[]` 就返回 | 后面的 `.field` 被丢掉，检测器拿到一堆 dict | `[]` 是"展开后继续"，不是终点 |

---

## M5 原定目标（保留备查）

**交付**：

- 确定性检测器（`config/evolution/detectors.yaml` 里全部指标）
- LLM 分析（在证据包上归因，含注入防御）
- `Suggestion` 与 `PolicyPatch` 的确定性校验、`locked_paths` 强制
- `PolicyVersion` 存储、审批 / 拒绝端点
- 金丝雀 + 自动回滚护栏

**可运行标志**：

> 从真实运行里生成一条真实建议 → 呈现 → 批准 → 金丝雀 → 转正或自动回滚，
> 全链路走通。

**排序理由**：没有日志就没有进化的依据。M2 的 `RunLog` 与 M4 的 `human_signal`
必须先跑一段时间攒出样本，M5 才有真东西可分析。

**一个必须一起想清楚的问题**：建议在界面上怎么呈现。如果没有人看，这套机制就是死的。
契约只提供端点，界面是消费端的事——但"没人看"是一个真实且常见的失败模式，
值得在 M5 连同端点一起定下来。

---

## M6 — SDK 与适配器

**交付**：

- Python 参考 SDK、Dart/Flutter SDK（含 `task_repo.dart` 状态映射）
- `PostgresStateStore`
- 两个真实适配器：
  - **`duowei-ai`**：用策略路由替换 `agents/router.py::_match_group()` 的关键词匹配
  - **`ai-workmate`**：替换其 `Graph` / `agent_node` 流水线

**可运行标志**：

> 调度层同时服务两个真实产品，且改一条策略不需要发版。

**为什么这两个适配器是最后的验收**：一套已经跑起来的真实产品能被接缝吸收
而不需要改调度层核心——这是最有力的证明。特别是 `duowei-ai`：
它现在用的是关键词匹配路由，把它换成策略路由，等于把"不写死规则"这条要求
在真实代码上兑现一次。

---

## 一张图

```
M0 契约冻结 ────────────────► 本轮，文档与契约
     │
     ▼
M1 01+02 ────────────────────► 证明「LLM 消费策略」成立   ← 风险最高，所以最先
     │
     ▼
M2 状态+流式+单步 ────────────► 可观测的任务执行
     │
     ▼
M3 拆解+并行+多Agent ─────────► 复杂任务与自主执行者
     │
     ▼
M4 记账 handler ─────────────► 接缝验收（改动调度层文件数 = 0）
     │
     ▼
M5 自进化 ───────────────────► 用真实数据调配置
     │
     ▼
M6 SDK + 适配真实产品 ────────► 两个现有产品被接缝吸收
```

---

## 明确不在本路线内的

- **记账 APP 本体**：UI、账本 schema、分类词表、投资与规划逻辑。另开一轮。
- **日程 handler**：列入 M4 之后的第一个验证用例（它会验证"不需要 LLM 的路径"），
  但不在本轮。
- **具体实现语言选型**：契约先行，语言后定。

---

## 相关文档

- 完整设计 → [01-architecture.md](01-architecture.md)、[02-stages.md](02-stages.md)
- 验收清单 → [10-conformance-checklist.md](10-conformance-checklist.md)
- 多 Agent → [11-multi-agent.md](11-multi-agent.md)
