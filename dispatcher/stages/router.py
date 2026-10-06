"""阶段 02 — Router。

把 ``TaskProfile`` 映射成 ``RouteDecision``。这里是决策表落地的地方：

* **LLM 从菜单里选。** 菜单由 ``policy_menu()`` 从策略渲染而来——菜单是策略的函数，
  策略里没有的路由它写不出来。
* **代码做校验。** ``apply_guard()`` 用集合代数把它收敛成一个合法决策，
  并记录每一次修正。

一个刻意的不对称：LLM 超时或输出非法时**不重试**，直接走兜底路由。
理由是这样能给出确定的、可预测的行为——路由失败时系统应该退化到"保守但确定"，
而不是"再赌一次"。真正的重试预算花在评估器上（那里重试有意义：画像抽错了
后面全错）。这个取舍写在这里，是因为它不显然。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.contract import RouteDecision, TaskEnvelope, TaskProfile
from ..core.errors import DispatcherError
from ..core.guard import RawDecision, apply_guard
from ..core.policy import Policy
from ..core.pricing import Pricing
from ..core.prompts import PromptLibrary, fill, hard_constraints, policy_menu
from ..core.registry import HandlerRegistry
from ..ports.llm import LLMError, LLMMessage, LLMPort

ROUTER_PROMPT = "router.md"


@dataclass
class RouteMeta:
    tier: str = ""
    model_resolved: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    fallback_used: bool = False
    guard_applied: list[str] = field(default_factory=list)
    guard_violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class RouteOutcome:
    decision: RouteDecision
    raw: RawDecision
    meta: RouteMeta


class Router:
    def __init__(
        self,
        *,
        policy: Policy,
        registry: HandlerRegistry,
        prompts: PromptLibrary,
        llm: LLMPort,
        pricing: Pricing | None = None,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._prompts = prompts
        self._llm = llm
        self._pricing = pricing

    # ------------------------------------------------------------------
    def _build_messages(
        self, envelope: TaskEnvelope, profile: TaskProfile, menu_override: str | None = None
    ) -> list[LLMMessage]:
        system = fill(
            self._prompts.get(ROUTER_PROMPT),
            {
                "policy_menu": menu_override if menu_override is not None else policy_menu(self._policy),
                "available_tools": self._registry.tool_catalog_for_prompt(),
                "hard_constraints": hard_constraints(
                    self._policy, envelope.constraints, profile
                ),
            },
        )
        # 画像作为数据块进入 user 消息，而不是拼进 system——它是模型产出的内容，
        # 而模型产出同样属于不可信输入。
        user = (
            "【请求画像】\n"
            + "```json\n"
            + profile.model_dump_json(indent=2, exclude_none=True)
            + "\n```\n\n"
            + "请按系统提示的要求，只输出一个 JSON 对象。"
        )
        return [LLMMessage.system(system), LLMMessage.user(user)]

    # ------------------------------------------------------------------
    async def route(
        self,
        envelope: TaskEnvelope,
        profile: TaskProfile,
        *,
        tier_health: dict[str, str] | None = None,
        menu_override: str | None = None,
        budget: Any | None = None,
    ) -> RouteOutcome:
        meta = RouteMeta(tier=self._policy.router.tier)
        messages = self._build_messages(envelope, profile, menu_override)

        requires = ("text",)
        raw_dict: dict[str, Any] | None = None
        try:
            raw_dict, result = await self._llm.generate_json(
                messages,
                tier=self._policy.router.tier,
                requires=requires,
                max_repair_attempts=self._policy.router.max_repair_attempts,
                temperature=self._policy.router.temperature,
                timeout_ms=self._policy.router.timeout_ms,
                # 关掉深度思考才能让上面那个 temperature=0 真的生效——
                # 当前供应商在思考模式下会强制覆盖温度。路由必须可复现。
                options=self._policy.router.options,
            )
            meta.model_resolved = result.model_resolved
            meta.latency_ms = result.latency_ms
            meta.input_tokens = result.input_tokens
            meta.output_tokens = result.output_tokens
            # 记账。路由这一步花的钱不该因为"它发生在计划确定之前"就消失。
            if budget is not None and self._pricing is not None:
                budget.charge(
                    self._pricing.cost_of(
                        self._policy.router.tier, result.input_tokens, result.output_tokens
                    ),
                    subtask_id="__router__",
                    note="stage:router",
                )
        except LLMError as e:
            if e.fatal:
                # 同评估器：凭证/余额问题不降级，一路上抛。
                # 兜底路由是给"这次选不出来"用的，不是给"整个供应商用不了"用的。
                raise e.to_dispatcher_error() from e
            meta.fallback_used = True
            meta.notes.append(f"路由器调用失败，走兜底路由：{e}")
        except DispatcherError as e:
            if e.fatal:
                raise
            meta.fallback_used = True
            meta.notes.append(f"路由器调用失败，走兜底路由：{e}")

        candidate = self._parse_raw(raw_dict or {})

        outcome = apply_guard(
            self._policy,
            candidate,
            profile=profile,
            handler_ids=self._registry.ids,
            handler_tools=self._registry.tool_map(),
            constraints_max_cost=envelope.constraints.max_cost,
            constraints_max_wall_ms=envelope.constraints.max_wall_ms,
            mode_preference=envelope.constraints.mode_preference,
            tier_health=tier_health or {},
        )
        meta.guard_applied = list(outcome.decision.guard.applied)
        meta.guard_violations = list(outcome.decision.guard.violations)
        meta.notes.extend(outcome.notes)
        if not raw_dict:
            # 没有拿到 LLM 输出时，候选是空的，守卫会走 route_fallback。
            # 这里把 fallback_used 标真，使"兜底"在指标里可见。
            meta.fallback_used = True
        return RouteOutcome(decision=outcome.decision, raw=candidate, meta=meta)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_raw(d: dict[str, Any]) -> RawDecision:
        """宽容读取 LLM 输出。

        模型偶尔会把预算写成嵌套对象、把升级规则写成扁平字段，或者在数值位置给字符串。
        宽容是因为这些偏差无害且难避免；**严格的是守卫**——无论读进来的是什么，
        它都要经过集合校验。宽容读取 + 严格校验，比严格读取 + 信任输出安全得多。
        """
        budget = d.get("budget") if isinstance(d.get("budget"), dict) else {}
        esc = d.get("escalation_rule") if isinstance(d.get("escalation_rule"), dict) else {}

        tools = d.get("tool_set")
        if tools is None:
            tools = d.get("tools")
        tool_set = [str(t) for t in tools] if isinstance(tools, list) else []

        return RawDecision(
            route_id=str(d.get("route_id") or ""),
            model_tier=str(d.get("model_tier") or ""),
            handler=(str(d["handler"]) if d.get("handler") else None),
            tool_set=tool_set,
            execution_mode=str(d.get("execution_mode") or "auto"),
            max_cost=_as_float(d.get("max_cost", budget.get("max_cost"))),
            max_wall_ms=_as_int(d.get("max_wall_ms", budget.get("max_wall_ms"))),
            max_llm_calls=_as_int(d.get("max_llm_calls", budget.get("max_llm_calls"))),
            escalation_to_tier=(
                str(esc["to_tier"]) if esc.get("to_tier") else None
            ),
            escalation_on=[str(x) for x in (esc.get("on") or [])] if isinstance(esc.get("on"), list) else [],
            parallelism_hint=_as_int(d.get("parallelism_hint")),
            rationale=str(d.get("rationale") or ""),
            confidence=_clamp01(d.get("confidence")),
        )


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp01(v: Any) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["RouteMeta", "RouteOutcome", "Router"]
