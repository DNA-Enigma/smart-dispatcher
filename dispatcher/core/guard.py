"""PolicyGuard —— 决策的确定性校验。

这是整套设计的支点，因此它的边界值得写清楚：

**守卫只做集合代数。** ``in`` / ``issubset`` / ``min`` / ``clamp``。它**永不检视语义**——
没有一句 ``if task_type == "..."``。所有含义都活在 ``when:`` 那段散文里（给 LLM 读）
和编辑者的头脑中。这条纪律的收益是：**加一个领域概念不会加一个分支。**

**LLM 的每个越界输出都回落到配置派生的集合再校验一次。** 于是提示注入最多只能
选到另一条*合法*路由，无法发明非法路由。

**``guard.applied`` 由这里写入，不是 LLM 的输出。** 它记录每一次修正，
使"LLM 反复被纠正"从一个隐性问题变成一个可观测信号——某条路由的守卫推翻率
持续偏高，几乎总意味着它的 ``when:`` 散文已经与真实流量不符。这正是 04
自进化提出 ``route_guidance_patch`` 建议的依据。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .contract import (
    DecisionBudget,
    EscalationRule,
    GuardAction,
    GuardReport,
    RouteDecision,
    TaskProfile,
)
from .policy import Policy, Route

# 供应商健康状态。open 表示该档位的供应商熔断打开，此时守卫会确定性地
# 落到 allowed_tiers 中最近的健康档位。
HealthState = str  # "closed" | "half_open" | "open"
HEALTHY_STATES = frozenset({"closed", "half_open"})


@dataclass
class RawDecision:
    """LLM 给出的原始选择，**尚未校验**。

    这个类故意保持"脏"：它承载的可能是越界的路由 id、未声明的工具名、
    不存在的档位。把它和 ``RouteDecision`` 分开，是为了让"校验"这件事在类型上
    就可见——你不可能把 RawDecision 直接交给执行器。
    """

    route_id: str = ""
    model_tier: str = ""
    handler: str | None = None
    tool_set: list[str] = field(default_factory=list)
    execution_mode: str = "auto"
    max_cost: float | None = None
    max_wall_ms: int | None = None
    max_llm_calls: int | None = None
    escalation_to_tier: str | None = None
    escalation_on: list[str] = field(default_factory=list)
    parallelism_hint: int | None = None
    rationale: str = ""
    confidence: float = 0.0


@dataclass
class GuardOutcome:
    decision: RouteDecision
    # 记录被拒的具体原因，供指标与排查使用（不进入 RouteDecision 的 violations 之外的通道）
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
def _nearest_healthy_tier(
    policy: Policy, allowed: list[str], health: Mapping[str, HealthState]
) -> str | None:
    """在 allowed 中找与"当前档位序数"最近的健康档位。

    纯序数比较：先看距离，距离相同取更便宜的那个（序数更小）。
    这里没有任何关于"哪个档位更好"的语义判断——那由策略里的档位顺序表达。
    """
    order = policy.tier_order
    healthy = [t for t in allowed if health.get(t, "closed") in HEALTHY_STATES]
    if not healthy:
        return None
    return min(healthy, key=lambda t: order.get(t, 0))


# ---------------------------------------------------------------------------
def apply_guard(
    policy: Policy,
    candidate: RawDecision,
    *,
    profile: TaskProfile,
    handler_ids: frozenset[str],
    handler_tools: Mapping[str, frozenset[str]],
    constraints_max_cost: float | None = None,
    constraints_max_wall_ms: int | None = None,
    mode_preference: str = "auto",
    tier_health: Mapping[str, HealthState] | None = None,
) -> GuardOutcome:
    """把 LLM 的原始选择收敛成一个合法决策。

    参数里的 ``handler_ids`` / ``handler_tools`` 来自 HandlerRegistry，
    因此本函数**不依赖具体的 handler 实现**，只依赖它声明的集合。
    """
    health: Mapping[str, HealthState] = tier_health or {}
    applied: list[GuardAction] = []
    violations: list[str] = []
    notes: list[str] = []

    # ---- 1. 路由：必须是一条真实存在的路由 ------------------------------
    route: Route | None = policy.route(candidate.route_id)
    original_route_id = candidate.route_id or None
    if route is None:
        violations.append("route_not_in_policy")
        notes.append(f"LLM 选择的路由 {candidate.route_id!r} 不在策略中")
        applied.append("route_fallback")
        route = policy.route(policy.fallback.route_id)
        if route is None:  # 策略自洽性校验已保证不会发生，此处仅防御
            raise AssertionError("fallback.route_id 不是一条真实路由：策略校验被绕过")
    fallback_used = "route_fallback" in applied

    # ---- 2. handler：路由要求时必须已注册 ---------------------------------
    handler = candidate.handler

    # 模型没给 handler 时，**从 tool_set 推导**。
    #
    # 这不是猜：工具名是全局唯一的，而每个 handler 声明了自己的工具集，
    # 因此"这组工具归谁"是一次纯子集判定。拿真实模型跑出来的现象是：
    # 它正确选出了路由与工具（query_ledger / compare_entries 都对），
    # 但把 handler 留空——**因为它本来就是可推导的冗余信息**，
    # 问模型要一个能推出来的字段，它就会漏。
    #
    # 这跟别处"能推导的就让代码推"是同一条原则（complexity.band、decompose 都如此），
    # 我这里一开始违反了它。只有当这组工具跨多个 handler、无法唯一确定时才失败。
    if handler is None and candidate.tool_set:
        owners = [
            hid for hid, tools in handler_tools.items() if set(candidate.tool_set) <= tools
        ]
        if len(owners) == 1:
            handler = owners[0]
            notes.append(f"handler 由 tool_set 推导得出：{handler}")
        elif len(owners) > 1:
            notes.append(f"tool_set 同时属于多个 handler {sorted(owners)}，无法唯一确定")

    if route.requires_handler:
        if handler is None or handler not in handler_ids:
            violations.append("handler_not_registered")
            notes.append(f"路由 {route.id} 需要一个已注册的 handler，但得到 {handler!r}")
            # 退到兜底路由；兜底路由同样要求 handler，若它也不能满足，任务会被
            # 上层以 no_capability_match 拒绝——这里不替它做决定。
            applied.append("handler_fallback")
            route = policy.route(policy.fallback.route_id) or route
            fallback_used = True
            handler = candidate.handler if candidate.handler in handler_ids else None
    else:
        # 不需要 handler 的路由：把 handler 与工具集清空。
        # 这不是"领域判断"，是结构性约束——direct_llm 路径上根本没有 handler 的概念。
        handler = None

    # ---- 3. 工具集：必须是该 handler 已声明工具的子集 ---------------------
    tool_set: list[str] = []
    if handler is not None:
        declared = handler_tools.get(handler, frozenset())
        requested = [t for t in candidate.tool_set]
        tool_set = [t for t in requested if t in declared]
        if len(tool_set) != len(requested):
            applied.append("tool_set_intersected")
            rejected = [t for t in requested if t not in declared]
            violations.append("tool_not_declared_by_handler")
            notes.append(f"剔除 handler {handler} 未声明的工具：{rejected}")
    elif candidate.tool_set:
        violations.append("tool_set_without_handler")
        # 分两种情形说清楚，否则排查时会以为"这条路由本来不需要 handler"
        if route.requires_handler:
            notes.append(
                f"路由 {route.id} 需要 handler，但 tool_set {candidate.tool_set} "
                f"无法归属到唯一 handler，工具集被丢弃"
            )
        else:
            notes.append(f"路由 {route.id} 不需要 handler，丢弃 LLM 给出的工具集")

    # ---- 4. 档位：必须在路由允许的集合内，且供应商健康 ---------------------
    original_tier = candidate.model_tier or None
    tier = candidate.model_tier
    if tier not in route.allowed_tiers:
        violations.append("tier_not_allowed")
        notes.append(f"档位 {tier!r} 不在路由 {route.id} 的 allowed_tiers 内")
        tier = route.default_tier
        applied.append("tier_downgraded")

    if tier in policy.model_tier_ids and health.get(tier, "closed") not in HEALTHY_STATES:
        alt = _nearest_healthy_tier(policy, route.allowed_tiers, health)
        if alt is not None and alt != tier:
            notes.append(f"档位 {tier} 的供应商熔断，落到健康档位 {alt}")
            upgraded = policy.tier_order.get(alt, 0) > policy.tier_order.get(tier, 0)
            applied.append("tier_upgraded_health" if upgraded else "tier_downgraded")
            tier = alt

    # ---- 5. 预算：取所有来源的更严者 -------------------------------------
    caps = [route.max_cost]
    if constraints_max_cost is not None:
        caps.append(constraints_max_cost)
    if candidate.max_cost is not None:
        caps.append(candidate.max_cost)
    max_cost = min(caps)
    if candidate.max_cost is not None and max_cost < candidate.max_cost:
        applied.append("budget_clamped")
        notes.append(f"成本上限由 {candidate.max_cost} 收敛到 {max_cost}")

    wall_candidates = [policy.limits.default_task_wall_ms]
    if constraints_max_wall_ms is not None:
        wall_candidates.append(constraints_max_wall_ms)
    if candidate.max_wall_ms is not None:
        wall_candidates.append(candidate.max_wall_ms)
    max_wall_ms = min(wall_candidates)

    # max_llm_calls 没有策略级默认值：由候选给出，缺省为 0，表示"不按调用次数额外设限"
    # （成本上限仍然是有效约束）。不在这里发明一个次数上限——那会是一条拍脑袋的规则。
    max_llm_calls = max(0, int(candidate.max_llm_calls or 0))
    budget = DecisionBudget(
        max_cost=max_cost,
        max_wall_ms=max_wall_ms,
        max_llm_calls=max_llm_calls,
    )

    # ---- 6. 模式仲裁：async 是基底，sync 是优化 ---------------------------
    # 优先级与配置分层一致：**请求 > 路由配置 > 模型建议**。
    #   结构上不可能 sync（要拆解）      → async
    #   调用方明确要 async                → async
    #   调用方明确要 sync                 → sync（路由与评估器的建议都只是建议，
    #                                        不能推翻调用方的明确主张）
    #   调用方写 auto                     → 路由配置或评估器建议任一倾向 async 即 async
    # 最后一条里 async 是安全方向：它只是慢一点，而错误地选 sync 会中途超时。
    mode = "sync"
    mode_reason: str | None = None
    if route.path == "decompose":
        mode = "async"
        mode_reason = "route_requires_decomposition"
    elif mode_preference == "async":
        mode = "async"
        mode_reason = "request_prefers_async"
    elif mode_preference == "sync":
        mode = "sync"
        mode_reason = None
    elif route.mode == "async":
        mode = "async"
        mode_reason = "route_defaults_to_async"
    elif profile.recommended_mode == "async":
        mode = "async"
        mode_reason = "evaluator_recommends_async"

    # 调用方明确要了 sync 却拿到 async —— 这是"被提升"，必须留痕。
    # 调用方写 auto 时不算提升：它没有主张，async 只是路由本身的默认或建议。
    if mode_preference == "sync" and mode == "async":
        applied.append("mode_promoted")
        violations.append("requested_sync_downgraded_to_async")

    # ---- 组装 -------------------------------------------------------------
    escalation: EscalationRule | None = None
    to_tier = candidate.escalation_to_tier or policy.escalation_target(tier)
    if to_tier not in policy.model_tier_ids:
        violations.append("escalation_target_not_a_tier")
        to_tier = tier
    escalation = EscalationRule(
        on=candidate.escalation_on,
        to_tier=to_tier,
        # 硬上限来自策略，不受 LLM 影响——否则升级会成环
        max_escalations=policy.thresholds.max_escalations,
    )
    if candidate.escalation_to_tier is not None and candidate.escalation_to_tier != to_tier:
        violations.append("escalation_target_replaced")

    decision = RouteDecision(
        policy_version=policy.policy_version,
        route_id=route.id,
        path=route.path,
        model_tier=tier,
        vision_tier=route.vision_tier,
        handler=handler,
        tool_set=tool_set,
        execution_mode=mode,  # type: ignore[arg-type]
        # decompose 由路由的 path 派生，而不是采信 LLM 的声明——
        # 单一事实来源，两者不可能不一致。
        decompose=route.path == "decompose",
        parallelism_hint=candidate.parallelism_hint,
        budget=budget,
        escalation_rule=escalation,
        rationale=candidate.rationale,
        confidence=candidate.confidence,
        guard=GuardReport(
            applied=applied,
            original_tier=original_tier,
            original_route_id=original_route_id,
            original_mode=candidate.execution_mode if candidate.execution_mode in {"sync", "async"} else None,  # type: ignore[arg-type]
            fallback_used=fallback_used,
            violations=violations,
        ),
        mode_change_reason=mode_reason,
    )
    return GuardOutcome(decision=decision, notes=notes)


__all__ = ["GuardOutcome", "HealthState", "RawDecision", "apply_guard"]
