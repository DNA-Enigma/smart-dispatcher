"""阶段 03 上半 — Decomposer。

两条来源，都是配置驱动：

1. **``config/flow_templates/*.yaml``** —— 具名、参数化、预先校验过的 DAG。
   形状已知的流程不该每次请求都让 LLM 重新拆一遍：那既费 token，
   又会在同一个形状上反复产生小幅幻觉（顺序变化、多加一个节点）。
2. **自由拆解** —— LLM 在 ``tool_set`` 与 ``required_capabilities`` 约束下自行生成。

模板优先。**模板未命中率**是一个被监控的指标：某类任务反复走自由拆解，
说明该补一个模板——那是 04 提出 ``flow_template_add`` 建议的依据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.agents import AgentSpec
from ..core.contract import RouteDecision, TaskEnvelope, TaskProfile
from ..core.errors import DispatcherError
from ..core.plan import ExecutionPlan, Node, PlanBudget, Retry, validate_plan
from ..core.policy import Policy
from ..core.pricing import Pricing
from ..core.prompts import PromptLibrary, data_block, fill
from ..core.registry import HandlerRegistry
from ..core.yamlio import load_yaml
from ..ports.llm import LLMError, LLMMessage, LLMPort

DECOMPOSER_PROMPT = "decomposer.md"


@dataclass
class DecomposeMeta:
    source: str = "llm_decomposition"
    template_miss: bool = False
    revisions: int = 0
    violations: list[str] = field(default_factory=list)
    tier: str = ""
    latency_ms: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class DecomposeOutcome:
    plan: ExecutionPlan
    meta: DecomposeMeta


def load_templates(directory: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not directory.exists():
        return out
    for p in sorted(directory.glob("*.yaml")):
        raw = load_yaml(p)
        out[raw["template_id"]] = raw
    return out


class Decomposer:
    def __init__(
        self,
        *,
        policy: Policy,
        registry: HandlerRegistry,
        agents: AgentSpec,
        prompts: PromptLibrary,
        llm: LLMPort,
        templates_dir: Path,
        pricing: Pricing | None = None,
    ) -> None:
        self._policy = policy
        self._pricing = pricing
        self._registry = registry
        self._agents = agents
        self._prompts = prompts
        self._llm = llm
        self._templates = load_templates(templates_dir)
        # 记下来供热换策略时复用：策略换了，模板目录没换
        self._templates_dir = templates_dir

    # ------------------------------------------------------------------
    def _matching_template(
        self, decision: RouteDecision, profile: TaskProfile
    ) -> dict | None:
        """按 handler + 能力交集找模板。

        这是**集合交集**判定，不是领域判断：它比较的是能力名集合，
        与"什么算记账"无关。换一个领域只是换一个能力名。
        """
        has_image = "image" in (profile.modality or [])

        hint = self._policy.route(decision.route_id)
        wanted = hint.flow_template_hint if hint else None
        if wanted and wanted in self._templates:
            tpl = self._templates[wanted]
            # **模板自己的绑定会说明它需要什么输入。** 引用 `envelope.input.media`
            # 的模板天然需要媒体；请求里没有图就不该命中它。
            #
            # 这条判定是从模板推导的，不是写死的领域规则——换一个模板、换一种输入，
            # 它照样成立。之前的实现完全不看模态，于是"午饭花了38"这种纯文本请求
            # 会被套进票据模板，第一个节点就去取 `media[0]`，必然
            # `bad_input_reference` 失败（P0-1）。而那个模板的 applies_when 里
            # 明明写着"输入中含有一张主要作为凭证的图像"。
            if (tpl.get("handler") == decision.handler
                    and (has_image or not _requires_media(tpl))):
                return tpl

        if not decision.handler:
            return None
        caps = set(profile.candidate_capabilities) | set(profile.required_capabilities)
        for tpl in self._templates.values():
            if tpl.get("handler") != decision.handler:
                continue
            if not (set(tpl.get("applies_to_capabilities") or []) & caps):
                continue
            if not has_image and _requires_media(tpl):
                continue
            return tpl
        return None

    def _from_template(
        self, tpl: dict, decision: RouteDecision, task_id: str
    ) -> ExecutionPlan:
        nodes: list[Node] = []
        for n in tpl["nodes"]:
            body: dict[str, Any] = {
                "subtask_id": n["id"],
                "name": n.get("name"),
                "handler": tpl["handler"],
                "executor": n["executor"],
                "depends_on": list(n.get("depends_on") or []),
                "inputs": dict(n.get("inputs") or {}),
                "output_schema_ref": n.get("output_schema_ref"),
                "required_capabilities": list(n.get("required_capabilities") or []),
                "timeout_ms": n.get("timeout_ms"),
                "optional": bool(n.get("optional")),
                "on_failure": n.get("on_failure", "fail_task"),
                "default_output": n.get("default_output"),
                "idempotent": bool(n.get("idempotent")),
            }
            if n["executor"] == "tool":
                body["tool"] = n["tool"]
                body["model_tier"] = n.get("tier")
            else:
                body["role"] = n["role"]
                body["tool_whitelist"] = list(n.get("tool_whitelist") or [])
                body["max_rounds"] = n.get("max_rounds")
                body["on_round_limit"] = n.get("on_round_limit")
            if n.get("retry"):
                body["retry"] = Retry(**n["retry"])
            if n.get("verification"):
                body["verification"] = n["verification"]
            nodes.append(Node(**body))

        # 决策里的并发提示优先，但不得超过模板与全局上限
        max_p = min(
            tpl.get("max_parallelism") or self._policy.decomposer.parallel["default_max_parallelism"],
            self._policy.limits.max_parallelism,
        )
        return ExecutionPlan(
            task_id=task_id,
            strategy="dag" if len(nodes) > 1 else "single_step",
            source=f"flow_template:{tpl['template_id']}",
            max_parallelism=max_p,
            nodes=nodes,
            edges=[{"from": e["from"], "to": e["to"]} for e in (tpl.get("edges") or [])],
            join={k: list(v) for k, v in (tpl.get("join") or {}).items()},
            plan_budget=PlanBudget(
                max_cost=decision.budget.max_cost,
                max_wall_ms=decision.budget.max_wall_ms,
                max_llm_calls=decision.budget.max_llm_calls,
            ),
        )

    # ------------------------------------------------------------------
    async def _slot_fill(
        self,
        envelope: TaskEnvelope,
        profile: TaskProfile,
        decision: RouteDecision,
        tool: str | None,
        meta: DecomposeMeta,
        budget: Any | None = None,
    ) -> dict:
        """把用户的话抽成工具的具名参数。

        失败时返回空 dict 而不是抛错：参数抽不出来只是这一步做不好，
        不该让整条任务在拆解阶段就死掉——让工具自己去报"缺参数"更贴合实际，
        而且那个失败带上的是工具自己的错误码，比这里的猜测更准。
        """
        if not tool or not decision.handler:
            return {}
        decl = next(
            (t for h, t in self._registry.all_tool_decls()
             if h == decision.handler and t.name == tool),
            None,
        )
        if decl is None:
            return {}

        schema_hint = (
            json.dumps(decl.input_schema, ensure_ascii=False)
            if decl.input_schema
            else "（未声明 input_schema：按工具的语义与描述推断参数名）"
        )
        ask = (
            f"工具 {tool}：{decl.description or '（无描述）'}\n"
            f"参数结构：{schema_hint}\n"
            f"用户的原话用数据块给出。请抽出调用该工具所需的参数，"
            f"只输出一个 JSON 对象（就是参数本身，不要包一层）。"
            f"确实无法从原话推断的参数不要编造，留空或省略。"
        )
        try:
            raw, res = await self._llm.generate_json(
                [
                    LLMMessage.system("你把用户的自然语言请求抽成工具调用的具名参数。只输出 JSON。"),
                    LLMMessage.user(data_block("用户请求", _request_text(envelope)) + "\n\n" + ask),
                ],
                tier=self._policy.decomposer.tier,
                requires=("text",),
                max_repair_attempts=0,
                options=self._policy.decomposer.options,
            )
            meta.latency_ms += res.latency_ms
            if budget is not None and self._pricing is not None:
                budget.charge(
                    self._pricing.cost_of(
                        self._policy.decomposer.tier, res.input_tokens, res.output_tokens
                    ),
                    subtask_id="__slot_fill__",
                    note="stage:slot_fill",
                )
            return raw if isinstance(raw, dict) else {}
        except Exception as e:
            meta.notes.append(f"参数抽取失败，工具将收到空参数：{e}")
            return {}

    def _messages(
        self, envelope: TaskEnvelope, profile: TaskProfile, decision: RouteDecision
    ) -> list[LLMMessage]:
        catalog: list[str] = []
        for hid in sorted(self._registry.ids):
            for t in self._registry.manifest(hid).tools:  # type: ignore[union-attr]
                if decision.tool_set and t.name not in decision.tool_set:
                    continue
                catalog.append(
                    json.dumps(
                        {
                            "handler": hid,
                            "tool": t.name,
                            "side_effects": t.side_effects,
                            "requires": t.requires_capabilities,
                            "idempotent": t.idempotent,
                            "output_schema_ref": t.output_schema_ref,
                        },
                        ensure_ascii=False,
                    )
                )
        templates = [
            json.dumps(
                {"template_id": t["template_id"], "applies_when": t["applies_when"],
                 "handler": t["handler"]},
                ensure_ascii=False,
            )
            for t in self._templates.values()
            if t.get("handler") == decision.handler
        ]
        roles = [
            json.dumps({"role": r.id, "when": r.when, "requires": r.requires,
                        "allowed_tools": r.allowed_tools, "max_rounds": r.max_rounds},
                       ensure_ascii=False)
            for r in self._agents.roles
        ]
        system = fill(
            self._prompts.get(DECOMPOSER_PROMPT),
            {
                "candidate_templates": "\n".join(templates) or "（无）",
                "tool_signatures": "\n".join(catalog) or "（无）",
                "limits": json.dumps(
                    {
                        "max_parallelism": self._policy.limits.max_parallelism,
                        "max_agent_rounds": self._policy.limits.max_agent_rounds,
                        "max_agent_nodes_per_plan": self._policy.limits.max_agent_nodes_per_plan,
                        "max_total_rounds_per_task": self._policy.limits.max_total_rounds_per_task,
                        "available_roles": roles,
                    },
                    ensure_ascii=False, indent=2,
                ),
            },
        )
        user = (
            data_block(
                "本次任务",
                json.dumps(
                    {
                        "profile": profile.model_dump(mode="json", exclude_none=True),
                        "decision": decision.model_dump(mode="json", exclude_none=True),
                    },
                    ensure_ascii=False, indent=2,
                ),
            )
            + "\n\n请按系统提示的要求，只输出一个 JSON 对象。"
        )
        return [LLMMessage.system(system), LLMMessage.user(user)]

    # ------------------------------------------------------------------
    async def decompose(
        self,
        envelope: TaskEnvelope,
        profile: TaskProfile,
        decision: RouteDecision,
        *,
        task_id: str,
        budget: Any | None = None,
    ) -> DecomposeOutcome:
        meta = DecomposeMeta(tier=self._policy.decomposer.tier)

        # 单步路径不做拆解，但仍然需要**把工具参数抽出来**。
        #
        # 这是个容易被忽略的环节：路由阶段只选定了"用哪个工具"，没说"用什么参数"。
        # 少了这一步，create_event 拿到的是一组空参数——任务会"成功"，
        # 但写进去的东西是空的。宁可多花一次调用，也不产出空壳结果。
        route_cfg = self._policy.route(decision.route_id)

        # 路由说"单步"却点不出工具 → 这份决策**本来就不可执行**。
        #
        # 不硬失败，也不拿字符串哨兵顶替（之前写的是 `or "none"`，于是 "none"
        # 变成一个看起来像真 handler 的 id，一路传到执行器才报"没有可执行实现"，
        # 把人往错的方向引）。改成退到自由拆解：让拆解器用已声明的工具自己拼一张图。
        #
        # 这是**确定性修复，不是提示词祈祷**。真实模型在"明天下午三点开会"上反复
        # 选了 single_tool_action 却不给 tool——它知道要先解析时间，但不愿走多步。
        # 与其继续调提示词，不如承认"决策不可执行"这件事本身有明确的处理方式。
        unexecutable_single_step = (
            not decision.decompose
            and route_cfg is not None
            and route_cfg.requires_handler
            and (decision.handler is None or not decision.tool_set)
        )
        if unexecutable_single_step:
            meta.notes.append(
                f"路由 {decision.route_id} 需要 handler 与工具，但决策给出的是 "
                f"handler={decision.handler!r} tool_set={decision.tool_set}——"
                f"不可执行，改为自由拆解"
            )
            meta.template_miss = True
            plan = await self._freeform(envelope, profile, decision, task_id, meta, budget)
            return DecomposeOutcome(plan=plan, meta=meta)

        if not decision.decompose:
            tool = decision.tool_set[0] if decision.tool_set else None
            inputs = await self._slot_fill(envelope, profile, decision, tool, meta, budget)
            node = Node(
                subtask_id="main",
                name=profile.intent_summary or "执行",
                handler=decision.handler or "",
                executor="tool",
                tool=tool,
                inputs=inputs,
                model_tier=decision.model_tier,
                required_capabilities=[],
                on_failure="fail_task",
            )
            plan = ExecutionPlan(
                task_id=task_id, strategy="single_step", source="direct",
                max_parallelism=1, nodes=[node],
                plan_budget=PlanBudget(
                    max_cost=decision.budget.max_cost,
                    max_wall_ms=decision.budget.max_wall_ms,
                    max_llm_calls=decision.budget.max_llm_calls,
                ),
            )
            meta.source = "direct"
            return DecomposeOutcome(plan=plan, meta=meta)

        tpl = self._matching_template(decision, profile)
        if tpl is not None:
            plan = self._from_template(tpl, decision, task_id)
            meta.source = plan.source
            violations = validate_plan(
                plan, policy=self._policy, registry=self._registry,
                tool_set=decision.tool_set, agents=self._agents,
                decision_budget=decision.budget,
            )
            if not violations:
                return DecomposeOutcome(plan=plan, meta=meta)
            # 模板自己过不了校验是**模板的问题**，不是模型的问题。
            # 记下来然后退回自由拆解，不要拿一张非法图去执行。
            meta.notes.append(f"模板 {tpl['template_id']} 未通过校验：{violations}")
            meta.violations = violations

        meta.template_miss = True
        plan = await self._freeform(envelope, profile, decision, task_id, meta, budget)
        return DecomposeOutcome(plan=plan, meta=meta)

    async def _freeform(
        self,
        envelope: TaskEnvelope,
        profile: TaskProfile,
        decision: RouteDecision,
        task_id: str,
        meta: DecomposeMeta,
        budget: Any | None = None,
    ) -> ExecutionPlan:
        max_replans = self._policy.decomposer.max_replans
        messages = self._messages(envelope, profile, decision)
        violations: list[str] = []

        for revision in range(1, max_replans + 2):
            try:
                raw, res = await self._llm.generate_json(
                    messages,
                    tier=self._policy.decomposer.tier,
                    requires=("text",),
                    max_repair_attempts=1,
                    options=self._policy.decomposer.options,
                )
                meta.latency_ms = res.latency_ms
                if budget is not None and self._pricing is not None:
                    budget.charge(
                        self._pricing.cost_of(
                            self._policy.decomposer.tier, res.input_tokens, res.output_tokens
                        ),
                        subtask_id="__decomposer__",
                        note="stage:decomposer",
                    )
            except (LLMError, DispatcherError) as e:
                if getattr(e, "fatal", False):
                    raise
                meta.notes.append(f"拆解调用失败：{e}")
                raw = {}

            plan = self._plan_from_raw(raw, decision, task_id, revision)
            violations = validate_plan(
                plan, policy=self._policy, registry=self._registry,
                tool_set=decision.tool_set, agents=self._agents,
                decision_budget=decision.budget,
            )
            meta.revisions = revision
            if not violations:
                meta.violations = []
                return plan

            meta.notes.append(f"第 {revision} 版计划未通过校验：{violations}")
            if revision > max_replans:
                break
            # 把违规点回灌再试——原样重试往往得到同样的问题，而"哪里不合法"
            # 是模型能修的信息（它不需要自己猜规则）
            messages = messages + [
                LLMMessage.assistant(json.dumps(raw, ensure_ascii=False)),
                LLMMessage.user(
                    "你上一次的输出未通过校验，问题如下：\n- "
                    + "\n- ".join(violations)
                    + "\n请只输出修正后的 JSON 对象。"
                ),
            ]

        meta.violations = violations
        raise DispatcherError(
            "policy_violation",
            f"拆解在 {meta.revisions} 次尝试后仍未产出合法计划：{violations}",
            context={"violations": violations},
        )

    # ------------------------------------------------------------------
    def _plan_from_raw(
        self, raw: dict, decision: RouteDecision, task_id: str, revision: int
    ) -> ExecutionPlan:
        """把 LLM 的输出读成计划。

        **宽容读取**（缺字段给默认值），但接下来会过**严格校验**——
        宽容 + 严格比严格 + 信任安全得多：模型的输出永远会有小偏差，
        而把偏差交给集合校验处理，比试图让它一次就完全正确现实。
        """
        nodes: list[Node] = []
        for i, n in enumerate(raw.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            executor = n.get("executor") or ("agent" if n.get("role") else "tool")
            body: dict[str, Any] = {
                "subtask_id": str(n.get("subtask_id") or n.get("id") or f"step_{i + 1}"),
                "name": n.get("name"),
                "handler": str(n.get("handler") or decision.handler or ""),
                "executor": executor,
                "depends_on": [str(d) for d in (n.get("depends_on") or [])],
                "inputs": dict(n.get("inputs") or {}),
                "output_schema_ref": n.get("output_schema_ref"),
                "model_tier": n.get("model_tier") if n.get("model_tier") in
                self._policy.model_tier_ids else None,
                "required_capabilities": [str(c) for c in (n.get("required_capabilities") or [])],
                "timeout_ms": _int(n.get("timeout_ms")),
                "optional": bool(n.get("optional")),
                "on_failure": n.get("on_failure")
                if n.get("on_failure") in {"fail_task", "skip", "continue_with_default"}
                else "fail_task",
                "default_output": n.get("default_output"),
                "idempotent": bool(n.get("idempotent")),
            }
            if executor == "tool":
                body["tool"] = n.get("tool")
            else:
                body["role"] = n.get("role")
                body["tool_whitelist"] = [str(t) for t in (n.get("tool_whitelist") or [])]
                body["max_rounds"] = _int(n.get("max_rounds"))
                body["on_round_limit"] = n.get("on_round_limit")
            nodes.append(Node(**body))

        edges = []
        for e in raw.get("edges") or []:
            if isinstance(e, dict) and e.get("from") and e.get("to"):
                edges.append({"from": str(e["from"]), "to": str(e["to"])})
        if not edges:
            edges = [
                {"from": d, "to": n.subtask_id} for n in nodes for d in n.depends_on
            ]

        join = {
            str(k): [str(x) for x in (v or [])]
            for k, v in (raw.get("join") or {}).items()
            if isinstance(v, list)
        }

        pb = raw.get("plan_budget") if isinstance(raw.get("plan_budget"), dict) else {}
        return ExecutionPlan(
            task_id=task_id,
            strategy="dag" if len(nodes) > 1 else "single_step",
            source="llm_decomposition",
            max_parallelism=min(
                _int(pb.get("max_parallelism")) or decision.parallelism_hint
                or self._policy.decomposer.parallel["default_max_parallelism"],
                self._policy.limits.max_parallelism,
            ),
            revision=revision,
            nodes=nodes,
            edges=edges,
            join=join,
            plan_budget=PlanBudget(
                max_cost=min(
                    _float(pb.get("max_cost")) or decision.budget.max_cost,
                    decision.budget.max_cost,
                ),
                max_wall_ms=min(
                    _int(pb.get("max_wall_ms")) or decision.budget.max_wall_ms,
                    decision.budget.max_wall_ms,
                ),
                max_llm_calls=_int(pb.get("max_llm_calls")) or decision.budget.max_llm_calls,
            ),
        )


def _requires_media(tpl: dict) -> bool:
    """模板是否依赖请求里的媒体。

    判定依据是**模板自己的输入绑定**：只要某个节点的 inputs 里引用了
    ``envelope.input.media``，这个模板就需要媒体。这样"模板需要什么输入"
    与"它怎么写绑定"不会分叉——不需要在模板里再声明一遍、也不会忘记同步。
    """
    def walk(v: Any) -> bool:
        if isinstance(v, dict):
            ref = v.get("$ref")
            if isinstance(ref, str) and ref.startswith("envelope.input.media"):
                return True
            return any(walk(x) for x in v.values())
        if isinstance(v, list):
            return any(walk(x) for x in v)
        return False

    return any(walk(n.get("inputs") or {}) for n in tpl.get("nodes") or [])


def _request_text(envelope: TaskEnvelope) -> str:
    parts = [envelope.input.text or ""]
    for m in envelope.input.media or []:
        parts.append(f"[媒体 {m.media_id}，{m.kind}/{m.mime}]")
    return "\n".join(p for p in parts if p)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


__all__ = ["DecomposeMeta", "DecomposeOutcome", "Decomposer", "load_templates"]
