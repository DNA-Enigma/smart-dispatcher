# 06 自进化

系统从自己的运行记录里发现配置问题、提出改进、经使用者批准后生效。

---

## 一句话概括

> **进化的是配置和提示词，永不是代码。审查人是使用者本人，不是某个管理员。**

---

## 为什么代码不可改

这一段必须写在最前面，因为它是整个机制的安全边界，而不是一句谨慎的口号。
四条理由，每一条都是具体的：

### 1. 可验证性不对称

使用者能判断"这个成本上限该是 0.05 还是 0.08"——它是一个数字加一句后果说明。
**没有人能判断一个代码 diff。** 看不懂却批准，比没有批准更糟：它给出了一种
"已经审过了"的虚假安全感。

### 2. 可回滚性不对称

配置回滚是即时的、彻底的、无副作用的——切回上一个 `PolicyVersion` 就完了。
代码回滚要重新部署、重启服务，还可能已经跑过一次不可逆的数据库迁移。
坏配置的爆炸半径**由构造保证有界**（它只能改一个白名单键的一个合法取值）；
坏代码的爆炸半径没有这个保证。

### 3. 注入升级

`RunLog` 里含用户书写的文本与模型生成的文本。若分析环节能写代码，就打通了一条
**用户内容 → LLM → 可执行代码**的路径——一次成功的提示注入就升级成了任意代码执行。

把输出约束成"白名单配置键 + 类型校验的值 + 幅度上限"，这条路就不存在了。
即使注入成功，能产出的也只是"某个配置键的某个合法取值，交给人审"。

### 4. 根本不需要

要调的每一样都能表达为配置：阈值、提示词、档位、路由、工具白名单、词表、模板。

若系统真的需要**新类型**的可调项，正确做法是把它做成一个配置键——一次性的、
由开发者写的、经评审的代码改动——而不是让进化循环去写代码。
`config/evolution/suggestion_kinds.yaml` 就是这份"可调项清单"，它是一份封闭词表。

---

## 谁审批

契约里**不建模 admin 角色**。它建模的是"策略范围的所有者"：

| 场景 | 审批人 |
|---|---|
| 单用户（你现在的记账应用） | 使用者本人 |
| 多人租户 | 租户所有者 |
| 平台级 | 平台运营 |

这不是为了省一个角色，而是因为**使用者是唯一有资格判断"这个分类对不对"的人**——
而那恰恰是所有建议的推导来源。加一个 admin 角色只会引入一个信息更少、
权力更大的主体。

建议在界面上怎么呈现由消费方决定（应用内「改进建议」页 / 通知 / 仅 API）。
调度层只提供端点。但有一点要提醒：**如果建议没有人看，这套机制就是死的。**
本轮只交付契约，界面是消费端的事——这是 M4/M5 阶段必须一起想清楚的问题。

---

## 日志

一次运行产生一条 `RunLog`（`schemas/run_log.json`），外加它引用的事件日志。

两个字段值得单独说。

### `human_signal` —— 最有价值也最难拿到

```json
{
  "verdict": "edited",
  "edits": [{"field": "category", "from": "其他", "to": "餐饮"}],
  "reason": "分类不对",
  "rework_count": 1
}
```

记账场景的自然采集点就是确认/修改页。用户把分类从「其他」改成「餐饮」，
这一下同时给出了**错在哪**和**对的是什么**——是一条带真值的标注。

没有它，04 只能靠延迟和成本反推质量，效果差一个量级。这也是为什么
`POST /tasks/{id}/feedback` 和 `flow_templates` 里的 `confirmation` 段
在契约里占这么重的位置。

### `redaction` —— 必填，不是可选

金融截图的留存必须**显式决定**，不能靠默认行为蒙过去。见 [05-media.md](05-media.md)。

---

## 分析：两段式

沿用 `duowei-ai/backend/app/services/quality_service.py` 的"便宜的确定性 + 贵的可选 LLM"
写法。

### 第一段：确定性检测器（无 LLM）

便宜、确定、每次都跑。定义与阈值全在 `config/evolution/detectors.yaml`，
**代码里零字面量**。

| 检测器 | 指标 | 说明 |
|---|---|---|
| 预算压力 | `budget_exceeded_rate` / `budget_warn_rate` | 上限是否设得不合实际 |
| 成本估算偏差 | `est_cost_error_ratio` | 01 的估算是否系统性偏高 |
| **守卫推翻** | `guard_overrule_rate` | 守卫推翻率高 → `when:` 散文已过时 |
| **档位通胀** | `mean_tier_index` | 均值逼近最强档 → 路由描述或档位设置有问题 |
| 兜底率 | `fallback_used_rate` | 路由器或评估器在失效 |
| 升级率 | `escalation_rate` | 默认档位偏低 |
| 低置信度 | `decision_low_confidence_rate` | 路由描述不够明确 |
| 词表未命中 | `taxonomy_miss_rate` | 词表缺一类意图 |
| 评估器降级 | `evaluator_degraded_rate` | 提示词或超时设置有问题 |
| **模板未命中** | `flow_template_miss_rate` | 某个形状该固化成模板了 |
| 重规划率 | `replan_rate` | 拆解提示词质量 |
| schema 校验失败 | `schema_validation_failed_rate` | 提示词或 schema 过严 |
| **人工改单** | `human_edit_rate` | 最重的质量信号 |
| 字段改单热点 | `field_edit_rate`（按字段） | 总改单率不高但某字段总被改 |
| 返工率 | `rework_rate`（按 `parent_task_id`） | 质量或路由问题 |
| 延迟 / 排队 | `node_latency_p95` / `node_queue_p95` | 超时与并发度调参 |
| 建议丢弃率 | `suggestion_discard_rate` | 分析产出不合规 |
| 建议拒绝率 | `suggestion_reject_rate` | 分析在提"看起来合理但没用"的东西 |

最后两个是**机制自身的健康度**——它们让"自进化"也能被监督。

每个检测器都声明 `source_field`，即它从 `RunLog` 的哪个字段算出来。
一致性检查会验证两边一一对应：检测器引用的字段必须存在，
`RunLog` 里的字段也必须有检测器在用（避免采集了却从不使用的死字段）。

### 第二段：LLM 在证据包上归因

只把**触发了的**检测器 + 有界抽样的 `RunLog` 交给 LLM。它的工作是提出假设与建议，
**不计算指标**——指标已经算好了，且是可信的。

这与 `duowei-ai` 的 `profile_evolution.py::_extract_changes` 完全同构：
LLM 提议，校验独立进行。

### 第三段：确定性校验每条建议

- 目标路径存在
- 目标路径不在 `locked_paths`
- 新值类型与当前值一致
- 变化幅度 ≤ `evolution.max_delta_ratio`
- 证据样本量 ≥ `evolution.min_sample_size`
- 影响面能解析到真实存在的路由 / 工具 id

不过关的**静默丢弃并计数**。丢弃率升高本身会被检测到，并指向分析提示词的问题。

---

## 建议

`schemas/suggestion.json`。种类是封闭词表，在
`config/evolution/suggestion_kinds.yaml`：

| 类型 | 目标 | 例 |
|---|---|---|
| `threshold_change` | 阈值、路由成本上限 | 某路由上限高于实际成本 p99，下调 |
| `route_guidance_patch` | 路由的 `when:` 散文 | 补一句"涉及用户私有账目数据不得选本路由" |
| `tier_change` | 默认档位、允许档位 | 某路由 94% 的情况 standard 够用，降档 |
| `tool_set_change` | 工具白名单 | 收窄过宽的工具集 |
| `flow_template_add` | 新增流程模板 | 31 次同类任务各自重新拆了同一张图 |
| `prompt_patch` | 评估器/路由器/拆解器提示词 | — |
| `taxonomy_add` | 任务类型词表（只增） | 反复出现的意图没有对应类型 |
| `validator_relaxation` | handler 输入输出 schema | 57 次 schema 失败其实是 schema 比工具更严 |

每条建议带：证据（含**反事实**：改了之后好在哪里，基于采样里的事实而非预测）、
理由、置信度、风险、影响面、确定性校验结果、回滚凭据。

反事实是给人看的核心内容。例如：

> 近 7 天该用户 37 次"我上个月花了多少"类提问中，有 7 次被判为 `direct_answer`，
> 随后被守卫改判或用户重问，实质是 `when` 描述漏掉了"查询本人私有数据"这一条。

使用者据此能独立判断，而不必信任系统。

---

## 审批 → 金丝雀 → 生效或回滚

```
定时分析
   ↓
Suggestion(status=proposed) ──→ 呈现给使用者
   │
   ├─ reject(reason) → status=rejected      （理由本身是后续分析的信号）
   │
   └─ approve(scope) → PolicyPatch
             ↓
     新的 PolicyVersion { parent: 上一版, approved_by: 使用者, status: canary }
             ↓
     金丝雀期（evolution.canary）：单用户场景按时间窗分片
             ↓
     确定性护栏检查：task_failure_rate / guard_overrule_rate /
                     human_edit_rate / budget_warn_rate
     任一相对基线劣化超过 canary.rollback_margin
             ↓
     通过 → status=active（全面生效）
     不通过 → 自动回滚到 parent 版本，status=rolled_back
             并产生一条回滚记录（本身也是一条建议）
```

### 为什么是 `PolicyVersion` 而不是原地修改

- **即时回滚**：切回去就行。
- **完整审计**：谁在什么时候批了什么。
- **可比较**：每条 `RunLog` 都记录 `policy_version`，因此可以回答
  "这个改动之后指标变了吗"，也可以做金丝雀与基线的对比。
- **可追溯**：几个月后仍能回答"这条 run 是哪个策略产生的"。

### 单用户场景下金丝雀是什么

按流量比例分片在单用户场景下没有意义（一个人没有对照组）。因此金丝雀模式
默认是 `time_window`：新版本先跑一天，护栏指标不劣化才转正。

这不是形式主义——它真的能挡住"这个改动让改单率从 12% 涨到 30%"这类问题，
而且对使用者完全无感。

---

## 什么可改，什么不可改

| 产物 | 04 可改 | 说明 |
|---|---|---|
| `routing.policy.yaml` 的 `routes[].when` / `allowed_tiers` / `default_tier` / `max_cost` / `tool_policy` | 是（经批准） | |
| `routing.policy.yaml` 的 `thresholds.*` / `budget.*`（仅取值） | 是（经批准） | |
| `routing.policy.yaml` 的 `model_tiers.*.provider` / `model_ref` | **否** | 改错了会静默换掉用户实际在用的模型 |
| `routing.policy.yaml` 的 `fallback` | **否** | 这是"出问题时还能跑"的最后保障 |
| `routing.policy.yaml` 的 `limits.*` | **否** | 资源与安全硬边界 |
| `routing.policy.yaml` 的 `evolution.*` | **否** | 不允许自我放宽 |
| `config/agents.yaml` 的 `hard_bounds` | **否** | 多 Agent 不会失控的保证 |
| 角色的 `max_rounds`（在 `limits` 之内） | 是（经批准） | |
| `prompts/evaluator.md` / `router.md` / `decomposer.md` / `agents/*.md` | 是（经批准） | |
| `prompts/evolution_analysis.md` | **否** | 见下 |
| `config/taxonomy.yaml` | 是（只增） | |
| `config/flow_templates/*.yaml` | 是（只增） | |
| `config/pricing.yaml` | **否** | 价格来自供应商，不是能"学"出来的 |
| handler 的 `input_schema` / `output_schema_ref` | 是（只放宽） | |
| handler 的 `execute_tool` 代码 | **永不** | |
| 调度层核心代码 | **永不** | |

**唯一必须由人手工改的提示词是 `evolution_analysis.md`。** 理由：
允许进化循环修改"决定它如何进化"的提示词，等于允许它放宽对自己的约束。

---

## 相关文档

- 日志 schema → `schemas/run_log.json`
- 检测器与阈值 → `config/evolution/detectors.yaml`
- 建议种类 → `config/evolution/suggestion_kinds.yaml`
- 分析提示词 → `prompts/evolution_analysis.md`
- 媒体与脱敏 → [05-media.md](05-media.md)
- 审批端点 → `openapi.yaml` 的 `/evolution/*`
