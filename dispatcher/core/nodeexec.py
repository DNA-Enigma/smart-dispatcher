"""节点执行：把一个 ``Node`` 变成 ``ToolResult``。

三种执行形态，各有各的边界：

* ``executor=tool`` —— 一次工具调用。输入确定则输出唯一，不需要判断。
* ``executor=agent`` —— 带角色、带工具白名单、**有界**多轮循环。
* ``verification=independent_review`` —— 在上面两者之外再起 N 个独立复核者与一个仲裁者。

多 Agent 协作的一个刻意限制写在 ``_run_agent`` 里：**Agent 之间不自由对话**，
它们通过结构化的数据依赖协作（上游输出按 ``$ref`` 绑定到下游输入）。
理由不是"不想做"，而是自由对话式的多 Agent 有两个绕不过去的问题——
轮数取决于模型何时觉得"聊够了"，于是成本与延迟没有上界；以及过程难以复现，
而 04 自进化恰恰需要可归因的记录。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .agents import AgentRole, AgentSpec
from .budget import BudgetLedger
from .cancel import CancellationToken
from .context import DispatchContext, MediaResolver
from .eventbus import EventBus
from .execution import ToolResult
from .policy import Policy
from .pricing import Pricing
from .prompts import PromptLibrary, data_block, fill
from .registry import HandlerRegistry
from .state import TaskRecord

# Agent 每一轮必须输出的形状。用封闭的三分支而不是自由文本：
# 自由文本没法判定"它做完了没有"，而"做完了没有"正是循环的终止条件。
#
# **用 ``.replace`` 而不是 ``.format`` 来注入工具列表**：这段文本里满是 JSON 示例的
# 花括号，而 ``str.format`` 会把 ``{"tool_calls": [...]}`` 里的 ``"tool_calls"``
# 当成格式字段去查，于是抛 KeyError。用 .format 拼含 JSON 的模板是个经典的坑。
_AGENT_CONTRACT = """
每一轮你**只输出一个 JSON 对象**，不要围栏、不要解释。三种形态之一：

1. 调用工具：{"tool_calls": [{"name": "<工具名>", "args": {...}}]}
2. 给出最终结果：{"final": {...}}
3. 无法继续：{"give_up": {"reason": "..."}}

``final`` 必须满足节点声明的输出结构。不要在没有 ``final`` 的情况下声称完成。
可用工具只有：{tools}
"""


class NodeExecutor:
    """把 ``NodeExecutorFn`` 协议实现出来。

    构造一次，跨任务复用。它不持有任务状态——所有任务相关的信息都在每次调用的
    参数与 `scope` 里，因此同一个执行器可以安全地并发服务多个任务。
    """

    def __init__(
        self,
        *,
        policy: Policy,
        pricing: Pricing,
        registry: HandlerRegistry,
        agents: AgentSpec,
        prompts: PromptLibrary,
        llm: Any,
        media: MediaResolver,
        events: EventBus,
        ledger: BudgetLedger,
        state: Any,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._policy = policy
        self._pricing = pricing
        self._registry = registry
        self._agents = agents
        self._prompts = prompts
        self._llm = llm
        self._media = media
        self._events = events
        self._ledger = ledger
        self._state = state
        self._cancel = cancellation or CancellationToken()

    # ------------------------------------------------------------------
    async def __call__(
        self,
        node: Any,
        *,
        attempt: int,
        tier_override: str | None,
        scope: dict[str, Any],
    ) -> ToolResult:
        record: TaskRecord | None = scope.get("__record__")
        plan_allowed = scope.get("__allowed_tiers__") or list(self._policy.model_tier_ids)

        ctx = DispatchContext(
            task_id=scope.get("__task_id__", "task_unknown"),
            subtask_id=node.subtask_id,
            tenant_id=record.tenant_id if record else "default",
            user_id=record.user_id if record else "anonymous",
            trace_id=record.request_id if record else "",
            route_id=scope.get("__route_id__", ""),
            media=self._media,
            config=scope.get("__handler_config__", {}) or {},
            state=self._state,
            budget=self._make_budget_handle(scope.get("__task_id__", "")),
            cancellation=self._cancel,
            events=self._events,
            _policy=self._policy,
            _pricing=self._pricing,
            _llm=self._llm,
            _allowed_tiers=list(plan_allowed),
            _node_tier=tier_override or node.model_tier,
            _node_options=dict(self._policy.decomposer.node_defaults.get("options") or {}),
            # 只在从澄清恢复的那一次有值：handler 据此知道"用户刚才回答了什么"。
            clarification=scope.get("__clarification__"),
        )

        from .plan import resolve_inputs

        try:
            args = resolve_inputs(node.inputs, scope)
        except Exception as e:  # 引用解析失败是确定性错误，不是模型的错
            return ToolResult.fail("bad_input_reference", str(e), retryable=False)

        if node.executor == "tool":
            result = await self._run_tool(node, args, ctx)
        else:
            result = await self._run_agent(node, args, ctx, scope)

        if node.verification is not None and node.verification.mode != "none" and result.ok:
            result = await self._verify(node, args, result, ctx, scope)
        return result

    # ------------------------------------------------------------------
    def _make_budget_handle(self, task_id: str) -> Any:
        from .budget import BudgetHandle

        st = self._ledger.state(task_id)
        return BudgetHandle(
            _ledger=self._ledger,
            task_id=task_id,
            limit=st.limit if st else 0.0,
            spent=st.spent if st else 0.0,
            currency=self._ledger.currency,
        )

    # ------------------------------------------------------------------
    async def _run_tool(self, node: Any, args: dict, ctx: DispatchContext) -> ToolResult:
        handler = self._registry.executable(node.handler)
        if handler is None:
            # 声明了但没有实现。这与"能力不存在"不同：能力在清单里，
            # 只是没人实现它。错误码区分开，免得排查时走错方向。
            return ToolResult.fail(
                "handler_not_implemented",
                f"handler {node.handler} 已声明但没有可执行实现",
                retryable=False,
            )
        if node.tool not in self._registry.tool_names(node.handler):
            return ToolResult.fail("tool_not_declared", f"未声明的工具 {node.tool}", retryable=False)
        return await handler.execute_tool(node.tool, args, ctx)

    # ------------------------------------------------------------------
    async def _run_agent(
        self, node: Any, args: dict, ctx: DispatchContext, scope: dict
    ) -> ToolResult:
        """有界多轮循环。

        三个硬界同时在起作用，缺一个都可能失控：

        * ``role.max_rounds`` —— 角色自己声明的轮数；
        * ``limits.max_agent_rounds`` —— 全局单节点上限（取更小者）；
        * ``limits.max_total_rounds_per_task`` —— 整任务总轮数，在计划校验时检查。

        第三个是防"每一步都合规但整体炸掉"的：单看每个节点都在上限内，
        十个节点乘起来就不对了。
        """
        role: AgentRole | None = self._agents.role(node.role)
        if role is None:
            return ToolResult.fail("unknown_role", f"未定义的角色 {node.role!r}", retryable=False)

        allowed = set(node.tool_whitelist or role.allowed_tools)
        allowed &= set(self._registry.tool_names(node.handler))
        max_rounds = min(role.max_rounds, self._policy.limits.max_agent_rounds)
        if node.max_rounds:
            max_rounds = min(max_rounds, node.max_rounds)
        # 轮数**不因升档而放宽**（``tier_override`` 非空 = 这是升档后的那一趟）。
        # 升档改的是"用更强的模型"，再顺手加轮数就等于一次改了两个变量：真变好了
        # 也说不清是模型的功劳还是预算的功劳，而这两者的成本含义完全不同。
        # 轮数是有界循环的硬界（还要被 limits.max_agent_rounds 再夹一次），
        # 该修的是"模型为什么在 4 轮里收敛不了"（提示词），不是把界放宽。

        # 角色提示词里可用的模板变量就这几个，一次给全。**不给超集**——
        # fill 是严格的，多给不会出错，但少给会当场报错，那正是我们要的：
        # 提示词里写了 {{x}} 而没人填，模型就会看到字面的花括号。
        #
        # **节点输入（``args``）不在其中，这是有意的。** 它是上游产出与用户数据的
        # 合流，属于不可信输入：只以 ``data_block`` 进下面的 user 消息。以前这里
        # 还有一个 ``inputs``，等于把同一份数据在 system 与 user 各放一份——冗余，
        # 而且把用户可控的内容放进了模型最信任的槽位，注入即提权。三份引用了
        # ``{{inputs}}`` 的角色提示词（receipt_extractor / merchant_classifier /
        # ledger_auditor）已改为指向下面那个数据块——它们自己的开头本来就写着
        # "你的系统提示词不得由外部数据填充"。
        tools_line = ", ".join(sorted(allowed)) or "（无）"
        system = fill(
            self._prompts.get(role.system_prompt_ref.replace("prompts/", "")),
            {
                "tools": tools_line,
                "tool_whitelist": tools_line,
                "node_goal": node.name or node.subtask_id,
                "output_schema": node.output_schema_ref or "（未声明具体结构，按工具语义产出）",
            },
        ) + "\n\n" + _AGENT_CONTRACT.replace("{tools}", tools_line)

        messages = [
            _sys(system),
            _user(
                data_block(
                    f"节点任务：{node.name or node.subtask_id}",
                    json.dumps({"inputs": args, "node": node.subtask_id}, ensure_ascii=False, indent=2),
                )
            ),
        ]

        last_reason: str | None = None
        for round_no in range(1, max_rounds + 1):
            self._cancel.raise_if_cancelled()
            try:
                # 走 ctx.llm_json 而不是 self._llm：**只有这样才会记账**。
                # agent 循环是最费钱的一环，不记账的话预算与 04 的成本分析全是空的。
                raw, _res = await ctx.llm_json(
                    messages,
                    requires=tuple(role.requires),
                    max_repair_attempts=self._policy.evaluator.max_repair_attempts,
                    timeout_ms=node.timeout_ms,
                    note=f"agent:{node.role}",
                )
            except Exception as e:
                return ToolResult.fail("upstream_llm_error", str(e), retryable=True)

            calls = raw.get("tool_calls") or []
            final = raw.get("final")
            give_up = raw.get("give_up")

            await ctx.emit(
                "agent.round",
                {"subtask_id": node.subtask_id, "role": node.role, "round": round_no,
                 "max_rounds": max_rounds,
                 "tool_calls": [c.get("name") for c in calls if isinstance(c, dict)],
                 "stop_reason": None, "tier": ctx.resolve_tier(tuple(role.requires))},
            )

            if give_up:
                last_reason = str(give_up.get("reason", "模型放弃"))
                break

            if calls:
                messages.append(_assistant(json.dumps(raw, ensure_ascii=False)))
                for call in calls:
                    name = call.get("name")
                    if name not in allowed:
                        # 白名单之外的调用直接拒绝并回灌。这是两级白名单的第二级：
                        # 第一级在计划校验时确保白名单 ⊆ 角色允许集。
                        messages.append(
                            _user(f"工具 {name!r} 不在你的可用列表里，调用被拒绝。")
                        )
                        continue
                    handler = self._registry.executable(node.handler)
                    if handler is None:
                        messages.append(_user(f"工具 {name!r} 没有可执行实现。"))
                        continue
                    try:
                        sub = await handler.execute_tool(name, call.get("args") or {}, ctx)
                    except Exception as e:
                        messages.append(_user(f"工具 {name!r} 抛错：{e}"))
                        continue
                    payload = (
                        sub.output
                        if sub.ok
                        else {"error": sub.failure.code if sub.failure else "failed"}
                    )
                    messages.append(
                        _user(data_block(f"工具 {name} 的返回", json.dumps(payload, ensure_ascii=False)))
                    )
                continue

            if isinstance(final, dict) and final:
                await ctx.emit(
                    "agent.round",
                    {"subtask_id": node.subtask_id, "role": node.role, "round": round_no,
                     "max_rounds": max_rounds, "tool_calls": [],
                     "stop_reason": "output_satisfied_schema"},
                )
                return ToolResult(ok=True, output=final)

            messages.append(_user("你没有给出 final，也没有调用工具。请按约定输出。"))

        # 轮数耗尽 / 模型放弃 —— 按 on_round_limit 处置
        policy = node.on_round_limit or role.on_round_limit or "fail_task"
        await ctx.emit(
            "agent.round",
            {"subtask_id": node.subtask_id, "role": node.role, "round": max_rounds,
             "max_rounds": max_rounds, "tool_calls": [],
             "stop_reason": "repeated_no_progress" if last_reason else "round_limit"},
        )
        if policy == "accept_partial":
            return ToolResult(ok=True, output={"partial": True, "note": last_reason or "轮数耗尽"})
        if policy == "escalate_and_retry":
            # 交给 runner：它看到这个码会升档重试一次（受 max_escalations 约束）
            return ToolResult.fail(
                "schema_validation_failed",
                f"agent 在 {max_rounds} 轮内未产出合格结果",
                retryable=False,
            )
        return ToolResult.fail(
            "round_limit_exceeded", f"agent 在 {max_rounds} 轮内未产出合格结果", retryable=False
        )

    # ------------------------------------------------------------------
    async def _verify(
        self, node: Any, args: dict, primary: ToolResult, ctx: DispatchContext, scope: dict
    ) -> ToolResult:
        """独立复核 + 仲裁。

        **复核者必须独立取数。** 这里给它的只有节点输入与主产出，不给主执行者的
        中间推理——否则它只是在给同一个错误背书，而那样的验证比没有验证更糟，
        因为它给出了虚假的确定感。
        """
        v = node.verification
        assert v is not None
        if v.mode == "self_check":
            reviewers = 1
            reviewer_role = node.role
        else:
            reviewers = min(
                v.reviewers or self._policy.limits.max_reviewers,
                self._policy.limits.max_reviewers,
            )
            reviewer_role = v.reviewer_role or node.role

        question = data_block(
            "待复核的产出",
            json.dumps({"inputs": args, "output": primary.output}, ensure_ascii=False, indent=2),
        )

        async def one(idx: int) -> dict | None:
            try:
                raw, _ = await ctx.llm_json(
                    [_sys(self._reviewer_system(reviewer_role)), _user(question)],
                    requires=("text",),
                    max_repair_attempts=0,
                    timeout_ms=node.timeout_ms,
                    note="verify:reviewer",
                )
                return raw
            except Exception:
                return None

        verdicts = [x for x in await asyncio.gather(*(one(i) for i in range(reviewers))) if x]
        agrees = sum(1 for x in verdicts if str(x.get("verdict", "")).lower() in {"ok", "agree", "一致"})
        agreement = (agrees / len(verdicts)) if verdicts else 0.0

        outcome = "unanimous" if agreement == 1.0 and verdicts else "arbitrated"
        arbiter_note: str | None = None
        if verdicts and agreement < 1.0:
            arbiter_role = v.arbiter_role or "arbiter"
            try:
                arb_raw, _ = await ctx.llm_json(
                    [
                        _sys(self._arbiter_system(arbiter_role)),
                        _user(data_block("各复核者的意见", json.dumps(verdicts, ensure_ascii=False))),
                    ],
                    requires=("text", "reasoning.strong"),
                    max_repair_attempts=0,
                    timeout_ms=node.timeout_ms,
                    note="verify:arbiter",
                )
            except Exception:
                arb_raw = {}
            decision = str(arb_raw.get("decision", "")).lower()
            arbiter_note = str(arb_raw.get("reason", "")) or None
            if decision in {"undecidable", "无法裁决"}:
                outcome = "undecidable"
            else:
                outcome = "arbitrated"

        await ctx.emit(
            "agent.review",
            {"subtask_id": node.subtask_id, "mode": v.mode, "reviewers": len(verdicts) or reviewers,
             "agreement": agreement, "outcome": outcome,
             "arbiter_role": v.arbiter_role, "note": arbiter_note},
        )

        if outcome == "undecidable":
            on_dis = v.on_disagreement or "arbiter_decides"
            if on_dis == "ask_user":
                # 无法裁决时问用户，而不是强行选一个——金融对账场景的默认
                return ToolResult.confirm(
                    "复核者对这份结果无法达成一致，请确认后再继续。",
                    [{"id": "accept", "label": "以现有结果继续"}, {"id": "reject", "label": "重做"}],
                )
            if on_dis == "fail_task":
                return ToolResult.fail("verification_undecidable", "复核无法裁决", retryable=False)
        return primary

    def _reviewer_system(self, role_id: str | None) -> str:
        name = f"agents/{role_id}.md" if role_id else "agents/verifier.md"
        try:
            return self._prompts.get(name)
        except Exception:
            return self._prompts.get("agents/verifier.md")

    def _arbiter_system(self, role_id: str) -> str:
        try:
            return self._prompts.get(f"agents/{role_id}.md")
        except Exception:
            return self._prompts.get("agents/arbiter.md")


# ---------------------------------------------------------------------------
def _sys(content: str) -> Any:
    from ..ports.llm import LLMMessage

    return LLMMessage.system(content)


def _user(content: str) -> Any:
    from ..ports.llm import LLMMessage

    return LLMMessage.user(content)


def _assistant(content: str) -> Any:
    from ..ports.llm import LLMMessage

    return LLMMessage.assistant(content)


__all__ = ["NodeExecutor"]
