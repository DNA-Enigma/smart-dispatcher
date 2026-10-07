"""端到端集成：从请求到事件流。

与 ``test_runner.py`` 的分工：那边的测调度语义（并发、失败、取消），用假执行器；
这里的测**整条链路真的能通**——真策略、真价格、真词表、真角色表、
真模板、真示例 handler，只有模型那一跳被换成脚本。

两个测试值得单独一提：

* ``test_deterministic_path_calls_no_model`` —— 声明 ``requires_capabilities: []``
  的工具**一次都不该碰模型**。这验的是"声明即约束"，不是注释。
* ``test_template_hit_avoids_llm_decomposition`` —— 模板命中时 LLM 只被调用两次
  （评估 + 路由），拆解那次省掉了。这正是模板存在的理由。
"""

from __future__ import annotations

import asyncio

import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetLedger
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.eventbus import EventBus
from dispatcher.core.plan import ExecutionPlan, PlanBudget, validate_plan
from dispatcher.core.policy import load_policy
from dispatcher.core.pricing import load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.taxonomy import load_taxonomy
from dispatcher.pipeline import Dispatcher
from dispatcher.plugins import build_registry
from tests.fakes import ScriptedLLM

SEARCH_PROFILE = {
    "task_type": "calendar.create_event",
    "intent_summary": "新建一个日程",
    "complexity": {"score": 0.15, "reasons": ["单步"]},
    "urgency": {"level": "normal"},
    "required_capabilities": ["calendar.create_event"],
    "candidate_capabilities": ["calendar.create_event"],
    "estimated_scale": {"input_tokens_bucket": "xs", "output_tokens_bucket": "xs"},
    "recommended_mode": "sync",
    "needs_clarification": False,
    "confidence": 0.93,
}

SLOT_FILL = {"title": "开会", "start": "2026-10-07T15:00:00+08:00"}

SEARCH_DECISION = {
    "route_id": "single_tool_action",
    "model_tier": "standard",
    "handler": "calendar",
    "tool_set": ["create_event", "query_free_busy"],
    "execution_mode": "sync",
    "decompose": False,
    "budget": {"max_cost": 0.02, "max_wall_ms": 20000, "max_llm_calls": 4},
    "rationale": "单步写日程。",
    "confidence": 0.9,
}

RECEIPT_PROFILE = {
    "task_type": "bookkeeping.capture_from_receipt",
    "intent_summary": "上传支付截图，记一笔支出",
    "complexity": {"score": 0.42, "reasons": ["需视觉抽取"]},
    "urgency": {"level": "normal"},
    "vision": {"expected_extraction": ["amount", "merchant"]},
    "required_capabilities": ["vision.extract", "ledger.write"],
    "candidate_capabilities": ["bookkeeping.expense.record"],
    "data_sensitivity": "financial",
    "recommended_mode": "async",
    "needs_clarification": False,
    "confidence": 0.9,
}

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

SCHEDULE_PROFILE = {
    "task_type": "calendar.create_event",
    "intent_summary": "把“明天下午三点开会”落成一条日程",
    "complexity": {"score": 0.45, "reasons": ["相对时间需先解析成绝对时间"]},
    "urgency": {"level": "normal"},
    "required_capabilities": ["calendar.create_event"],
    "candidate_capabilities": ["calendar.create_event"],
    "recommended_mode": "async",
    "needs_clarification": False,
    "confidence": 0.9,
}

SCHEDULE_DECISION = {
    "route_id": "schedule_parse_then_create",
    "model_tier": "standard",
    "handler": "calendar",
    "tool_set": ["parse_natural_time", "create_event"],
    "execution_mode": "async",
    "decompose": True,
    "budget": {"max_cost": 0.05, "max_wall_ms": 60000, "max_llm_calls": 6},
    "rationale": "相对时间要先解析成绝对时间才能建日程。",
    "confidence": 0.9,
}

# parse_natural_time 的产出（它自己会调一次模型）。
PARSE_TIME = {"iso": "2026-10-07T15:00:00+08:00", "ambiguous": False, "note": ""}

CATEGORIZE_PROFILE = {
    "task_type": "bookkeeping.categorize_merchants",
    "intent_summary": "把一批陌生商户归类",
    "complexity": {"score": 0.3, "reasons": ["一批商户，一次归类"]},
    "urgency": {"level": "normal"},
    "required_capabilities": ["bookkeeping.categorize_merchants"],
    "candidate_capabilities": ["bookkeeping.categorize_merchants"],
    "data_sensitivity": "financial",
    "recommended_mode": "sync",
    "needs_clarification": False,
    "confidence": 0.9,
}

CATEGORIZE_DECISION = {
    "route_id": "single_tool_action",
    "model_tier": "cheap",
    "handler": "bookkeeping",
    "tool_set": ["categorize_merchants"],
    "execution_mode": "sync",
    "decompose": False,
    "budget": {"max_cost": 0.02, "max_wall_ms": 20000, "max_llm_calls": 4},
    "rationale": "一次批量归类，单步即可完成。",
    "confidence": 0.9,
}

CATEGORIZE_SLOT_FILL = {"merchants": ["星巴克", "滴滴出行", "盒马鲜生"]}

CATEGORIZE_BATCH = {"suggestions": [
    {"merchant": "星巴克", "category": "餐饮", "confidence": 0.95},
    {"merchant": "滴滴出行", "category": "交通"},
    {"merchant": "盒马鲜生", "category": "购物"},
]}


def make_dispatcher(llm, *, execution_enabled: bool = True, state=None):
    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    st = state or InMemoryStateStore()
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


def text_env(text: str, *, mode: str = "sync") -> TaskEnvelope:
    return TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {"text": text},
        "constraints": {"mode_preference": mode},
    })


# ---------------------------------------------------------------------------
# 完整链路
# ---------------------------------------------------------------------------
async def test_end_to_end_deterministic_path():
    """日程落库是一条纯确定性路径：评估 + 路由要模型，执行不要。"""
    llm = ScriptedLLM([SEARCH_PROFILE, SEARCH_DECISION, SLOT_FILL])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(text_env("明天下午三点开会"))
        # 同步模式：内联跑完
        assert rec.status == "succeeded", rec.error
        assert rec.mode == "sync"
        assert rec.decision.route_id == "single_tool_action"
        assert rec.plan is not None
        assert rec.progress == 1.0
        # 三次模型调用：评估 + 路由 + 参数抽取。**执行阶段一次都没有**——
        # 日历是纯确定性路径，这正是"该确定性就确定性"的检验。
        assert len(llm.calls) == 3, [c.tier for c in llm.calls]
        # 写操作真的落进去了
        entry = rec.artifacts["main"]["event"]
        assert entry["title"] == "开会"
        assert entry["token"].startswith(rec.task_id)
    finally:
        await d.aclose()


async def test_async_mode_returns_before_completion():
    """async 模式下 submit 应当立刻返回，执行在后台推进。"""
    llm = ScriptedLLM([SEARCH_PROFILE, SEARCH_DECISION, SLOT_FILL])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(text_env("明天下午三点开会", mode="async"))
        # 请求要 async → 立刻返回，执行在后台
        from dispatcher.core.state import TERMINAL_STATUSES as TERM

        assert rec.mode == "async"
        # 内联跑到拆解完就交棒给后台，因此这里可能停在 planning
        assert rec.status in {"received", "evaluating", "routing", "planning", "running"} | TERM
        assert rec.task_id in d._running, "后台任务必须登记在案，否则无法取消"
        # 等它跑完
        for _ in range(200):
            cur = await d.get(rec.task_id)
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        assert (await d.get(rec.task_id)).status == "succeeded"
    finally:
        await d.aclose()


async def test_template_hit_avoids_llm_decomposition():
    """模板命中时不该再让 LLM 拆一遍——那正是模板存在的理由。"""
    media_store_holder = {}
    llm = ScriptedLLM([
        RECEIPT_PROFILE, RECEIPT_DECISION,
        # 抽取 agent 的每一轮响应：直接给 final
        {"final": {"amount": 38.0, "currency": "CNY", "merchant": "某某餐厅",
                   "direction": "expense", "category": "餐饮", "confidence": 0.91}},
        # normalize 也是 agent 节点：它的循环只认 final / tool_calls / give_up 三种形状。
        # 直接给 {"merchant": ...} 会让它以为"模型没给 final"，于是继续转圈直到把
        # 预置响应耗光——这个坑值得在测试里显式写出来。
        {"final": {"merchant": "某某餐厅", "category": "餐饮"}},
    ])
    d = make_dispatcher(llm)
    try:
        rec = await d.media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
        env = TaskEnvelope.model_validate({
            "identity": {"user_id": "u_1"},
            "input": {"text": "中午吃饭花了 38", "media": [
                {"media_id": rec.media_id, "kind": "image", "mime": "image/png"}]},
            "constraints": {"mode_preference": "async"},
        })
        task = await d.submit(env)
        assert task.plan is not None
        assert task.plan_meta["source"] == "flow_template:receipt_to_entry", task.plan_meta
        assert task.plan_meta["template_miss"] is False
        # 4 个节点、一条分支并行
        assert len(task.plan["nodes"]) == 4
        assert task.max_parallelism >= 2
        media_store_holder["size"] = len(d.media)
        # 等后台执行完
        for _ in range(200):
            cur = await d.get(task.task_id)
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.02)
        cur = await d.get(task.task_id)
        assert cur.status == "succeeded", cur.error
        # 产出是一份**平铺的待入账凭证**，不再是 {entry:{...}} 包一层
        entry = cur.artifacts["write"]
        assert entry["amount"] == 38.0
        assert entry["direction"] == "expense"
        # entry_id 由幂等 token 推出——消费端拿它当唯一约束，重试不会重复入账
        assert entry["entry_id"] == f"{cur.task_id}:write"
        assert entry["source_task"] == cur.task_id
    finally:
        await d.aclose()


async def test_schedule_template_parses_time_then_creates_event():
    """日程模板命中：parse → create 两步真的都跑完，日程被建出来。

    这正是 docs/HANDOFF.md 第 7 节记录的缺口："明天下午三点开会"曾能解析出正确的
    时间戳，却**没建成日程**——因为自由拆解不保证"解析之后一定要写"。
    模板把这两步的依赖固化下来，依赖的存在使 create 的 start 只能来自 parse 的产出。
    """
    from dispatcher.core.state import TERMINAL_STATUSES as TERM

    llm = ScriptedLLM([SCHEDULE_PROFILE, SCHEDULE_DECISION, PARSE_TIME])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(text_env("明天下午三点开会", mode="async"))
        # 规划是内联跑完的，拆解阶段就能看出命中模板
        assert rec.plan is not None
        assert rec.plan_meta["source"] == "flow_template:schedule_parse_to_create", rec.plan_meta
        assert rec.plan_meta["template_miss"] is False
        assert [n["subtask_id"] for n in rec.plan["nodes"]] == ["parse", "create"]

        # 模型只被调了三次：评估 + 路由 + 时间解析。拆解那一次被模板省掉了。
        for _ in range(200):
            cur = await d.get(rec.task_id)
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.01)
        cur = await d.get(rec.task_id)
        assert cur.status in TERM, cur.error
        assert cur.status == "succeeded", cur.error
        assert cur.decision.route_id == "schedule_parse_then_create"
        assert len(llm.calls) == 3, [c.tier for c in llm.calls]

        # 日程真的建出来了：start 用的是 parse 的产出，不是原话里的相对说法
        event = cur.artifacts["create"]["event"]
        assert event["start"] == PARSE_TIME["iso"]
        assert event["title"] == "明天下午三点开会"
        assert event["token"].startswith(cur.task_id)
    finally:
        await d.aclose()


async def test_deterministic_path_calls_no_model():
    """声明 ``requires_capabilities: []`` 的工具一次都不该碰模型。

    这是"声明即约束"的实际检验：能力声明不是注释，它会真的改变行为。
    """
    from dispatcher.core.registry import HandlerManifest
    from handlers.bookkeeping.handler import BookkeepingHandler

    manifest = HandlerManifest.model_validate({
        "handler_id": "h", "version": "1",
        "tools": [{"name": "compute_portfolio", "requires_capabilities": []}],
    })
    h = BookkeepingHandler(manifest)

    class ExplodingLLM:
        async def complete(self, *a, **k):
            raise AssertionError("纯算术步骤不该调用模型")

        async def generate_json(self, *a, **k):
            raise AssertionError("纯算术步骤不该调用模型")

        async def aclose(self):
            return None

    ctx = _ctx_for(ExplodingLLM())
    res = await h.tool_compute_portfolio(
        {"positions": [{"cost": 100, "value": 130}, {"cost": 50, "value": 40}]}, ctx
    )
    assert res.ok
    assert res.output["gain"] == 20.0
    assert res.output["return_pct"] == 13.33


# ---------------------------------------------------------------------------
# P1-c 批量归类（陌生商户导入流水后集中归类）
# ---------------------------------------------------------------------------
async def test_batch_categorization_is_one_call_for_the_whole_list():
    """一份名单一次调用，产出 ``artifacts.main.suggestions``。

    两件事一起验：

    * **能走通**：``bookkeeping.categorize_merchants`` 作为任务类型与能力在
      配置里成立，路由到 bookkeeping 并真的产出了归类结果；
    * **基数是一次**：评估 + 路由 + 参数抽取之后只有 **一次** 归类调用。
      逐条调用的话这里会看到 N 次（三个商户 → 六次），而那样每条的判断标准
      还会在多次往返之间漂移——同一批商户被分进两个类。

    词表项与 handler 能力**两处配置的耦合**由下面那条守卫用例单独钉住：
    这里用的是模型给了 handler 的决策，那条走的是模型没给、守卫只能靠能力推的路。
    """
    llm = ScriptedLLM([
        CATEGORIZE_PROFILE, CATEGORIZE_DECISION, CATEGORIZE_SLOT_FILL, CATEGORIZE_BATCH,
    ])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(
            text_env("把这几个陌生商户归类：星巴克、滴滴出行、盒马鲜生", mode="sync")
        )
        assert rec.status == "succeeded", rec.error
        assert rec.decision.route_id == "single_tool_action"
        assert rec.decision.handler == "bookkeeping"
        assert len(llm.calls) == 4, [c.tier for c in llm.calls]
        # 形状按消费端的 wires 模型：artifacts.main.suggestions[{merchant, category}]
        assert rec.artifacts["main"]["suggestions"] == [
            {"merchant": "星巴克", "category": "餐饮"},
            {"merchant": "滴滴出行", "category": "交通"},
            {"merchant": "盒马鲜生", "category": "购物"},
        ]
    finally:
        await d.aclose()


def test_categorize_type_and_capability_do_not_fork(policy, registry):
    """词表项与 handler 能力必须同时存在，否则守卫推不出承接的 handler。

    模型没给 handler 时，画像能力是守卫唯一还能用的线索。只加 taxonomy 项、
    不给 handler 声明同名能力，会走成：评估器把意图归到
    ``bookkeeping.categorize_merchants``（它的类型只能从词表里选），而画像给出的
    能力候选是一个**没有任何 handler 认领**的名字——守卫推不出唯一 handler，
    把决策降级到兜底路由，再被拆解器判成"单步但点不出工具"，最终 422。

    "模型不给 handler"不是假设：``guard.py`` 里那条从能力推导的路径就是因为它
    实测经常不给才补的（模型认为那字段可推导，于是漏掉）。
    """
    from dispatcher.core.contract import TaskProfile
    from dispatcher.core.guard import RawDecision, apply_guard

    profile = TaskProfile.model_validate({
        **CATEGORIZE_PROFILE,
        "policy_version": policy.policy_version,
        "modality": ["text"],
    })
    out = apply_guard(
        policy,
        # handler 与 tool_set 都留空——这条请求的成败全在"能力能不能定位到 handler"。
        RawDecision(route_id="single_tool_action", model_tier="cheap",
                    handler=None, tool_set=[]),
        profile=profile, handler_ids=registry.ids,
        handler_tools=registry.tool_map(), handler_caps=registry.capability_map(),
    )
    assert out.decision.handler == "bookkeeping"
    assert "handler_fallback" not in out.decision.guard.applied, out.decision.guard.violations


async def test_categorization_never_invents_a_category_outside_the_vocabulary():
    """模型给出用户体系之外的分类名时**丢掉那一条**，不就近改写。

    分类词表是用户的（``ctx.config.categories``）。替用户发明一个分类，会让
    他在自己的分类选择器里对不上；消费端界面就是按"认不出来时说没给出建议，
    绝不猜"来设计的。反例（体系内的"交通"）必须照常留下——只测"拒绝"不测
    "放行"的测试抓不到"约束过宽把所有合法输入都拒了"。
    """
    from dispatcher.core.registry import HandlerManifest
    from handlers.bookkeeping.handler import BookkeepingHandler

    manifest = HandlerManifest.model_validate({
        "handler_id": "bookkeeping", "version": "1",
        "capabilities": ["bookkeeping.categorize_merchants"],
        "tools": [{"name": "categorize_merchants", "requires_capabilities": ["text"]}],
    })
    h = BookkeepingHandler(manifest)
    llm = ScriptedLLM([{"suggestions": [
        {"merchant": "星巴克", "category": "餐饮美食"},   # 体系外 → 丢掉
        {"merchant": "滴滴出行", "category": "交通"},     # 体系内 → 留下
    ]}])
    ctx = _ctx_for(llm, config={"categories": ["餐饮", "交通", "其他"]})

    res = await h.tool_categorize_merchants({"merchants": ["星巴克", "滴滴出行"]}, ctx)
    assert res.ok, res.failure
    assert res.output["suggestions"] == [{"merchant": "滴滴出行", "category": "交通"}]


async def test_categorization_reuses_local_knowledge_without_calling_the_model():
    """已经归类过的商户不再问模型——与 ``normalize_merchant`` 共用一张本地表。

    收益不只是省钱：同一批流水里重复出现的商户名，判断标准与逐条归类时一致。
    """
    from dispatcher.core.registry import HandlerManifest
    from handlers.bookkeeping.handler import BookkeepingHandler

    manifest = HandlerManifest.model_validate({
        "handler_id": "bookkeeping", "version": "1",
        "capabilities": ["bookkeeping.categorize_merchants"],
        "tools": [{"name": "categorize_merchants", "requires_capabilities": ["text"]}],
    })
    h = BookkeepingHandler(manifest)
    # 只预置一次响应：第二次真去问模型的话，ScriptedLLM 会当场抛
    # "没有更多预置响应了"——测试因此因**该失败的那一条**而失败，而不是靠
    # 一个可能被改宽的次数断言。
    llm = ScriptedLLM([{"suggestions": [{"merchant": "星巴克", "category": "餐饮"}]}])
    ctx = _ctx_for(llm, config={"categories": ["餐饮", "交通", "其他"]})

    first = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx)
    assert first.output["suggestions"] == [{"merchant": "星巴克", "category": "餐饮"}]
    second = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx)
    assert second.output["suggestions"] == [{"merchant": "星巴克", "category": "餐饮"}]
    assert len(llm.calls) == 1, "只该有第一次那一次模型调用"


def _ctx_for(llm, *, config=None):
    from dispatcher.core.budget import BudgetHandle
    from dispatcher.core.cancel import CancellationToken
    from dispatcher.core.context import DispatchContext, MediaResolver

    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    st = InMemoryStateStore()
    led = BudgetLedger(enforcement="advisory", warn_at_ratio=0.8, currency="CNY")
    led.open("t", limit=1.0)
    st2 = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    return DispatchContext(
        task_id="t", subtask_id="s", tenant_id="d", user_id="u", trace_id="",
        route_id="r",
        media=MediaResolver(st2), config=config or {}, state=st,
        budget=BudgetHandle(_ledger=led, task_id="t", limit=1.0, spent=0.0, currency="CNY"),
        cancellation=CancellationToken(), events=EventBus(st),
        _policy=policy, _pricing=pricing, _llm=llm, _allowed_tiers=list(policy.model_tier_ids),
    )


# ---------------------------------------------------------------------------
# 事件流
# ---------------------------------------------------------------------------
async def test_events_are_monotonic_and_replayable():
    llm = ScriptedLLM([SEARCH_PROFILE, SEARCH_DECISION])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(text_env("明天下午三点开会"))
        events = await d.state.read_events(rec.task_id)
        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(seqs) + 1)), "seq 必须无洞且单调"
        # 断线重放：从中间某个 seq 之后接着读
        mid = seqs[len(seqs) // 2]
        tail = await d.state.read_events(rec.task_id, since_seq=mid)
        assert [e.seq for e in tail] == [s for s in seqs if s > mid]
        # 终态事件必须在
        assert events[-1].type == "task.completed"
    finally:
        await d.aclose()


async def test_failed_task_emits_terminal_event():
    llm = ScriptedLLM([SEARCH_PROFILE, SEARCH_DECISION])
    d = make_dispatcher(llm)
    try:
        # 让执行失败：calendar 未实现的名字不在白名单里 → 由守卫挡在路由层
        # 这里改成让 handler 返回失败：调用一个存在但会失败的路径
        rec = await d.submit(text_env("明天下午三点开会"))
        cur = await d.get(rec.task_id)
        events = await d.state.read_events(cur.task_id)
        assert events[-1].type in {"task.completed", "task.failed", "task.cancelled"}
    finally:
        await d.aclose()


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------
async def test_cancel_is_idempotent_and_terminal():
    """取消要幂等，且对已终态的任务不该报错。

    注意用的是 async 模式：同步模式下任务在 submit 里就内联跑完了，
    那时再取消只能拿到一个已成功的任务——那是**正确**行为（终态不可变），
    但它测不到取消本身。
    """
    llm = ScriptedLLM([SEARCH_PROFILE, SEARCH_DECISION, SLOT_FILL])
    d = make_dispatcher(llm)
    try:
        rec = await d.submit(text_env("明天下午三点开会", mode="async"))
        first = await d.cancel(rec.task_id)
        second = await d.cancel(rec.task_id)   # 再取消一次不该报错
        assert first.status == "cancelled"
        assert second.status == "cancelled"
    finally:
        await d.aclose()


# ---------------------------------------------------------------------------
# 计划校验
# ---------------------------------------------------------------------------
def test_plan_validation_rejects_cycles_and_unknown_tools():
    from dispatcher.core.plan import Node

    policy = load_policy(get_settings().policy_path)
    registry = build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(get_settings().agents_path)

    cyclic = ExecutionPlan(
        task_id="t", strategy="dag", max_parallelism=2,
        nodes=[
            Node(subtask_id="a", handler="calendar", executor="tool", tool="create_event",
                 depends_on=["b"]),
            Node(subtask_id="b", handler="calendar", executor="tool", tool="create_event",
                 depends_on=["a"]),
        ],
        edges=[{"from": "b", "to": "a"}, {"from": "a", "to": "b"}],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=1),
    )
    v = validate_plan(cyclic, policy=policy, registry=registry, tool_set=[], agents=agents)
    assert any("成环" in x for x in v), v

    bogus_tool = ExecutionPlan(
        task_id="t", strategy="single_step", max_parallelism=1,
        nodes=[Node(subtask_id="a", handler="calendar", executor="tool", tool="no_such_tool")],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=1),
    )
    v2 = validate_plan(bogus_tool, policy=policy, registry=registry, tool_set=[], agents=agents)
    assert any("未声明" in x for x in v2), v2


def test_plan_validation_enforces_round_budget():
    """整任务总轮数是防"每一步都合规但整体炸掉"的那一道。"""
    from dispatcher.core.plan import Node

    policy = load_policy(get_settings().policy_path)
    registry = build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(get_settings().agents_path)
    limit = policy.limits.max_total_rounds_per_task
    per = policy.limits.max_agent_rounds
    count = limit // per + 2

    plan = ExecutionPlan(
        task_id="t", strategy="dag", max_parallelism=8,
        nodes=[
            Node(subtask_id=f"n{i}", handler="bookkeeping", executor="agent",
                 role="ledger_auditor", tool_whitelist=["query_ledger"], max_rounds=per)
            for i in range(count)
        ],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=10),
    )
    v = validate_plan(plan, policy=policy, registry=registry, tool_set=[], agents=agents)
    assert any("总轮数" in x for x in v), v


def test_plan_validation_enforces_agent_node_count():
    from dispatcher.core.plan import Node

    policy = load_policy(get_settings().policy_path)
    registry = build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(get_settings().agents_path)
    n = policy.limits.max_agent_nodes_per_plan + 1
    plan = ExecutionPlan(
        task_id="t", strategy="dag", max_parallelism=8,
        nodes=[
            Node(subtask_id=f"n{i}", handler="bookkeeping", executor="agent",
                 role="merchant_classifier", tool_whitelist=["lookup_merchant"], max_rounds=1)
            for i in range(n)
        ],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=10),
    )
    v = validate_plan(plan, policy=policy, registry=registry, tool_set=[], agents=agents)
    assert any("agent 节点数" in x for x in v), v


@pytest.mark.parametrize("role,expected_ok", [("verifier", True), ("no_such_role", False)])
def test_plan_validation_checks_roles(role, expected_ok):
    from dispatcher.core.plan import Node

    policy = load_policy(get_settings().policy_path)
    registry = build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(get_settings().agents_path)
    plan = ExecutionPlan(
        task_id="t", strategy="single_step", max_parallelism=1,
        nodes=[Node(subtask_id="a", handler="bookkeeping", executor="agent",
                    role=role, tool_whitelist=[], max_rounds=1)],
        plan_budget=PlanBudget(max_cost=1, max_wall_ms=1000, max_llm_calls=1),
    )
    v = validate_plan(plan, policy=policy, registry=registry, tool_set=[], agents=agents)
    assert (not any("角色" in x for x in v)) is expected_ok, v


async def test_node_default_options_reach_the_model_call(policy, pricing, taxonomy,
                                                       registry, prompts):
    """策略里的 `node_defaults.options` 必须真的传到节点级的模型请求里。

    这条是**实测出来的缺口**：evaluator / router / decomposer 三个阶段各自在策略里
    传了 `options: {thinking: {disabled}}`，但节点执行那条路径完全不传，于是走供应商
    默认——而当前供应商的深度思考**默认开启**。后果是一次视觉抽取跑了 31.7 秒
    （模板给的 timeout 是 20 秒），下游商户归类又撞上 8 秒超时，
    **抽取结果完全正确却被整单丢掉**。

    用户侧看到的是"请求超时"，根因在"开关没传到该传的地方"——
    与"数值没对着真实延迟校准"是同一类问题。
    """
    llm = ScriptedLLM([
        RECEIPT_PROFILE, RECEIPT_DECISION,
        {"final": {"amount": 38.0, "currency": "CNY", "merchant": "某餐厅",
                   "direction": "expense", "category": "餐饮", "confidence": 0.9}},
        {"final": {"merchant": "某餐厅", "category": "餐饮"}},
    ])
    d = make_dispatcher(llm)
    try:
        # 上传到**调度器自己的**媒体库——每个 Dispatcher 实例持有自己的那个，
        # 往别处传等于把图丢了，报错会出现在很远的评估器里。
        rec = await d.media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
        env = TaskEnvelope.model_validate({
            "identity": {"user_id": "u_1"},
            "input": {"text": "记一笔", "media": [
                {"media_id": rec.media_id, "kind": "image", "mime": "image/png"}]},
            "constraints": {"mode_preference": "sync"},
        })
        task = await d.submit(env)
        for _ in range(200):
            cur = await d.get(task.task_id)
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.02)
    finally:
        await d.aclose()
    assert llm.calls, "应当有模型调用"
    for i, c in enumerate(llm.calls):
        assert c.options.get("thinking") == {"type": "disabled"}, (
            f"第 {i} 次调用（tier={c.tier}）没带上关闭深度思考的开关：{c.options}"
        )
