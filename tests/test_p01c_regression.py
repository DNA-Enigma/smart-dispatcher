"""P0-1c 回归：已声明意图的带图记账被拆成空计划后整单 422。

来自消费端 2026-10-06 的实测报告。原始症状：

    subtitle  = generic.unknown · generic      ← 画像落到兜底类型
    decision  = single_tool_action
    error     = 拆解在 2 次尝试后仍未产出合法计划：['strategy=single_step 但节点数为 0']

查证结论是**两条互不相干的缺陷**，因此这里也分两组测试：

**A. 空计划被说成了策略不匹配。** ``validate_plan`` 里唯一会亮的是
``strategy=single_step 但节点数为 0``——那是从「单步策略却不是一个节点」反推出来的
派生症状。真正发生的事是"拆解器一个节点都没产出"，而这句话在错误消息里根本没出现，
排查的人会去翻策略。现在空计划作为**一等条件**最早判、单独说，并且不再往下走
（空集上其余检查全部恒真，让它们参与只会往 violations 里灌噪音）。

**B. 评估器降级时把调用方的权威声明丢了。** ``declared.authoritative: true`` 是契约里
唯一的合法快捷路径（``schemas/task_envelope.json``），调用方在做结构化断言"我已经知道
这是什么"。评估器一失败，``_fallback_raw()`` 无条件返回 ``generic.unknown``，于是：

    画像能力候选为空 → 流程模板匹配不上 → 自由拆解拿着空画像拆 → 空计划 → 422

声明本来就在手里。现在只要声明**在封闭词表里**就沿用它，并据其 ``domain``
（按命名约定即 ``handler_id``）从 handler 自己的声明里推出候选能力——纯集合读取，
降级路径上不再引入一次抽样。

关于 ``authoritative`` 的一句真话：它在修复前**是一个惰性字段**，全仓没有任何读取点；
报告里"true 失败、false 成功"的对照是 LLM 非确定性（路由与拆解都是采样调用），
不是该标志造成的。因此 A 组与 B 组都不是"让 true 变成 true"的开关，
而是让**真正会坏的那条路径**不再静默地产出垃圾。
"""

from __future__ import annotations

import asyncio

import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetLedger
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.core.eventbus import EventBus
from dispatcher.core.plan import ExecutionPlan, Node, PlanBudget, validate_plan
from dispatcher.core.policy import Policy, load_policy
from dispatcher.core.pricing import load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.taxonomy import Taxonomy, load_taxonomy
from dispatcher.pipeline import Dispatcher
from dispatcher.plugins import build_registry
from dispatcher.ports.llm import LLMError
from tests.fakes import ScriptedLLM

DECLARED_INTENT = "bookkeeping.capture_from_receipt"

RECEIPT_DECISION = {
    "route_id": "vision_extract_then_write",
    "model_tier": "standard",
    "handler": "bookkeeping",
    "tool_set": ["extract_receipt_fields", "normalize_merchant", "dedupe_check",
                 "build_ledger_entry"],
    "execution_mode": "async",
    "decompose": True,
    "budget": {"max_cost": 0.08, "max_wall_ms": 60000, "max_llm_calls": 8},
    "rationale": "含截图，需抽取后落账。",
    "confidence": 0.88,
}

# 拆解器在"什么都不产出"时给出的两种真实形状：显式空数组，以及干脆不给 nodes。
EMPTY_PLAN_SHAPES = [{"strategy": "single_step", "nodes": []}, {"strategy": "single_step"}]

# 兜底画像的形状：词表兜底类型 + 空能力候选。P0-1c 现场就是这个样子。
DEGRADED_PROFILE = {
    "task_type": "generic.unknown",
    "intent_summary": "评估器未能完成，使用兜底画像。",
    "complexity": {"score": 0.0, "reasons": ["评估器降级"]},
    "candidate_capabilities": [],
    "required_capabilities": [],
    "recommended_mode": "async",
    "needs_clarification": False,
    "confidence": 0.0,
}

# 无 hint、path=decompose 的路由：模板匹配只能靠能力交集，而交集为空 ⇒ 必然走自由拆解。
FREEFORM_DECISION = {
    "route_id": "multi_step_analysis",
    "model_tier": "standard",
    "handler": "bookkeeping",
    "tool_set": ["extract_receipt_fields"],
    "execution_mode": "async",
    "decompose": True,
    "budget": {"max_cost": 0.08, "max_wall_ms": 60000, "max_llm_calls": 8},
    "rationale": "多步分析。",
    "confidence": 0.5,
}


# ---------------------------------------------------------------------------
def make_dispatcher(llm, *, execution_enabled: bool = True) -> Dispatcher:
    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    st = InMemoryStateStore()
    return Dispatcher(
        policy=policy,
        pricing=pricing,
        registry=build_registry(REPO_ROOT / "config" / "handlers.yaml"),
        prompts=PromptLibrary(s.prompts_dir),
        llm=llm,
        media=InMemoryMediaStore(
            allowed_mime=policy.limits.media.allowed_mime,
            max_bytes=policy.limits.media.max_bytes,
        ),
        state=st,
        taxonomy=load_taxonomy(s.taxonomy_path),
        agents=load_agents(s.agents_path),
        events=EventBus(st),
        ledger=BudgetLedger(
            enforcement=policy.enforcement_mode,
            warn_at_ratio=policy.budget.warn_at_ratio,
            currency=policy.budget.currency,
        ),
        execution_enabled=execution_enabled,
    )


async def receipt_envelope(dispatcher: Dispatcher, *, authoritative: bool, intent: str | None):
    rec = await dispatcher.media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
    declared: dict = {"authoritative": authoritative}
    if intent is not None:
        declared["intent"] = intent
    return TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {
            "text": "午饭花了38",
            "media": [{"media_id": rec.media_id, "kind": "image", "mime": "image/png",
                       "role": "screenshot"}],
        },
        "declared": declared,
        "constraints": {"mode_preference": "async"},
    })


async def drain(dispatcher: Dispatcher, task_id: str) -> None:
    from dispatcher.core.state import TERMINAL_STATUSES

    for _ in range(300):
        if (await dispatcher.get(task_id)).status in TERMINAL_STATUSES:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在等待窗口内没有到达终态")


# ===========================================================================
# A 组 —— 空计划必须因"空"这一条而失败，且只因这一条
# ===========================================================================
def test_empty_plan_fails_specifically_for_being_empty(
    policy: Policy, registry, taxonomy: Taxonomy
):
    """反例因**该失败的那一条**而失败：violations 里只有这一条，且说清了是"没有节点"。

    断言"只有一条"是刻意的：空集上依赖、工具、join、预算检查全部恒真，
    若它们也被放进来，这条消息会淹没在噪音里，而真正的原因会被稀释。
    """
    agents = load_agents(get_settings().agents_path)
    plan = ExecutionPlan(
        task_id="t", strategy="single_step", max_parallelism=1, nodes=[],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=1),
    )
    v = validate_plan(plan, policy=policy, registry=registry, tool_set=[], agents=agents)

    assert len(v) == 1, f"空计划应当只报一条，实际：{v}"
    assert "没有任何节点" in v[0]
    # 旧消息是把"没有步骤"说成"策略与节点数不匹配"——排查时会去翻策略，方向错了
    assert "strategy=single_step" not in v[0]


def test_nonempty_plans_are_not_flagged_by_the_empty_check(policy: Policy, registry):
    """放行路径必须有测试盖住——只测拒绝的测试抓不到"约束过宽把合法输入全拒了"。"""
    agents = load_agents(get_settings().agents_path)

    single = ExecutionPlan(
        task_id="t", strategy="single_step", max_parallelism=1,
        nodes=[Node(subtask_id="main", handler="calendar", executor="tool", tool="create_event")],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=1),
    )
    assert validate_plan(single, policy=policy, registry=registry, tool_set=[],
                         agents=agents) == []

    dag = ExecutionPlan(
        task_id="t", strategy="dag", max_parallelism=2,
        nodes=[
            Node(subtask_id="extract", handler="bookkeeping", executor="agent",
                 role="receipt_extractor", tool_whitelist=["extract_receipt_fields"],
                 max_rounds=1),
            Node(subtask_id="write", handler="bookkeeping", executor="tool",
                 tool="build_ledger_entry", depends_on=["extract"]),
        ],
        edges=[{"from": "extract", "to": "write"}],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=4),
    )
    assert validate_plan(dag, policy=policy, registry=registry, tool_set=[],
                         agents=agents) == []


@pytest.mark.parametrize("empty_raw", EMPTY_PLAN_SHAPES)
async def test_empty_decomposition_names_the_empty_plan_not_the_strategy(empty_raw):
    """端到端：拆解器两次都产出空计划 → 报错必须指向"没有节点"，不是策略不匹配。

    这是 P0-1c 消费端看到的那条消息。它当时是
    ``['strategy=single_step 但节点数为 0']``——一个对消费端没有可操作性的说法。
    """
    llm = ScriptedLLM([DEGRADED_PROFILE, FREEFORM_DECISION, empty_raw, empty_raw])
    d = make_dispatcher(llm)
    try:
        env = await receipt_envelope(d, authoritative=False, intent=None)
        with pytest.raises(DispatcherError) as ei:
            await d.submit(env)
        assert ei.value.code == "policy_violation"
        assert ei.value.fatal is True
        assert "没有任何节点" in ei.value.detail
        assert "strategy=single_step 但节点数为 0" not in ei.value.detail
    finally:
        await d.aclose()


# ===========================================================================
# B 组 —— 权威声明在评估器降级时不能丢
# ===========================================================================
def _evaluator_fails_twice() -> ScriptedLLM:
    """评估器的两次尝试（原档 + 升级档）都失败，之后恢复正常。

    策略里 ``evaluator.escalation.max_escalations=1``，所以带图请求会有两次尝试；
    两次都要失败才会走兜底画像。
    """
    return ScriptedLLM(
        [RECEIPT_DECISION,
         {"final": {"amount": 38.5, "currency": "CNY", "merchant": "星巴克咖啡（国贸店）",
                    "direction": "expense", "category": "餐饮", "confidence": 0.91}},
         {"final": {"merchant": "星巴克咖啡（国贸店）", "category": "餐饮"}}],
        fail_with=LLMError("上游抖动（503）", retryable=True, kind="transient"),
        fail_first_n=2,
    )


async def test_authoritative_declaration_survives_evaluator_degradation(taxonomy: Taxonomy):
    """评估器降级后，画像沿用调用方声明的类型与能力——而不是 generic.unknown。"""
    d = make_dispatcher(_evaluator_fails_twice())
    try:
        env = await receipt_envelope(d, authoritative=True, intent=DECLARED_INTENT)
        task = await d.submit(env)

        assert task.profile is not None
        assert task.profile.degraded is True, "降级这件事仍然要如实记录"
        assert task.profile.task_type == DECLARED_INTENT
        assert task.profile.task_type != taxonomy.fallback_type
        # 能力候选从声明的 domain（= handler_id）旗下的声明推出，非空才有模板可命中
        assert "bookkeeping.expense.record" in task.profile.candidate_capabilities
        # 敏感级也来自词表，不是编的
        assert task.profile.data_sensitivity == "financial"
    finally:
        await d.aclose()


async def test_degraded_without_authoritative_still_uses_fallback_type(taxonomy: Taxonomy):
    """反向对照：没有权威声明时兜底行为**不变**。

    契约写得很清楚——``authoritative: false`` 时 intent 只是"待验证的提示"，不是事实。
    所以这条路径必须仍然落到 generic.unknown。没有这条测试，上面那条修复就可能是
    "把所有降级都当成声明处理"这种过宽的改动而没人发现。
    """
    d = make_dispatcher(_evaluator_fails_twice())
    try:
        env = await receipt_envelope(d, authoritative=False, intent=DECLARED_INTENT)
        task = await d.submit(env)
        assert task.profile.task_type == taxonomy.fallback_type
        assert task.profile.candidate_capabilities == []
    finally:
        await d.aclose()


async def test_authoritative_intent_outside_taxonomy_is_not_a_privilege(taxonomy: Taxonomy):
    """声明是断言，不是特权：越出封闭词表的声明等于没有声明。

    词表封闭是刻意的（``core/taxonomy.py``）——开放词表会让 04 无法统计词表缺项。
    若这里放行，调用方就能用一个词表外的类型名把任意字符串塞进 task_type。
    """
    d = make_dispatcher(_evaluator_fails_twice())
    try:
        env = await receipt_envelope(d, authoritative=True, intent="totally.made.up")
        task = await d.submit(env)
        assert task.profile.task_type == taxonomy.fallback_type
    finally:
        await d.aclose()


async def test_p01c_degraded_authoritative_reaches_succeeded(taxonomy: Taxonomy):
    """P0-1c 的验收：评估器降级 + 权威声明 → 模板命中 → 整单 succeeded。

    这正是消费端复现命令跑的那条路径。修复前它会在自由拆解拿到空画像后
    以 422 收场；现在画像带着声明与能力，模板按 hint + handler + 带图命中，
    四个节点真的跑完，产出待入账凭证。
    """
    d = make_dispatcher(_evaluator_fails_twice())
    try:
        env = await receipt_envelope(d, authoritative=True, intent=DECLARED_INTENT)
        task = await d.submit(env)
        await drain(d, task.task_id)
        cur = await d.get(task.task_id)

        assert cur.status == "succeeded", cur.error

        # 画像没落到兜底类型——这是消费端报的第一条症状
        assert cur.profile.task_type == DECLARED_INTENT

        # 走的是模板，不是"自由拆解撞运气"
        assert cur.plan_meta["source"] == "flow_template:receipt_to_entry"
        assert cur.plan_meta["template_miss"] is False
        assert len(cur.plan["nodes"]) == 4

        # 真的产出了凭证，而不是"成功但空手而归"
        assert cur.artifacts["write"]["amount"] == 38.5
        assert cur.artifacts["write"]["direction"] == "expense"
    finally:
        await d.aclose()
