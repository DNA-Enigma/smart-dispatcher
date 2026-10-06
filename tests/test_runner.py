"""DagRunner 的调度语义。

这些测试针对的是**并发与失败处理**，与"节点具体做什么"无关——所以它们用一个
可编程的假执行器，而不是真工具。这正是把"调度"与"执行"分开的收益：
并发语义可以脱离领域单独验证。

它们逐条对应 ``duowei-ai`` 现有实现里的缺陷（硬编码波次、模块级并发常量、
没有取消传播、异常被拼成字符串）。所以这些测试同时也是"没有把那套照搬过来"的证明。
"""

from __future__ import annotations

import asyncio

import pytest

from dispatcher.core.budget import BudgetLedger
from dispatcher.core.cancel import CancellationToken
from dispatcher.core.eventbus import EventBus
from dispatcher.core.execution import ToolResult
from dispatcher.core.plan import ExecutionPlan, Node, PlanBudget, Retry
from dispatcher.core.runner import DagRunner


class FakeExecutor:
    """可编程的节点执行器。

    每个节点用 ``behaviour`` 描述：一个可调用 ``(node, attempt, tier_override, scope)``。
    同时记录**实际并发峰值**与每步的进入/退出顺序——并发语义只能这样验。
    """

    def __init__(self, behaviours: dict | None = None, default=None) -> None:
        self.behaviours = behaviours or {}
        self.default = default
        self.peak_concurrency = 0
        self.current = 0
        self.order: list[str] = []
        self.attempts: dict[str, int] = {}
        self.tiers_used: dict[str, list[str | None]] = {}

    async def __call__(self, node, *, attempt, tier_override, scope):
        self.current += 1
        self.peak_concurrency = max(self.peak_concurrency, self.current)
        self.order.append(node.subtask_id)
        self.attempts[node.subtask_id] = attempt
        self.tiers_used.setdefault(node.subtask_id, []).append(tier_override)
        try:
            fn = self.behaviours.get(node.subtask_id, self.default)
            if fn is None:
                return ToolResult(ok=True, output={"node": node.subtask_id})
            return await fn(node, attempt, tier_override, scope)
        finally:
            self.current -= 1


def node(sid: str, *, deps=None, **kw) -> Node:
    base = dict(
        subtask_id=sid, handler="h", executor="tool", tool="t", depends_on=deps or []
    )
    base.update(kw)
    return Node(**base)


def plan_of(*nodes: Node, max_parallelism: int = 4, max_cost: float = 1.0) -> ExecutionPlan:
    edges = [{"from": d, "to": n.subtask_id} for n in nodes for d in n.depends_on]
    return ExecutionPlan(
        task_id="task_test",
        strategy="dag",
        max_parallelism=max_parallelism,
        nodes=list(nodes),
        edges=edges,
        plan_budget=PlanBudget(max_cost=max_cost, max_wall_ms=30_000, max_llm_calls=10),
    )


def build(executor: FakeExecutor, *, enforcement: str = "advisory", cancel=None):
    from dispatcher.core.policy import load_policy
    from dispatcher.core.settings import REPO_ROOT

    policy = load_policy(REPO_ROOT / "config" / "routing.policy.yaml")
    ledger = BudgetLedger(enforcement=enforcement, warn_at_ratio=0.8, currency="CNY")
    store = __import__(
        "dispatcher.adapters.memory_state", fromlist=["InMemoryStateStore"]
    ).InMemoryStateStore()
    bus = EventBus(store)
    runner = DagRunner(
        events=bus, ledger=ledger, policy=policy, executor=executor, cancellation=cancel
    )
    return runner, bus, store


async def slow_ok(delay: float = 0.02, output=None):
    async def fn(n, attempt, tier, scope):
        await asyncio.sleep(delay)
        return ToolResult(ok=True, output=output or {"node": n.subtask_id})
    return fn


# ---------------------------------------------------------------------------
# 并发与拓扑
# ---------------------------------------------------------------------------
async def test_independent_nodes_run_concurrently():
    ex = FakeExecutor(default=await slow_ok(0.03))
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(node("a"), node("b"), node("c")), task_id="t1", scope={}
    )
    assert report.status == "succeeded"
    assert ex.peak_concurrency == 3, "三个互不依赖的节点应当真的并行"


async def test_parallelism_is_capped_by_the_plan_not_a_constant():
    """并发度来自计划。改成 2 就必须真的只有 2——这是它区别于模块常量的地方。"""
    ex = FakeExecutor(default=await slow_ok(0.03))
    runner, _, _ = build(ex)
    await runner.run(
        plan_of(node("a"), node("b"), node("c"), node("d"), max_parallelism=2),
        task_id="t1", scope={},
    )
    assert ex.peak_concurrency == 2


async def test_dependencies_are_respected():
    ex = FakeExecutor(default=await slow_ok(0.005))
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(node("a"), node("b", deps=["a"]), node("c", deps=["b"])),
        task_id="t1", scope={},
    )
    assert report.status == "succeeded"
    assert ex.order.index("a") < ex.order.index("b") < ex.order.index("c")


async def test_diamond_join_waits_for_both_branches():
    ex = FakeExecutor(default=await slow_ok(0.01))
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(
            node("head"),
            node("left", deps=["head"]),
            node("right", deps=["head"]),
            node("tail", deps=["left", "right"]),
        ),
        task_id="t1", scope={},
    )
    assert report.status == "succeeded"
    assert ex.order.index("tail") > ex.order.index("left")
    assert ex.order.index("tail") > ex.order.index("right")
    assert report.progress == 1.0


# ---------------------------------------------------------------------------
# 失败处理
# ---------------------------------------------------------------------------
async def test_retry_on_retryable_failure_then_succeed():
    calls = {"n": 0}

    async def flaky(n, attempt, tier, scope):
        calls["n"] += 1
        if calls["n"] < 3:
            return ToolResult.fail("upstream_unavailable", "暂时不可用", retryable=True)
        return ToolResult(ok=True, output={"ok": True})

    ex = FakeExecutor({"a": flaky})
    runner, bus, store = build(ex)
    report = await runner.run(
        plan_of(node("a", retry=Retry(max=3, backoff_ms=0))), task_id="t1", scope={}
    )
    assert report.status == "succeeded"
    assert report.nodes["a"].attempts == 3
    types = [e.type for e in await store.read_events("t1")]
    assert "subtask.retrying" in types, "重试必须发事件，否则它是神秘的"


async def test_non_retryable_failure_does_not_retry():
    ex = FakeExecutor({"a": lambda n, a, t, s: _fail("bad_input", retryable=False)})
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(node("a", retry=Retry(max=5, backoff_ms=0))), task_id="t1", scope={}
    )
    assert report.status == "failed"
    assert report.nodes["a"].attempts == 1
    assert report.nodes["a"].failure.code == "bad_input"


async def _fail(code: str, *, retryable: bool):
    return ToolResult.fail(code, "boom", retryable=retryable)


async def test_continue_with_default_lets_downstream_proceed():
    """可选步骤失败不该阻断整条流程——这是 ``defaulted`` 存在的意义。"""
    ex = FakeExecutor(
        {
            "dedupe": lambda n, a, t, s: _fail("upstream_unavailable", retryable=False),
        },
        default=await slow_ok(0.001),
    )
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(
            node("extract"),
            node("dedupe", deps=["extract"], optional=True,
                 on_failure="continue_with_default", default_output={"duplicate": False}),
            node("write", deps=["dedupe"]),
        ),
        task_id="t1", scope={},
    )
    assert report.status == "succeeded"
    assert report.nodes["dedupe"].status == "defaulted"
    assert report.nodes["dedupe"].output == {"duplicate": False}
    assert report.nodes["write"].status == "succeeded"


async def test_fail_task_aborts_and_skips_downstream():
    ex = FakeExecutor(
        {"extract": lambda n, a, t, s: _fail("bad_document", retryable=False)},
        default=await slow_ok(0.001),
    )
    runner, _, store = build(ex)
    report = await runner.run(
        plan_of(node("extract"), node("write", deps=["extract"])),
        task_id="t1", scope={},
    )
    assert report.status == "failed"
    assert report.nodes["extract"].status == "failed"
    assert report.nodes["write"].status == "skipped"
    # 被跳过的节点也要发事件——"为什么这步没跑"是使用者一定会问的问题
    skipped = [
        e for e in await store.read_events("t1")
        if e.type == "subtask.failed" and e.data.get("error", {}).get("code") == "upstream_failed"
    ]
    assert skipped and skipped[0].subtask_id == "write"


async def test_skip_policy_marks_skipped_and_frees_downstream():
    ex = FakeExecutor(
        {"opt": lambda n, a, t, s: _fail("unsupported", retryable=False)},
        default=await slow_ok(0.001),
    )
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(node("opt", on_failure="skip"), node("after", deps=["opt"])),
        task_id="t1", scope={},
    )
    assert report.status == "succeeded"
    assert report.nodes["opt"].status == "skipped"
    assert report.nodes["after"].status == "succeeded"


# ---------------------------------------------------------------------------
# 档位升级
# ---------------------------------------------------------------------------
async def test_schema_failure_triggers_one_escalation():
    calls = {"n": 0}

    async def picky(n, attempt, tier_override, scope):
        calls["n"] += 1
        if tier_override is None:
            return ToolResult.fail("schema_validation_failed", "输出不合 schema", retryable=False)
        return ToolResult(ok=True, output={"ok": True})

    ex = FakeExecutor({"a": picky})
    runner, _, store = build(ex)
    report = await runner.run(
        plan_of(node("a", model_tier="standard")), task_id="t1", scope={}, max_escalations=1
    )
    assert report.status == "succeeded"
    assert ex.tiers_used["a"] == [None, "strong"], "第一次用原档位，升级后用更强档位"
    types = [e.type for e in await store.read_events("t1")]
    assert "task.escalated" in types


async def test_escalation_is_bounded_by_the_given_limit():
    """升级次数是硬界。设成 0 就一次都不许升——无界升级就是无界花钱。"""
    ex = FakeExecutor(
        {"a": lambda n, a, t, s: ToolResult.fail("schema_validation_failed", "x")}
    )
    runner, _, _ = build(ex)
    report = await runner.run(
        plan_of(node("a", model_tier="standard")), task_id="t1", scope={}, max_escalations=0
    )
    assert report.status == "failed"
    assert all(t is None for t in ex.tiers_used["a"])


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
async def test_cancellation_propagates_to_inflight_nodes():
    """取消必须传到执行中的节点。现有实现里没有这个——断连后 gather 还在跑。"""
    token = CancellationToken()
    started = asyncio.Event()

    async def blocked(n, attempt, tier, scope):
        started.set()
        await asyncio.sleep(5)
        return ToolResult(ok=True)

    ex = FakeExecutor({"a": blocked, "b": blocked})
    runner, _, _ = build(ex, cancel=token)
    task = asyncio.create_task(
        runner.run(plan_of(node("a"), node("b")), task_id="t1", scope={})
    )
    await started.wait()
    token.cancel("client_requested")
    report = await asyncio.wait_for(task, timeout=2)

    assert report.status == "cancelled"
    assert all(r.status == "cancelled" for r in report.nodes.values())


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------
async def test_advisory_mode_never_aborts_even_when_over_budget():
    """当前默认模式下超限**不中断**——这是 advisory 的全部意义。"""
    async def costly(n, attempt, tier, scope):
        return ToolResult(ok=True, output={"x": 1}, cost=0.6)

    ex = FakeExecutor(default=costly)
    runner, _, store = build(ex, enforcement="advisory")
    report = await runner.run(
        plan_of(node("a"), node("b"), max_cost=1.0), task_id="t1", scope={}
    )
    assert report.status == "succeeded"
    assert report.spent == pytest.approx(1.2)
    types = [e.type for e in await store.read_events("t1")]
    assert "budget.warning" in types


async def test_hard_mode_aborts_and_reports_budget_exceeded():
    async def costly(n, attempt, tier, scope):
        return ToolResult(ok=True, output={"x": 1}, cost=0.6)

    ex = FakeExecutor(default=costly)
    runner, _, store = build(ex, enforcement="hard")
    report = await runner.run(
        plan_of(node("a"), node("b"), node("c"), max_parallelism=1, max_cost=1.0),
        task_id="t1", scope={},
    )
    assert report.status == "failed"
    types = [e.type for e in await store.read_events("t1")]
    assert "budget.exceeded" in types


async def test_node_cost_is_per_node_not_the_task_total():
    """节点的成本必须是它自己的。记成任务总成本的话，"贵在哪一步"就永远答不出来。"""
    async def cost_a(n, attempt, tier, scope):
        return ToolResult(ok=True, output={"n": "a"}, cost=0.1)

    async def cost_b(n, attempt, tier, scope):
        return ToolResult(ok=True, output={"n": "b"}, cost=0.25)

    ex = FakeExecutor({"a": cost_a, "b": cost_b})
    runner, _, _ = build(ex)
    report = await runner.run(plan_of(node("a"), node("b")), task_id="t1", scope={})
    assert report.nodes["a"].cost == pytest.approx(0.1)
    assert report.nodes["b"].cost == pytest.approx(0.25)
    assert report.spent == pytest.approx(0.35)


# ---------------------------------------------------------------------------
# 澄清
# ---------------------------------------------------------------------------
async def test_confirmation_pauses_the_run_and_keeps_completed_work():
    """停在等人**不是失败**：已完成的节点产物必须保留。"""
    from dispatcher.core.execution import ConfirmationRequest

    async def ask(n, attempt, tier, scope):
        return ToolResult(
            ok=True,
            needs_confirmation=ConfirmationRequest(question="支出还是收入？", blocking=True),
        )

    ex = FakeExecutor({"write": ask}, default=await slow_ok(0.001))
    runner, _, store = build(ex)
    report = await runner.run(
        plan_of(node("extract"), node("write", deps=["extract"])),
        task_id="t1", scope={},
    )
    assert report.status == "awaiting_clarification"
    assert report.paused_at == "write"
    assert report.nodes["extract"].status == "succeeded", "已完成的产物不该被丢掉"
    assert report.nodes["write"].status == "pending"
    types = [e.type for e in await store.read_events("t1")]
    assert "clarification.needed" in types


async def test_progress_counts_terminal_nodes():
    ex = FakeExecutor(default=await slow_ok(0.001))
    runner, _, _ = build(ex)
    report = await runner.run(plan_of(node("a"), node("b")), task_id="t1", scope={})
    assert report.progress == 1.0
