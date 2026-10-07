"""query_ledger 产出查询结构（LedgerQuerySpec）——验收 3/4/5/6 的机械证据。

**它不再返回账目。** 账本在用户手机的 Room 库里，服务端读不到；以前
``tool_query_ledger`` 去读自己那个进程内空列表，恒返回空表。现在它产出
「怎么查」，消费端拿这份结构在本机 SQL 里聚合。

这组用例钉住的是任务书里点名的几条：

* 「这个月餐饮花了多少」→ 含 ``direction``、``category=餐饮``、日期落在本月；
* 「交通银行花了多少」→ ``merchant`` 含「交通银行」、``category`` **不是**「交通」
  （中文无词边界，本地关键词表会把「交通银行」里的「交通」认成分类）；
* 方向判不出 → **没有** ``direction`` 键，而不是默认成 ``expense``；
* 产出能被消费端 ``toQuery()`` 的规则接受。
"""

from __future__ import annotations

import re

import pytest

from dispatcher.core.registry import HandlerManifest
from handlers.bookkeeping.handler import BookkeepingHandler, _sanitize_ledger_query
from tests.fakes import ScriptedLLM

# 与 tests/test_integration.py 的 _ctx_for 同一套构造，避免跨文件依赖私有辅助。
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIENT_DIRECTIONS = {"expense", "income", "both"}
CLIENT_GROUP_BY = {"none", "category", "merchant", "month"}


def _manifest() -> HandlerManifest:
    return HandlerManifest.model_validate({
        "handler_id": "bookkeeping", "version": "1",
        "capabilities": ["bookkeeping.ledger.query"],
        "tools": [{"name": "query_ledger", "requires_capabilities": ["text"],
                   "output_schema_ref": "bookkeeping.LedgerQuery"}],
    })


def _ctx_for(llm, *, config=None):
    from dispatcher.core.budget import BudgetHandle, BudgetLedger
    from dispatcher.core.cancel import CancellationToken
    from dispatcher.core.context import DispatchContext, MediaResolver
    from dispatcher.core.eventbus import EventBus
    from dispatcher.core.policy import load_policy
    from dispatcher.core.pricing import load_pricing
    from dispatcher.core.settings import REPO_ROOT, get_settings
    from dispatcher.adapters.memory_media import InMemoryMediaStore
    from dispatcher.adapters.memory_state import InMemoryStateStore

    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    st = InMemoryStateStore()
    led = BudgetLedger(enforcement="advisory", warn_at_ratio=0.8, currency="CNY")
    led.open("t", limit=1.0)
    media = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    return DispatchContext(
        task_id="t", subtask_id="s", tenant_id="d", user_id="u", trace_id="",
        route_id="r",
        media=MediaResolver(media), config=config or {}, state=st,
        budget=BudgetHandle(_ledger=led, task_id="t", limit=1.0, spent=0.0, currency="CNY"),
        cancellation=CancellationToken(), events=EventBus(st),
        _policy=policy, _pricing=pricing, _llm=llm,
        _allowed_tiers=list(policy.model_tier_ids),
    )


def assert_client_accepts(spec: dict) -> None:
    """等价复述消费端 ``toQuery()`` 的校验，供自查验收第 6 条。

    逐条对应 ``LedgerQueryModels.kt`` 里抄录的规则：``direction`` 是判别键，
    值必须落在封闭枚举内；日期必须是 ``yyyy-MM-dd`` 且 ``from <= to``；
    ``group_by`` 越界退回 ``none``（不算错误）；``limit`` 空或 ≤0 用默认值；
    ``category`` / ``merchant`` 空白串当 null。
    """
    if "direction" in spec:
        assert spec["direction"] in CLIENT_DIRECTIONS, f"direction 非法：{spec['direction']!r}"
        assert spec["direction"] == str(spec["direction"]).lower(), "direction 应为小写"
    for key in ("from", "to"):
        if key in spec:
            assert isinstance(spec[key], str) and ISO_DAY.match(spec[key]), f"{key} 非法：{spec[key]!r}"
    if "from" in spec and "to" in spec:
        assert spec["from"] <= spec["to"], f"from > to：{spec}"
    if "group_by" in spec:
        assert spec["group_by"] in CLIENT_GROUP_BY, f"group_by 非法：{spec['group_by']!r}"
    if "limit" in spec:
        assert isinstance(spec["limit"], int) and not isinstance(spec["limit"], bool)
        assert spec["limit"] > 0, f"limit 非法：{spec['limit']!r}"
    for key in ("category", "merchant"):
        if key in spec:
            assert isinstance(spec[key], str) and spec[key].strip(), f"{key} 是空白串"


# ---------------------------------------------------------------------------
# 验收 3：「这个月餐饮花了多少」
# ---------------------------------------------------------------------------
async def test_this_month_dining_expense_yields_a_query_spec():
    """真实问句端到端（工具层）：方向 + 餐饮分类 + 日期落在本月。"""
    judgment = {
        "from": "2026-10-01", "to": "2026-10-31",
        "direction": "expense", "category": "餐饮",
    }
    llm = ScriptedLLM([judgment])
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger({"question": "这个月餐饮花了多少"}, _ctx_for(llm))
    assert res.ok, res.failure
    spec = res.output
    assert_client_accepts(spec)
    assert spec["direction"] == "expense"
    assert spec["category"] == "餐饮"
    assert spec["from"].startswith("2026-10") and spec["to"].startswith("2026-10")
    # 产出的就是查询结构本身，挂 artifacts.main（单步节点名）时消费端递归找 direction 即得
    assert "entries" not in spec, "不该再返回账目——账本在消费端"


# ---------------------------------------------------------------------------
# 验收 4：「交通银行花了多少」——分类 vs 商户
# ---------------------------------------------------------------------------
async def test_bank_name_is_merchant_not_category():
    """「交通银行」含分类名「交通」，必须判成 merchant，不是 category。

    这是用户点名要修的缺陷：本地关键词表按子串匹配会把「交通」认成分类，
    于是 `LIKE '%交通银行%'` 匹配不到任何商户、答「没有记录」。这里断言
    **category 键不存在**（不是 category != 交通——空白的 category 也不能留键）。
    """
    judgment = {"direction": "expense", "merchant": "交通银行"}
    llm = ScriptedLLM([judgment])
    h = BookkeepingHandler(_manifest())
    # 草稿故意带上错误的 category，检验最终产出以判定为准
    res = await h.tool_query_ledger(
        {"question": "交通银行花了多少", "category": "交通", "direction": "expense"},
        _ctx_for(llm, config={"categories": ["餐饮", "交通", "购物", "居住", "其他"]}),
    )
    assert res.ok, res.failure
    spec = res.output
    assert_client_accepts(spec)
    assert "交通银行" in spec["merchant"]
    assert spec.get("category") is None, f"category 不该是「交通」：{spec}"
    assert "category" not in spec, "判不成分类就整键省略，不留空串"


async def test_category_vocabulary_is_injected_into_the_judgment_prompt():
    """分类词表必须出现在判定提示词里——「分类 vs 商户」靠它，不靠关键词表。"""
    llm = ScriptedLLM([{"direction": "expense", "merchant": "交通银行"}])
    h = BookkeepingHandler(_manifest())
    ctx = _ctx_for(llm, config={"categories": ["吃饭", "打车"]})
    await h.tool_query_ledger({"question": "交通银行花了多少"}, ctx)
    assert len(llm.calls) == 1
    prompt = llm.calls[0].user_text
    assert "吃饭" in prompt and "打车" in prompt, "用户自己的词表要注入提示词"
    assert "交通银行" in prompt, "用户原话要给到判定"


# ---------------------------------------------------------------------------
# 验收 5：方向判不出 → 没有 direction 键
# ---------------------------------------------------------------------------
async def test_undetermined_direction_is_omitted_not_defaulted():
    """判不出方向时**整键省略**，绝不默认成 expense。"""
    llm = ScriptedLLM([{"group_by": "month"}])
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger({"question": "我花了多少次"}, _ctx_for(llm))
    assert res.ok, res.failure
    spec = res.output
    assert_client_accepts(spec)
    assert "direction" not in spec, f"方向判不出必须省略键，不是给默认值：{spec}"


async def test_direction_from_llm_is_normalized_but_never_invented():
    llm = ScriptedLLM([{"direction": "Expense"}])  # 大小写不敏感，会先 lowercase
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger({"question": "这个月花了多少"}, _ctx_for(llm))
    assert res.output["direction"] == "expense"

    llm2 = ScriptedLLM([{"direction": "省下的"}])  # 枚举外 → 整键省略
    res2 = await h.tool_query_ledger({"question": "？"}, _ctx_for(llm2))
    assert "direction" not in res2.output


# ---------------------------------------------------------------------------
# 验收 6 + 清洗规则：产出必须能被客户端 toQuery() 接受
# ---------------------------------------------------------------------------
def test_sanitize_enforces_every_client_rule():
    spec = _sanitize_ledger_query({
        "from": "2026-10-31", "to": "2026-10-01",  # from>to → 对调
        "direction": "INCOME",
        "category": "  ",  # 空白 → 省略
        "merchant": " 星巴克 ",
        "group_by": "year",  # 枚举外 → 省略（消费端退回 none）
        "limit": 0,  # ≤0 → 省略
    })
    assert_client_accepts(spec)
    assert spec == {
        "direction": "income", "from": "2026-10-01", "to": "2026-10-31",
        "merchant": "星巴克",
    }

    # 日期解析不出来就丢掉，不能把毒字段交给消费端（客户端会整条作废）
    spec2 = _sanitize_ledger_query({"from": "2026-10", "to": "昨天", "direction": "both"})
    assert_client_accepts(spec2)
    assert "from" not in spec2 and "to" not in spec2
    assert spec2["direction"] == "both"


# ---------------------------------------------------------------------------
# 兜底：模型调用失败 → 退到草稿，不猜方向
# ---------------------------------------------------------------------------
async def test_falls_back_to_slot_fill_draft_when_the_model_fails():
    """兜底触发条件：判定那次模型调用抛错（含输出不是 JSON 对象）。

    退到参数抽取给的草稿并按消费端规则清洗；草稿里判不出的方向同样省略——
    兜底不许比主判定更激进。
    """
    llm = ScriptedLLM([], fail_with=RuntimeError("upstream down"))
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger(
        {"question": "这个月餐饮花了多少", "direction": "expense", "category": "餐饮",
         "from": "2026-10-01", "to": "2026-10-31"},
        _ctx_for(llm),
    )
    assert res.ok, res.failure
    spec = res.output
    assert_client_accepts(spec)
    assert spec["direction"] == "expense"
    assert spec["category"] == "餐饮"

    # 草稿也没有方向 → 产出缺 direction，而不是补 expense
    llm2 = ScriptedLLM([], fail_with=RuntimeError("down"))
    res2 = await h.tool_query_ledger({"question": "查一下", "category": "餐饮"}, _ctx_for(llm2))
    assert "direction" not in res2.output


async def test_empty_input_produces_an_empty_spec_not_a_fake_query():
    llm = ScriptedLLM([])  # 一次都不该被调用
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger({}, _ctx_for(llm))
    assert res.ok, res.failure
    assert res.output == {}
    assert llm.calls == []


# ---------------------------------------------------------------------------
# 端到端：single_tool_action 下产出挂 artifacts.main
# ---------------------------------------------------------------------------
async def test_end_to_end_single_tool_action_mounts_spec_at_artifacts_main():
    from dispatcher.core.agents import load_agents
    from dispatcher.core.eventbus import EventBus
    from dispatcher.core.taxonomy import load_taxonomy
    from dispatcher.pipeline import Dispatcher
    from dispatcher.core.budget import BudgetLedger
    from dispatcher.core.contract import TaskEnvelope
    from dispatcher.core.settings import REPO_ROOT, get_settings
    from dispatcher.adapters.memory_media import InMemoryMediaStore
    from dispatcher.adapters.memory_state import InMemoryStateStore
    from dispatcher.core.policy import load_policy
    from dispatcher.core.pricing import load_pricing
    from dispatcher.core.prompts import PromptLibrary
    from dispatcher.plugins import build_registry

    profile = {
        "task_type": "bookkeeping.ledger.query",
        "intent_summary": "查这个月餐饮花了多少",
        "complexity": {"score": 0.2, "reasons": ["单步查询"]},
        "urgency": {"level": "normal"},
        "required_capabilities": ["bookkeeping.ledger.query"],
        "candidate_capabilities": ["bookkeeping.ledger.query"],
        "data_sensitivity": "financial",
        "recommended_mode": "sync",
        "needs_clarification": False,
        "confidence": 0.9,
    }
    decision = {
        "route_id": "single_tool_action",
        "model_tier": "cheap",
        "handler": "bookkeeping",
        "tool_set": ["query_ledger"],
        "execution_mode": "sync",
        "decompose": False,
        "budget": {"max_cost": 0.02, "max_wall_ms": 20000, "max_llm_calls": 4},
        "rationale": "单步查询账目。",
        "confidence": 0.9,
    }
    slot_fill = {"question": "这个月餐饮花了多少"}
    judgment = {
        "from": "2026-10-01", "to": "2026-10-31",
        "direction": "expense", "category": "餐饮",
    }
    llm = ScriptedLLM([profile, decision, slot_fill, judgment])

    s = get_settings()
    policy = load_policy(s.policy_path)
    d = Dispatcher(
        policy=policy,
        pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=build_registry(REPO_ROOT / "config" / "handlers.yaml"),
        prompts=PromptLibrary(s.prompts_dir),
        llm=llm,
        media=InMemoryMediaStore(allowed_mime=policy.limits.media.allowed_mime,
                                 max_bytes=policy.limits.media.max_bytes),
        state=InMemoryStateStore(),
        taxonomy=load_taxonomy(s.taxonomy_path),
        agents=load_agents(s.agents_path),
        events=EventBus(InMemoryStateStore()),
        ledger=BudgetLedger(enforcement=policy.enforcement_mode,
                            warn_at_ratio=policy.budget.warn_at_ratio,
                            currency=policy.budget.currency),
        execution_enabled=True,
    )
    try:
        rec = await d.submit(TaskEnvelope.model_validate({
            "identity": {"user_id": "u_1"},
            "input": {"text": "这个月餐饮花了多少"},
            "constraints": {"mode_preference": "sync"},
        }))
        assert rec.status == "succeeded", rec.error
        assert rec.decision.route_id == "single_tool_action"
        spec = rec.artifacts["main"]
        assert_client_accepts(spec)
        assert spec["direction"] == "expense"
        assert spec["category"] == "餐饮"
        # 消费端 ledgerQuerySpec() 的过滤条件是 it.containsKey("direction")
        assert "direction" in spec
    finally:
        await d.aclose()
