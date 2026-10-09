# Kotlin / Android 移植设计：把 smart-dispatcher 的设计搬进记账 App

> 目标读者：要在 `ai-bookkeeping`（Kotlin + Compose + Hilt）里实现调度核心的开发者。
> 本文移植的是**设计**——`plan / execute / tool` 分层、`Problem` 错误体、tier 路由思路——
> 不是把 Python 翻译成 Kotlin。Python 代码只作为形状参照，契约以
> `schemas/`、`config/`、`docs/` 为准。

---

## 1. 移植范围与硬约束

| 约束 | 含义 | 对设计的影响 |
|---|---|---|
| 不能把 Python 塞进 App | 无 CPython、无嵌入解释器、无 polyglot | 全部核心用 Kotlin 重写，契约靠 fixtures 对拍 |
| 推理走 llama.cpp JNI | 纯端侧、无服务器、无供应商 API | `LLMPort` 的适配器从 `OpenAICompatibleLLM` 换成本地 JNI 实现，**端口签名一个字都不改** |
| 移植的是设计不是实现 | HTTP 层、部署、进程内 SSE 不适用 | 不移植 `dispatcher/interface/`、`main.py`、auth、限流中间件 |
| 接缝验收标准不变 | 接一个新领域 handler，改动调度核心的文件数 = 0 | Kotlin 版同样要能 lint 出领域谓词 |

### 移植（Yes）

- **core**：`errors.py`（Problem 码表）、`plan.py`（ExecutionPlan + 确定性校验）、
  `registry.py`（HandlerRegistry / ToolDecl）、`guard.py`（PolicyGuard）、
  `execution.py`（ToolResult / ToolFailure / ConfirmationRequest）、
  `context.py`（DispatchContext）、`nodeexec.py` + `runner.py`（节点执行与 DAG 并行）、
  `budget / cancel / events / state`（任务状态机、事件、预算、取消）
- **ports**：`llm.py`、`handler.py`、`state.py`、`media.py`（接口本身）
- **stages**：`evaluator` / `router` / `decomposer` 的**形状**（菜单渲染 → LLM 选择 → 守卫收敛）
- **config + prompts**：`config/*.yaml`、`prompts/*.md` 原样进 `assets/`，不改格式

### 不移植（No）

- `dispatcher/interface/`（FastAPI 路由、SSE over HTTP、鉴权、Body 限流）——
  端侧没有跨进程契约面，事件流退化为进程内 `Flow`
- `adapters/openai_compat.py` 的 HTTP/密钥/QPS 限流——被 llama.cpp JNI 适配器替代
- `evolution/`（04 自进化）——**二期**；端侧没有多租户运行日志聚合，先留 `RunLog` 采集口
- `schemas/task_envelope.json` 的传输语义（幂等键、`202` 异步接受）——进程内调用直接 `suspend`

### 保留的可选缝

现有 `core/network/DispatcherClient.kt`（HTTP 客户端）不动。调度核心抽象成一个接口，
两种实现并存：

```kotlin
interface DispatcherEngine {
    suspend fun submit(envelope: TaskEnvelope): TaskAccepted
    fun snapshot(taskId: String): Flow<TaskSnapshot>     // 进程内 = StateStore 变更流
    suspend fun clarify(taskId: String, answer: ClarificationAnswer)
    suspend fun cancel(taskId: String)
}
// LocalDispatcher  —— 本次移植的 Kotlin 核心（端侧 llama.cpp）
// RemoteDispatcher —— 现有 DispatcherClient 包一层（服务端 Python 实现）
```

同一份契约、两个引擎，也正好用服务端 511 个测试的 fixtures 给端侧实现对拍。

---

## 2. 包结构与依赖方向

```
dev.dzsun.bookkeeping.core.dispatcher/
├── contract/      # 数据契约：TaskEnvelope、TaskProfile、RouteDecision、ExecutionPlan、Problem
├── error/         # DispatcherError + ERROR_TABLE（Problem 唯一渲染点）
├── config/        # Policy、Pricing、Taxonomy、PromptLibrary（YAML/MD 从 assets 加载）
├── registry/      # HandlerManifest、ToolDecl、HandlerRegistry（启动期 fail-fast）
├── guard/         # RawDecision → applyGuard → RouteDecision（纯集合代数）
├── stage/         # Evaluator、Router、Decomposer（LLM 判断 + 菜单渲染）
├── plan/          # ExecutionPlan 解析 + 确定性校验（环检测、join 一致性…）
├── exec/          # NodeExecutor、DagRunner、BudgetHandle、CancellationToken、EventBus
├── context/       # DispatchContext（handler 唯一可见的世界）
├── port/          # LLMPort、StateStorePort、MediaStorePort、CapabilityHandler 接口
└── adapter/
    ├── llama/     # LLMPort 的 llama.cpp JNI 实现（tier → 本地模型）
    ├── room/      # StateStorePort 的 Room 实现
    └── file/      # MediaStorePort 的 CacheDir 实现
dev.dzsun.bookkeeping.feature.<领域>/
└── handler/       # 各领域 handler（bookkeeping、stats…）——在 core.dispatcher 之外
```

**依赖方向**：`feature.*.handler → port`；`adapter → port`；`stage/exec/guard → contract`。
core 不 import 任何 adapter、任何 feature、任何 Compose/Android API（`android.*` 只允许出现在
`adapter/` 与 UI 层）。

### 三条强制规则的 Kotlin 版（配 detekt 规则，进 CI）

| 规则 | 拦截什么 | 实现方式 |
|---|---|---|
| **R1** 只有 `registry` 与 `guard` 两个包允许出现路由 `if`，且必须是集合代数 | `if (profile.taskType == "…")` | detekt 自定义规则：`guard/`、`registry/` 内禁止字符串字面量与 `==` 于 `taskType/capability` 上 |
| **R2** `stage/`、`exec/`、`plan/` 不得 import `okhttp/retrofit/HttpURLConnection/Room/Compose` | 传输类型渗入编排层 | detekt `ForbiddenImport` |
| **R3** 出现模型名字符串 / GGUF 路径的位置只有 `adapter/llama/` | handler 硬编码模型 | detekt 正则：`\.gguf`、已知模型 id 字面量只允许出现在 `adapter/llama/` 与 `config/` |

与 Python 侧 `no-literal-policy` / `no-orphan-config` 两条 CI 检查一一对应；配置键孤儿检查
（代码引用的键默认配置里不存在、反之亦然）在 Kotlin 里做成单测：加载 `assets/config/*.yaml`
后双向断言。

---

## 3. `Problem` 错误体（原样移植，最硬的一块契约）

**来源**：`dispatcher/core/errors.py` + `schemas/problem.json`。

### 3.1 码表是协议，不是配置

```kotlin
// error/ProblemCodes.kt —— 与 schemas/problem.json 的 code 枚举一一对应，顺序一致
enum class ProblemCode(
    val status: Int,
    val defaultRetryable: Boolean,
    val title: String,
) {
    UNAUTHORIZED(401, false, "Missing or invalid credentials"),
    INVALID_REQUEST(400, false, "Request does not satisfy the schema"),
    UNSUPPORTED_MEDIA(415, false, "Unsupported media type"),
    MEDIA_TOO_LARGE(413, false, "Media exceeds the size limit"),
    IDEMPOTENCY_CONFLICT(409, false, "Idempotency key reused with a different body"),
    NO_CAPABILITY_MATCH(422, false, "No registered handler satisfies the required capabilities"),
    BUDGET_EXCEEDED(402, false, "Cost exceeds the configured ceiling"),
    POLICY_VIOLATION(422, false, "The guard could not produce a legal decision"),
    HANDLER_ERROR(502, false, "The capability handler raised"),
    UPSTREAM_LLM_ERROR(502, true, "The model provider failed"),
    TIMEOUT(504, true, "Wall-clock budget exceeded"),
    CANCELLED(499, false, "Cancelled by the client"),
    RATE_LIMITED(429, true, "Rate limited"),
    NOT_FOUND(404, false, "Resource not found"),
    RESULT_NOT_READY(409, true, "The task has not reached a terminal state");

    val type: String get() = "https://smart-dispatcher/errors/" + name.lowercase().replace('_', '-')
}
```

wire 名（`snake_case`）通过 `@SerialName` 与 JSON 编解码对齐——**码表写在代码里**，
因为改它等于改契约（可调的量进配置，协议不进）。

### 3.2 有类型的异常 + 单一渲染点

```kotlin
// error/DispatcherError.kt —— 所有对外可见的失败都走这里
class DispatcherError(
    val code: ProblemCode,
    val detail: String = "",
    val retryableOverride: Boolean? = null,   // null = 用码表默认
    val taskId: String? = null,
    val retryAfterMs: Int? = null,
    val context: Map<String, Any?> = emptyMap(),
    val internal: String? = null,             // 只进日志，绝不进响应体
) : Exception(if (detail.isNotEmpty()) "$code: $detail" else code.name) {

    val status: Int get() = code.status
    val retryable: Boolean get() = retryableOverride ?: code.defaultRetryable

    /** 是否一路上抛、不许被降级路径吞掉（配置错误 + 不可重试的上游错误）。 */
    val fatal: Boolean
        get() = code in NON_DEGRADABLE || (code == ProblemCode.UPSTREAM_LLM_ERROR && !retryable)

    /** 唯一渲染点：schemas/problem.json 的实例。 */
    fun toProblem(requestId: String): Problem = Problem(
        type = code.type, title = code.title, status = status,
        code = code, retryable = retryable, requestId = requestId,
        detail = detail.ifEmpty { null }, taskId = taskId,
        retryAfterMs = retryAfterMs, context = context.ifEmpty { null },
    )
}

val NON_DEGRADABLE = setOf(
    ProblemCode.UNAUTHORIZED, ProblemCode.POLICY_VIOLATION,
    ProblemCode.NO_CAPABILITY_MATCH, ProblemCode.INVALID_REQUEST,
    ProblemCode.UNSUPPORTED_MEDIA, ProblemCode.MEDIA_TOO_LARGE,
    ProblemCode.IDEMPOTENCY_CONFLICT,
)
```

四条与 Python 侧逐字对齐的行为（有测试钉住）：

1. **未知 code 编译期不可能**——`enum` 天然封闭，比 Python 的运行期 `ValueError` 更早失败。
2. **`internal` 永不进 `Problem`**：供应商原文、堆栈只进 Timber/Logcat；
   `detail` 只能是本方写的分类文案（审计项「错误体泄露」）。
3. **`fatal` 不可被降级吞掉**：密钥未配置（`policy_violation`）、媒体不存在（`unsupported_media`）
   这类必须当场抛给 UI，不允许"降级到兜底画像 → 任务成功返回"。
4. **请求问题 ≠ 任务问题**：`unsupported_media` / `invalid_request` 在**提交时**就抛，
   不建任务再标失败——否则任务列表会堆满从未开始的"失败任务"，污染失败率。

`ToolFailure.code`（工具级、可携带 `schema_validation_failed` / `upstream_unavailable` 两个
特殊码）与 `ProblemCode` 是**两张表**：前者是节点内的类型化失败，由执行器决定重试/升档；
后者是对外协议。翻译只发生在 `stage → interface` 边界的一处。

---

## 4. `plan / execute / tool` 分层

三层各管一段，handler 只站在最底下一层。

```
plan   ExecutionPlan = DAG（节点、边、join、预算）
        └── 确定性校验：工具名 ∈ 声明集合、依赖无环、join=入边、轮数≤配置上限
execute DagRunner = 按拓扑波次并行（并发度来自计划，不来自常量）
        └── NodeExecutor：tool 一次调用 / agent 有界多轮 / verification 复核
tool   CapabilityHandler.executeTool(toolName, args, ctx): ToolResult
        └── 唯一可执行面。重试、取消、超时、退避、升档、并发、记账全在上面层
```

### 4.1 数据类型（`contract/` + `exec/`）

全部 `@Serializable`，**inbound 严格、outbound 宽容**：

```kotlin
@Serializable
data class Node(
    val subtaskId: String,
    val name: String? = null,
    val handler: String,
    val executor: ExecutorKind,                 // TOOL | AGENT
    val dependsOn: List<String> = emptyList(),
    val tool: String? = null,                   // executor=TOOL
    val role: String? = null, val toolWhitelist: List<String>? = null,
    val maxRounds: Int? = null, val onRoundLimit: OnRoundLimit? = null,
    val inputs: Map<String, JsonElement> = emptyMap(),
    val modelTier: String? = null,
    val requiredCapabilities: List<String> = emptyList(),
    val timeoutMs: Int? = null, val retry: Retry? = null,
    val idempotent: Boolean = false, val optional: Boolean = false,
    val onFailure: OnFailure = FAIL_TASK, val defaultOutput: JsonElement? = null,
)

@Serializable
data class Edge(@SerialName("from") val from: String, val to: String)  // Python 关键字冲突 → alias 同款处理

@Serializable
data class ExecutionPlan(
    val planVersion: String = "1.1",
    val taskId: String, val strategy: Strategy,                     // SINGLE_STEP | DAG
    val source: String = "llm_decomposition",
    val maxParallelism: Int, val revision: Int = 1,
    val nodes: List<Node>, val edges: List<Edge> = emptyList(),
    val join: Map<String, List<String>> = emptyMap(),
    val planBudget: PlanBudget,
)
```

> **Pydantic `extra="forbid"` 的替代**：kotlinx.serialization 默认忽略未知字段。
> inbound 契约（LLM 输出、assets 里的 YAML、跨引擎信封）必须显式拒绝多余键——
> 写一个 `StrictJson.parse<T>()`：先用 `JsonObject.keys` 对照
> `T.serializer().descriptor` 的字段名集（含 `@SerialName`）做差集，非空即
> `DispatcherError(INVALID_REQUEST, "unknown fields: …")`。这一层是幻觉拦截网的一部分，
> 不能省。`fixtures/*.invalid.json` 里"多一个字段必须被拒"的用例全靠它。

工具结果与澄清对（形状照抄 `core/execution.py`）：

```kotlin
@Serializable data class ToolFailure(val code: String, val message: String = "", val retryable: Boolean = false)
@Serializable data class ToolResult(
    val ok: Boolean = true,
    val output: Map<String, JsonElement>? = null,
    val failure: ToolFailure? = null,
    val cost: Double = 0.0,
    val needsConfirmation: ConfirmationRequest? = null,
)
// 特殊 failure.code：schema_validation_failed → 触发档位升级；upstream_unavailable → 降健康档
@Serializable data class ClarificationAnswer(
    val questionId: String? = null, val answerId: String? = null,
    val freeText: String? = null, val edits: Map<String, JsonElement>? = null,
) { val isCancel: Boolean get() = answerId?.trim()?.lowercase() == "cancel" }
```

`isCancel` 由**调度层**处理（任务直接置 `cancelled`），handler 不必各自实现取消。

### 4.2 声明层：HandlerRegistry（启动期 fail-fast）

```kotlin
@Serializable data class ToolDecl(
    val name: String, val description: String? = null,
    val sideEffects: String = "none",                 // none | read | write
    val requiresCapabilities: List<String> = emptyList(),  // 能力名，永不出现模型名
    val idempotent: Boolean = false,
    val inputSchema: JsonObject? = null, val outputSchemaRef: String? = null,
)
@Serializable data class HandlerManifest(
    val handlerId: String, val version: String,
    val capabilities: List<String> = emptyList(),
    val tools: List<ToolDecl> = emptyList(),
    val flows: List<String> = emptyList(),
    val configSchema: JsonObject? = null,
)

class HandlerRegistry(manifests: List<HandlerManifest>) {   // 构造后不可变
    fun bind(handler: CapabilityHandler)                     // 声明与实现 modelDump 必须一致
    fun toolMap(): Map<String, Set<String>>                 // 守卫用
    fun capabilityMap(): Map<String, Set<String>>
}
```

启动期（Hilt `@Provides` 组装 `DispatcherEngine` 时）fail-fast 的五件事，一条不少：
`handlerId` 重复；工具名在 handler 内重复；`sideEffects` 非法；
`requiresCapabilities` 无任何档位能满足；实现与已注册声明不一致（**不一致比缺失更危险：
缺失会报错，不一致会安静地做错事**）。

### 4.3 计划层：ExecutionPlan 确定性校验

`plan/validate.kt`，一个纯函数，判不过不执行：

- 每个 `node.handler` ∈ registry 的 handler 集合；`executor=TOOL` 时
  `node.tool` ∈ 该 handler 的工具集合（**集合成员判定**）
- `edges` 的端点存在、无环（Kahn 拓扑排序）、`join` 键与实际入边两集合相等
- `maxRounds ≤ config.decomposer.max_rounds`，`maxParallelism ≤ 上限`（数值比较，上限来自配置）
- 预算字段 ≥ 0 / ≥ 1 的下界校验

**没有一条关于领域**——加一个新领域不会加一条检查。

### 4.4 执行层：DagRunner + NodeExecutor（Kotlin 版差异最大的地方）

Python 用 `asyncio.gather` + `Semaphore` 波次并行；Kotlin 用结构化并发：

```kotlin
class DagRunner(
    private val nodeExecutor: NodeExecutor,
    private val ledger: BudgetHandle, private val events: EventBus,
) {
    suspend fun run(plan: ExecutionPlan, store: StateStorePort) = coroutineScope {
        val done = ConcurrentHashMap<String, Deferred<ToolResult>>()   // 拓扑波次
        while (true) {
            val wave = plan.readyNodes(done.keys)                       // 入边全部完成
            if (wave.isEmpty()) break
            wave.map { node ->
                async(Dispatchers.IO) { runNodeWithRetry(plan, node, done) }
            }.awaitAll().forEach { /* 记账、发事件、推进 NodeRun 状态 */ }
            ledger.checkLimits().let { if (it.exceeded) cancelAll(it) } // 预算打满 → 取消传播
        }
    }
}
```

- **并发度来自计划**（`plan.maxParallelism` 与 `readyNodes`），不是模块级常量——
  Python 侧 `MAX_CONCURRENT_AGENTS = 9` 那种反模式不许出现。
- **取消是结构化并发的原生能力**：`CancellationToken` 映射到 `Job.cancel()` + 协程内
  `ensureActive()`；任务取消要**传播到执行中的节点**，这在 coroutines 里比 `asyncio` 更自然。
- **agent 多轮循环**：每轮必须输出三分支封闭 JSON（`tool_calls` / `final` / `give_up`），
  轮数上限由代码判、"做完了没有"由模型判；Agent 之间**不自由对话**，只通过 `$ref`
  数据依赖传产物（成本与延迟才有上界，RunLog 才可归因）。
- **超时**：`withTimeoutOrNull(node.timeoutMs)`；超时归 `TIMEOUT(504, retryable=true)`。
- **节点失败三档**（`OnFailure`）：`FAIL_TASK` / `SKIP` / `CONTINUE_WITH_DEFAULT`
  （用 `defaultOutput` 继续）——类型是枚举，不是字符串哨兵。

### 4.5 DispatchContext：handler 唯一可见的世界

```kotlin
data class DispatchContext(
    // 身份
    val taskId: String, val subtaskId: String, val tenantId: String,
    val userId: String, val traceId: String, val routeId: String,
    // 能力（字段即权限清单，加权限 = 加字段 = 一处需评审的 diff）
    val media: MediaResolver, val config: Map<String, JsonElement>,
    val state: StateStorePort, val budget: BudgetHandle,
    val cancellation: CancellationToken, val events: EventBus,
    val clock: () -> Instant,                                // 测试注入固定时刻
    val clarification: ClarificationAnswer? = null,          // 澄清恢复时非空
    // 内部（不给 handler 看，给 llm() 实现用）
    internal val policy: Policy, internal val pricing: Pricing,
    internal val llm: LLMPort, internal val allowedTiers: List<String>,
) {
    /** 稳定幂等 token：重试拿到同一个值，写操作据此保证"重试不重复入账"。 */
    val idempotencyToken: String get() = "$taskId:$subtaskId"

    /** 收能力需求，签名里没有模型名参数——不是靠约定，是靠没地方可写。 */
    suspend fun llm(messages: List<LLMMessage>, requires: List<String> = emptyList()): LLMResult
}
```

`ctx.llm()` 内部完成：能力 → 档位（在 `allowedTiers` 内选满足 `requires` 的）→ 记账 →
事件上报。handler 拿不到模型名、拿不到改预算的入口（`BudgetHandle` 只有 `record/remaining`）。

---

## 5. tier 路由思路（设计的支点）

### 5.1 三层分离，Kotlin 版逐字保留

| 层 | 是什么 | 谁改 |
|---|---|---|
| 策略基底 | `assets/config/*.yaml` + `prompts/*.md`，带版本号 | 人（二期才谈 04） |
| 判断 | LLM 读**菜单**（策略渲染出的路由 id、适用条件、允许档位、成本上限），在封闭集合里选 | LLM |
| 守卫 | `applyGuard()` 集合代数收敛 + 记录每次修正 | 确定性代码 |

不变式：

- **I1 守卫永不检视语义**——没有一句 `if (taskType == "…")`。
- **I2 跨信任边界的每个 LLM 输出都回落到配置派生的枚举再校验一次**
  （提示注入最多只能选到另一条**合法**路由，发明不出非法路由）。
- **I3 `rationale` 与 `policyVersion` 一并持久化**，漂移可审计。

### 5.2 RawDecision 与 RouteDecision 在类型上分开

```kotlin
data class RawDecision(                 // 故意保持"脏"：可能含越界的 routeId、不存在的档位
    val routeId: String = "", val modelTier: String = "", val handler: String? = null,
    val toolSet: List<String> = emptyList(), val executionMode: String = "auto",
    val rationale: String = "", val confidence: Double = 0.0, /* …预算字段… */
)

fun applyGuard(
    policy: Policy, candidate: RawDecision, profile: TaskProfile,
    handlerIds: Set<String>, handlerTools: Map<String, Set<String>>,
    handlerCaps: Map<String, Set<String>>,
    tierHealth: Map<String, HealthState> = emptyMap(),
): GuardOutcome   // → RouteDecision + applied: List<GuardAction> + violations
```

守卫规则（全部集合代数，照抄 `guard.py`）：

```
routeId ∈ policy.routes.ids                       否则 → 兜底路由
tier    ∈ routes[routeId].allowedTiers            否则 → 降到默认档
          （该档供应商熔断 open 时 → allowedTiers 中序数最近的健康档，距离同取更便宜）
toolSet ⊆ handler.toolMap[handler]                否则 → 取交集
requires ⊆ tier.capabilities                      否则 → NO_CAPABILITY_MATCH（不许硬降级）
```

**`guardApplied` 由守卫写入、不由 LLM 输出**：修正记录是可观测信号（某路由推翻率持续偏高
≈ 它的 `when:` 散文已与真实流量不符），RunLog 要带上。

**路由的不对称取舍保留**：LLM 超时或输出非法时**不重试**，直接走兜底路由——
路由失败要退化到"保守但确定"，重试预算留给评估器（画像抽错后面全错，重试在那里才有意义）。

### 5.3 LLMPort：签名即约束

```kotlin
interface LLMPort {
    suspend fun complete(messages: List<LLMMessage>, tier: String,
                         requires: List<String> = emptyList()): LLMResult
    suspend fun generateJson(messages: List<LLMMessage>, tier: String,
                             requires: List<String> = emptyList()): Pair<JsonObject, LLMResult>
    fun setPolicy(policy: Policy)   // 热换档位定义，不重建底层资源
}
// LLMResult(text, tier, modelResolved, latencyMs, inputTokens, outputTokens,
//            reasoningTokens /* 单独记：思考 token 按输出计费 */, finishReason)
```

**没有 model 参数**——调用方给档位名与能力需求，端口验证能力覆盖并解析档位。
`LLMError(kind)` 区分 `transient`（可重试/可降级）与 `auth/quota/bad_request`
（**降级有害**：凭证失效还安静产兜底结果 = 每条都是垃圾，必须一路上抛），
由端口层翻成 `UPSTREAM_LLM_ERROR`，`message` 必须是本方文案。

### 5.4 llama.cpp 适配器（`adapter/llama/`）

替代 `OpenAICompatibleLLM`，唯一发生"档位 → 具体模型"映射的地方：

```kotlin
class LlamaCppLLM @Inject constructor(
    private val policy: Policy, private val settings: LlamaSettings,
    private val bridge: LlamaBridge,          // JNI：load(modelPath) / generate(...)
) : LLMPort {

    private fun resolve(tier: String, requires: List<String>): ResolvedTier {
        val declared = policy.modelTiers[tier]
            ?: throw DispatcherError(POLICY_VIOLATION, "未知档位：$tier")
        val missing = requires.toSet() - declared.capabilities
        if (missing.isNotEmpty())
            throw DispatcherError(NO_CAPABILITY_MATCH,
                "档位 $tier 不具备所需能力：$missing",
                context = mapOf("tier" to tier, "missing_capabilities" to missing))
        return ResolvedTier(tier, settings.modelRef(declared.modelRef))  // modelRef → assets/ 或 files/ 下的 GGUF
    }
}
```

端侧语义对齐（**tier 思路不变，映射的内容变**）：

| 概念 | 服务端 | 端侧 |
|---|---|---|
| tier | 价格/延迟档（如 flash / strong） | 本地模型档：小模型（快、q4、摘要/分类）↔ 大模型（慢、复杂抽取/拆解）；`model_tiers.*.capabilities` 仍是能力集 |
| `model_ref` | `secret://…` → API key + base URL | GGUF 路径 + 上下文长度 + 是否带 mmproj（视觉） |
| `requires=["vision.extract"]` | 档位能力子集判定 | 同样子集判定——不支持视觉的档位直接 `NO_CAPABILITY_MATCH`，**不许让模型做它做不到的事**（会产出看起来合理的假结果） |
| 限流 | QPS 令牌桶 + 并发闸 | 单模型串行队列 + 内存/热节流（JNI 推理吃内存与电池），仍是"速率 + 并发"两道闸 |
| 密钥 | 每次调用时解析 | 无密钥；模型文件存在性/校验和在解析时检查，缺失 → `POLICY_VIOLATION`（**fatal，不许降级**） |

JSON 输出：llama.cpp 没有服务端的 `json_mode`，因此 `generateJson` 在适配器里做
**围栏剥离**（` ```json ` 外壳）+ 首个平衡花括号截取，**不做修复性猜测**——
猜测会让错误数据看起来像正确数据；仍不合法则 `INVALID_REQUEST` 交给上层策略
（评估器重试 / 路由走兜底，与 Python 侧行为一致）。

### 5.5 阶段流程（`stage/`）

```
Evaluator   每次都跑，跑在最便宜档位；信息不足 → 升档重评（有界）；相似请求复用画像
Router      渲染菜单(policyMenu + toolCatalog + hardConstraints) → LLM 出 RawDecision
            → applyGuard → RouteDecision(routeId/tier/handler/toolSet/mode/budget/rationale/policyVersion)
Decomposer  复杂任务 → ExecutionPlan：具名 flow 模板优先，否则 LLM 自由拆解
            → plan.validate() 判不过 → POLICY_VIOLATION（不执行）
```

画像（`TaskProfile`）作为**数据块**进 user 消息、不拼进 system——模型产出同样属于不可信输入。

---

## 6. 状态机、事件与端口实现

### 6.1 状态机（`core/state.py` → `contract/`）

```
received → evaluating → routing → planning → running
    ⇄ awaiting_clarification（停住等人，答复后从断点继续，不重跑已完成节点）
    ⇄ escalated
→ succeeded | failed | cancelled | budget_exceeded | rejected      （TERMINAL，五个）
```

- **同步是优化，异步是基底**端侧版：进程内调用本身就是异步，`submit()` 直接挂
  `Flow<TaskSnapshot>` 观测，不存在"同步内联 vs 202"的分叉。
- `awaiting_clarification` 用 **prior 恢复**：已完成的抽取/查重都还有效，重跑浪费 token
  且用户刚确认过的那份重跑后未必一样。
- 进度 = `TERMINAL_NODE_STATUS` 已完成节点 / 总节点，词汇对齐
  `ai-workmate/app/lib/data/task_repo.dart`（Flutter 侧 1:1 映射，两端共用一套说法）。

### 6.2 端口 → 适配器（Hilt 绑定）

| 端口 | Kotlin 接口要点 | 端侧实现 |
|---|---|---|
| `LLMPort` | 见 §5.3 | `LlamaCppLLM`（JNI） |
| `StateStorePort` | 任务快照读写、事件追加与重放、不假定 Postgres | Room（`@Dao` + `Flow` 变更流），支持跨重启重放 |
| `MediaStorePort` | 媒体字节存取与生命周期；`put` 时校验 MIME ∈ {jpeg,png,webp} 与大小 | `context.cacheDir`，任务终态后按配置清理 |
| `EventSink` / `EventBus` | 事件追加、`Last-Event-ID` 语义的序列号、终态事件封闭集 | 进程内 `MutableSharedFlow` + Room 持久事件表（断线/杀进程后可重放） |
| `Clock` | 注入点 | `Clock.systemUTC()`，测试注入固定时刻 |
| `ConfigProvider` | YAML/MD 加载 | `assets/config` + `assets/prompts`，启动期一次性编译成不可变 `Policy` |

`POLICY_VIOLATION` 的一个端侧特例：密钥未配的问题在端侧变成"GGUF 模型文件缺失/损坏"，
同属 `NON_DEGRADABLE`——**绝不能降级到兜底画像让任务"成功"返回**（这正是 Python 侧
踩过的坑：密钥为空而系统看起来正常工作）。

### 6.3 事件类型（进程内 `Flow` 替代 SSE）

`EventType` 封闭集照抄 `core/events.py`（含 `error`、`heartbeat`），
终态事件 `task.completed / task.failed / task.cancelled`；
`heartbeat` 端侧保留为"UI 存活感"的 tick（可关），序列号与 `Last-Event-ID`
对应 `EventRecord.seq`，杀进程重启后从 Room 表续读。

---

## 7. Python → Kotlin 差异对照（实现时最容易翻车的点）

| Python | Kotlin | 注意 |
|---|---|---|
| Pydantic `extra="forbid"` | `StrictJson.parse<T>()` 显式拒未知键 | kotlinx 默认忽略未知字段，**必须补**，fixtures.invalid 全靠它 |
| `async def` + asyncio 事件循环 | `suspend` + 结构化并发 | 不要引 `Dispatchers.Default` 跑 IO；JNI 推理放 `Dispatchers.IO` 专用单线程队列 |
| `@dataclass` 可变 | `data class` 不可变 + `internal` 字段做权限分层 | DispatchContext 的"字段即权限清单"直接用主构造器表达 |
| `dict[str, Any]` | `Map<String, JsonElement>` | 不用 `Any`：进程内也要保持可序列化（快照/RunLog 要落库） |
| `field(alias="from")` | `@SerialName("from")` + `val from_` | Edge 字段与 `execution_plan.json` 对齐 |
| `frozenset` 常量 | `setOf` 顶层 val | `TERMINAL_STATUSES`、`NON_DEGRADABLE` 等照搬 |
| `datetime` UTC | `java.time.Instant` | `clock: () -> Instant` 是注入点，别在核心里 `Instant.now()` |
| 枚举字符串散落 | `enum class` + `@SerialName` | `ProblemCode`、`ExecutorKind`、`OnFailure`、状态机全部类型化 |
| 模块级常量/单例 | Hilt `@Singleton` 提供 | 但**核心对象不许持有任务状态**（NodeExecutor 构造一次跨任务复用） |
| `str.format` 嵌 JSON 模板踩花括号坑 | `String.replace("{tools}", …)` 同款规避 | 提示词注入工具列表用替换，不用模板格式化 |
| 日志 `logging` | Timber | `internal` 字段只进 Timber |

---

## 8. 分期与验收

| 期 | 交付 | 完成标志（可执行） |
|---|---|---|
| **K1 契约层** | `contract/` + `error/` + `StrictJson`；`schemas/*.json` 的 10 个 schema 与 `fixtures/*` 全部移植为单测 | 20 个 fixture：example 全通过、invalid 全被拒；Problem 码表与 `problem.json` 枚举 diff 为空 |
| **K2 路由层** | `config/` 加载、`registry`、`guard`、`stage.evaluator/router`，LLM 可先用假 LLMPort | 给定画像与假 LLM 输出，`applyGuard` 的修正记录与 Python `test_guard*` 期望一致；detekt R1–R3 规则上线 |
| **K3 执行层** | `plan/` 校验、`exec/DagRunner`、`NodeExecutor`、`DispatchContext`、Room 状态存储 | 端到端跑通一个多节点 DAG（含一个失败节点 `SKIP` + 一次 `awaiting_clarification` 恢复）；取消传播到在飞节点 |
| **K4 llama.cpp 接入** | `adapter/llama` JNI、tier→GGUF 解析、视觉能力判定 | `ctx.llm(requires=["vision.extract"])` 在不支持视觉的档位上抛 `NO_CAPABILITY_MATCH`；两档模型真实推理成功 |
| **K5 记账 handler** | `feature/…/handler` 实现 `executeTool`（记账抽取、查重、落账） | **验收标准：`core/dispatcher` 下改动文件数 = 0**；415/413 在提交时当场抛，不建任务 |
| **K6（二期）** | RunLog 采集、`evolution/` 端侧版（只改 assets 配置与提示词，人审批） | 有建议→审批→回滚的完整链路；改动白名单键之外一律拒绝 |

贯穿各期的验收口径：

1. **对拍**：同一份 `fixtures/`，Python 服务端与 Kotlin 端侧对 `Problem`、`ExecutionPlan`、
   `RouteDecision` 三类输出必须同判（合法/非法、码表、状态码）。
2. **lint 有牙齿**：R1–R3 的 detekt 规则进 CI，红了不许合。
3. **无死配置**：配置键双向孤儿检查（引用了但不存在 / 存在但从不引用）做成单测。
4. **错误契约**：任何路径的失败响应都是合法 `Problem`——没有 `{"detail": ...}` 式的
   拼接正文，没有字符串哨兵错误，`internal` 永不出现在对外数据里。

---

## 附：Python 源文件 → Kotlin 目标速查

| Python | Kotlin 包/文件 |
|---|---|
| `core/errors.py` | `error/ProblemCodes.kt`、`error/DispatcherError.kt` |
| `core/contract.py`（TaskEnvelope/Profile/RouteDecision） | `contract/*.kt` |
| `core/plan.py` | `contract/ExecutionPlan.kt` + `plan/Validate.kt` |
| `core/registry.py` | `registry/HandlerRegistry.kt` |
| `core/guard.py` | `guard/ApplyGuard.kt` |
| `core/execution.py` | `contract/ToolResult.kt` |
| `core/context.py` | `context/DispatchContext.kt` |
| `core/nodeexec.py`、`core/runner.py` | `exec/NodeExecutor.kt`、`exec/DagRunner.kt` |
| `core/budget.py`、`core/cancel.py`、`core/events.py`、`core/state.py` | `exec/BudgetHandle.kt`、`exec/CancellationToken.kt`、`exec/EventBus.kt`、`contract/TaskRecord.kt` |
| `core/policy.py`、`core/pricing.py`、`core/prompts.py`、`core/taxonomy.py` | `config/Policy.kt` 等（assets 加载） |
| `ports/llm.py`、`ports/handler.py`、`ports/state.py`、`ports/media.py` | `port/LLMPort.kt` 等 |
| `stages/{evaluator,router,decomposer,direct}.py` | `stage/*.kt` |
| `adapters/openai_compat.py` | `adapter/llama/LlamaCppLLM.kt` |
| `adapters/memory_state.py` / `memory_media.py` | `adapter/room/RoomStateStore.kt`、`adapter/file/FileMediaStore.kt` |
| `dispatcher/interface/*`、`main.py` | **不移植**（端侧无 HTTP 面；对应能力由 `DispatcherEngine` 进程内接口承担） |
