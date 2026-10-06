"""契约自洽性：schema 合法、fixture 双向校验、镜像与 schema 一致。

三层意义：

1. **schema 自身合法**（draft 2020-12）。
2. **fixture 双向**：``.example`` 必须通过，``.invalid`` 必须被拒，
   且反例要因**预期的那一条**而失败——被别的字段带崩等于没测到那条规则。
3. **Python 镜像与 schema 不分叉**：把镜像构造出的实例拿去跑 JSON Schema。
   这一层是关键：模型和 schema 是两份手写的东西，没有这层检查它们必然漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from dispatcher.core.contract import (
    Clarification,
    Complexity,
    DecisionBudget,
    EscalationRule,
    GuardReport,
    RouteDecision,
    TaskEnvelope,
    TaskProfile,
    Urgency,
    wire_dump,
)

# 每个反例**应当**失败的原因。把这条对应关系写下来，是为了让"反例通过"
# 这件事有确定含义——不是"因为某个字段写错了所以失败了"，而是"因为这条规则失败了"。
INVALID_REASONS: dict[str, str] = {
    "event.invalid.json": "subtask.started 的 data 缺 attempt",
    "execution_plan.invalid.json": "on_failure 不在枚举内",
    "policy_patch.invalid.json": "artifact 指向代码文件；op=delete 不允许",
    "problem.invalid.json": "code 不在封闭词表内、缺 retryable（字符串哨兵反例）",
    "route_decision.invalid.json": "path=direct_llm 却给了 handler 与 tool_set",
    "run_log.invalid.json": "缺 policy_version 与 redaction",
    "suggestion.invalid.json": "kind 不在封闭词表内（提议改代码）",
    "task_envelope.invalid.json": "input 既无 text 也无 media",
    "task_profile.invalid.json": "needs_clarification=true 但 clarification=null",
    "task_snapshot.invalid.json": "status 不在枚举内 / progress 越界",
}


@pytest.fixture(scope="module")
def registry(schemas: dict[str, dict]) -> Registry:
    return Registry().with_resources(
        [(sid, Resource.from_contents(s)) for sid, s in schemas.items()]
    )


def _schema_key(path: Path) -> str:
    name = path.name[: -len(".json")]
    for suffix in (".example", ".invalid"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def test_all_schemas_are_valid(repo_root: Path):
    files = sorted((repo_root / "schemas").glob("*.json"))
    assert files, "schemas/ 为空"
    for p in files:
        Draft202012Validator.check_schema(json.loads(p.read_text(encoding="utf-8")))


def test_every_fixture_has_a_schema(repo_root: Path, schemas: dict[str, dict]):
    for p in sorted((repo_root / "fixtures").glob("*.json")):
        sid = f"https://smart-dispatcher.dev/schemas/{_schema_key(p)}.json"
        assert sid in schemas, f"{p.name} 没有对应的 schema"


def test_examples_validate(repo_root: Path, schemas: dict[str, dict], registry: Registry):
    examples = sorted((repo_root / "fixtures").glob("*.example.json"))
    assert examples, "没有找到任何 example fixture"
    for p in examples:
        sid = f"https://smart-dispatcher.dev/schemas/{_schema_key(p)}.json"
        v = Draft202012Validator(schemas[sid], registry=registry)
        errs = list(v.iter_errors(json.loads(p.read_text(encoding="utf-8"))))
        assert not errs, f"{p.name} 应当通过但失败了：{[e.message for e in errs][:3]}"


def test_invalid_fixtures_are_rejected(repo_root: Path, schemas: dict[str, dict], registry: Registry):
    invalids = sorted((repo_root / "fixtures").glob("*.invalid.json"))
    assert invalids, "没有找到任何 invalid fixture"
    for p in invalids:
        sid = f"https://smart-dispatcher.dev/schemas/{_schema_key(p)}.json"
        v = Draft202012Validator(schemas[sid], registry=registry)
        errs = list(v.iter_errors(json.loads(p.read_text(encoding="utf-8"))))
        assert errs, f"{p.name} 应当被拒但通过了"


def test_every_invalid_fixture_documents_its_reason(repo_root: Path):
    """每个反例都要在 INVALID_REASONS 里写明它测的是哪条规则。

    没有这一条，反例很容易变成"随便改坏一个字段"——那样它测不到任何规则，
    只是增加了 fixture 数量。
    """
    for p in sorted((repo_root / "fixtures").glob("*.invalid.json")):
        assert p.name in INVALID_REASONS, f"{p.name} 未说明它违反的是哪条规则"


# ---------------------------------------------------------------------------
# 镜像与 schema 不分叉
# ---------------------------------------------------------------------------
def _validate(instance: dict, schema_id: str, schemas: dict, registry: Registry):
    v = Draft202012Validator(schemas[schema_id], registry=registry)
    errs = list(v.iter_errors(instance))
    assert not errs, f"{schema_id} 校验失败：{[e.message for e in errs][:3]}"


def test_task_profile_mirror_matches_schema(schemas, registry):
    p = TaskProfile(
        policy_version="pv_test",
        task_type="bookkeeping.capture_from_receipt",
        intent_summary="记一笔餐饮支出",
        modality=["image", "text"],
        complexity=Complexity(score=0.42, band="medium", reasons=["需视觉抽取"]),
        urgency=Urgency(level="normal"),
        recommended_mode="async",
        confidence=0.86,
    )
    _validate(wire_dump(p), "https://smart-dispatcher.dev/schemas/task_profile.json",
              schemas, registry)


def test_task_profile_band_is_optional_in_both(schemas, registry):
    """band 是派生字段，schema 与镜像都必须允许缺失。"""
    p = TaskProfile(
        task_type="chat.explain", modality=["text"],
        complexity=Complexity(score=0.1), urgency=Urgency(level="low"),
        recommended_mode="sync", confidence=0.5,
    )
    assert p.complexity.band is None
    _validate(wire_dump(p), "https://smart-dispatcher.dev/schemas/task_profile.json",
              schemas, registry)


def test_task_profile_clarification_consistency(schemas, registry):
    p = TaskProfile(
        task_type="bookkeeping.capture_from_receipt", modality=["image"],
        complexity=Complexity(score=0.5), urgency=Urgency(level="normal"),
        recommended_mode="async", confidence=0.4,
        needs_clarification=True,
        clarification=Clarification(question="支出还是收入？", blocking=True),
    )
    _validate(wire_dump(p), "https://smart-dispatcher.dev/schemas/task_profile.json",
              schemas, registry)


def test_route_decision_mirror_matches_schema(schemas, registry):
    d = RouteDecision(
        policy_version="pv_test", route_id="direct_answer", path="direct_llm",
        model_tier="cheap", handler=None, tool_set=[], execution_mode="sync",
        decompose=False,
        budget=DecisionBudget(max_cost=0.003, max_wall_ms=8000, max_llm_calls=1),
        escalation_rule=EscalationRule(on=["low_confidence"], to_tier="standard", max_escalations=1),
        rationale="单步闲聊", confidence=0.9,
        guard=GuardReport(applied=[], fallback_used=False),
    )
    _validate(wire_dump(d),
              "https://smart-dispatcher.dev/schemas/route_decision.json", schemas, registry)


def test_route_decision_direct_llm_forbids_handler(schemas, registry):
    """镜像必须复现 schema 里那条 allOf：direct_llm 不得带 handler。"""
    d = RouteDecision(
        policy_version="pv", route_id="direct_answer", path="direct_llm",
        model_tier="cheap", handler="bookkeeping", tool_set=[], execution_mode="sync",
        decompose=False,
        budget=DecisionBudget(max_cost=0.003, max_wall_ms=1, max_llm_calls=0),
        confidence=0.5, guard=GuardReport(applied=[], fallback_used=False),
    )
    with pytest.raises(AssertionError):
        _validate(wire_dump(d),
                  "https://smart-dispatcher.dev/schemas/route_decision.json", schemas, registry)


def test_envelope_mirror_matches_schema(schemas, registry):
    e = TaskEnvelope.model_validate({
        "identity": {"user_id": "u_1"},
        "input": {"text": "中午吃饭花了 38"},
    })
    _validate(wire_dump(e),
              "https://smart-dispatcher.dev/schemas/task_envelope.json", schemas, registry)


def test_envelope_requires_some_input(schemas, registry):
    e = TaskEnvelope.model_validate({"identity": {"user_id": "u_1"}, "input": {}})
    with pytest.raises(AssertionError):
        _validate(wire_dump(e),
                  "https://smart-dispatcher.dev/schemas/task_envelope.json", schemas, registry)


# ---------------------------------------------------------------------------
# 一个刻意的"不检查项"
# ---------------------------------------------------------------------------
def test_route_ids_and_tiers_are_not_pinned_in_schema(schemas):
    """路由 id 与档位名**不应**出现在 schema 枚举里。

    它们是配置性词表：写死在 schema 里会让"加一档模型"变成契约变更。
    与之相对，``path``/``executor``/状态/事件类型/错误码是结构性词表，必须封闭。
    这条测试锁住这个区分，因为它极易被"顺手补上"。
    """
    rd = schemas["https://smart-dispatcher.dev/schemas/route_decision.json"]
    props = rd["properties"]
    assert "enum" not in props["route_id"], "route_id 不该是 schema 枚举"
    assert "enum" not in props["model_tier"], "model_tier 不该是 schema 枚举"
    assert props["path"]["enum"], "path 是结构性词表，应当封闭"
