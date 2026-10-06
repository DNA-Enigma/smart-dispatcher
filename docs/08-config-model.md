# 08 配置模型

配置怎么分层、优先级如何、以及怎么让"不写死规则"这件事有牙齿。

---

## 分层与优先级

严格从高到低：

```
请求约束  >  用户配置  >  部署默认  >  模块默认
```

- **请求约束**：`TaskEnvelope.constraints.*`。取更严者（例如 `max_cost` 取更小值）。
- **用户配置**：按 `user_id` 隔离。这是本设计一开始就定下的粒度——
  现在只有一个人用时不增加多少工作量，但将来多人用不必重构。
- **部署默认**：该部署的策略文件。
- **模块默认**：随模块发布的 `config/dispatcher.default.yaml`。

优先级冲突的裁决原则：**安全相关的取更严，体验相关的取更宽。**

---

## 配置产物

| 文件 | 内容 | 04 可改 |
|---|---|---|
| `config/dispatcher.default.yaml` | 模块默认值 | 部分（同下） |
| `config/routing.policy.yaml` | **决策表**：路由、`when:` 散文、档位、阈值、预算、兜底、禁区 | 部分，见 [06](06-self-evolve.md) |
| `config/taxonomy.yaml` | 任务类型词表 | 只增 |
| `config/agents.yaml` | Agent 角色与协作硬界 | 角色轮数可调；`hard_bounds` 不可 |
| `config/pricing.yaml` | 模型价格表 | **否** |
| `config/evolution/detectors.yaml` | 检测器与阈值 | 是（经批准） |
| `config/evolution/suggestion_kinds.yaml` | 可提的建议种类（封闭集） | 否（加种类属于代码变更） |
| `config/flow_templates/*.yaml` | 具名参数化 DAG | 只增 |
| `config/handlers/<handler_id>.yaml` | handler 的部署级默认 | 按 kind 规则 |
| `prompts/*.md` | 四阶段提示词 | 是（经批准） |
| `prompts/agents/*.md` | 角色提示词 | 是（经批准） |
| `prompts/evolution_analysis.md` | 分析提示词 | **否** |

环境变量只用于两类东西：**密钥**（`*_API_KEY`）与**部署事实**
（`*_BASE_URL`、端口）。不做业务阈值——那是配置文件的事。

沿用 `ai-workmate/server/app/config.py` 的写法：`pydantic-settings` +
环境变量覆盖 + 结构化值用 JSON-in-env。

---

## 为什么决策表是配置

这是整套设计的支点，值得再讲一遍。

`config/routing.policy.yaml` 里每条路由的 `when:` 是一段**散文**：

```yaml
- id: direct_answer
  when: >
    单步、无副作用、不需要外部事实、也不需要任何用户私有数据即可回答的请求
    （解释、改写、闲聊、常识问答、语言翻译）。
    凡涉及查询本人流水、余额、持仓、日程等用户私有数据，一律不得选择本路由——
    这类请求必须走需要 handler 的路由。
```

它**就是**决策表。之所以用散文，是因为读它的是 LLM，而散文正是 LLM 最擅长的接口。
它之所以放在 YAML 里，是因为这样它可编辑、有版本、可被 04 提出改进、可回滚。

对比现有做法：

```python
# duowei-ai/backend/app/agents/router.py
GROUP_TEMPLATES = {
    "code": {"keywords": ["python", "代码", "编程"], "plan": [...]},
    ...
}
for kw in tpl["keywords"]:
    if kw.lower() in haystack:
        ...
```

加一个领域概念就得加一组关键词，再加一个分支。而换成散文之后，
**新增一条路由 = 追加一段 YAML**，判定逻辑一行都不用改。

---

## 三层分离与三条不变式

| 层 | 是什么 | 谁改 |
|---|---|---|
| 策略基底 | `config/*.yaml` + `prompts/*.md` | 人或 04（经人批准） |
| 判断 | LLM 在策略渲染出的菜单里选择 | LLM |
| 守卫 | `PolicyGuard` 做集合校验与截断 | 确定性代码 |

- **I1 — 守卫永不检视语义。** 只有 `in` / `issubset` / `min` / `clamp`。
- **I2 — 每个跨信任边界的 LLM 输出都回落配置派生的枚举再校验一次。**
- **I3 — `rationale` 与 `policy_version` 一并持久化。**

---

## 哪些词表封闭，哪些不封闭

这条区分很重要，因为搞反了会同时伤害两边。

### 在 schema 里封闭（结构性词表）

这些是**实现结构的一部分**，改它们就是改契约：

- `RouteDecision.path`：`direct_llm` / `single_step_tool` / `decompose`
  （对应实现里的三种执行器）
- `ExecutionPlan.Node.executor`：`tool` / `agent`
- 任务状态、子任务状态、事件类型
- `Problem.code`
- `Suggestion.kind`

### 不封闭，由 `PolicyGuard` 按配置校验（配置性词表）

这些是**数据**，加一项只该是加一段 YAML：

- **模型档位名**（`model_tiers` 的键）
- **路由 id**
- **任务类型**（`taxonomy.yaml`）
- **Agent 角色 id**（`agents.yaml`）
- **能力名**、**工具名**（handler 声明）

判断方法很简单：**加一项需要改代码吗？** 需要，就是结构性词表，放 schema；
不需要，就是配置性词表，放配置。

如果档位名写死在 schema 枚举里，那么"加一档模型"就变成了契约变更——
而它明明只是加一个 YAML 键。

---

## 常量去哪

| 类型 | 去处 |
|---|---|
| 阈值、上限、超时、轮数 | `routing.policy.yaml` 的 `thresholds` / `limits` |
| 检测器阈值 | `config/evolution/detectors.yaml` |
| 价格 | `config/pricing.yaml` |
| 并发度 | **来自 `ExecutionPlan.max_parallelism`**，不是模块常量 |
| 模型绑定 | `model_tiers.*.model_ref` 的 `secret://` 引用 |
| 密钥 | 环境变量 |
| 提示词 | `prompts/` |

对照反例：`duowei-ai/backend/app/core/graph.py` 的 `MAX_CONCURRENT_AGENTS = 9`
是模块常量，`services/quality_service.py` 的 `if char_count > 2000: return 0.9`
是函数内字面量。这两处都应当是配置键。

---

## CI 检查：让"不写死"有牙齿

没有检查的规则会在第一次赶工期时被忘掉。因此这三条是构建时必须通过的：

### `no-literal-policy`

在 `dispatcher/core/**` 与 `dispatcher/stages/**` 中拦截：

- 数值字面量（白名单：`0`、`1`、`-1` 等结构性常量）
- 模型名字符串（形如 `*-v\d`、含 `deepseek` / `kimi` / `qwen` 等供应商词）
- 形如 `if x > <数字>` 的阈值比较

### `no-orphan-config`

两个方向都查：

- 代码引用了但默认配置里不存在的键 → 拼写错误或漏了默认值
- 默认配置里存在但从不被引用的键 → **死配置**

第二个方向正好让"无死代码"这条要求也有了牙齿。

### `config-key-typed`

配置键的类型与代码读取时的期望一致。防止把字符串读成数字静默变成 NaN。

---

## 版本化

- `routing.policy.yaml` 带 `policy_version`，每次变更产生**新的不可变版本**，
  父版本可追溯。
- 每条 `RunLog` 记录 `policy_version`。
- 因此可以回答："这条 run 是哪个策略产生的"、"这个改动之后指标变了吗"、
  "我要回到改动前的状态"。

**永不原地修改配置。** 这是回滚能力的前提。

---

## 相关文档

- 决策表机制 → [01-architecture.md](01-architecture.md)
- 策略文件本身 → `config/routing.policy.yaml`
- 04 能改什么不能改什么 → [06-self-evolve.md](06-self-evolve.md)
- 一致性检查 → [10-conformance-checklist.md](10-conformance-checklist.md)
