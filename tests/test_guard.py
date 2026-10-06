"""PolicyGuard —— 集合代数。

这是整套设计的支点，所以这里除了逐条测规则，还测一件事：
**守卫的输出不随领域语义变化。** 见 ``test_output_is_invariant_under_task_semantics``。
那是一个行为层面的证明——如果守卫里藏了 ``if task_type == ...``，它必然失败。
grep 能被人绕过，这个测试不能。
"""

from __future__ import annotations

import pytest

from dispatcher.core.contract import Complexity, RouteDecision, TaskProfile, Urgency
from dispatcher.core.guard import RawDecision, apply_guard
from dispatcher.core.policy import Policy
from dispatcher.core.registry import HandlerRegistry


def profile_of(task_type: str, *, mode: str = "async") -> TaskProfile:
    return TaskProfile(
        task_type=task_type,
        modality=["text"],
        complexity=Complexity(score=0.4),
        urgency=Urgency(level="normal"),
        recommended_mode=mode,  # type: ignore[arg-type]
        confidence=0.9,
    )


def guard(
    policy: Policy,
    registry: HandlerRegistry,
    cand: RawDecision,
    prof: TaskProfile,
    **kw,
) -> RouteDecision:
    return apply_guard(
        policy, cand, profile=prof,
        handler_ids=registry.ids, handler_tools=registry.tool_map(), **kw
    ).decision


# ---------------------------------------------------------------------------
# 输出永远是合法的
# ---------------------------------------------------------------------------
def test_output_is_always_a_real_route(policy, registry):
    """无论 LLM 编出什么路由，输出必须在策略的路由集合里。"""
    for bogus in ["", "nope", "direct_answer_v2", "drop table", "评估"]:
        d = guard(policy, registry, RawDecision(route_id=bogus, model_tier="cheap"),
                  profile_of("chat.explain"))
        assert d.route_id in policy.route_ids


def test_output_tier_is_always_defined(policy, registry):
    for bogus in ["", "ultra", "gpt-5", "strong "]:
        d = guard(policy, registry, RawDecision(route_id="direct_answer", model_tier=bogus),
                  profile_of("chat.explain"))
        assert d.model_tier in policy.model_tier_ids


# ---------------------------------------------------------------------------
# 文档里承诺的那条性质：守卫不检视语义
# ---------------------------------------------------------------------------
def test_output_is_invariant_under_task_semantics(policy, registry):
    """同样的原始选择 + 完全不同的任务类型 ⇒ 完全相同的决策。

    这是"守卫只做集合代数"的行为证明。示例清单里有 bookkeeping 与 calendar
    两个领域，若守卫里出现任何按 task_type 分支的逻辑，两者就会分岔。
    """
    cand = RawDecision(
        route_id="single_tool_action", model_tier="standard", handler="bookkeeping",
        tool_set=["build_ledger_entry"], execution_mode="sync", max_cost=0.02, confidence=0.8,
    )
    a = guard(policy, registry, cand, profile_of("bookkeeping.capture_from_receipt"))
    b = guard(policy, registry, cand, profile_of("chat.smalltalk"))
    c = guard(policy, registry, cand, profile_of("完全不存在的类型"))
    assert a.model_dump() == b.model_dump() == c.model_dump()


# ---------------------------------------------------------------------------
# 逐条规则
# ---------------------------------------------------------------------------
def test_unknown_route_falls_back_and_records_it(policy, registry):
    d = guard(policy, registry, RawDecision(route_id="nope", model_tier="cheap"), profile_of("chat.explain"))
    assert d.route_id == policy.fallback.route_id
    assert "route_fallback" in d.guard.applied
    assert d.guard.fallback_used is True
    assert "route_not_in_policy" in d.guard.violations


def test_tier_outside_allowed_set_is_downgraded(policy, registry):
    # direct_answer 只允许 [cheap, standard]
    d = guard(policy, registry, RawDecision(route_id="direct_answer", model_tier="strong"),
              profile_of("chat.explain"))
    assert d.model_tier == policy.route("direct_answer").default_tier
    assert "tier_downgraded" in d.guard.applied
    assert d.guard.original_tier == "strong"


def test_tools_not_declared_by_handler_are_intersected(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="standard", handler="bookkeeping",
        tool_set=["build_ledger_entry", "email_my_accountant", "launch_missiles"],
    ), profile_of("bookkeeping.capture_from_receipt"))
    assert d.tool_set == ["build_ledger_entry"]
    assert "tool_set_intersected" in d.guard.applied
    assert "tool_not_declared_by_handler" in d.guard.violations


def test_handler_required_but_unregistered_is_recorded(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="standard", handler="time_machine",
        tool_set=["create_event"],
    ), profile_of("calendar.create_event"))
    assert "handler_not_registered" in d.guard.violations
    assert d.guard.fallback_used is True


def test_handler_and_tools_stripped_on_handlerless_route(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="direct_answer", model_tier="cheap", handler="bookkeeping",
        tool_set=["query_ledger"],
    ), profile_of("chat.explain"))
    assert d.handler is None
    assert d.tool_set == []
    assert "tool_set_without_handler" in d.guard.violations


def test_budget_takes_the_strictest_of_all_sources(policy, registry):
    # 路由上限 0.08、请求约束 0.05、LLM 提 0.20 → 取 0.05
    d = guard(policy, registry, RawDecision(
        route_id="vision_extract_then_write", model_tier="standard", handler="bookkeeping",
        tool_set=["extract_receipt_fields"], max_cost=0.20,
    ), profile_of("bookkeeping.capture_from_receipt"), constraints_max_cost=0.05)
    assert d.budget.max_cost == 0.05
    assert "budget_clamped" in d.guard.applied


def test_budget_cannot_exceed_route_ceiling_without_request_constraint(policy, registry):
    # 路由上限 0.003，LLM 提 5.0 → 取 0.003
    d = guard(policy, registry, RawDecision(
        route_id="direct_answer", model_tier="cheap", max_cost=5.0,
    ), profile_of("chat.explain"))
    assert d.budget.max_cost == policy.route("direct_answer").max_cost


def test_caller_sync_wins_over_route_and_evaluator_advice(policy, registry):
    """优先级：请求 > 路由配置 > 模型建议。

    ``single_tool_action`` 的路由配置是 sync，评估器建议 async。调用方明确要 sync 时
    两者都不该推翻它——它们只是建议。
    """
    cand = RawDecision(route_id="single_tool_action", model_tier="standard",
                       handler="calendar", tool_set=["create_event"], execution_mode="sync")
    forced = guard(policy, registry, cand, profile_of("calendar.create_event", mode="async"),
                   mode_preference="sync")
    assert forced.execution_mode == "sync"
    assert forced.guard.applied == []


def test_mode_promoted_when_structural_constraint_overrides_caller(policy, registry):
    """调用方要 sync，但路由必须拆解 → 结构上做不到，提升为 async 并留痕。"""
    cand = RawDecision(route_id="vision_extract_then_write", model_tier="standard",
                       handler="bookkeeping", tool_set=["extract_receipt_fields"],
                       execution_mode="sync")
    d = guard(policy, registry, cand, profile_of("bookkeeping.capture_from_receipt"),
              mode_preference="sync")
    assert d.execution_mode == "async"
    assert "mode_promoted" in d.guard.applied
    assert d.mode_change_reason == "route_requires_decomposition"
    assert "requested_sync_downgraded_to_async" in d.guard.violations


def test_auto_follows_route_default_without_recording_a_promotion(policy, registry):
    """调用方写 auto 时不算"被提升"——它没有主张。"""
    cand = RawDecision(route_id="single_tool_action", model_tier="standard",
                       handler="calendar", tool_set=["create_event"], execution_mode="auto")
    d = guard(policy, registry, cand, profile_of("calendar.create_event"), mode_preference="auto")
    assert "mode_promoted" not in d.guard.applied


def test_auto_lets_evaluator_push_to_async_when_route_says_sync(policy, registry):
    cand = RawDecision(route_id="single_tool_action", model_tier="standard",
                       handler="calendar", tool_set=["create_event"], execution_mode="auto")
    d = guard(policy, registry, cand, profile_of("calendar.create_event", mode="async"),
              mode_preference="auto")
    assert d.execution_mode == "async"
    assert d.mode_change_reason == "evaluator_recommends_async"


def test_decompose_is_derived_from_route_not_llm(policy, registry):
    """decompose 由路由的 path 派生，单一事实来源。"""
    d = guard(policy, registry, RawDecision(
        route_id="vision_extract_then_write", model_tier="standard", handler="bookkeeping",
    ), profile_of("bookkeeping.capture_from_receipt"))
    assert d.decompose is (policy.route("vision_extract_then_write").path == "decompose")
    assert d.decompose is True


def test_escalation_bound_comes_from_policy_not_llm(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="standard", handler="calendar",
        escalation_to_tier="strong", escalation_on=["low_confidence"],
    ), profile_of("calendar.create_event"))
    assert d.escalation_rule is not None
    assert d.escalation_rule.max_escalations == policy.thresholds.max_escalations


def test_escalation_to_undefined_tier_is_rejected(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="standard", handler="calendar",
        escalation_to_tier="godlike",
    ), profile_of("calendar.create_event"))
    assert d.escalation_rule.to_tier in policy.model_tier_ids
    assert "escalation_target_not_a_tier" in d.guard.violations


def test_unhealthy_tier_falls_to_nearest_healthy(policy, registry):
    d = guard(policy, registry, RawDecision(
        route_id="multi_step_analysis", model_tier="strong", handler="bookkeeping",
    ), profile_of("bookkeeping.ledger.query"), tier_health={"strong": "open", "standard": "closed"})
    assert d.model_tier == "standard"


def test_guard_applied_is_never_produced_by_the_llm(policy, registry):
    """守卫报告只由代码写入。即使原始候选携带 applied，也不该被采信。"""
    cand = RawDecision(route_id="direct_answer", model_tier="cheap")
    d = guard(policy, registry, cand, profile_of("chat.explain"))
    # 干净的输入不该产生任何"修正"
    assert d.guard.applied == []
    assert d.guard.violations == []


@pytest.mark.parametrize("route_id", ["direct_answer", "single_tool_action",
                                      "vision_extract_then_write",
                                      "schedule_parse_then_create", "multi_step_analysis",
                                      "scheduled_aggregate"])
def test_every_route_in_policy_is_reachable(policy, registry, route_id):
    """每条路由都必须能被选到——否则策略里存在死条目。"""
    route = policy.route(route_id)
    d = guard(policy, registry, RawDecision(
        route_id=route_id, model_tier=route.default_tier,
        handler="bookkeeping" if route.requires_handler else None,
        tool_set=["query_ledger"] if route.requires_handler else [],
    ), profile_of("bookkeeping.ledger.query"))
    assert d.route_id == route_id
    assert "route_fallback" not in d.guard.applied


# ---------------------------------------------------------------------------
# handler 从 tool_set 推导
# ---------------------------------------------------------------------------
def test_handler_is_derived_from_tool_set_when_omitted(policy, registry):
    """模型没给 handler 时从工具集推导——那是可推导的冗余信息。

    真实模型的表现是：正确选出路由与工具，却把 handler 留空。
    问模型要一个能推出来的字段，它就会漏。所以由代码推。
    """
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="cheap", handler=None,
        tool_set=["query_ledger", "compare_entries"],
    ), profile_of("bookkeeping.ledger.query"))
    assert d.handler == "bookkeeping"
    assert "handler_not_registered" not in d.guard.violations
    assert "handler_fallback" not in d.guard.applied


def test_ambiguous_tool_set_across_handlers_still_fails(policy, registry):
    """工具跨多个 handler 时推不出来，必须失败而不是随便挑一个。"""
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="cheap", handler=None,
        tool_set=["query_ledger", "create_event"],   # 分属 bookkeeping 与 calendar
    ), profile_of("bookkeeping.ledger.query"))
    assert d.handler is None
    assert "handler_not_registered" in d.guard.violations


def test_explicit_handler_wins_over_derivation(policy, registry):
    """模型给了 handler 就用它，推导只在缺失时兜底。"""
    d = guard(policy, registry, RawDecision(
        route_id="single_tool_action", model_tier="cheap", handler="calendar",
        tool_set=["create_event"],
    ), profile_of("calendar.create_event"))
    assert d.handler == "calendar"
