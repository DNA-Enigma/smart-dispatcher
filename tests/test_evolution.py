"""M5 自进化：检测器、校验、版本、金丝雀。

这里**不调用真实模型**——分析层用 ``ScriptedLLM`` 喂预置建议，其余全部走真实代码：
真检测器配置、真校验规则、真版本管理、真护栏计算。

一套"自我修改"的机制，测试的重点必然是**它拒绝什么**：
改代码的建议、越过禁区的建议、幅度失控的建议、样本不足的建议。
只测"它能不能提出建议"是不够的——那等于只验证了它有能力做危险的事。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from dispatcher.adapters.memory_evolution import InMemoryEvolutionStore
from dispatcher.core.policy import resolve_policy_path, set_policy_path
from dispatcher.core.runlog import HumanSignal, RunLog
from dispatcher.core.settings import REPO_ROOT
from dispatcher.evolution.detectors import DetectorEngine
from dispatcher.evolution.loop import EvolutionLoop
from dispatcher.evolution.policy_store import (
    PolicyVersionManager,
    apply_patches,
    compute_guardrails,
)
from dispatcher.evolution.validator import SuggestionValidator
from tests.fakes import ScriptedLLM

NOW = datetime.now(UTC)


def log_of(
    i: int,
    *,
    policy_version: str = "pv_1",
    route: str = "vision_extract_then_write",
    tier: str = "standard",
    guard: list[str] | None = None,
    confidence: float = 0.8,
    status: str = "succeeded",
    warned: bool = False,
    signal: HumanSignal | None = None,
    task_type: str = "bookkeeping.capture_from_receipt",
) -> RunLog:
    return RunLog.model_validate({
        "run_id": f"run_{i}", "task_id": f"task_{i}", "user_id": "u",
        "policy_version": policy_version, "started_at": NOW - timedelta(hours=1),
        "profile": {"task_type": task_type, "complexity_band": "medium",
                    "confidence": confidence, "degraded": False, "taxonomy_miss": False,
                    "est_cost_max": 0.02},
        "decision": {"route_id": route, "model_tier": tier, "path": "decompose",
                     "confidence": confidence, "guard_applied": guard or [],
                     "fallback_used": False},
        "outcome": {"status": status, "wall_ms": 4000, "total_cost": {"amount": 0.01},
                    "llm_calls": 3, "budget_warned": warned, "retries": 0},
        "human_signal": signal.model_dump(mode="json") if signal else None,
    })


@pytest.fixture
def engine(policy):
    return DetectorEngine(
        policy=policy, config_path=REPO_ROOT / "config" / "evolution" / "detectors.yaml"
    )


@pytest.fixture
def validator(policy):
    return SuggestionValidator(
        policy=policy, kinds_path=REPO_ROOT / "config" / "evolution" / "suggestion_kinds.yaml"
    )


def suggestion(**over) -> dict:
    base = {
        "suggestion_id": "sg_1", "kind": "threshold_change", "sample_size": 100,
        "target": {"artifact": "routing.policy.yaml",
                   "path": "routes[direct_answer].max_cost",
                   "current": 0.003, "proposed": 0.002},
        "blast_radius": ["route:direct_answer"],
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------
def test_detector_engine_fires_on_the_right_metric(engine):
    logs = [log_of(i, guard=["route_fallback"] if i < 12 else []) for i in range(40)]
    rep = engine.run(logs)
    fired = {f.detector_id: f for f in rep.findings}
    assert "guard_overrule" in fired
    assert fired["guard_overrule"].observed == pytest.approx(12 / 40)


def test_detector_does_not_fire_below_threshold(engine):
    logs = [log_of(i, guard=["route_fallback"] if i < 2 else []) for i in range(40)]
    assert not [f for f in engine.run(logs).findings if f.detector_id == "guard_overrule"]


def test_detector_skips_when_sample_too_small(engine):
    rep = engine.run([log_of(i) for i in range(5)])
    reasons = dict(rep.skipped)
    assert any("样本不足" in r for _, r in reasons.items()) or rep.evaluated == 0
    assert rep.evaluated == 0, "样本不足时不该有任何检测器被求值"


def test_non_runlog_detectors_are_reported_not_silently_skipped(engine):
    """``source_field: internal`` 的检测器必须**显式报告跳过**。

    安静地跳过等于一个看起来在跑、实际永不报警的监控——那比不装监控更糟。
    """
    rep = engine.run([log_of(i) for i in range(100)])
    skipped_ids = {d for d, _ in rep.skipped}
    assert "suggestion_drop_rate" in skipped_ids
    assert "approval_reject_rate" in skipped_ids


def test_predicate_type_mismatch_is_reported_not_swallowed(engine):
    """谓词把数值与字符串比时应当被记为求值失败，而不是静默算成 0。"""
    logs = [log_of(i, confidence=0.5) for i in range(40)]
    rep = engine.run(logs)
    # low_confidence 的谓词引用 thresholds.*，必须能解析出数字而不是 None
    assert "low_confidence" not in {d for d, _ in rep.skipped}, dict(rep.skipped)


def test_percentile_detector_uses_node_values(engine):
    logs = []
    for i in range(60):
        x = log_of(i)
        x.nodes = [{"subtask_id": "a", "tool": "t", "status": "succeeded", "attempts": 1,
                    "latency_ms": 20000, "queue_ms": 10}]
        logs.append(x)
    fired = {f.detector_id for f in engine.run(logs).findings}
    assert "node_latency" in fired


# ---------------------------------------------------------------------------
# 校验：契约承诺的那些约束
# ---------------------------------------------------------------------------
def test_validator_accepts_a_normal_threshold_change(validator):
    assert validator.validate(suggestion()).ok


def test_validator_accepts_a_prose_rewrite(validator):
    r = validator.validate(suggestion(
        kind="route_guidance_patch",
        target={"artifact": "routing.policy.yaml", "path": "routes[direct_answer].when",
                "current": "旧", "proposed": "新"},
    ))
    assert r.ok, r.notes


@pytest.mark.parametrize(
    "label,over,expected_note",
    [
        ("改代码", {"kind": "prompt_patch",
                    "target": {"artifact": "dispatcher/core/guard.py", "path": "apply_guard",
                               "current": "a", "proposed": "b"}}, "指向代码"),
        ("改 pricing", {"target": {"artifact": "pricing.yaml", "path": "models.cheap.in",
                                   "current": 0.001, "proposed": 0.0005}}, "不允许改"),
        ("命中 limits 禁区", {"target": {"artifact": "routing.policy.yaml",
                                        "path": "limits.max_parallelism",
                                        "current": 8, "proposed": 12}}, "forbidden_target_artifacts"),
        ("幅度过大", {"target": {"artifact": "routing.policy.yaml",
                                 "path": "routes[direct_answer].max_cost",
                                 "current": 0.003, "proposed": 5.0}}, "改动幅度"),
        ("路径不存在", {"target": {"artifact": "routing.policy.yaml",
                                   "path": "routes[nope].max_cost",
                                   "current": 1, "proposed": 2}}, "不存在"),
        ("类型不一致", {"target": {"artifact": "routing.policy.yaml",
                                   "path": "routes[direct_answer].max_cost",
                                   "current": 0.003, "proposed": "0.002"}}, "类型不一致"),
        ("样本不足", {"sample_size": 3}, "样本量"),
        ("未知类型", {"kind": "rewrite_everything"}, "未知的建议类型"),
        ("影响面解析不到", {"blast_radius": ["route:no_such_route"]}, "影响面"),
    ],
)
def test_validator_rejects(validator, label, over, expected_note):
    r = validator.validate(suggestion(**over))
    assert not r.ok, f"{label} 不该通过"
    assert any(expected_note in n for n in r.notes), f"{label}: {r.notes}"


def test_validator_distinguishes_locked_flag_from_the_other_guards(validator):
    """``guards.locked_path`` 是**反语义**的（true 表示非法）。

    把它和"必须为真"的守卫混在一起用 ``all()``，会让每一条建议都被拒，
    而理由栏是空的。这个 bug 只有"正常建议应当通过"的用例能抓到。
    """
    r = validator.validate(suggestion())
    assert r.guards["locked_path"] is False
    assert r.ok, r.notes


# ---------------------------------------------------------------------------
# 策略路径与补丁
# ---------------------------------------------------------------------------
def test_policy_path_resolution_and_write(policy):
    d = policy.model_dump(mode="json")
    ok, val = resolve_policy_path(d, "routes[direct_answer].max_cost")
    assert ok and val == 0.003
    assert set_policy_path(d, "routes[direct_answer].max_cost", 0.009)
    assert resolve_policy_path(d, "routes[direct_answer].max_cost")[1] == 0.009
    assert not set_policy_path(d, "routes[nope].x", 1), "不存在的路径不该被创建"


def test_apply_patches_is_atomic_on_failure(policy):
    """一处补丁失败就整份不改——半途而废的策略比不应用更糟。"""
    d = policy.model_dump(mode="json")
    before = d["routes"][0]["max_cost"]
    new, errors, _ = apply_patches(d, [
        {"op": "set", "artifact": "routing.policy.yaml",
         "path": "routes[direct_answer].max_cost", "value": 0.002},
        {"op": "set", "artifact": "routing.policy.yaml",
         "path": "routes[nope].x", "value": 1},
    ])
    assert errors
    assert new is d, "失败时应当返回原件"
    assert d["routes"][0]["max_cost"] == before


def test_apply_patches_rejects_structurally_broken_result(policy):
    """能通过建议校验、却让策略自相矛盾的改动必须在生效前挡住。"""
    d = policy.model_dump(mode="json")
    new, errors, _ = apply_patches(d, [
        {"op": "set", "artifact": "routing.policy.yaml",
         "path": "routes[direct_answer].default_tier", "value": "nonexistent_tier"},
    ])
    assert not errors, "补丁本身可以应用"
    from pydantic import ValidationError

    from dispatcher.core.policy import Policy

    with pytest.raises(ValidationError):
        Policy.model_validate(new)


# ---------------------------------------------------------------------------
# 护栏与金丝雀
# ---------------------------------------------------------------------------
def test_guardrails_are_plain_ratios():
    logs = [log_of(i, status="failed" if i < 2 else "succeeded") for i in range(10)]
    g = compute_guardrails(logs)
    assert g["task_failure_rate"] == pytest.approx(0.2)
    assert set(g) >= {"task_failure_rate", "guard_overrule_rate", "human_edit_rate", "budget_warn_rate"}


async def test_canary_refuses_to_conclude_on_thin_samples(policy):
    store = InMemoryEvolutionStore()
    mgr = PolicyVersionManager(store=store, policy=policy)
    await mgr.ensure_initial_version(policy)
    v = await mgr.create_from_suggestion(
        suggestion={**suggestion(), "basis_policy_version": policy.policy_version},
        approved_by="u_1",
    )
    assert v["status"] == "canary"
    result = await mgr.check_canary(logs=[log_of(i) for i in range(3)])
    assert result["outcome"] == "pending"
    assert "样本不足" in result["detail"]


async def test_canary_rolls_back_when_guardrails_degrade(policy):
    store = InMemoryEvolutionStore()
    mgr = PolicyVersionManager(store=store, policy=policy)
    await mgr.ensure_initial_version(policy)
    v = await mgr.create_from_suggestion(
        suggestion={**suggestion(), "basis_policy_version": policy.policy_version},
        approved_by="u_1",
    )
    canary_id, base_id = v["policy_version"], v["parent_version"]

    logs = [log_of(i, policy_version=base_id) for i in range(20)]
    logs += [log_of(100 + i, policy_version=canary_id, status="failed") for i in range(20)]

    # 让金丝雀窗口过期，否则会停在 pending
    rec = await store.get_version(canary_id)
    rec["scope"]["canary"]["until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await store.put_version(rec)

    result = await mgr.check_canary(logs=logs)
    assert result["outcome"] == "rolled_back"
    assert "task_failure_rate" in result["degradations"]
    assert (await store.active_version())["policy_version"] == base_id


async def test_canary_promotes_when_healthy(policy):
    store = InMemoryEvolutionStore()
    mgr = PolicyVersionManager(store=store, policy=policy)
    await mgr.ensure_initial_version(policy)
    v = await mgr.create_from_suggestion(
        suggestion={**suggestion(), "basis_policy_version": policy.policy_version},
        approved_by="u_1",
    )
    canary_id, base_id = v["policy_version"], v["parent_version"]
    logs = [log_of(i, policy_version=base_id) for i in range(20)]
    logs += [log_of(100 + i, policy_version=canary_id) for i in range(20)]
    rec = await store.get_version(canary_id)
    rec["scope"]["canary"]["until"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await store.put_version(rec)

    result = await mgr.check_canary(logs=logs)
    assert result["outcome"] == "promoted"
    assert (await store.active_version())["policy_version"] == canary_id


# ---------------------------------------------------------------------------
# 循环：审批 / 拒绝 / 反馈
# ---------------------------------------------------------------------------
def make_loop(policy, responses):
    from pathlib import Path

    from dispatcher.core.prompts import PromptLibrary
    from dispatcher.evolution.analyzer import Analyzer

    store = InMemoryEvolutionStore()
    loop = EvolutionLoop(
        store=store,
        analyzer=Analyzer(
            policy=policy, prompts=PromptLibrary(REPO_ROOT / "prompts"),
            llm=ScriptedLLM(responses),
            engines_dir=Path(REPO_ROOT) / "config" / "evolution",
        ),
        policy=policy,
        evolution_cfg=policy.evolution.model_dump(mode="json"),
    )
    return loop, store


async def test_loop_does_not_call_the_model_when_nothing_fires(policy):
    """没有检测器触发时不该调模型。

    让模型在"一切正常"上找问题，它一定会找出一些来——那正是最典型的
    "看起来合理但没用"的建议。
    """
    loop, store = make_loop(policy, [])          # 没有任何预置响应：调用即失败
    await loop.bootstrap()
    for i in range(40):
        await store.append_run_log(log_of(i, policy_version=policy.policy_version))
    out = await loop.run_pass()
    assert out.analysis["accepted"] == 0
    assert out.analysis.get("notes")


async def test_loop_persists_discarded_suggestions_with_reasons(policy):
    """被丢弃的建议也要入库并附原因——丢弃率本身是一个信号。"""
    bad = {"suggestions": [{
        "kind": "prompt_patch",
        "target": {"artifact": "dispatcher/core/nodeexec.py", "path": "x",
                   "current": "a", "proposed": "b"},
        "sample_size": 100, "rationale": "改执行器", "confidence": 0.9,
        "blast_radius": [],
    }]}
    loop, store = make_loop(policy, [bad])
    await loop.bootstrap()
    for i in range(40):
        await store.append_run_log(log_of(i, guard=["route_fallback"] if i < 12 else [],
                                          policy_version=policy.policy_version))
    out = await loop.run_pass()
    assert out.analysis["discarded"] == 1
    assert out.discarded[0]["status"] == "discarded"
    assert any("指向代码" in n for n in out.discarded[0]["validation_notes"])
    assert await store.get_suggestion(out.discarded[0]["suggestion_id"]) is not None


async def test_approve_rejects_a_stale_base_version(policy):
    """基底不是当前生效版本时拒绝——基于旧版本猜的改动，落地后是什么效果没人说得清。"""
    loop, store = make_loop(policy, [])
    await loop.bootstrap()
    await store.put_suggestion({
        **suggestion(), "status": "proposed",
        "basis_policy_version": "pv_does_not_exist", "scope": {"level": "user"},
    })
    from dispatcher.core.errors import DispatcherError

    with pytest.raises(DispatcherError) as ei:
        await loop.approve("sg_1", approved_by="u_1")
    assert ei.value.code == "policy_violation"


async def test_reject_requires_a_reason(policy):
    loop, store = make_loop(policy, [])
    await loop.bootstrap()
    await store.put_suggestion({**suggestion(), "status": "proposed",
                                "basis_policy_version": policy.policy_version})
    updated = await loop.reject("sg_1", reason="这个改动会掩盖真正的症状", decided_by="u_1")
    assert updated["status"] == "rejected"
    assert updated["decision_note"].startswith("这个改动")


async def test_feedback_attaches_to_the_existing_run_log(policy):
    loop, store = make_loop(policy, [])
    await store.append_run_log(log_of(1))
    ok = await loop.record_feedback("task_1", HumanSignal(
        verdict="edited", edits=[{"field": "category", "from": "其他", "to": "餐饮"}]
    ))
    assert ok
    logs = await store.list_run_logs(tenant_id="default")
    assert logs[0].human_signal is not None
    assert logs[0].human_signal.edits[0]["to"] == "餐饮"


async def test_feedback_for_a_pruned_run_returns_false_not_an_error(policy):
    """信号总是晚于运行到达。run 已过保留期不是错误，是正常的时间差。"""
    loop, _ = make_loop(policy, [])
    assert await loop.record_feedback("task_gone", HumanSignal(verdict="accepted")) is False
