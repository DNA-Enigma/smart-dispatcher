"""执行计划：模型 + **确定性校验**。

校验这一层是**幻觉的拦截网**。LLM 会编出不存在的工具名、画成环的依赖、
让 join 与入边对不上、把并发度提到超过上限。这些都不用"更聪明的提示词"来解决——
它们全都是可以用集合运算与图算法判定的，因此在这里一次判完，判不干净就不执行。

关键在于这些检查里**没有一条是关于领域的**：

* "工具名在 handler 声明的集合里" —— 集合成员判定
* "依赖存在且无环" —— 图算法
* "join 与入边一致" —— 两集合相等
* "轮数不超过上限" —— 数值比较，上限来自配置

所以加一个新领域不会加一条检查。这是"校验与语义无关"在计划层的体现。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .agents import AgentSpec
from .errors import DispatcherError
from .policy import Policy
from .registry import HandlerRegistry

Strict = ConfigDict(extra="forbid")

ExecutorKind = Literal["tool", "agent"]
OnFailure = Literal["fail_task", "skip", "continue_with_default"]
OnRoundLimit = Literal["fail_task", "accept_partial", "escalate_and_retry"]


class Retry(BaseModel):
    model_config = Strict
    max: int = Field(ge=0)
    backoff_ms: int = Field(ge=0)


class Verification(BaseModel):
    model_config = Strict
    mode: Literal["none", "self_check", "independent_review"]
    reviewers: int | None = Field(default=None, ge=2)
    reviewer_role: str | None = None
    arbiter_role: str | None = None
    on_disagreement: Literal["arbiter_decides", "ask_user", "fail_task"] | None = None
    max_cost_multiplier: float | None = Field(default=None, ge=1)


class Node(BaseModel):
    model_config = Strict

    subtask_id: str
    name: str | None = None
    handler: str
    executor: ExecutorKind
    depends_on: list[str] = Field(default_factory=list)

    # executor=tool
    tool: str | None = None
    # executor=agent
    role: str | None = None
    tool_whitelist: list[str] | None = None
    max_rounds: int | None = Field(default=None, ge=1)
    on_round_limit: OnRoundLimit | None = None

    inputs: dict[str, Any] = Field(default_factory=dict)
    output_schema_ref: str | None = None
    model_tier: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    timeout_ms: int | None = Field(default=None, ge=1)
    retry: Retry | None = None
    idempotent: bool = False
    optional: bool = False
    verification: Verification | None = None
    on_failure: OnFailure = "fail_task"
    default_output: Any = None


class Edge(BaseModel):
    # ``from`` 是 Python 关键字，因此字段名用 ``from_`` + alias。
    # 序列化时 by_alias=True 会写回 ``from``，与 schemas/execution_plan.json 一致。
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str


class PlanBudget(BaseModel):
    model_config = Strict
    max_cost: float = Field(ge=0)
    max_wall_ms: int = Field(ge=1)
    max_llm_calls: int = Field(ge=0)


class ExecutionPlan(BaseModel):
    model_config = Strict

    plan_version: str = "1.1"
    task_id: str
    strategy: Literal["single_step", "dag"]
    source: str = "llm_decomposition"
    max_parallelism: int = Field(ge=1)
    revision: int = Field(default=1, ge=1)
    nodes: list[Node]
    edges: list[Edge] = Field(default_factory=list)
    join: dict[str, list[str]] = Field(default_factory=dict)
    plan_budget: PlanBudget

    # -- 派生 ------------------------------------------------------------
    def node(self, subtask_id: str) -> Node | None:
        return next((n for n in self.nodes if n.subtask_id == subtask_id), None)

    @property
    def node_ids(self) -> list[str]:
        return [n.subtask_id for n in self.nodes]

    def dependents(self, subtask_id: str) -> list[str]:
        return [e.to for e in self.edges if e.from_ == subtask_id]

    def in_edges(self, subtask_id: str) -> list[str]:
        return [e.from_ for e in self.edges if e.to == subtask_id]

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


# ---------------------------------------------------------------------------
def validate_plan(
    plan: ExecutionPlan,
    *,
    policy: Policy,
    registry: HandlerRegistry,
    tool_set: list[str],
    agents: AgentSpec | None = None,
    decision_budget: Any | None = None,
) -> list[str]:
    """返回违规清单（空表示合法）。**不抛错**，好让上层决定是重规划还是失败。

    调用方拿到非空清单时应当先试有界重规划（``decomposer.max_replans``），
    再失败——因为 LLM 的第一次拆解有杂质是常态，直接判死太脆。

    **``tool_set`` 不参与节点级的子集判定。** 这一点反直觉，值得说清楚：

    路由决策里的 ``tool_set`` 是**路由 LLM 产出的**。把它当作节点工具的白名单上界，
    等于把模型输出当成安全边界——方向正好反了。而且它必然偏窄：路由发生时还
    不知道会命中哪张流程模板，模板里的 agent 需要的工具（比如放大重看用的
    ``crop_and_zoom``）根本没机会被列进去，于是合法模板会被判成非法。
    （这个问题是被 ``test_template_hit_avoids_llm_decomposition`` 抓出来的。）

    真正的边界必须是人写的配置，一共两道，都在这里检查：

    * 工具的 ``handler`` 声明（``HandlerRegistry``）—— 谁能提供这个工具；
    * 角色的 ``allowed_tools``（``config/agents.yaml``）—— 这个角色被允许用哪些。

    两者都是人写的、可评审的、进版本控制的。``tool_set`` 仍然有用——它记录
    路由的**意图**，进 RunLog 供 04 分析；只是它不该有强制力。
    """
    v: list[str] = []
    ids = plan.node_ids

    # **空计划先判，而且只判这一条。** 一个节点都没有时，下面每一条检查
    # （依赖存在性、工具合法性、join 一致性、预算）都在空集上恒真——它们会全部
    # 静默通过，只有 `strategy=single_step 但节点数为 0` 会亮，而那条消息把
    # "根本没有步骤"说成了"策略与节点数不匹配"，排查的人会去翻策略。
    #
    # 这是 P0-1c 的现场：拆解器返回了空 nodes，消费端收到的却是一个指向
    # strategy 的错误。空计划是**模型没能产出计划**，不是计划写错了——
    # 两者该说的话不一样，所以这里单独说，并且不再往下走。
    if not plan.nodes:
        return [
            "计划没有任何节点：拆解器没有产出任何可执行步骤（LLM 拆解返回了空 nodes）。"
            "这与节点内容是否合法无关——是这一版拆解整体为空，需要重新拆解。"
        ]

    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        v.append(f"subtask_id 重复：{dupes}")
    if plan.strategy == "single_step" and len(plan.nodes) != 1:
        v.append(f"strategy=single_step 但节点数为 {len(plan.nodes)}")
    if plan.max_parallelism > policy.limits.max_parallelism:
        v.append(
            f"max_parallelism={plan.max_parallelism} 超过 limits.max_parallelism="
            f"{policy.limits.max_parallelism}"
        )

    id_set = set(ids)

    # -- 依赖存在性 + 无环 ----------------------------------------------
    for n in plan.nodes:
        for dep in n.depends_on:
            if dep not in id_set:
                v.append(f"节点 {n.subtask_id} 依赖不存在的节点 {dep}")
        if n.subtask_id in n.depends_on:
            v.append(f"节点 {n.subtask_id} 依赖自己")
    cycle = _find_cycle(ids, plan.edges)
    if cycle:
        v.append(f"依赖成环：{' -> '.join(cycle)}")

    # -- 每个节点的工具/角色合法性 ---------------------------------------
    agent_nodes = 0
    total_rounds = 0
    for n in plan.nodes:
        declared_tools = registry.tool_names(n.handler)
        if not declared_tools:
            v.append(f"节点 {n.subtask_id} 的 handler {n.handler!r} 未注册或未声明任何工具")
            continue

        if n.executor == "tool":
            if not n.tool:
                v.append(f"节点 {n.subtask_id} 的 executor=tool 但未给 tool")
            elif n.tool not in declared_tools:
                v.append(
                    f"节点 {n.subtask_id} 用了 handler {n.handler} 未声明的工具 {n.tool!r}"
                )
        else:
            agent_nodes += 1
            if not n.role:
                v.append(f"节点 {n.subtask_id} 的 executor=agent 但未给 role")
            elif agents is not None and n.role not in agents.role_ids:
                v.append(f"节点 {n.subtask_id} 用了未定义的角色 {n.role!r}")
            elif agents is not None:
                # 两级白名单的第一级：节点白名单 ⊆ 角色允许集
                extra = set(n.tool_whitelist or []) - set(agents.allowed_tools(n.role))
                if extra:
                    v.append(f"节点 {n.subtask_id} 的白名单超出角色 {n.role} 允许集：{sorted(extra)}")
            if n.max_rounds:
                total_rounds += n.max_rounds
                if n.max_rounds > policy.limits.max_agent_rounds:
                    v.append(
                        f"节点 {n.subtask_id} 的 max_rounds={n.max_rounds} 超过 "
                        f"limits.max_agent_rounds={policy.limits.max_agent_rounds}"
                    )
            for t in n.tool_whitelist or []:
                if t not in declared_tools:
                    v.append(f"节点 {n.subtask_id} 的白名单含未声明工具 {t!r}")

    if agent_nodes > policy.limits.max_agent_nodes_per_plan:
        v.append(
            f"agent 节点数 {agent_nodes} 超过 limits.max_agent_nodes_per_plan="
            f"{policy.limits.max_agent_nodes_per_plan}"
        )
    if total_rounds > policy.limits.max_total_rounds_per_task:
        # 这一条是防"每一步都合规但整体炸掉"：单看每个节点都在上限内，
        # 十个节点乘起来就不对了。
        v.append(
            f"总轮数 {total_rounds} 超过 limits.max_total_rounds_per_task="
            f"{policy.limits.max_total_rounds_per_task}"
        )

    # -- output_schema_ref 必须来自注册表 ---------------------------------
    # 这是**结构性防线**，与 ``nodeexec`` 里那道执行期兜底成对存在：那边保证
    # "不在集合里的引用绝不进 system 槽"，这边保证"不合法的计划根本走不到执行"。
    # 只有一道都不够——执行期兜底是静默降级（模型看到一句"未声明具体结构"），
    # 计划期拒绝才说得清是**这一版计划的错**，而且它进的是重规划的回灌信息，
    # 模型能据此改对；反过来只有计划期检查，则任何绕过拆解器的构造（测试、
    # 内嵌调用、模板）仍会把模型产出直接送进 system。
    #
    # 集合来自人写的工具声明（``HandlerRegistry.schema_refs``），不是模型输出——
    # 这正是关键：白名单的成员名单不能由被白名单约束的一方提供。
    known_schemas = registry.schema_refs
    for n in plan.nodes:
        if n.output_schema_ref and n.output_schema_ref not in known_schemas:
            v.append(
                f"节点 {n.subtask_id} 的 output_schema_ref {n.output_schema_ref!r} "
                f"不在注册表声明的 schema 集合里：{sorted(known_schemas)}"
            )

    # -- join 与入边一致 -------------------------------------------------
    for target, preds in plan.join.items():
        if target not in id_set:
            v.append(f"join 指向不存在的节点 {target}")
            continue
        if set(preds) != set(plan.in_edges(target)):
            v.append(
                f"join[{target}]={sorted(preds)} 与入边 {sorted(plan.in_edges(target))} 不一致"
            )

    # -- 预算 -------------------------------------------------------------
    if decision_budget is not None:
        if plan.plan_budget.max_cost > decision_budget.max_cost + 1e-9:
            v.append(
                f"计划预算 {plan.plan_budget.max_cost} 超过决策预算 {decision_budget.max_cost}"
            )
        if plan.plan_budget.max_wall_ms > decision_budget.max_wall_ms:
            v.append("计划墙钟预算超过决策预算")

    return v


def _find_cycle(ids: list[str], edges: list[Edge]) -> list[str] | None:
    """DFS 找环。返回环上的节点序列，没有则 ``None``。"""
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for e in edges:
        adj.setdefault(e.from_, []).append(e.to)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(ids, WHITE)
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = GRAY
        stack.append(u)
        for w in adj.get(u, ()):  # type: ignore[arg-type]
            if color.get(w, WHITE) == GRAY:
                idx = stack.index(w)
                return stack[idx:] + [w]
            if color.get(w, WHITE) == WHITE:
                found = dfs(w)
                if found:
                    return found
        stack.pop()
        color[u] = BLACK
        return None

    for i in ids:
        if color[i] == WHITE:
            found = dfs(i)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# $ref 解析
# ---------------------------------------------------------------------------
def resolve_inputs(inputs: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    """解析 ``{"$ref": "extract.amount"}`` 形式的绑定。

    解析失败是**确定性错误**，与 LLM 无关：说明计划引用了不存在的上游字段。
    这类问题在计划校验阶段抓不干净（要等上游真的产出了才知道），
    因此在这里抛成有类型的错误——它会在事件流里留下痕迹，而不是变成一个 None
    悄悄传下去。
    """
    return {k: _resolve(v, scope, k) for k, v in inputs.items()}


def _resolve(value: Any, scope: dict[str, Any], path: str) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            ref = str(value["$ref"])
            cur: Any = scope
            for part in ref.split("."):
                # 支持 media[0] 这类带下标的写法
                name, _, idx = part.partition("[")
                if isinstance(cur, dict) and name in cur:
                    cur = cur[name]
                else:
                    raise DispatcherError(
                        "handler_error",
                        f"节点输入引用无法解析：{ref!r}（在 {name!r} 处断了）",
                        context={"ref": ref, "field": path, "missing": name},
                    )
                if idx:
                    n = int(idx.rstrip("]"))
                    if not isinstance(cur, list) or n >= len(cur):
                        raise DispatcherError(
                            "handler_error",
                            f"节点输入引用无法解析：{ref!r}（下标 {n} 越界）",
                            context={"ref": ref, "field": path},
                        )
                    cur = cur[n]
            return cur
        return {k: _resolve(v, scope, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, scope, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


__all__ = [
    "Edge", "ExecutionPlan", "Node", "PlanBudget", "Retry", "Verification",
    "resolve_inputs", "validate_plan",
]
