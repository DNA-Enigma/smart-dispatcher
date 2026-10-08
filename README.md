# Smart 调度层

一套通用的任务调度契约：任务进来先**评估**（01）、按配置**路由**到合适的处理框架与
模型档位（02）、复杂任务**拆解并行**执行且状态全程可见（03）、靠运行日志**自我进化**（04）。

**本仓库当前只包含设计文档与接口契约，不含实现代码。** 目的是先把语言无关的契约冻结
下来，让后续任何语言、任何端的实现都有共同的、可验证的靶子。

---

## 它解决什么问题

一个 AI 应用迟早会遇到同一组问题，而每个应用都各自糊一遍：

- 同一套系统里，简单请求被送了强模型，复杂请求又被便宜模型敷衍；
- 判断"这是什么任务"靠一串关键词匹配，加一个新概念就得加一个 `if`；
- 长任务没有状态，用户看不到进度，断网重连后一切从头开始；
- 出了问题想知道"哪一步慢、哪一步贵、哪一步经常错"，但日志里什么都没有。

这套契约把这四件事收敛成四个阶段，并给出统一的接口。

## 四阶段

| 阶段 | 做什么 | 关键设计 |
|---|---|---|
| **01 评估** | 把请求变成结构化画像（类型、复杂度、所需能力、成本区间、是否需要澄清） | 每次都跑，但跑在最便宜的档位；信息不足时升档重评；相似请求复用画像 |
| **02 路由** | 从策略菜单里选一条执行路径、一个模型档位、一组工具 | 判断交给 LLM，合法性校验交给无语义的 `PolicyGuard` |
| **03 拆解与执行** | 把复杂任务变成 DAG，动态并行执行，全程上报状态 | 具名流程模板 + 自由拆解；并发度来自计划而非常量；断线可重放 |
| **04 自进化** | 从运行日志里发现配置问题，提出改进建议 | 确定性检测器 + LLM 归因；只改配置与提示词；使用者审批后金丝雀生效 |

## 三个贯穿全局的设计选择

### 1. 决策表是配置，判断是 LLM，守卫是集合代数

`config/routing.policy.yaml` 里每条路由的 `when:` 是一段**散文**。这段散文就是决策表——
因为读它的是 LLM，而散文正是 LLM 最擅长的接口。

LLM 拿到的是由策略渲染出的**菜单**：路由 id、适用条件、允许档位、成本上限。它只能从中
选，没有能力发明一个不存在的路由或一个不存在的模型。

选择之后，`PolicyGuard` 做确定性校验，全部是集合代数：

```
route_id ∈ policy.routes.ids                             否则 → 兜底路由
tier     ∈ routes[route_id].allowed_tiers                否则 → 降到默认档
tool_set ⊆ handler.tool_names                            否则 → 取交集
```

**它从不检视语义。** 没有一句 `if task_type == "receipt"`。这是"新增能力不新增分支"的
实现方式：加一条路由是追加一段 YAML，加一个领域是接入一个 handler。

### 2. 契约里永不出现模型名

handler 通过 `ctx.llm(messages, requires=["vision.extract"])` 请求**能力**，
由调度层把能力映射到档位、把档位映射到实际模型。

handler 连 `deepseek-v4-flash` 这样的字符串都拿不到。这不是靠约定，是靠签名——
它没有那个参数可传。配置里同样只有档位名，模型绑定在 `secret://` 引用后面。

### 3. 自我进化只动配置和提示词，永不动代码

这不是谨慎，是有具体理由的：

- **可验证性不对称**：使用者能判断"这个上限该是 0.05 还是 0.08"，没人能判断一个代码 diff。
  看不懂却批准，比没有批准更糟。
- **可回滚性不对称**：配置回滚即时、彻底、无副作用；代码回滚要重新部署、重启，
  还可能已经跑过迁移。
- **注入升级**：运行日志里含用户文本与模型输出。若分析环节能写代码，就打通了
  "用户内容 → LLM → 可执行代码"的路径。把输出约束成白名单配置键，这条路就不存在。
- **根本不需要**：要调的每一样都能表达为配置。若真需要新的可调项，正确做法是把它做成
  一个配置键——一次性的、由开发者写、经评审的代码改动。

## 从这里开始读

| 你想知道 | 读 |
|---|---|
| 整体分层与核心抽象 | [docs/01-architecture.md](docs/01-architecture.md) |
| 四个阶段具体怎么设计的 | [docs/02-stages.md](docs/02-stages.md) |
| HTTP 接口、SSE 事件、错误码 | [docs/03-http-contract.md](docs/03-http-contract.md) |
| 任务状态机与持久化契约 | [docs/04-state-model.md](docs/04-state-model.md) |
| 图像怎么进系统 | [docs/05-media.md](docs/05-media.md) |
| 自进化怎么工作 | [docs/06-self-evolve.md](docs/06-self-evolve.md) |
| 怎么接一个新的领域 | [docs/07-handler-seam.md](docs/07-handler-seam.md) |
| 配置怎么分层，怎么保证不硬编码 | [docs/08-config-model.md](docs/08-config-model.md) |
| 分几步实现，每步做到什么算完成 | [docs/09-roadmap.md](docs/09-roadmap.md) |
| 实现要满足什么才算合格 | [docs/10-conformance-checklist.md](docs/10-conformance-checklist.md) |
| 多 Agent 怎么协作 | [docs/11-multi-agent.md](docs/11-multi-agent.md) |

契约文件：

| 文件 | 内容 |
|---|---|
| `openapi.yaml` | 全部端点 |
| `schemas/*.json` | 10 个 JSON Schema（含 `run_log.json`） |
| `fixtures/*.{example,invalid}.json` | 20 个校验用例：合法示例必须通过，非法示例必须被拒 |
| `config/routing.policy.yaml` | **决策表本身**：路由、`when:` 散文、档位、阈值、禁区 |
| `config/taxonomy.yaml` | 任务类型词表 |
| `config/agents.yaml` | Agent 角色注册表与协作硬界 |
| `config/pricing.yaml` | 价格表（自进化不可改） |
| `config/evolution/detectors.yaml` | 04 的确定性检测器与阈值 |
| `config/evolution/suggestion_kinds.yaml` | 系统能提出的建议种类（封闭集） |
| `config/flow_templates/receipt_to_entry.yaml` | 具名流程模板示例 |
| `prompts/*.md` | 四个阶段的系统提示词骨架 |
| `prompts/agents/*.md` | 7 个 Agent 角色提示词 |

## 校验契约

```bash
# 1. YAML 语法
python3 -c "import yaml,glob;[yaml.safe_load(open(f)) for f in glob.glob('config/**/*.yaml',recursive=True)+['openapi.yaml']]"

# 2. Schema 自洽：example 必须通过，invalid 必须被拒
#    （见 docs/10-conformance-checklist.md 中给出的校验命令）

# 3. OpenAPI
npx @redocly/cli lint openapi.yaml
```

## 实现（M1–M3 + M5：评估 → 路由 → 拆解 → 并行执行 → 自进化）

`dispatcher/` 是契约的第一个实现，只做到 **01 评估 + 02 路由**，没有执行层。
实现语言是 Python（FastAPI），因为两个现有后端都是 Python，可直接 import；
安卓记账 APP 作为客户端走 HTTP。

**可复现安装**（锁文件 `requirements.lock`，全部精确钉版；换台机器结果一致）：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock   # 第三方依赖，含 dev（pytest / ruff）
.venv/bin/pip install --no-deps -e .         # 本项目自身；--no-deps 不可省，见下

cp .env.example .env                         # 填 LLM_API_KEY / LLM_BASE_URL / 三个档位的模型名

.venv/bin/python -m pytest -q                # 全部测试 + 契约一致性 + 架构不变式
.venv/bin/python -m uvicorn main:app --port 8000
```

`--no-deps` 是必需的：不加的话 pip 会按 `pyproject.toml` 的区间（`>=`）重新解析，
把锁文件钉住的版本升降掉，锁就白锁了。

改动依赖时：编辑 `pyproject.toml`，在**干净 venv** 里 `pip install -e ".[dev]"`
后 `pip freeze` 重新生成 `requirements.lock`（生成与安装的完整说明见文件头注释）。
开发时想装可变区间，仍可用 `.venv/bin/pip install -e ".[dev]"`。

云服务器上的部署（systemd、反代、健康检查、进程内存态清单）见
[docs/12-deployment.md](docs/12-deployment.md)。

**能做什么**

| 端点 | 说明 |
|---|---|
| `POST /v1/tasks` | 评估 → 路由 → 拆解 → 执行。同步内联跑完返回 `200`，异步立刻 `202` |
| `GET /v1/tasks/{id}` | 任务快照，含逐节点状态与进度（对齐 `task_repo.dart` 的词汇） |
| `GET /v1/tasks/{id}/events` | **SSE 事件流**，支持 `Last-Event-ID` 断线重放与心跳 |
| `GET /v1/tasks/{id}/result` | 终态产物；失败或取消时也返回已保留的部分产物 |
| `POST /v1/tasks/{id}/clarify` | 回答澄清问题，任务**从断点继续**（不重跑已完成的工作） |
| `POST /v1/tasks/{id}/cancel` | 取消并**传播到执行中的节点**；幂等 |
| `POST /v1/media` | 上传媒体，得到 `media_id` |
| `GET /v1/capabilities` · `/v1/agents` · `/v1/policy` · `/v1/usage` · `/v1/health` | 自省 |

**自进化**（M5，全部已实现）

| 端点 | 说明 |
|---|---|
| `POST /v1/evolution/analyze` | 跑一次分析：检测器 → LLM 归因 → 确定性校验 → 存入待审批 |
| `GET /v1/evolution/suggestions` | 待审批建议列表（含 `pending_count` 与丢弃率/拒绝率） |
| `POST /v1/evolution/suggestions/{id}/approve` | **使用者本人**批准 → 新策略版本 → 金丝雀 → 热换生效 |
| `POST /v1/evolution/suggestions/{id}/reject` | 拒绝；理由必填，因为理由本身是信号 |
| `GET /v1/policy/versions` · `POST /v1/policy/rollback` | 版本历史与回滚（即时、无副作用） |
| `POST /v1/tasks/{id}/feedback` | 人工质量信号 —— 04 最有价值的输入 |

**不做什么**

- **没有真 handler。** `dispatcher/adapters/example_handlers.py` 是**可执行的示例**
  （日程 + 记账的简化实现），它存在的目的是让管道**真的跑得起来**——
  一套执行器、事件流、并行调度如果没有任何可执行的工具，就只能靠 mock 测试，
  而 mock 测试证明不了接缝能通。真正的记账 handler 在 M4 接入。
- **账本 schema 不在本仓库。** M4 交付的是**接缝**：`handlers/`（在 `dispatcher/` 之外）
  里的参考实现通过 `LedgerPort` 存取账目，你的应用实现那个端口、把真实数据库接进去。
  调度层不知道账本长什么样——这是刻意的，也是 `tests/test_handler_seam.py` 机械验证的事。

**关键实验**（M1 存在的理由）

```bash
.venv/bin/python scripts/experiment_when_edit.py
```

它删掉 `direct_answer.when` 里"涉及本人私有数据不得选本路由"那一句，其余一字不动，
然后对比一批请求的分流结果。**代码一行未改而行为改变**，就证明"决策表是配置"
这条断言成立；不成立则应回炉改设计，而不是带着疑点进入 M2。

## 现状与边界

- **M1 已实现**：`dispatcher/` 下的评估与路由（含守卫、提示词渲染、档位解析、
  内存状态与媒体存储、薄 FastAPI 层），86 个测试，架构不变式的静态检查。
- **M1 未含**：执行层、事件流、真 handler、SDK、自进化。
- **不在本仓库范围**：将其接入的各个消费端产品（记账 APP、日程、学习平台）——
  它们各自拥有自己的 UI、认证、领域数据库与产品逻辑。
- **接口接缝的验收标准**：接入一个新的领域 handler，
  需要改动调度层核心的文件数为 **0**。
