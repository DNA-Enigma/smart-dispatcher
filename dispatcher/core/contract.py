"""契约的 Python 镜像。

``schemas/*.json`` 是**事实来源**；这里的模型是它在运行时的镜像。
两者的关系由 ``tests/test_contract_mirror.py`` 校验——它会拿这里构造出的
每个模型实例去跑对应的 JSON Schema。因此模型和 schema 不可能悄悄分叉。

一个刻意的选择：路由 id、存档位名、任务类型、能力名、工具名**都不在这里设枚举**。
它们是配置性词表（``docs/08-config-model.md``），写死在代码里会让"加一档模型"
变成代码变更。只有结构性词表（执行路径、状态、事件类型）才封闭。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 结构性词表 —— 封闭，改它等于改契约
TaskStatus = Literal[
    "received", "evaluating", "routing", "planning", "running",
    "awaiting_clarification", "escalated", "succeeded", "failed",
    "cancelled", "budget_exceeded", "rejected",
]
SubtaskStatus = Literal[
    "pending", "ready", "running", "retrying", "succeeded",
    "failed", "skipped", "cancelled", "defaulted",
]
ExecPath = Literal["direct_llm", "single_step_tool", "decompose"]
GuardAction = Literal[
    "tier_downgraded", "tier_upgraded_health", "tool_set_intersected",
    "budget_clamped", "route_fallback", "handler_fallback", "mode_promoted",
]
TokenBucket = Literal["xs", "s", "m", "l", "xl"]

# 与 schemas 对齐：多余字段一律拒绝，避免"多输出一个字段"这类契约漂移
Strict = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------
class MediaRef(BaseModel):
    model_config = Strict
    media_id: str
    kind: Literal["image", "audio", "pdf", "document"]
    mime: str
    bytes: int | None = None
    sha256: str | None = None
    role: Literal["source_document", "screenshot", "photo", "audio_note"] | None = None
    capabilities: list[str] | None = None


class EnvelopeInput(BaseModel):
    model_config = Strict
    text: str | None = None
    media: list[MediaRef] | None = None


class Identity(BaseModel):
    model_config = Strict
    tenant_id: str = "default"
    user_id: str
    locale: str | None = None
    timezone: str | None = None


class Declared(BaseModel):
    model_config = Strict
    intent: str | None = None
    capability: str | None = None
    authoritative: bool = False


class Constraints(BaseModel):
    model_config = Strict
    mode_preference: Literal["auto", "sync", "async"] = "auto"
    max_cost: float | None = None
    max_wall_ms: int | None = None
    allowed_model_tiers: list[str] | None = None
    data_sensitivity: Literal["public", "internal", "personal", "financial"] | None = None


class ClientInfo(BaseModel):
    model_config = Strict
    app: str | None = None
    app_version: str | None = None
    min_supported_client_version: str | None = None
    supports_sse: bool = True
    supports_last_event_id: bool = False


class TaskEnvelope(BaseModel):
    model_config = Strict
    request_id: str | None = None
    idempotency_key: str | None = None
    parent_task_id: str | None = None
    identity: Identity
    input: EnvelopeInput
    declared: Declared = Field(default_factory=Declared)
    constraints: Constraints = Field(default_factory=Constraints)
    client: ClientInfo = Field(default_factory=ClientInfo)
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 01 输出
# ---------------------------------------------------------------------------
class Complexity(BaseModel):
    """复杂度：LLM 给连续分数，代码按 ``thresholds.complexity_band_cutoffs`` 查表得档位。

    ``band`` 因此是**派生字段**，不要求 LLM 输出。让模型同时给分数和档位，
    只会引入两者不一致的可能，而档位是纯查表的结果，不需要判断。
    """

    model_config = Strict
    score: float = Field(ge=0, le=1)
    band: Literal["low", "medium", "high"] | None = None
    reasons: list[str] = Field(default_factory=list)


class Urgency(BaseModel):
    model_config = Strict
    level: Literal["low", "normal", "high"]
    reasons: list[str] = Field(default_factory=list)


class VisionNeeds(BaseModel):
    model_config = Strict
    required: bool
    image_roles: list[str] = Field(default_factory=list)
    expected_extraction: list[str] = Field(default_factory=list)


class EstimatedScale(BaseModel):
    model_config = Strict
    input_tokens_bucket: TokenBucket | None = None
    output_tokens_bucket: TokenBucket | None = None
    est_tool_calls: int | None = Field(default=None, ge=0)


class CostEstimate(BaseModel):
    model_config = Strict
    currency: str
    min: float = Field(ge=0)
    max: float = Field(ge=0)
    basis: str | None = None


class LatencyEstimate(BaseModel):
    model_config = Strict
    min: int = Field(ge=0)
    max: int = Field(ge=0)


class ClarificationOption(BaseModel):
    model_config = Strict
    id: str
    label: str


class Clarification(BaseModel):
    model_config = Strict
    question: str
    options: list[ClarificationOption] = Field(default_factory=list)
    blocking: bool
    partial_profile: dict[str, Any] | None = None


class ProfileCache(BaseModel):
    model_config = Strict
    hit: bool
    key: str | None = None
    source_task_id: str | None = None
    similarity: float | None = None


class EvaluatorTelemetry(BaseModel):
    model_config = Strict
    tier: str | None = None
    model_resolved: str | None = None
    escalated_from: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    tokens: dict[str, int] | None = None


class TaskProfile(BaseModel):
    model_config = Strict
    profile_version: str = "1.0"
    policy_version: str | None = None
    task_type: str
    intent_summary: str = ""
    modality: list[Literal["text", "image", "audio", "pdf"]]
    complexity: Complexity
    urgency: Urgency
    vision: VisionNeeds | None = None
    candidate_capabilities: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    data_sensitivity: Literal["public", "internal", "personal", "financial"] | None = None
    estimated_scale: EstimatedScale | None = None
    est_cost: CostEstimate | None = None
    est_latency_ms: LatencyEstimate | None = None
    recommended_mode: Literal["sync", "async"]
    needs_clarification: bool = False
    clarification: Clarification | None = None
    confidence: float = Field(ge=0, le=1)
    degraded: bool = False
    profile_cache: ProfileCache | None = None
    evaluator: EvaluatorTelemetry | None = None


# ---------------------------------------------------------------------------
# 02 输出
# ---------------------------------------------------------------------------
class DecisionBudget(BaseModel):
    model_config = Strict
    max_cost: float = Field(ge=0)
    max_wall_ms: int = Field(ge=1)
    max_llm_calls: int = Field(ge=0)


class EscalationRule(BaseModel):
    model_config = Strict
    on: list[str] = Field(default_factory=list)
    to_tier: str
    max_escalations: int = Field(ge=0)


class GuardReport(BaseModel):
    """守卫的确定性修正记录。**由代码写入，LLM 不得产出本字段。**"""
    model_config = Strict
    applied: list[GuardAction] = Field(default_factory=list)
    original_tier: str | None = None
    original_route_id: str | None = None
    original_mode: Literal["sync", "async"] | None = None
    fallback_used: bool = False
    violations: list[str] = Field(default_factory=list)


class RouteDecision(BaseModel):
    model_config = Strict
    decision_version: str = "1.0"
    policy_version: str
    route_id: str
    path: ExecPath
    model_tier: str
    vision_tier: str | None = None
    handler: str | None = None
    tool_set: list[str] = Field(default_factory=list)
    execution_mode: Literal["sync", "async"]
    decompose: bool
    parallelism_hint: int | None = Field(default=None, ge=1)
    budget: DecisionBudget
    escalation_rule: EscalationRule | None = None
    rationale: str = ""
    confidence: float = Field(ge=0, le=1)
    guard: GuardReport
    mode_change_reason: str | None = None


__all__ = [
    "Clarification", "Complexity", "Constraints", "CostEstimate", "DecisionBudget",
    "Declared", "EnvelopeInput", "EscalationRule", "EstimatedScale", "EvaluatorTelemetry",
    "GuardAction", "GuardReport", "Identity", "LatencyEstimate", "MediaRef", "ProfileCache",
    "RouteDecision", "TaskEnvelope", "TaskProfile", "TaskStatus", "Urgency", "VisionNeeds",
    "wire_dump",
]


def wire_dump(model: BaseModel) -> dict[str, Any]:
    """序列化成上线格式。

    **必须 ``exclude_none=True``。** 契约里大量的可选字段声明为 ``{"type": "string"}``
    且不进 ``required``——它们的意思是"可以没有"，不是"可以为 null"。
    发一个 ``"band": null`` 会被 schema 判为非法，而"省略"才是正确表达。

    这个区别在响应体里同样重要：``null`` 说的是"这个值存在且为空"，
    "缺失"说的是"这个字段与本次响应无关"。把两者混为一谈，会让客户端
    无法区分"没有澄清问题"和"澄清问题为空"这类语义。
    """
    return model.model_dump(mode="json", exclude_none=True)
