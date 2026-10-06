# 11 多 Agent 协作

多个 Agent 怎么在这个框架里协作，以及一条刻意的限制。

---

## 结论先行

| 问题 | 答案 |
|---|---|
| 有多个 Agent 吗 | 有。节点可以声明 `executor: agent`，是带角色、带工具白名单、有界多轮循环的自主执行者 |
| Agent 之间自由对话吗 | **不。** 通过结构化的数据依赖协作：上游输出按 `$ref` 绑定到下游输入 |
| 拓扑写死吗 | **不。** 角色在配置里声明，拓扑由 03 拆解器动态生成 |
| 需要多方案竞争吗 | 用 `verification.independent_review`：固定 N 份独立产出 + 一个仲裁者 |
| 会失控吗 | 不会。单节点轮数、复核份数、单计划 agent 节点数、整任务总轮数，四道硬界 |

---

## 为什么不让 Agent 自由对话

这是本设计里最重要的一个取舍，必须先讲清楚。

自由对话式的多 Agent（Agent 之间互相发消息、互相质疑、辩论到收敛）看起来更"智能"，
但有两个无法回避的问题：

1. **成本与延迟没有上界。** 对话轮数取决于模型什么时候觉得"聊够了"，
   而这个判断本身就不稳定。一个本该 3 秒完成的任务可能聊 20 轮。
   在记账这种高频、低单价的操作上，这是不可接受的。
2. **过程难以观测与复现。** 事后想回答"它当时为什么那么说"，
   面对一堆自由的对话消息是做不到的。而 04 自进化恰恰需要可归因的记录——
   没有可归因的过程，就只能靠最终结果反推，效果差一个量级。

因此，本设计用 **DAG + 有类型的输出 + 明确的仲裁者** 来表达协作：

- 谁依赖谁的产出，写在 `depends_on` 与 `$ref` 里，是显式且可校验的。
- 每一步的产出必须满足声明的 `output_schema_ref`，因此"说了什么"是结构化的。
- 需要竞争时用固定份数的独立产出 + 仲裁者，**成本与轮数都是常数**。
- 全过程落在事件日志里，可重放、可审计。

**同样能表达协作，但每一步都可边界、可审计、可回放。**

---

## 两种执行者

`ExecutionPlan` 的每个节点声明 `executor`：

### `tool` —— 单次确定性工具调用

输入确定则输出唯一，不需要中途看结果再改策略。

```yaml
- id: dedupe
  executor: tool
  tool: dedupe_check
  tier: null                        # 纯查表，不需要模型
  required_capabilities: []
```

例：按 `media_id` 查重、按已定字段写库、纯算术汇总。

### `agent` —— 有界多轮的自主执行者

需要"先看到中间结果才能决定下一步做什么"的步骤。

```yaml
- id: extract
  executor: agent
  role: receipt_extractor
  tool_whitelist: [extract_receipt_fields, crop_and_zoom, read_media_region]
  requires: [vision.extract, text]
  tier: vision
  max_rounds: 4
  on_round_limit: escalate_and_retry
```

例：图糊了要先放大局部重看；商户名不规范要结合用户历史习惯判断。

### 怎么选

判错的代价不对称：

- 本该用 `tool` 的写成 `agent` → 浪费成本与延迟，但有 `max_agent_nodes_per_plan`
  与轮数上限兜底。
- 本该用 `agent` 的写成 `tool` → **根本产不出结果**。

所以拿不准时可以偏向 `agent`，但不可滥用——每一步都变成 agent 会让成本和延迟
同时失控，而且大部分步骤本来就不需要判断。

判定依据写在策略里，是散文（因为判定者是 LLM）：

```yaml
decomposer:
  agents:
    enabled: true
    use_agent_when: >
      该步骤需要先看到中间结果才能决定下一步做什么……
    use_tool_when: >
      该步骤的输入确定则输出唯一，一次调用即可完成……
```

---

## 角色的配置化

`config/agents.yaml`。**这是与现有做法最关键的区别。**

现有 `duowei-ai` 的做法：9 个 Agent 的拓扑写死在 `core/graph.py` 里
（`N1(画像)→N2(路由)→batch→batch2`），每个 Agent 的职责与工具用 Python 常量和
`GROUP_TEMPLATES` 硬编码。

本设计的做法：

- **角色在配置里声明**：提示词、可用工具、所需能力、起始档位、轮数上限。
- **拓扑由拆解器动态生成**，角色只回答"这个位置由谁来做"，不回答"它后面接谁"。

于是新增一个角色 = 加一段 YAML + 一个提示词文件，调度层一行都不用改。

角色声明里**永不出现模型名**，只出现能力需求（`requires`）：

```yaml
- id: verifier
  when: 需要独立复核另一份产出的正确性。
  system_prompt_ref: prompts/agents/verifier.md
  allowed_tools: []
  requires: [text]
  default_tier: strong        # 档位，不是模型名
  max_rounds: 3
```

这是"禁止硬编码模型"在 Agent 层的落点：角色说"我需要判断力"，
档位由调度层映射，模型由配置映射。

---

## 有界循环

一个 `agent` 节点的执行：

```
round = 1
while round <= max_rounds:
    step = await llm(messages, requires=node.requires)     # 决定这一步做什么
    emit(agent.round, {round, max_rounds, tool_calls, stop_reason})
    if step.tool_calls:
        results = await call_tools(step.tool_calls)         # 只能调 tool_whitelist 内的
    if output_satisfies(step.final, node.output_schema_ref):
        return step.final                                   # 正常收敛
    round += 1

apply(node.on_round_limit)                                  # 轮数耗尽
```

三个要点：

- **「产出满足 schema」由 LLM 判断，"轮数用尽"由代码判断。** 前者是判断题，
  后者是安全约束，不能交给模型。
- **每一轮都发 `agent.round` 事件**，含轮次、本轮调用的工具、停止原因。
  于是"它转了几圈、都在干什么"是可见的。
- **`tool_whitelist` 是两级交集**：`⊆ 角色的 allowed_tools`，且 `⊆ 决策的 tool_set`。
  任一为空则计划非法。

轮数耗尽时按 `on_round_limit` 处置：

| 处置 | 用在什么步骤 |
|---|---|
| `fail_task` | 必须完整正确，残缺无用 |
| `accept_partial` | 部分结论仍有价值 |
| `escalate_and_retry` | 升档重跑（仍受 `max_escalations` 约束） |

这个选择是有领域含义的，两个记账角色的取值正好相反：

- `receipt_extractor` 用 `escalate_and_retry` —— 金额少读一位数字，
  账目就被静默污染，**残缺产出是失败**。
- `ledger_auditor` 用 `accept_partial` —— 一份"发现 3 处存疑、另有 5 笔未能核对"
  的报告**仍然有价值**，整体失败反而丢掉了已经查出来的东西。

---

## 独立复核与仲裁

节点可以声明 `verification`：

```yaml
verification:
  mode: independent_review
  reviewers: 2
  reviewer_role: verifier
  arbiter_role: arbiter
  on_disagreement: ask_user
  max_cost_multiplier: 3.0
```

| mode | 行为 |
|---|---|
| `none` | 不校验（**默认**） |
| `self_check` | 同一执行者自查一遍。便宜，但自我确认偏差大 |
| `independent_review` | 起 N 个独立执行者，再由仲裁者比对裁决 |

关键要求：**复核者必须独立取数，不得复用被复核者的中间推理。**
否则它只是在给同一个错误背书——那样的验证比没有验证更糟，
因为它给出了虚假的确定感。

仲裁者必须能输出**「无法裁决」**，而不是被迫选一个。
无法裁决时按 `on_disagreement` 处置：

| 处置 | 何时用 |
|---|---|
| `arbiter_decides` | 分歧不重要或仲裁者有足够依据 |
| `ask_user` | **金融对账场景的默认** —— 强行选一个的代价比问一次高得多 |
| `fail_task` | 分歧本身就说明任务不该继续 |

仲裁者判断的是**证据强度，不是票数**。两份有据可查的产出可以压过三份含糊的。

### 默认关闭

`verification` 默认是 `none`，因为 `independent_review` 会把该步成本乘约 3
（`max_cost_multiplier` 就是为计划校验准备的：它检查膨胀后的成本是否超出决策预算）。

`config/agents.yaml` 的 `verification_defaults` 按任务类型给默认值：

- `bookkeeping.*` → `none`。理由：记账的默认防线不是多 Agent 交叉验证，
  而是**让用户在确认页核对**。人工确认比机器复核更准，也更便宜。
  只有当 04 检测出某类抽取的人工改单率长期偏高时，才值得对那一步开启复核。
- `bookkeeping.reconciliation` / `investment.analysis` → `independent_review`。
  理由：对账与投资分析的结论错了，**用户很难事后发现，且代价高**。
  分歧无法裁决时问用户。

这个对比正是该不该开验证的判据：**错了以后用户能不能自己发现。**
能发现（分类、商户名），就别花那个钱；不能发现（对账结论、持仓分析），就花。

---

## 硬界

四道界，全部在配置里，且都在 `locked_paths` 内——**04 自进化不能放宽它们**：

```yaml
# config/routing.policy.yaml
limits:
  max_agent_rounds: 8              # 单节点轮数
  max_reviewers: 3                 # 单节点复核份数
  max_total_rounds_per_task: 40    # 整任务总轮数
  max_agent_nodes_per_plan: 6      # 单计划 agent 节点数

# config/agents.yaml
hard_bounds:
  max_rounds_ceiling: 8
  max_reviewers_ceiling: 3
  max_total_rounds_per_task: 40
```

取更小者。`max_total_rounds_per_task` 是防"每一步都合规但整体炸掉"的那一道——
单看每个节点都在 4 轮以内，十个节点乘起来就不对了。

角色表里有一项**可以**调：角色的 `max_rounds`（在 `limits` 之内）。
这是个合理的学习边界——"这个角色的 3 轮够不够"是能从数据里看出来的问题，
而"总轮数上限是多少"不是。

---

## 可观测

多 Agent 最容易被诟病的是"看不清它在干什么"。两个事件解决它：

### `agent.round`

```json
{
  "seq": 5,
  "type": "agent.round",
  "subtask_id": "extract",
  "data": {
    "role": "receipt_extractor",
    "round": 2,
    "max_rounds": 4,
    "tool_calls": ["crop_and_zoom", "extract_receipt_fields"],
    "stop_reason": "output_satisfied_schema",
    "tier": "standard"
  }
}
```

看到"第 2 轮调了放大再重抽，然后收敛"，比看到"extract 成功"有用得多。

### `agent.review`

```json
{
  "type": "agent.review",
  "subtask_id": "reconcile",
  "data": {
    "mode": "independent_review",
    "reviewers": 2,
    "agreement": 0.5,
    "outcome": "undecidable",
    "arbiter_role": "arbiter",
    "dissent_fieldds": ["closing_balance"]
  }
}
```

`agreement` 低本身就是一条值得上报的信号：如果某个步骤的复核者长期不一致，
说明这一步**本身**是模糊的——该改的是提示词或输入，而不是加更多复核者。
这个指标也进 `RunLog`，04 可以据此提建议。

---

## 与 04 的连接

多 Agent 的参数是可学习的，但边界不是：

| 参数 | 04 可建议修改 |
|---|---|
| 角色的 `max_rounds` | 是（在 `limits` 之内） |
| 角色的 `default_tier` | 是 |
| 角色的 `tool_whitelist`（收窄） | 是 |
| 某任务类型是否开 `verification` | 是 |
| `verification.reviewers` | 是（在 `limits.max_reviewers` 之内） |
| `limits.max_agent_rounds` 等四道界 | **否** |
| `hard_bounds` | **否** |

对应的检测器（见 `config/evolution/detectors.yaml`）：

- `replan_rate` —— 拆解质量
- `template_miss` —— 该固化模板了
- `escalation_rate` —— 档位偏低
- `node_latency_p95` / `node_queue_p95` —— 轮数或并发度调参
- `human_edit_rate` / `field_edit_rate` —— 某一步该开复核了
- `agent_review_disagreement`（待补）—— 某一步本身是模糊的

---

## 一句话总结

> 多个 Agent **并行做事**，通过**结构化数据流**协作，用**有界循环**自治，
> 需要竞争时用**固定份数的独立产出 + 仲裁者**，而不是让它们聊天。

---

## 相关文档

- 拆解与执行 → [02-stages.md](02-stages.md)
- 角色表 → `config/agents.yaml`
- 角色提示词 → `prompts/agents/*.md`
- 节点 schema → `schemas/execution_plan.json` 的 `Node`
- 事件 → `schemas/event.json` 的 `agent.round` / `agent.review`
