"""「extract 在 4 轮内未产出合格结果」——复现、定位、修复都用可复算的断言钉住。

用户原话：「我上传图片识别，返回结果但 extract 失败，agent 在 4 轮内未产出合格结果」。

本文件回答三个问题（对应 .mimocode/tasks/fix-extract-rounds-20261007.md）：

* **Q1 升档有没有生效** —— ``test_round_limit_escalation_actually_changes_tier``
  断言两趟 agent 循环用的档位**不同**。修复前两趟都是 ``standard``：
  runner 以 ``node.model_tier`` 为升档起点，而模板里的 ``tier: vision`` 被
  ``Decomposer._from_template`` 丢弃（agent 节点那一支从不赋值），于是起点退化成
  ``model_tier_ids[0]`` = ``cheap``、升档目标 = ``standard`` ——
  恰好就是该节点正在用的档位，升了个寂寞。
* **Q2 为什么 4 轮出不来** —— ``test_extract_converges_when_the_model_calls_once_then_final``
  钉住正常路径（一轮工具 + 一轮 ``final`` = 成功），
  ``test_round_limit_exhaustion_is_reported_with_the_real_round_count`` 钉住耗尽时的形状。
  注意：**schema 从来不是拦路虎** —— ``output_must_satisfy_schema`` 全仓无消费点，
  ``_run_agent`` 对 ``final`` 只判 ``isinstance(dict) and 非空``（``nodeexec.py:277``），
  所以"不满足 schema"既不会失败、也不会重试。真正的失败只有一种形状：
  模型连续 4 轮都没给出顶层 ``final``。
* **Q3 报错文案** —— ``test_task_level_error_is_a_generic_handler_error``
  钉住客户端实际看到的是 ``handler_error`` + 节点失败原文（实现细节）。
  改不改是 PM 的契约决策，这里只记录现状。

仓库里没有服务端运行记录可查（没有 ``data/dispatcher.db``，也没起过服务），
因此 Q1/Q2 的证据由这些用例现场产生，而不是引用日志。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetLedger
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.eventbus import EventBus
from dispatcher.core.policy import load_policy
from dispatcher.core.pricing import load_pricing
from dispatcher.core.prompts import PromptLibrary
from dispatcher.core.settings import REPO_ROOT, get_settings
from dispatcher.core.taxonomy import load_taxonomy
from dispatcher.pipeline import Dispatcher
from dispatcher.plugins import build_registry as real_build_registry
from tests.fakes import ScriptedLLM
from tests.test_node_timeout import RECEIPT_DECISION, RECEIPT_PROFILE

EXTRACT_REQUIRES = ("vision.extract", "text")

# extract 节点的白名单里，crop_and_zoom / read_media_region 都是"确定性镜头动作"，
# 自己不调模型。用它构造"每轮都在调工具、从不给 final"，LLM 调用次数就等于轮数，好数。
_TOOL_ONLY_ROUND = {"tool_calls": [{"name": "crop_and_zoom", "args": {"region": "amount"}}]}

_RECEIPT_FIELDS = {"amount": 38.5, "currency": "CNY", "merchant": "星巴克咖啡（国贸店）",
                   "datetime": "2026-10-07 12:31", "payment_method": "微信支付",
                   "direction": "expense", "category": "餐饮", "confidence": 0.91,
                   "notes": None}

_FINAL_NORMALIZE = {"final": {"merchant": "星巴克咖啡（国贸店）", "category": "餐饮"}}


def call_extract_tool(media_id: str) -> dict[str, Any]:
    """一轮"调 extract_receipt_fields"。``media_ref`` 必须是**真实存在的** media_id。

    否则工具会在 ``ctx.media.data_uri`` 上以 ``unsupported_media`` 提前失败，
    它内部那次视觉调用就不会发生——脚本里为它准备的那条响应会被后面某一轮误吃掉，
    轮数与断言全部错位。
    """
    return {"tool_calls": [{"name": "extract_receipt_fields",
                            "args": {"media_ref": media_id, "text_hint": "午饭"}}]}


def tool_only_failure_script() -> list[Any]:
    """2 趟（原档 + 升档）× 4 轮，每轮只调工具、从不给 final。"""
    return [RECEIPT_PROFILE, RECEIPT_DECISION] + [_TOOL_ONLY_ROUND] * 8


async def run_receipt_task(
    responses: list[Any] | Callable[[str], list[Any]],
) -> tuple[Dispatcher, Any, list, ScriptedLLM]:
    """跑一张真实的票据请求（走 template → policy → pipeline 全链路）。

    ``responses`` 可以是列表，也可以是 ``media_id -> 列表`` 的函数——需要让
    ``extract_receipt_fields`` 真的跑到视觉调用时，必须先知道 media_id。
    """
    s = get_settings()
    policy = load_policy(s.policy_path)
    st = InMemoryStateStore()
    media = InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
    )
    rec = await media.put(b"\x89PNG\r\n\x1a\n x", "image/png")
    script = responses(rec.media_id) if callable(responses) else responses
    llm = ScriptedLLM(script)

    d = Dispatcher(
        policy=policy,
        pricing=load_pricing(REPO_ROOT / "config" / "pricing.yaml"),
        registry=real_build_registry(REPO_ROOT / "config" / "handlers.yaml"),
        prompts=PromptLibrary(s.prompts_dir),
        llm=llm,
        media=media,
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
    env = TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {"text": "午饭花了38", "media": [
            {"media_id": rec.media_id, "kind": "image", "mime": "image/png"}]},
        "constraints": {"mode_preference": "async"},
    })
    task = await d.submit(env)
    for _ in range(400):
        cur = await d.get(task.task_id)
        if cur.status in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.02)
    record = await d.get(task.task_id)
    events = await d.state.read_events(task.task_id)
    return d, record, events, llm


def agent_rounds(events: list, subtask_id: str = "extract") -> list:
    return [e for e in events
            if e.type == "agent.round" and e.subtask_id == subtask_id]


# ===========================================================================
# Q1 —— 升档到底有没有生效
# ===========================================================================
async def test_round_limit_escalation_actually_changes_tier():
    """4 轮耗尽 → 升档重试 → **第二趟必须换到更强的档位**，而不是原地重跑。

    修复前的实测结果：两趟都是 ``standard``（原因见模块开头）。
    断言按"档位序列"写，不按下标：只要两趟的档位相同就会红。
    """
    d, record, events, llm = await run_receipt_task(tool_only_failure_script())
    try:
        assert record.status == "failed", "模型 8 轮都不给 final，任务必须失败"

        # 前两次调用是 01 评估 + 02 路由；模板命中，03 不再走 LLM 拆解。
        extract_calls = llm.calls[2:]
        assert len(extract_calls) == 8, (
            f"两趟 agent 循环各 4 轮，应恰好 8 次调用，实际 {len(extract_calls)}："
            f"{[(c.tier, c.requires) for c in llm.calls]}"
        )
        assert all(c.requires == EXTRACT_REQUIRES for c in extract_calls), [
            c.requires for c in extract_calls
        ]
        first = [c.tier for c in extract_calls[:4]]
        second = [c.tier for c in extract_calls[4:]]
        assert first == ["standard"] * 4, f"原档位应是 standard，实际 {first}"
        assert second == ["strong"] * 4, (
            f"升档后必须换档；两趟同为 {first} 说明升档是空转（Q1 的根因）"
        )

        esc = [e for e in events if e.type == "task.escalated"]
        assert esc, "升档必须发事件，否则它是神秘的"
        assert esc[0].data["from_tier"] == "standard"
        assert esc[0].data["to_tier"] == "strong"
        # 升档预算是硬界（thresholds.max_escalations=1），升档后再耗尽就到此为止
        assert len(esc) == 1

        # 两趟执行都要被数出来：修复前 attempts 报 1，排查时看到的执行次数比真实少一半
        assert record.node_runs["extract"]["attempts"] == 2, record.node_runs["extract"]
        # 节点 tier 也不再是 null（模板里的 tier: vision 现在被真正解析）
        assert record.node_runs["extract"]["tier"] == "standard"
    finally:
        await d.aclose()


# ===========================================================================
# Q2 —— 正常路径与耗尽路径的形状
# ===========================================================================
async def test_extract_converges_when_the_model_calls_once_then_final():
    """正常路径：一轮调 ``extract_receipt_fields``、下一轮用 ``final`` 包住字段 → 成功。

    这条同时钉住提示词里的输出契约（``prompts/agents/receipt_extractor.md``）：
    字段必须包在 ``final`` 里。模型若照角色提示词的字面把字段平铺在顶层，
    执行器判不出"它做完了"（``nodeexec.py:277``），每轮都被浪费，
    最终就是用户看到的那条失败。
    """

    def script(media_id: str) -> list[Any]:
        return [
            RECEIPT_PROFILE,
            RECEIPT_DECISION,
            call_extract_tool(media_id),   # extract 第 1 轮：调抽取工具
            _RECEIPT_FIELDS,               #   工具内部那次视觉调用
            {"final": _RECEIPT_FIELDS},    # extract 第 2 轮：包进 final
            _FINAL_NORMALIZE,              # normalize
        ]

    d, record, events, _ = await run_receipt_task(script)
    try:
        assert record.status == "succeeded", (record.error, record.node_runs)
        assert record.node_runs["extract"]["status"] == "succeeded"
        assert record.artifacts["extract"]["amount"] == 38.5
        assert record.artifacts["extract"]["direction"] == "expense"

        # ``agent.round`` 一轮发**两条**：进入这一轮时发一条（``stop_reason=None``，
        # 记录这一轮调了什么工具），循环退出时再发一条终态事件说明"为什么停"。
        # 三条退出路径都遵守这个形状，不是 final 这一支多发了一次：
        #   进入   nodeexec.py:240-246（每轮一条）
        #   final  nodeexec.py:283-288（``output_satisfied_schema``）
        #   耗尽   nodeexec.py:295-300（``round_limit`` / ``repeated_no_progress``）
        # 所以"2 轮并成功"= 2 条进入 + 1 条终态。只看 ``stop_reason`` 会把终态那条
        # 误当成第三个轮次——轮次要数 ``round`` 号，或者过滤掉 ``stop_reason is None``。
        rounds = agent_rounds(events)
        assert [r.data["round"] for r in rounds] == [1, 2, 2], [r.data for r in rounds]
        assert [r.data["stop_reason"] for r in rounds] == [None, None, "output_satisfied_schema"], (
            f"应当一轮工具 + 一轮 final 收敛，实际 {[r.data for r in rounds]}"
        )
        assert record.node_runs["extract"]["attempts"] == 1, "正常路径不该触发升档"
        assert not [e for e in events if e.type == "task.escalated"]
    finally:
        await d.aclose()


async def test_round_limit_exhaustion_is_reported_with_the_real_round_count():
    """耗尽时的形状：两趟各 4 轮，``stop_reason=round_limit``，**升档不放宽轮数**。

    ``stop_reason`` 只有在模型主动 ``give_up`` 时才是 ``repeated_no_progress``；
    一直在调工具是 ``round_limit``。两者的对外文案完全一样（见 Q3）。
    """
    d, record, events, _ = await run_receipt_task(tool_only_failure_script())
    try:
        rounds = agent_rounds(events)
        # 进入事件：两趟各 4 条（round 号各自从 1 数起），说明升档那一趟**重新从第 1 轮开始**
        entered = [r for r in rounds if r.data["stop_reason"] is None]
        assert [r.data["round"] for r in entered] == [1, 2, 3, 4, 1, 2, 3, 4], [
            r.data for r in rounds
        ]

        exhausted = [r for r in rounds if r.data["stop_reason"] == "round_limit"]
        assert len(exhausted) == 2, [r.data for r in rounds]
        assert [r.data["max_rounds"] for r in exhausted] == [4, 4], (
            "升档一次只改一个变量：换更强的模型，不加轮数（见 nodeexec._run_agent 注释）"
        )

        err = record.node_runs["extract"]["error"]
        # 协议上，轮数耗尽借的是 escalatable 码 schema_validation_failed；
        # 但它**不**设 retryable——升档只有一次，之后不该再自动重试
        assert err["code"] == "schema_validation_failed", err
        assert err["retryable"] is False, "升档耗尽后不该再自动重试"
        assert err["message"] == "agent 在 4 轮内未产出合格结果", err
        # retry: {max: 1} 在这个失败模式上是死配置：retryable=False 让 runner 直接 break
        assert record.node_runs["extract"]["attempts"] == 2, "2 = 原档 1 趟 + 升档 1 趟，与 retry.max 无关"
    finally:
        await d.aclose()


async def test_upstream_failure_skips_the_rest_of_the_template():
    """extract 失败 → 下游按 ``upstream_failed`` 跳过，而不是凭空消失。"""
    d, record, events, _ = await run_receipt_task(tool_only_failure_script())
    try:
        for sid in ("normalize", "dedupe", "write"):
            assert record.node_runs[sid]["status"] == "skipped", record.node_runs[sid]
        failed = [e for e in events
                  if e.type == "subtask.failed"
                  and e.data.get("error", {}).get("code") == "upstream_failed"]
        assert {e.subtask_id for e in failed} >= {"normalize", "write"}
    finally:
        await d.aclose()


# ===========================================================================
# 提示词契约：字段必须包在 final 里
# ===========================================================================
def test_extract_role_prompt_states_the_final_envelope():
    """角色提示词必须说清三轮契约里的 ``final`` 包法。

    修复前这个文件里 **一次都没出现** ``final``：它只说"输出一份满足
    {{output_schema}} 的 JSON"，与执行器要求的 ``{"final": {...}}`` 直接冲突。
    模型照角色提示词办，就会把字段平铺在顶层，每一轮都被判成"没输出"。
    """
    text = PromptLibrary(get_settings().prompts_dir).get("agents/receipt_extractor.md")
    assert '"final"' in text, "角色提示词丢了 final 包法——模型会把字段平铺在顶层，白烧轮数"
    assert "tool_calls" in text and "give_up" in text, "三轮契约要写全，只写 final 不够"
    assert "max_rounds" in text, "得让模型知道轮数是有界的，否则它不会收敛"


# ===========================================================================
# Q3 —— 客户端实际看到的错误（现状记录，不改契约）
# ===========================================================================
async def test_task_level_error_is_a_generic_handler_error():
    """**现状**：具体失败码不进客户端，进去的是 ``handler_error`` + 节点失败原文。

    证据链：``nodeexec`` 报 ``schema_validation_failed`` →
    ``runner.run`` 把它折成 ``{"code": "handler_error", "detail": "节点 extract 失败：…"}``
    → ``pipeline`` 直接 ``record.error = report.error`` → ``/v1/tasks/{id}`` 原样返回。于是：

    * 精确原因（轮数耗尽 vs 真的 schema 不合）在客户端**不可见**；
    * ``"agent 在 4 轮内未产出合格结果"`` 这句实现细节**原样**进了 UI。

    这是**现状记录**，不是期望：要改得走契约（PM 决策），所以断言写死当前值——
    哪天有人动了对外文案，这里会亮，提醒同时更新客户端与说明文档。
    """
    d, record, events, _ = await run_receipt_task(tool_only_failure_script())
    try:
        assert record.error["code"] == "handler_error"
        assert record.error["status"] == 502
        assert "extract" in record.error["detail"]
        assert "4 轮" in record.error["detail"], record.error

        # 对比：节点级事件里留的是**精确码**，任务级 problem 里被折成了笼统的 handler_error。
        # 也就是说信息不是"丢了"，而是"没被折叠到客户端能看到的那一层"。
        node_failed = [e for e in events
                       if e.type == "subtask.failed" and e.subtask_id == "extract"]
        assert [e.data["error"]["code"] for e in node_failed] == ["schema_validation_failed"], (
            [e.data for e in node_failed]
        )
        # 精确码留在 node_runs 里，客户端若愿意读是读得到的
        assert record.node_runs["extract"]["error"]["code"] == "schema_validation_failed"
    finally:
        await d.aclose()
