"""节点 ``timeout_ms`` 到底怎么生效——用机械证据回答，不靠读代码下结论。

2026-10-06 的实测疑点：``receipt_to_entry.yaml`` 里 ``extract.timeout_ms: 20000``，
但该节点实跑 54.2 秒仍 ``succeeded``；``normalize.timeout_ms: 8000`` 实跑 14.7 秒也成功。

结论：**它是"每次 LLM 调用"的超时，不是节点级墙钟。**

* 接线点只有一处：``NodeExecutor._run_agent`` 在**每一轮**循环里
  ``ctx.llm_json(..., timeout_ms=node.timeout_ms)``；
* 该值一路传到 ``OpenAICompatibleLLM``，在那里变成 httpx 单次 POST 的
  ``timeout=``（``adapters/openai_compat.py``），即"这一次 HTTP 请求"的上限；
* ``DagRunner`` 里**没有任何节点级墙钟**——翻遍 runner 没有 ``wait_for``、
  也没有对 ``node.timeout_ms`` 的求和。

于是 ``extract``（``max_rounds: 4``、``retry.max: 1``）最坏可跑 2×4×20s，
54.2 秒完全在预算内；``normalize``（``max_rounds: 3``、8s）最坏 24 秒，14.7 秒同理。
**观察到的"超时没生效"是误读**：数值单位是"每次调用"，而模板读起来像是"每个节点"。

另有一条**真的没接线**的路径，见文件末尾那条测试：``executor: tool`` 的节点上
``timeout_ms`` 完全不参与——``_run_tool`` 不传它，工具自己调 ``ctx.llm(...)`` 时
也不带超时，于是退回全局默认。这是已知缺口，用测试钉住，不让它悄悄变化。
"""

from __future__ import annotations

import asyncio

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetLedger
from dispatcher.core.cancel import CancellationToken
from dispatcher.core.context import MediaResolver
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.eventbus import EventBus
from dispatcher.core.nodeexec import NodeExecutor
from dispatcher.core.plan import Node
from dispatcher.core.policy import load_policy
from dispatcher.core.pricing import load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.taxonomy import load_taxonomy
from dispatcher.core.yamlio import load_yaml
from dispatcher.pipeline import Dispatcher
from dispatcher.plugins import build_registry as real_build_registry
from tests.fakes import ScriptedLLM

TEMPLATE_PATH = REPO_ROOT / "config" / "flow_templates" / "receipt_to_entry.yaml"

RECEIPT_PROFILE = {
    "task_type": "bookkeeping.capture_from_receipt",
    "intent_summary": "上传支付截图，记一笔支出",
    "complexity": {"score": 0.42, "reasons": ["需视觉抽取"]},
    "urgency": {"level": "normal"},
    "vision": {"expected_extraction": ["amount", "merchant"]},
    "required_capabilities": ["vision.extract"],
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


def template_timeouts() -> dict[str, int]:
    """从模板文件读节点超时——避免在测试里复写魔数，让断言跟着配置走。"""
    tpl = load_yaml(TEMPLATE_PATH)
    return {n["id"]: n["timeout_ms"] for n in tpl["nodes"] if "timeout_ms" in n}


def make_dispatcher(llm) -> Dispatcher:
    s = get_settings()
    policy = load_policy(s.policy_path)
    st = InMemoryStateStore()
    return Dispatcher(
        policy=policy,
        pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=real_build_registry(REPO_ROOT / "config" / "handlers.yaml"),
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
        execution_enabled=True,
    )


# ===========================================================================
# (a) 每次 LLM 调用分别计时 —— 因此多轮节点会把预算乘起来
# ===========================================================================
async def test_agent_node_timeout_is_per_llm_call_not_per_node():
    """``extract`` 跑两轮 = 两次调用，各自带满 20000ms —— 加起来可以远超 20000ms。

    ``backend`` 的假模型只记录调用参数，不真的等待，所以这条测试证明的是
    **接线语义**（每次调用都拿到节点的那份预算），而不是真实墙钟。
    真实墙钟由 2026-10-06 的实测给出：extract 实跑 54.2s 仍成功。
    """
    timeouts = template_timeouts()
    extract_ms, normalize_ms = timeouts["extract"], timeouts["normalize"]

    llm = ScriptedLLM([
        RECEIPT_PROFILE,
        RECEIPT_DECISION,
        # extract 第 1 轮：调一个确定性工具（它自己不碰模型，便于数清调用次数）
        {"tool_calls": [{"name": "crop_and_zoom", "args": {"region": "full"}}]},
        # extract 第 2 轮：给 final
        {"final": {"amount": 38.5, "currency": "CNY", "merchant": "星巴克咖啡（国贸店）",
                   "direction": "expense", "category": "餐饮", "confidence": 0.91}},
        # normalize 第 1 轮
        {"final": {"merchant": "星巴克咖啡（国贸店）", "category": "餐饮"}},
    ])
    d = make_dispatcher(llm)
    try:
        rec = await d.media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
        env = TaskEnvelope.model_validate({
            "identity": {"user_id": "u_1"},
            "input": {"text": "午饭花了38", "media": [
                {"media_id": rec.media_id, "kind": "image", "mime": "image/png"}]},
            "constraints": {"mode_preference": "async"},
        })
        task = await d.submit(env)
        for _ in range(300):
            cur = await d.get(task.task_id)
            if cur.status in {"succeeded", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.02)
        assert (await d.get(task.task_id)).status == "succeeded"

        # calls[0] 评估、calls[1] 路由、calls[2..3] extract 两轮、calls[4] normalize
        assert len(llm.calls) == 5, [c.timeout_ms for c in llm.calls]

        assert llm.calls[2].timeout_ms == extract_ms
        assert llm.calls[3].timeout_ms == extract_ms
        assert llm.calls[4].timeout_ms == normalize_ms

        # 这就是"超时看似失效"的真相：同一个节点发起的两次调用各拿一份完整预算，
        # 节点总耗时因而可以接近 2×extract_ms —— 而模板那行读起来像是节点级上限。
        assert llm.calls[2].timeout_ms + llm.calls[3].timeout_ms == 2 * extract_ms
        assert extract_ms > normalize_ms, "两个节点的超时不同，才验得出是各节点各自的值"
    finally:
        await d.aclose()


def test_runner_has_no_node_level_wall_clock():
    """反面确认：``DagRunner`` 里没有任何节点级墙钟。

    这条测试的价值在于它会**随实现变化而变红**：哪天有人加了真正的节点级超时，
    它会失败，逼着把上面那段文档与 ``docs/02-stages.md`` 一起改掉——
    而不是让文档安静地过期。
    """
    import inspect

    from dispatcher.core import runner

    src = inspect.getsource(runner)
    assert "wait_for" not in src, "runner 出现了墙钟实现——文档与测试需要同步更新"
    assert "node.timeout_ms" not in src
    assert "timeout_ms" not in src


# ===========================================================================
# (b) 真没接线的那条：executor=tool 的节点上 timeout_ms 完全不参与
# ===========================================================================
async def test_tool_node_timeout_does_not_reach_the_tools_llm_call():
    """已知缺口，用测试钉住现状。

    ``_run_tool`` 只做"找 handler → 校验工具名 → 调 execute_tool"，**不传超时**；
    工具内部自己调 ``ctx.llm(...)`` 时也不带 ``timeout_ms``，于是退回全局默认
    （``settings.llm_request_timeout_s``）。因此：

    * 对 ``dedupe`` / ``write`` 这类纯确定性 tool 节点，``timeout_ms`` 形同虚设
      —— 但它们不碰模型，实际无害；
    * 对**会调模型**的 tool 节点（如 ``extract_receipt_fields``），节点声明的超时
      被绕过，真实上限是全局默认值。

    这条不写成"应该失败"的期望，而是把现状**钉死**：谁要改这个语义，
    这里会先亮，提醒同时更新文档与 ``docs/02-stages.md``。
    """
    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    registry = real_build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(s.agents_path)
    prompts = PromptLibrary(s.prompts_dir)
    state = InMemoryStateStore()
    ledger = BudgetLedger(enforcement=policy.enforcement_mode,
                          warn_at_ratio=policy.budget.warn_at_ratio,
                          currency=policy.budget.currency)
    media = InMemoryMediaStore(allowed_mime=policy.limits.media.allowed_mime,
                               max_bytes=policy.limits.media.max_bytes)

    llm = ScriptedLLM([{"amount": 38.5, "currency": "CNY", "merchant": "某店",
                        "direction": "expense", "confidence": 0.9}])
    executor = NodeExecutor(
        policy=policy, pricing=pricing, registry=registry, agents=agents,
        prompts=prompts, llm=llm, media=MediaResolver(media),
        events=EventBus(state), ledger=ledger, state=state,
        cancellation=CancellationToken(),
    )
    ledger.open("task_x", limit=1.0)

    rec = await media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
    node = Node(
        subtask_id="extract_one_shot", handler="bookkeeping", executor="tool",
        tool="extract_receipt_fields",
        inputs={"media_ref": rec.media_id, "text_hint": "午饭"},
        timeout_ms=7000,
    )
    result = await executor(node, attempt=1, tier_override=None,
                            scope={"__task_id__": "task_x"})

    assert result.ok, result.failure
    assert len(llm.calls) == 1
    # 缺口现状：节点声明了 7000ms，工具内部那次调用却拿到 None（= 用全局默认 60s）
    assert llm.calls[0].timeout_ms is None, (
        "tool 路径开始传超时了——请同步更新本文件与 docs/02-stages.md 的说明"
    )


async def test_agent_rounds_do_receive_the_node_timeout_via_the_same_context():
    """对照：同一个 ctx，经 agent 路径调用就带上了节点超时。

    与上一条并排看，缺口就非常具体——不是"超时没接线"，而是**只有 tool 那一跳漏了**。
    """
    s = get_settings()
    policy = load_policy(s.policy_path)
    pricing = load_pricing(REPO_ROOT / "config" / "pricing.yaml")
    registry = real_build_registry(REPO_ROOT / "config" / "handlers.yaml")
    agents = load_agents(s.agents_path)
    prompts = PromptLibrary(s.prompts_dir)
    state = InMemoryStateStore()
    ledger = BudgetLedger(enforcement=policy.enforcement_mode,
                          warn_at_ratio=policy.budget.warn_at_ratio,
                          currency=policy.budget.currency)
    media = InMemoryMediaStore(allowed_mime=policy.limits.media.allowed_mime,
                               max_bytes=policy.limits.media.max_bytes)

    llm = ScriptedLLM([{"final": {"merchant": "某店", "category": "餐饮"}}])
    executor = NodeExecutor(
        policy=policy, pricing=pricing, registry=registry, agents=agents,
        prompts=prompts, llm=llm, media=MediaResolver(media),
        events=EventBus(state), ledger=ledger, state=state,
        cancellation=CancellationToken(),
    )
    ledger.open("task_y", limit=1.0)

    node = Node(
        subtask_id="normalize", handler="bookkeeping", executor="agent",
        role="merchant_classifier", tool_whitelist=["lookup_merchant"],
        max_rounds=2, timeout_ms=8000,
        inputs={"merchant_raw": "某店"},
    )
    result = await executor(node, attempt=1, tier_override=None,
                            scope={"__task_id__": "task_y"})

    assert result.ok, result.failure
    assert llm.calls[0].timeout_ms == 8000
