"""RunLog —— 一次运行的结构化遥测，是 04 自进化的唯一输入。

对应 ``schemas/run_log.json``。两个字段值得单独说，因为整个自进化机制的价值
几乎都压在这两个字段上：

**``human_signal``** —— 最有价值也最难获得。记账场景的自然采集点就是确认/修改页：
用户把分类从"其他"改成"餐饮"，这一下同时给出了**错在哪**和**对的是什么**，
是一条带真值的标注。没有它，04 只能靠延迟和成本反推质量，效果差一个量级。

**``redaction``** —— 必填，不是可选。金融截图的留存必须显式决定，
不能靠默认行为蒙过去。分析只在结构化字段与哈希摘要上进行，**不碰原始图像**。

RunLog 是**派生**的：它从 TaskRecord 折叠出来，不额外维护一份真相。
这样不会出现"任务状态说成功、日志说失败"这种两边不一致的情况。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .state import TaskRecord

SignalVerdict = Literal["accepted", "edited", "rejected", "ignored"]


class HumanSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: SignalVerdict
    edits: list[dict[str, Any]] = Field(default_factory=list)
    reason: str | None = None
    rework_count: int | None = None
    signal_at: datetime | None = None


class Redaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_retained: bool
    media_retention_days: int | None = None
    text_stored: Literal["full", "hashed_summary", "none"] = "hashed_summary"
    sensitive_fields: list[str] | None = None


class RunLog(BaseModel):
    """一次运行。字段刻意是**扁平摘要**而不是嵌套的完整对象——
    检测器要在成千上万条上做聚合，把整个 TaskProfile 塞进来会让聚合变慢且没必要。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    parent_task_id: str | None = None
    tenant_id: str = "default"
    user_id: str
    source: str = "api"
    # **必填。** 没有它就无法比较不同策略版本的效果，金丝雀与 A/B 都做不了。
    policy_version: str
    started_at: datetime
    ended_at: datetime | None = None

    profile: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    outcome: dict[str, Any] = Field(default_factory=dict)

    human_signal: HumanSignal | None = None
    redaction: Redaction = Field(
        default_factory=lambda: Redaction(media_retained=False, media_retention_days=0)
    )

    # -- 派生：检测器最常用的那几个，直接放平，省得每条都挖一遍 ------------
    @property
    def status(self) -> str:
        return str(self.outcome.get("status", "unknown"))

    @property
    def route_id(self) -> str | None:
        return self.decision.get("route_id")

    @property
    def model_tier(self) -> str | None:
        return self.decision.get("model_tier")

    @property
    def task_type(self) -> str | None:
        return self.profile.get("task_type")

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


# ---------------------------------------------------------------------------
def build_run_log(record: TaskRecord, *, human_signal: HumanSignal | None = None) -> RunLog:
    """从任务记录折叠出 RunLog。

    这里**只读取、不推断**：凡是记录里没有的，宁可不填也不猜。
    一条"猜出来的"遥测会让 04 基于假数据提出建议，而假数据看不出是假的。
    """
    profile = record.profile
    decision = record.decision
    meta_eval = record.evaluation_meta or {}
    meta_route = record.route_meta or {}
    meta_plan = record.plan_meta or {}

    nodes: list[dict[str, Any]] = []
    for sid, r in (record.node_runs or {}).items():
        node = {
            "subtask_id": sid,
            "tool": _tool_of(record, sid),
            "tier": r.get("tier"),
            "status": r.get("status", "pending"),
            "attempts": r.get("attempts", 0),
            "cost": r.get("cost"),
            "escalated_from": None,
            "error_code": (r.get("error") or {}).get("code") if r.get("error") else None,
        }
        nodes.append(node)

    return RunLog(
        run_id=f"run_{record.task_id.removeprefix('task_')}",
        task_id=record.task_id,
        parent_task_id=record.parent_task_id,
        tenant_id=record.tenant_id,
        user_id=record.user_id,
        source=record.source,
        policy_version=record.policy_version or "unknown",
        started_at=record.created_at,
        ended_at=record.ended_at,
        profile={
            "task_type": profile.task_type if profile else None,
            "complexity_band": profile.complexity.band if profile else None,
            "complexity_score": profile.complexity.score if profile else None,
            "confidence": profile.confidence if profile else None,
            "degraded": bool(meta_eval.get("degraded")),
            "taxonomy_miss": bool(meta_eval.get("taxonomy_miss")),
            "est_cost_max": profile.est_cost.max if profile and profile.est_cost else None,
        },
        decision={
            "route_id": decision.route_id if decision else None,
            "model_tier": decision.model_tier if decision else None,
            "path": decision.path if decision else None,
            "handler": decision.handler if decision else None,
            "tool_set": list(decision.tool_set) if decision else None,
            "confidence": decision.confidence if decision else None,
            "guard_applied": list(meta_route.get("guard_applied") or []),
            "fallback_used": bool(meta_route.get("fallback_used")),
            "model_resolved": meta_route.get("model_resolved"),
            "rationale": (decision.rationale if decision else None),
        },
        plan=None
        if not record.plan
        else {
            "source": meta_plan.get("source") or record.plan.get("source"),
            "template_miss": bool(meta_plan.get("template_miss")),
            "revision": meta_plan.get("revisions", 1),
            "node_count": len(record.plan.get("nodes", [])),
            "max_parallelism": record.max_parallelism,
            "replans": max(0, int(meta_plan.get("revisions", 1)) - 1),
        },
        nodes=nodes,
        outcome={
            "status": record.status,
            "wall_ms": int(
                ((record.ended_at or record.updated_at) - record.created_at).total_seconds() * 1000
            ),
            "total_cost": {"amount": record.budget_spent, "currency": "CNY"},
            "llm_calls": _llm_calls(record),
            "clarifications": 1 if record.clarification else 0,
            "escalations": sum(1 for r in (record.node_runs or {}).values() if r.get("tier")),
            "retries": sum(
                max(0, int(r.get("attempts", 1)) - 1) for r in (record.node_runs or {}).values()
            ),
            "budget_warned": record.budget_warned,
            "errors": [record.error] if record.error else [],
        },
        human_signal=human_signal,
        redaction=Redaction(
            media_retained=False,   # 默认不留原图，见 docs/05-media.md
            media_retention_days=0,
            text_stored="hashed_summary",
            sensitive_fields=["amount", "merchant"] if profile else None,
        ),
    )


def _tool_of(record: TaskRecord, sid: str) -> str | None:
    for n in (record.plan or {}).get("nodes", []):
        if n.get("subtask_id") == sid:
            return n.get("tool") or n.get("role")
    return None


def _llm_calls(record: TaskRecord) -> int:
    """LLM 调用次数由**成本拆分**推出来，而不是另记一个计数器。

    记账是每次调用都发生的（``ctx.llm`` / ``ctx.llm_json``），而计数器要记得手动加——
    多一个需要手动维护的计数，就多一个会忘记维护的地方。
    """
    return len(record.llm_charges or [])


__all__ = ["HumanSignal", "Redaction", "RunLog", "SignalVerdict", "build_run_log"]
