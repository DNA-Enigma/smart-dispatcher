"""阶段备注（``*_meta.notes``）必须能被外面看见。

2026-10-08 实测：评估器降级、路由回退、拆解失败这三件事的**真因**都写在
各自 ``meta.notes`` 里，而快照与错误体都不暴露它们。外部只看到
``degraded=true`` / 一句 ``policy_violation``，排查只能靠猜——拆解器那条更是
被误导了一整轮（把"调用失败"说成"返回了空 nodes"，见
``test_decompose_call_failure_is_not_reported_as_an_empty_return``）。

这组测试钉住三件事：

1. 快照里 ``stage_notes`` 按阶段给出原因，且**字段恒在**（空数组表示这一步
   没什么要说的，不是"不知道"）；
2. 失败响应的 ``Problem.context.stage_notes`` 也带上——拿到 422/502 的人
   和拿到快照的人看到的是同一份线索；
3. 出进程前过一道密钥脱敏，且**只**脱敏密钥值，引用名与环境变量名要留着
   （"密钥没配好"这条线索正是靠它们才可行动）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core import settings as settings_mod
from dispatcher.core.agents import load_agents
from dispatcher.core.budget import BudgetLedger
from dispatcher.core.contract import RouteDecision, TaskEnvelope, TaskProfile
from dispatcher.core.errors import DispatcherError
from dispatcher.core.eventbus import EventBus
from dispatcher.core.settings import REPO_ROOT, redact_secrets
from dispatcher.core.state import TaskRecord
from dispatcher.pipeline import Dispatcher
from dispatcher.ports.llm import LLMError
from dispatcher.stages.decomposer import Decomposer
from tests.fakes import ScriptedLLM

# 无副作用、一次补全即可：这条路走 direct_llm，不经过拆解。
DIRECT_DECISION = {
    "route_id": "direct_answer",
    "model_tier": "cheap",
    "handler": None,
    "tool_set": [],
    "execution_mode": "sync",
    "decompose": False,
    "budget": {"max_cost": 0.003, "max_wall_ms": 8000, "max_llm_calls": 1},
    "rationale": "解释类请求，一次补全即可。",
    "confidence": 0.9,
}

UPSTREAM_FLAP = LLMError("上游抖动（503）", retryable=True, kind="transient")


def text_envelope() -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {"identity": {"user_id": "u_1"}, "input": {"text": "解释一下什么是复利"}}
    )


def build(policy, pricing, taxonomy, registry, prompts, llm, *,
          execution_enabled: bool = True) -> Dispatcher:
    state = InMemoryStateStore()
    return Dispatcher(
        policy=policy, pricing=pricing, registry=registry, prompts=prompts, llm=llm,
        media=InMemoryMediaStore(
            allowed_mime=policy.limits.media.allowed_mime,
            max_bytes=policy.limits.media.max_bytes,
        ),
        state=state, taxonomy=taxonomy,
        agents=load_agents(REPO_ROOT / "config" / "agents.yaml"),
        events=EventBus(state),
        ledger=BudgetLedger(enforcement=policy.enforcement_mode,
                            warn_at_ratio=policy.budget.warn_at_ratio,
                            currency=policy.budget.currency),
        execution_enabled=execution_enabled,
    )


def _evaluator_degrades_then_router_answers() -> ScriptedLLM:
    """评估器的两次尝试都失败（降级），之后路由器正常给出决策。

    策略里 ``evaluator.escalation.max_escalations=1``，所以评估固定两次尝试；
    前两次调用失败之后，第三次（路由）拿到预置决策。
    """
    return ScriptedLLM([DIRECT_DECISION], fail_with=UPSTREAM_FLAP, fail_first_n=2)


async def drain(dispatcher: Dispatcher, task_id: str):
    """等到终态。降级画像的 recommended_mode 是 async，任务可能走后台那一侧。"""
    from dispatcher.core.state import TERMINAL_STATUSES

    for _ in range(300):
        rec = await dispatcher.get(task_id)
        if rec.status in TERMINAL_STATUSES:
            return rec
        await asyncio.sleep(0.02)
    raise AssertionError(f"任务 {task_id} 在等待窗口内没有到达终态")


# ---------------------------------------------------------------------------
async def test_snapshot_exposes_the_reason_a_stage_degraded(
    policy, pricing, taxonomy, registry, prompts
):
    """降级的原因要出现在快照里，而不是只有一个 ``degraded=true``。"""
    d = build(policy, pricing, taxonomy, registry, prompts,
              _evaluator_degrades_then_router_answers(), execution_enabled=False)
    try:
        submitted = await d.submit(text_envelope())
        snap = (await drain(d, submitted.task_id)).to_snapshot()
    finally:
        await d.aclose()

    notes = snap["stage_notes"]
    # 字段恒在：三个键都在，缺的那个是空数组而不是缺键。
    assert set(notes) == {"evaluation", "routing", "planning"}
    assert notes["evaluation"], "评估器降级的真因必须出现"
    assert any("兜底画像" in n for n in notes["evaluation"])
    assert any("上游抖动" in n for n in notes["evaluation"]), "要带上真实原因"
    # 没走到的阶段是空数组——"这一步没有话说"，不是"不知道"
    assert notes["planning"] == []


async def test_failed_task_error_carries_the_same_notes(
    policy, pricing, taxonomy, registry, prompts
):
    """失败响应也带同一份备注：拿到 502 的人不该比拿到快照的人看到得更少。

    这里让直答那一次调用耗尽预置响应而失败（非致命 → 任务 failed 而非 rejected），
    而评估器此前已经降级——于是错误体里应当能看到评估为什么降级。
    """
    d = build(policy, pricing, taxonomy, registry, prompts,
              _evaluator_degrades_then_router_answers())
    try:
        submitted = await d.submit(text_envelope())
        task = await drain(d, submitted.task_id)
    finally:
        await d.aclose()

    assert task.status == "failed", task.error
    assert task.error is not None
    notes = (task.error.get("context") or {}).get("stage_notes")
    assert notes, f"失败体里没有 stage_notes：{task.error}"
    assert any("兜底画像" in n for n in notes["evaluation"])


async def test_decompose_failure_error_carries_planning_notes(
    policy, pricing, taxonomy, registry, prompts
):
    """拆解抛错时 ``DecomposeMeta`` 随异常消失——它的 notes 只能挂在错误体上。

    这一条是任务 2 与"调用失败 vs 返回空"两条修复的交汇点：错误文案说清了
    "调用失败"，notes 则给出完整的尝试历史。
    """
    # 预置响应耗尽 ⇒ 拆解的每次尝试都抛 LLMError
    llm = ScriptedLLM([])
    dec = Decomposer(
        policy=policy, registry=registry,
        agents=load_agents(REPO_ROOT / "config" / "agents.yaml"),
        prompts=prompts, llm=llm,
        templates_dir=REPO_ROOT / "config" / "flow_templates",
    )
    decision = RouteDecision.model_validate({
        "policy_version": policy.policy_version,
        "route_id": "multi_step_analysis", "path": "decompose",
        "model_tier": "standard", "handler": None, "tool_set": [],
        "execution_mode": "async", "decompose": True,
        "budget": {"max_cost": 0.08, "max_wall_ms": 60000, "max_llm_calls": 8},
        "rationale": "多步。", "confidence": 0.5,
        "guard": {"applied": [], "fallback_used": False, "violations": []},
    })
    profile = TaskProfile.model_validate({
        "task_type": "generic.unknown", "modality": ["text"],
        "complexity": {"score": 0.0, "reasons": []},
        "urgency": {"level": "normal"},
        "recommended_mode": "async", "confidence": 0.0,
    })

    with pytest.raises(DispatcherError) as ei:
        await dec.decompose(text_envelope(), profile, decision, task_id="t_notes")

    notes = ei.value.context["stage_notes"]["planning"]
    assert notes, "拆解失败的 notes 不能丢"
    assert any("调用失败" in n for n in notes)


# ---------------------------------------------------------------------------
def test_stage_notes_redact_the_key_but_keep_the_reference(monkeypatch):
    """出快照前按**值**脱敏：抹掉密钥本身，保留引用名与环境变量名。

    供应商回显请求头会把密钥顺着 ``_http_error`` 的 ``resp.text[:300]``
    带进 notes，所以这一步不是假想防护。反过来，"引用/环境变量名"必须留着——
    ``resolve_secret`` 的报错正是靠它们才可行动，抹掉就把排查线索一起抹了。
    """
    monkeypatch.setattr(
        settings_mod, "get_settings", lambda: SimpleNamespace(llm_api_key="sk-live-abc123")
    )

    leaked = '供应商 500：{"Authorization":"Bearer sk-live-abc123"}'
    assert redact_secrets(leaked) == '供应商 500：{"Authorization":"Bearer ***"}'

    # 引用名 / 环境变量名不含秘密，不脱敏
    ref = "密钥引用 secret://llm/api_key 解析到环境变量 LLM_API_KEY，但它为空。"
    assert redact_secrets(ref) == ref

    rec = TaskRecord(
        task_id="t", user_id="u",
        evaluation_meta={"notes": [leaked]},
        route_meta={"notes": ["路由器调用失败，走兜底路由：sk-live-abc123 被拒"]},
    )
    notes = rec.stage_notes()
    assert "sk-live-abc123" not in " ".join(notes["evaluation"] + notes["routing"])
    assert "***" in notes["evaluation"][0]
    # 形状不对的历史记录当作"没有备注"，而不是把快照渲染带崩
    assert TaskRecord(task_id="t", user_id="u",
                      plan_meta={"notes": "不是列表"}).stage_notes()["planning"] == []
