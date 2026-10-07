"""注入分槽：指令进 system，用户数据进 user；商户缓存按租户分槽。

2026-10-07 安全审计点名的三处「指令与数据同槽」，这里各钉一条机械证据。
每一条都是**改回旧写法就会红**的，而不是描述当前实现的同义反复：

1. ``NodeExecutor._run_agent`` —— 节点输入（上游产出与用户数据的合流）以前被
   ``fill`` 拼进了 **system** 槽位。模型对 system 的信任高于 user，把可能被污染
   的内容放进去，注入就等于提权。
2. ``BookkeepingHandler._judge_ledger_query`` —— 以前只有一条 user 消息，字段规则
   与用户原话混在一起，用户那句"忽略上面的规则"于是有了与规则同级的地位。
3. ``BookkeepingHandler._merchants`` —— 以前是一张挂在 handler 实例上的裸字典，
   而实例由调度层 build 一次、跨任务复用，于是所有租户共用一份商户缓存。

第三处的断言刻意用「两个租户各问了一次模型」这种**可数的**证据，而不是去断言
内部字典的键长什么样：键的形状是实现的自由，**B 租户读不到 A 租户的判定**
才是要保住的性质。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetHandle, BudgetLedger
from dispatcher.core.cancel import CancellationToken
from dispatcher.core.context import DispatchContext, MediaResolver
from dispatcher.core.eventbus import EventBus
from dispatcher.core.nodeexec import NodeExecutor
from dispatcher.core.plan import Node
from dispatcher.core.policy import load_policy
from dispatcher.core.pricing import load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.registry import HandlerManifest
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.yamlio import load_yaml
from dispatcher.plugins import build_registry
from handlers.bookkeeping.handler import BookkeepingHandler
from tests.fakes import ScriptedLLM

#: 一个在仓库里不会自然出现的串。它代表"用户/上游产出的数据"，
#: 断言它就是断言"这份数据有没有出现在 system 槽位里"。
SENTINEL = "SENTINEL_MERCHANT_9f3"


# ---------------------------------------------------------------------------
# 公共构造
# ---------------------------------------------------------------------------
def _executor(llm) -> NodeExecutor:
    s = get_settings()
    policy = load_policy(s.policy_path)
    state = InMemoryStateStore()
    ledger = BudgetLedger(
        enforcement=policy.enforcement_mode,
        warn_at_ratio=policy.budget.warn_at_ratio,
        currency=policy.budget.currency,
    )
    ledger.open("task_x", limit=1.0)
    media = InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
    )
    return NodeExecutor(
        policy=policy,
        pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=build_registry(REPO_ROOT / "config" / "handlers.yaml"),
        agents=load_agents(s.agents_path),
        prompts=PromptLibrary(s.prompts_dir),
        llm=llm,
        media=MediaResolver(media),
        events=EventBus(state),
        ledger=ledger,
        state=state,
        cancellation=CancellationToken(),
    )


def _frozen_clock(y: int, m: int, d: int, *, hour: int = 12) -> Callable[[], datetime]:
    fixed = datetime(y, m, d, hour, 0, tzinfo=UTC)
    return lambda: fixed


def _ctx(llm, *, tenant: str = "tenant_a", config: dict | None = None, clock=None):
    s = get_settings()
    policy = load_policy(s.policy_path)
    led = BudgetLedger(enforcement="advisory", warn_at_ratio=0.8, currency="CNY")
    led.open("t", limit=1.0)
    media = InMemoryMediaStore(allowed_mime=["image/png"], max_bytes=1000)
    return DispatchContext(
        task_id="t", subtask_id="s", tenant_id=tenant, user_id="u", trace_id="",
        route_id="r",
        media=MediaResolver(media), config=config or {}, state=InMemoryStateStore(),
        budget=BudgetHandle(_ledger=led, task_id="t", limit=1.0, spent=0.0, currency="CNY"),
        cancellation=CancellationToken(), events=EventBus(InMemoryStateStore()),
        _policy=policy,
        _pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        _llm=llm,
        _allowed_tiers=list(policy.model_tier_ids),
        clock=clock,
    )


def _manifest() -> HandlerManifest:
    return HandlerManifest.model_validate({
        "handler_id": "bookkeeping", "version": "1",
        "capabilities": ["bookkeeping.merchant.classify", "bookkeeping.ledger.query"],
        "tools": [
            {"name": "normalize_merchant", "requires_capabilities": ["text"]},
            {"name": "categorize_merchants", "requires_capabilities": ["text"]},
            {"name": "lookup_merchant", "requires_capabilities": []},
            {"name": "query_ledger", "requires_capabilities": ["text"],
             "output_schema_ref": "bookkeeping.LedgerQuery"},
        ],
    })


# ---------------------------------------------------------------------------
# 1. NodeExecutor：节点输入不进 system
# ---------------------------------------------------------------------------
async def test_agent_system_slot_never_carries_node_inputs():
    """节点输入只出现在 user 的 ``data_block`` 里，一处都不在 system。

    旧写法把 ``json.dumps(args)`` 填进了 ``{{inputs}}``——而 ``args`` 是上游节点
    产出与用户数据的合流。这条断言的就是"它现在不在 system 里了"。
    """
    llm = ScriptedLLM([{"final": {"merchant": "某店", "category": "餐饮"}}])
    node = Node(
        subtask_id="normalize", handler="bookkeeping", executor="agent",
        role="merchant_classifier", tool_whitelist=["lookup_merchant"],
        max_rounds=1, inputs={"merchant_raw": SENTINEL},
    )
    result = await _executor(llm)(node, attempt=1, tier_override=None,
                                  scope={"__task_id__": "task_x"})
    assert result.ok, result.failure

    call = llm.calls[0]
    assert SENTINEL not in call.system_text, (
        "节点输入进了 system 槽位——用户可控的数据一旦在 system 里，注入就等于提权"
    )
    assert SENTINEL in call.user_text, "输入本身要照常给到模型，只是换个槽位"
    assert "以下为数据" in call.user_text, "user 槽位里的数据必须带 data_block 围栏"
    # 分槽不等于把系统提示词掏空：指令还得在 system 里。
    assert "lookup_merchant" in call.system_text, "工具白名单是指令，属于 system"


async def test_no_agent_role_template_receives_inputs_in_its_system_slot():
    """逐个角色跑一轮：没有任何角色模板还把节点输入拼进 system。

    只测一个角色会漏掉另外两个。审计点名的三份模板——``receipt_extractor``、
    ``merchant_classifier``、``ledger_auditor``——都引用了 ``{{inputs}}``，
    所以这条会覆盖到全部三处。

    它同时钉住 ``fill`` 的严格性：哪个模板还留着 ``{{inputs}}`` 而实现不再提供这个
    变量，这里会以「提示词模板缺少变量」当场报错——那正是我们要的失败方式，
    好过模型看到字面的花括号。
    """
    roles = load_yaml(get_settings().agents_path)["roles"]
    assert roles, "agents.yaml 里没有角色，这条测试就什么也没证明"
    for role in roles:
        llm = ScriptedLLM([{"final": {"ok": True}}])
        node = Node(
            subtask_id="n", handler="bookkeeping", executor="agent",
            role=role["id"], max_rounds=1, inputs={"sentinel": SENTINEL},
        )
        result = await _executor(llm)(node, attempt=1, tier_override=None,
                                      scope={"__task_id__": "task_x"})
        assert result.ok, (role["id"], result.failure)
        call = llm.calls[0]
        assert SENTINEL not in call.system_text, f"角色 {role['id']} 的 system 里有节点输入"
        assert SENTINEL in call.user_text, f"角色 {role['id']} 的 user 里没有节点输入"


# ---------------------------------------------------------------------------
# 2. query_ledger：指令进 system，用户那句话与它产生的数据进 user
# ---------------------------------------------------------------------------
async def test_query_ledger_judgment_splits_instructions_from_user_data():
    """字段规则在 system，用户原话 / 草稿 / 分类词表在 user 的 ``data_block`` 里。

    旧写法只有一条 user 消息，两者同槽：用户原话里的一句"忽略上面的规则"在那条
    消息里与规则是同级内容。
    """
    q_sentinel = "SENTINEL_Q7"
    cat_sentinel = "SENTINEL_CAT"
    llm = ScriptedLLM([{"direction": "expense"}])
    h = BookkeepingHandler(_manifest())
    res = await h.tool_query_ledger(
        {"question": f"这个月 {q_sentinel} 花了多少", "category": cat_sentinel},
        _ctx(llm, config={"categories": [cat_sentinel, "其他"]}),
    )
    assert res.ok, res.failure
    assert len(llm.calls) == 1, "判定只该有一次模型调用"

    call = llm.calls[0]
    assert [m.role for m in call.messages] == ["system", "user"], (
        f"指令与数据必须分槽，实际是 {[m.role for m in call.messages]}"
    )
    system, user = call.system_text, call.user_text

    # 指令在 system：输出契约、封闭枚举、"判不出就省略"、以及那条点名要保的规则
    assert "只输出一个 JSON 对象" in system
    assert "expense" in system and "income" in system
    assert "yyyy-MM-dd" in system
    assert "整键省略" in system
    assert "交通银行" in system and "不是分类" in system, "「商户 vs 分类」的规则属于指令"

    # 用户可控的东西一处都不在 system
    assert q_sentinel not in system, "用户原话进了 system"
    assert cat_sentinel not in system, "用户自己的分类词表进了 system"

    # 它们照常在 user 的数据块里（模型仍然拿得到）
    assert q_sentinel in user and cat_sentinel in user
    assert "以下为数据" in user, "user 槽位里的数据必须带 data_block 围栏"


async def test_query_ledger_date_anchor_stays_in_the_user_slot():
    """「今天」这个锚点随数据块进 user：它是**值**，而"怎么用它"是 system 里的规则。

    这条同时钉住一个既有事实：``tests/test_ledger_query.py`` 那组用例是从
    ``user_text`` 里读「今天是哪天」来推算相对区间的。分槽不许把这个锚点搬走，
    否则那组用例会因为一个与它们无关的原因变红。
    """
    llm = ScriptedLLM([{"direction": "expense"}])
    h = BookkeepingHandler(_manifest())
    await h.tool_query_ledger(
        {"question": "这个月花了多少"},
        _ctx(llm, clock=_frozen_clock(2026, 10, 7)),
    )
    call = llm.calls[0]
    assert "今天是 2026-10-07" in call.user_text
    assert "星期三" in call.user_text and "UTC+8" in call.user_text
    assert "今天是 2026-10-07" not in call.system_text, "锚点是数据，不进 system"


# ---------------------------------------------------------------------------
# 3. _merchants：缓存按 (租户, 分类词表) 分槽
# ---------------------------------------------------------------------------
async def test_merchant_cache_does_not_leak_across_tenants():
    """A 租户的判定不得被 B 租户读到——旧写法里 B 会命中 A 留下的条目。

    证据是**可数的**：两个租户各问一次模型（``len(calls) == 2``），且各自拿到
    自己词表下的分类。共用一张表时第二次不会再问模型，B 会拿到「餐饮」。
    """
    h = BookkeepingHandler(_manifest())
    llm = ScriptedLLM([
        {"suggestions": [{"merchant": "星巴克", "category": "餐饮"}]},
        {"suggestions": [{"merchant": "星巴克", "category": "咖啡"}]},
    ])
    ctx_a = _ctx(llm, tenant="tenant_a", config={"categories": ["餐饮", "其他"]})
    ctx_b = _ctx(llm, tenant="tenant_b", config={"categories": ["咖啡", "其他"]})

    a = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx_a)
    assert a.output["suggestions"] == [{"merchant": "星巴克", "category": "餐饮"}]
    b = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx_b)
    assert b.output["suggestions"] == [{"merchant": "星巴克", "category": "咖啡"}], (
        "B 租户读到了 A 租户的判定（或者 B 那条被 A 的缓存顶掉了模型调用）"
    )
    assert len(llm.calls) == 2, "两个租户各问一次；共用一张表时第二次不会再问模型"

    # 纯查表这条路走的是同一张表，因此同样按租户分槽
    assert (await h.tool_lookup_merchant({"name": "星巴克"}, ctx_a)).output["category"] == "餐饮"
    assert (await h.tool_lookup_merchant({"name": "星巴克"}, ctx_b)).output["category"] == "咖啡"
    assert len(llm.calls) == 2, "查表不该产生模型调用"


async def test_normalize_merchant_cache_is_tenant_scoped():
    """逐条归类与批量归类共用一张表，因此这条路也要分槽。

    单独测它而不是只测批量：它们是两处独立的读写点，漏掉一处就是漏掉一个提权面。
    """
    h = BookkeepingHandler(_manifest())
    llm = ScriptedLLM([
        {"merchant": "某店", "category": "餐饮"},
        {"merchant": "某店", "category": "咖啡"},
    ])
    ctx_a = _ctx(llm, tenant="tenant_a", config={"categories": ["餐饮", "其他"]})
    ctx_b = _ctx(llm, tenant="tenant_b", config={"categories": ["咖啡", "其他"]})

    first = await h.tool_normalize_merchant({"merchant_raw": "某店"}, ctx_a)
    assert first.output["category"] == "餐饮"
    # 旧写法里这一步会直接命中 A 留下的条目，返回「餐饮」
    assert (await h.tool_lookup_merchant({"name": "某店"}, ctx_b)).output["category"] is None
    second = await h.tool_normalize_merchant({"merchant_raw": "某店"}, ctx_b)
    assert second.output["category"] == "咖啡"
    assert len(llm.calls) == 2


async def test_merchant_cache_key_carries_the_category_vocabulary():
    """同一个租户换了分类词表也不共用缓存。

    缓存的值是「商户名 → **该词表里的**分类名」，它的有效期就是那张词表；
    而 ``ctx.config`` 是按用户配置来的。读路径（组装 suggestions、``lookup_merchant``）
    不过词表校验，只有写路径过——所以一份跨词表的旧映射会被**静默**交给消费端，
    用户在自己的分类选择器里找不到那个名字。
    """
    h = BookkeepingHandler(_manifest())
    llm = ScriptedLLM([
        {"suggestions": [{"merchant": "星巴克", "category": "餐饮"}]},
        {"suggestions": [{"merchant": "星巴克", "category": "咖啡"}]},
    ])
    before = _ctx(llm, tenant="t1", config={"categories": ["餐饮", "其他"]})
    after = _ctx(llm, tenant="t1", config={"categories": ["咖啡", "其他"]})

    await h.tool_categorize_merchants({"merchants": ["星巴克"]}, before)
    res = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, after)
    assert res.output["suggestions"] == [{"merchant": "星巴克", "category": "咖啡"}], (
        "旧词表下的「餐饮」被带进了新词表的产出里"
    )
    assert len(llm.calls) == 2


async def test_merchant_cache_still_hits_within_the_same_tenant_and_vocabulary():
    """分槽不是"把缓存关掉"：同租户同词表下，第二次仍然不问模型。

    只测"隔离住了"会漏掉一个更简单的错误实现——每请求一张新表。那条路当然也
    隔离，但白扔掉了这个缓存存在的理由（同一批流水里重复的商户名判断一致、
    且不重复付费）。
    """
    h = BookkeepingHandler(_manifest())
    llm = ScriptedLLM([{"suggestions": [{"merchant": "星巴克", "category": "餐饮"}]}])
    ctx = _ctx(llm, tenant="t1", config={"categories": ["餐饮", "其他"]})

    first = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx)
    second = await h.tool_categorize_merchants({"merchants": ["星巴克"]}, ctx)
    assert first.output == second.output
    assert len(llm.calls) == 1, "同租户同词表下第二次不该再问模型"

    # 与 normalize_merchant 共用同一张表：批量归类过的商户，逐条归类不必再问
    third = await h.tool_normalize_merchant({"merchant_raw": "星巴克"}, ctx)
    assert third.output["category"] == "餐饮"
    assert len(llm.calls) == 1, "两条路要共用本租户本词表的那张表"
