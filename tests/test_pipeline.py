"""01 → 02 端到端。

用 ``ScriptedLLM`` 替掉网络那一跳，其余全部走真实代码路径：真实的策略、真实的
价格表、真实的词表、真实的提示词文件、真实的守卫。

因此这里能断言的不只是"最终结果对不对"，还包括关于**提示词内容**的事实——
比如渲染出来的菜单里确实包含策略里的每一条路由。那才是"决策表是配置"的可验证形态。
"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry as RefRegistry
from referencing import Resource

from dispatcher.adapters.memory_media import InMemoryMediaStore
from dispatcher.adapters.memory_state import InMemoryStateStore
from dispatcher.core.contract import TaskEnvelope
from dispatcher.core.errors import DispatcherError
from dispatcher.pipeline import Dispatcher
from dispatcher.ports.llm import LLMError
from tests.fakes import ScriptedLLM

PROFILE_JSON = {
    "task_type": "bookkeeping.capture_from_receipt",
    "intent_summary": "用户上传一张支付截图并附文字，意图记录一笔餐饮支出。",
    "complexity": {"score": 0.42, "reasons": ["需视觉字段抽取", "需商户归类"]},
    "urgency": {"level": "normal"},
    "vision": {"expected_extraction": ["amount", "merchant", "datetime"]},
    "candidate_capabilities": ["bookkeeping.expense.record"],
    "required_capabilities": ["vision.extract", "ledger.write"],
    "data_sensitivity": "financial",
    "estimated_scale": {
        "input_tokens_bucket": "s", "output_tokens_bucket": "m", "est_tool_calls": 4,
    },
    "recommended_mode": "async",
    "needs_clarification": False,
    "confidence": 0.86,
}

DECISION_JSON = {
    "route_id": "vision_extract_then_write",
    "model_tier": "standard",
    "handler": "bookkeeping",
    "tool_set": [
        "extract_receipt_fields", "normalize_merchant", "dedupe_check", "build_ledger_entry",
    ],
    "execution_mode": "async",
    "decompose": True,
    "budget": {"max_cost": 0.12, "max_wall_ms": 30000, "max_llm_calls": 8},
    "escalation_rule": {
        "on": ["schema_validation_failed"], "to_tier": "strong", "max_escalations": 9,
    },
    "rationale": "含支付截图，需视觉抽取后落账。",
    "confidence": 0.83,
}


@pytest.fixture
def media(policy) -> InMemoryMediaStore:
    return InMemoryMediaStore(
        allowed_mime=policy.limits.media.allowed_mime,
        max_bytes=policy.limits.media.max_bytes,
    )


def text_envelope(text: str = "你好") -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {"identity": {"user_id": "u_1"}, "input": {"text": text}}
    )


def image_envelope(media_id: str, text: str = "中午吃饭花了 38") -> TaskEnvelope:
    return TaskEnvelope.model_validate({
        "idempotency_key": "bookkeeping:u_1:abc",
        "identity": {"user_id": "u_1", "timezone": "Asia/Shanghai"},
        "input": {
            "text": text,
            "media": [{
                "media_id": media_id, "kind": "image", "mime": "image/png",
                "bytes": 100, "sha256": "a" * 64, "role": "source_document",
            }],
        },
        "constraints": {"max_cost": 0.05, "data_sensitivity": "financial"},
    })


def build(policy, pricing, taxonomy, registry, prompts, media, llm,
          *, execution_enabled: bool = False) -> Dispatcher:
    from dispatcher.core.agents import load_agents
    from dispatcher.core.budget import BudgetLedger
    from dispatcher.core.eventbus import EventBus
    from dispatcher.core.settings import REPO_ROOT

    state = InMemoryStateStore()
    return Dispatcher(
        policy=policy, pricing=pricing, registry=registry, prompts=prompts, llm=llm,
        media=media, state=state, taxonomy=taxonomy,
        agents=load_agents(REPO_ROOT / "config" / "agents.yaml"),
        events=EventBus(state),
        ledger=BudgetLedger(enforcement=policy.enforcement_mode,
                            warn_at_ratio=policy.budget.warn_at_ratio,
                            currency=policy.budget.currency),
        execution_enabled=execution_enabled,
    )


async def run(policy, pricing, taxonomy, registry, prompts, media, responses, envelope):
    llm = ScriptedLLM(responses)
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    return d, llm, await d.submit(envelope)


async def an_image(media) -> str:
    rec = await media.put(b"\x89PNG\r\n\x1a\n fake", "image/png")
    return rec.media_id


# ---------------------------------------------------------------------------
async def test_two_stage_happy_path(policy, pricing, taxonomy, registry, prompts, media):
    env = image_envelope(await an_image(media))
    _, llm, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                             [PROFILE_JSON, DECISION_JSON], env)

    # 01 产出
    assert task.profile is not None
    assert task.profile.task_type == "bookkeeping.capture_from_receipt"
    assert task.profile.complexity.band == "medium"      # 由代码从 0.42 派生
    assert task.profile.modality == ["image", "text"]    # 由请求推导，不采信模型
    assert task.profile.est_cost is not None             # 量级桶 → 金额，代码算的
    assert task.profile.est_cost.max > 0
    assert task.profile.est_cost.basis.startswith("pricing.yaml@")

    # 02 产出
    assert task.decision is not None
    assert task.decision.route_id == "vision_extract_then_write"
    assert task.decision.path == "decompose"

    # 守卫：LLM 提的成本 0.12 被截断到请求约束 0.05
    assert task.decision.budget.max_cost == 0.05
    assert "budget_clamped" in task.decision.guard.applied
    # 守卫：升级上限 9 被策略压回 1（否则升级会成环）
    assert task.decision.escalation_rule.max_escalations == policy.thresholds.max_escalations

    # 带图请求走了多模态通道，且评估器被升到具备视觉能力的档位
    assert llm.calls[0].has_image()
    assert llm.calls[0].tier == "standard"
    assert llm.calls[1].tier == policy.router.tier

    # M1 没有执行层：任务明确地停在"什么都没跑"
    assert task.status == "rejected"
    assert task.error is not None and task.error["code"] == "handler_error"


async def test_text_request_stays_on_the_cheapest_tier(policy, pricing, taxonomy,
                                                     registry, prompts, media):
    """纯文本请求不需要视觉能力，因此评估器停在最便宜档位上。

    这个判断完全由能力集合决定，没有"如果带图就用某档"这样的分支。
    """
    prof = dict(PROFILE_JSON, task_type="chat.explain", modality=["text"])
    prof.pop("vision", None)
    _, llm, _ = await run(policy, pricing, taxonomy, registry, prompts, media,
                          [prof, {**DECISION_JSON, "route_id": "direct_answer",
                                  "handler": None, "tool_set": []}],
                          text_envelope())
    assert llm.calls[0].tier == policy.evaluator.tier
    assert not llm.calls[0].has_image()


async def test_stage_options_come_from_policy(policy, pricing, taxonomy,
                                             registry, prompts, media):
    """阶段的供应商参数（如关掉深度思考）必须来自策略，而不是代码里写死。

    这条保证了一件具体的事：当供应商把「思考模式下强制覆盖 temperature」这种
    行为加进来时，我们改配置就能应对——而不用改代码。
    """
    _, llm, _ = await run(policy, pricing, taxonomy, registry, prompts, media,
                          [PROFILE_JSON, DECISION_JSON], text_envelope())
    assert llm.calls[0].options == policy.evaluator.options
    assert llm.calls[1].options == policy.router.options
    # 当前策略显式关掉了思考——不关的话 temperature=0 会被供应商静默覆盖
    assert llm.calls[1].options.get("thinking") == {"type": "disabled"}
    # 结构化输出被要求了
    assert llm.calls[0].json_mode is True
    assert llm.calls[1].json_mode is True


async def test_menu_contains_every_route_from_policy(policy, pricing, taxonomy,
                                                    registry, prompts, media):
    """菜单是策略的函数：策略里的每条路由都必须出现在给 LLM 的提示词里。

    这是"改 YAML 就能改行为"的前提——路由若不在菜单里，模型不可能选到它。
    """
    _, llm, _ = await run(policy, pricing, taxonomy, registry, prompts, media,
                          [PROFILE_JSON, DECISION_JSON], text_envelope())
    prompt = llm.calls[1].system_text
    for route in policy.routes:
        assert route.id in prompt, f"菜单里缺少路由 {route.id}"
        head = " ".join(route.when.split())[:12]
        assert head in prompt, f"路由 {route.id} 的 when 散文未进入菜单"


async def test_menu_reflects_a_policy_edit_without_code_change(policy, pricing, taxonomy,
                                                              registry, prompts, media):
    """改策略的 when 散文，菜单当场就变——不碰任何代码。

    这是 M1 存在的理由：证明"决策表是配置、判断交给 LLM"这套机制真的成立。
    """
    import copy

    edited = copy.deepcopy(policy)
    edited.routes[0].when = "ZZZ 这是一段只在测试里出现的适用条件"
    from dispatcher.core.prompts import policy_menu

    assert "ZZZ 这是一段只在测试里出现的适用条件" in policy_menu(edited)
    assert "ZZZ 这是一段只在测试里出现的适用条件" not in policy_menu(policy)


async def test_guard_corrects_an_invented_route(policy, pricing, taxonomy,
                                               registry, prompts, media):
    bogus = dict(DECISION_JSON, route_id="totally_made_up", handler=None, tool_set=[])
    _, _, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                           [PROFILE_JSON, bogus], text_envelope())
    assert task.decision.route_id in policy.route_ids
    assert "route_fallback" in task.decision.guard.applied
    assert task.decision.guard.fallback_used is True


async def test_missing_media_is_rejected_not_guessed(policy, pricing, taxonomy,
                                                    registry, prompts, media):
    """图不存在时直接拒绝，而不是"当作没有图"继续猜任务类型。"""
    env = TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {"media": [{"media_id": "missing", "kind": "image", "mime": "image/png"}]},
    })
    d = build(policy, pricing, taxonomy, registry, prompts, media,
              ScriptedLLM([PROFILE_JSON, DECISION_JSON]))
    with pytest.raises(DispatcherError) as ei:
        await d.submit(env)
    assert ei.value.code == "unsupported_media"


async def test_media_is_validated_before_any_model_spend(policy, media):
    """类型与大小在任何模型调用之前就校验掉。

    一张 12MB 的图不该先被送给模型，才发现太大——那笔钱白花了。
    允许列表来自策略，不写死在存储实现里。
    """
    assert "image/tiff" not in policy.limits.media.allowed_mime
    with pytest.raises(DispatcherError) as ei:
        await media.put(b"x", "image/tiff")
    assert ei.value.code == "unsupported_media"

    with pytest.raises(DispatcherError) as ei2:
        await media.put(b"x" * (policy.limits.media.max_bytes + 1), "image/png")
    assert ei2.value.code == "media_too_large"

    # 允许列表里的类型正常通过，且哈希由服务端计算（不采信客户端）
    rec = await media.put(b"payload", "image/png")
    import hashlib

    assert rec.sha256 == hashlib.sha256(b"payload").hexdigest()


async def test_clarification_parks_the_task(policy, pricing, taxonomy,
                                           registry, prompts, media):
    """需要澄清时任务停在 awaiting_clarification——这是正常状态，不是异常路径。"""
    env = image_envelope(await an_image(media))
    unclear = dict(
        PROFILE_JSON, needs_clarification=True, confidence=0.4,
        clarification={
            "question": "这张截图是支出还是收入？",
            "options": [{"id": "expense", "label": "支出"}, {"id": "income", "label": "收入"}],
            "blocking": True,
        },
    )
    llm = ScriptedLLM([unclear])
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    task = await d.submit(env)

    assert task.status == "awaiting_clarification"
    assert task.decision is None
    assert task.clarification is not None
    assert task.clarification["blocking"] is True
    assert len(llm.calls) == 1, "需要澄清时不该继续调用路由器"


async def test_evaluator_failure_degrades_instead_of_failing(policy, pricing, taxonomy,
                                                            registry, prompts, media):
    """评估器全挂时用兜底画像继续，而不是让任务失败。

    ``degraded`` 是很有用的信号：降级率升高说明提示词或超时设置有问题。
    """
    llm = ScriptedLLM([], fail_with=LLMError("upstream down", retryable=True))
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    task = await d.submit(text_envelope())

    assert task.profile is not None
    assert task.profile.degraded is True
    assert task.profile.task_type == taxonomy.fallback_type
    assert task.profile.confidence == 0.0
    assert task.evaluation_meta["degraded"] is True
    # 降级后仍然走完了路由，任务结构完整
    assert task.decision is not None


async def test_router_failure_uses_fallback_route(policy, pricing, taxonomy,
                                                 registry, prompts, media):
    """路由器失败不重试，直接兜底——退到"保守但确定"，而不是再赌一次。"""
    llm = ScriptedLLM([PROFILE_JSON])   # 只够评估器用；路由调用时无预置响应 → 抛错
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    task = await d.submit(text_envelope())

    assert task.route_meta["fallback_used"] is True
    assert task.decision.route_id == policy.fallback.route_id


async def test_credential_failure_fails_loudly_instead_of_degrading(policy, pricing, taxonomy,
                                                                  registry, prompts, media):
    """凭证/余额问题必须一路上抛，不能被"降级"吞掉。

    这条是在把适配器接到真实供应商、拿到一个 402 余额不足之后补的：
    当时的实现把它当成上游抖动降级掉，于是系统会在凭证已失效的情况下
    继续安静地产出兜底画像——看起来一切正常，实际每条结果都是垃圾。
    """
    llm = ScriptedLLM([], fail_with=LLMError(
        "账户余额不足（402）", retryable=False, status=402, kind="quota"
    ))
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    with pytest.raises(DispatcherError) as ei:
        await d.submit(text_envelope())

    assert ei.value.code == "upstream_llm_error"
    assert ei.value.retryable is False
    assert ei.value.context["kind"] == "quota"
    assert ei.value.context["provider_status"] == 402
    # 错误信息要告诉人怎么办，而不只是"失败了"
    assert "充值" in ei.value.detail or "余额" in ei.value.detail


async def test_transient_failure_still_degrades(policy, pricing, taxonomy,
                                               registry, prompts, media):
    """上游抖动仍然降级——区分就在这里：抖动会好，凭证问题不会。"""
    llm = ScriptedLLM([], fail_with=LLMError("503", retryable=True, kind="transient"))
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    task = await d.submit(text_envelope())
    assert task.profile.degraded is True


async def test_missing_api_key_fails_loudly_instead_of_degrading(policy, pricing, taxonomy,
                                                                registry, prompts, media):
    """密钥没配时不能"降级成功"。

    这条是踩出来的：``resolve_secret`` 抛 policy_violation，而评估器把它当成上游抖动
    降级掉，于是一路降级到兜底画像 + 兜底路由，任务"成功"返回。
    **密钥为空而系统看起来在正常工作**——比直接报错危险得多。
    """
    llm = ScriptedLLM([], fail_with=DispatcherError(
        "policy_violation", "密钥引用 secret://llm/api_key 解析到环境变量 LLM_API_KEY，但它为空。"
    ))
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    with pytest.raises(DispatcherError) as ei:
        await d.submit(text_envelope())
    assert ei.value.code == "policy_violation"
    assert ei.value.fatal is True


@pytest.mark.parametrize("code", ["policy_violation", "no_capability_match", "invalid_request"])
def test_configuration_errors_are_fatal(code):
    assert DispatcherError(code, "x").fatal is True


def test_retryable_upstream_error_is_not_fatal():
    assert DispatcherError("upstream_llm_error", "x", retryable=True).fatal is False


def test_non_retryable_upstream_error_is_fatal():
    """不可重试的上游错误（凭证失效、余额不足）也不该被降级吞掉。"""
    assert DispatcherError("upstream_llm_error", "x", retryable=False).fatal is True


async def test_taxonomy_miss_falls_back_and_is_recorded(policy, pricing, taxonomy,
                                                       registry, prompts, media):
    weird = dict(PROFILE_JSON, task_type="totally.new.type")
    _, _, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                           [weird, DECISION_JSON], text_envelope())
    assert task.profile.task_type == taxonomy.fallback_type
    assert task.evaluation_meta["taxonomy_miss"] is True


async def test_idempotency_returns_the_existing_task(policy, pricing, taxonomy,
                                                    registry, prompts, media):
    env = image_envelope(await an_image(media))
    llm = ScriptedLLM([PROFILE_JSON, DECISION_JSON])
    d = build(policy, pricing, taxonomy, registry, prompts, media, llm)
    first = await d.submit(env)
    calls_after_first = len(llm.calls)
    second = await d.submit(env)

    assert second.task_id == first.task_id
    assert len(llm.calls) == calls_after_first, "幂等命中不该重新调用模型"


async def test_snapshot_matches_contract(policy, pricing, taxonomy, registry,
                                        prompts, media, schemas):
    """``to_snapshot()`` 的输出必须能过 ``task_snapshot.json``。

    状态模型与契约是两份手写的东西，这层检查防止它们漂移。
    """
    env = image_envelope(await an_image(media))
    _, _, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                           [PROFILE_JSON, DECISION_JSON], env)
    reg = RefRegistry().with_resources(
        [(sid, Resource.from_contents(s)) for sid, s in schemas.items()]
    )
    v = Draft202012Validator(
        schemas["https://smart-dispatcher.dev/schemas/task_snapshot.json"], registry=reg
    )
    errs = list(v.iter_errors(task.to_snapshot()))
    assert not errs, [e.message for e in errs][:5]


async def test_decision_matches_contract(policy, pricing, taxonomy, registry,
                                         prompts, media, schemas):
    """路由决策也必须能过 ``route_decision.json``。"""
    env = image_envelope(await an_image(media))
    _, _, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                           [PROFILE_JSON, DECISION_JSON], env)
    reg = RefRegistry().with_resources(
        [(sid, Resource.from_contents(s)) for sid, s in schemas.items()]
    )
    v = Draft202012Validator(
        schemas["https://smart-dispatcher.dev/schemas/route_decision.json"], registry=reg
    )
    from dispatcher.core.contract import wire_dump

    errs = list(v.iter_errors(wire_dump(task.decision)))
    assert not errs, [e.message for e in errs][:5]


async def test_profile_matches_contract(policy, pricing, taxonomy, registry,
                                        prompts, media, schemas):
    """画像也必须能过 ``task_profile.json``。"""
    env = image_envelope(await an_image(media))
    _, _, task = await run(policy, pricing, taxonomy, registry, prompts, media,
                           [PROFILE_JSON, DECISION_JSON], env)
    reg = RefRegistry().with_resources(
        [(sid, Resource.from_contents(s)) for sid, s in schemas.items()]
    )
    v = Draft202012Validator(
        schemas["https://smart-dispatcher.dev/schemas/task_profile.json"], registry=reg
    )
    from dispatcher.core.contract import wire_dump

    errs = list(v.iter_errors(wire_dump(task.profile)))
    assert not errs, [e.message for e in errs][:5]
