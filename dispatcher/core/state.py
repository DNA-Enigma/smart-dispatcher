"""任务记录与状态存储端口。

契约（``schemas/task_snapshot.json``）描述的是**对外可见**的那部分；
这里额外承载一些只用于内部与自省的字段（request_id、路由原始候选、评估遥测），
它们不进入快照——快照是 ``additionalProperties: false`` 的，多一个字段就是契约泄漏。

状态字段刻意对齐 ``ai-workmate/app/lib/data/task_repo.dart`` 的词汇，
使 Flutter 侧的任务看板可 1:1 映射。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .contract import RouteDecision, TaskProfile, TaskStatus
from .errors import DispatcherError
from .guard import RawDecision
from .settings import redact_secrets


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    tenant_id: str = "default"
    user_id: str
    parent_task_id: str | None = None
    request_id: str = ""

    status: TaskStatus = "received"
    source: str = "api"
    title: str | None = None
    subtitle: str | None = None

    mode: str = "async"
    mode_changed: bool = False
    mode_change_reason: str | None = None
    # 提升的**可读说明**（含具体数字）。契约里只放原因码，因为那是给程序判断的；
    # 数字放这里，给人和日志看——"为什么升了"和"超了多少"是两个问题。
    mode_change_detail: str | None = None

    # ---- 阶段产物 ----
    profile: TaskProfile | None = None
    decision: RouteDecision | None = None

    # ---- 阶段产物 ----
    plan: dict[str, Any] | None = None
    plan_meta: dict[str, Any] = Field(default_factory=dict)
    # 逐节点的运行状态。快照里的 plan.nodes 由它与计划合并得出。
    node_runs: dict[str, Any] = Field(default_factory=dict)
    # 已成功节点的产出。澄清后恢复时用它做 prior——**不重跑**已完成的工作。
    node_outputs: dict[str, Any] = Field(default_factory=dict)
    max_parallelism: int | None = None

    artifacts: dict[str, Any] | None = None
    clarification: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    budget_spent: float = 0.0
    budget_enforcement: str = "advisory"
    budget_warned: bool = False

    # 原始请求。澄清恢复时要用它重建 envelope——只存 id 不够，
    # 因为答复之后整条流水线要重走一遍（评估、路由都可能因为新信息而变）。
    envelope: dict[str, Any] | None = None

    # 逐笔成本明细（谁花的、花在哪一步）。RunLog 从它推出 LLM 调用次数——
    # 而不是另记一个需要手动维护的计数器：多一个手动维护的计数，
    # 就多一个会忘记维护的地方。
    llm_charges: list[dict[str, Any]] = Field(default_factory=list)

    # 人工质量信号。这是 04 最有价值的输入：用户的每一次修改都同时给出了
    # 错在哪（field）和对的是什么（to），是一条带真值的标注。
    human_signal: dict[str, Any] | None = None

    policy_version: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    # ---- 仅内部：原始候选与评估/路由遥测，用于 04 的归因 ----
    raw_decision: RawDecision | None = None
    evaluation_meta: dict[str, Any] = Field(default_factory=dict)
    route_meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def progress(self) -> float:
        """与 ``task_repo.dart`` 一致：终态节点数 ÷ 总节点数。

        没有计划时按任务状态折算（中途 0、终态 1）——公式不变，
        只是分子分母换了来源。这样 Flutter 侧已有的进度控件不需要改。
        """
        if self.node_runs:
            done = sum(
                1 for r in self.node_runs.values() if r.get("status") in TERMINAL_NODE_STATUS
            )
            return done / len(self.node_runs)
        return 1.0 if self.status in TERMINAL_STATUSES else 0.0

    def stage_notes(self) -> dict[str, list[str]]:
        """三个阶段各自的诊断备注，按阶段分组。

        ``notes`` 是"这一步为什么降级 / 回退 / 没跑成"的**唯一线索**——例如
        "评估器全部尝试失败，使用兜底画像"、"模板 receipt_to_entry 未通过校验"。
        它们此前只留在内部的 ``*_meta`` 里，快照与错误体都不暴露，于是外面只能
        看到一个 ``degraded=true``，看不到原因（2026-10-08 实测：排障只能靠猜）。

        分组而不是合成一个平表：同一条文案在不同阶段含义不同，而"哪一步出的问题"
        正是排障时要回答的第一个问题。

        快照与错误体**共用这一个实现**——两处各写一份一定会分叉。
        """
        return {
            "evaluation": _redact_notes(self.evaluation_meta.get("notes")),
            "routing": _redact_notes(self.route_meta.get("notes")),
            "planning": _redact_notes(self.plan_meta.get("notes")),
        }

    def to_snapshot(self) -> dict[str, Any]:
        """渲染成 ``schemas/task_snapshot.json`` 的实例。

        只放契约允许的字段——内部遥测（original 候选、评估延迟等）留在这里，
        不往上冒。测试会把本方法的输出拿去跑 JSON Schema。
        """
        snap: dict[str, Any] = {
            "task_id": self.task_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "parent_task_id": self.parent_task_id,
            "status": self.status,
            "source": self.source,
            "title": self.title,
            "subtitle": self.subtitle,
            "progress": self.progress,
            "mode": self.mode,
            "mode_changed": self.mode_changed,
            "mode_change_reason": self.mode_change_reason,
            "profile": self.profile.model_dump(mode="json") if self.profile else None,
            "decision": self.decision.model_dump(mode="json") if self.decision else None,
            "plan": self._plan_snapshot(),
            "artifacts": self.artifacts,
            "budget": {
                "max_cost": self.decision.budget.max_cost if self.decision else 0.0,
                "spent": self.budget_spent,
                "currency": "CNY",
                "max_wall_ms": self.decision.budget.max_wall_ms if self.decision else 0,
                "elapsed_ms": int(
                    ((self.ended_at or self.updated_at) - self.created_at).total_seconds() * 1000
                ),
                "enforcement": self.budget_enforcement,
                "warned": self.budget_warned,
            },
            "clarification": self.clarification,
            "error": self.error,
            "stage_notes": self.stage_notes(),
            "policy_version": self.policy_version,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "updated_at": self.updated_at.isoformat().replace("+00:00", "Z"),
            "ended_at": (
                self.ended_at.isoformat().replace("+00:00", "Z") if self.ended_at else None
            ),
            "links": {
                "events_url": None if self.ended_at else f"/v1/tasks/{self.task_id}/events",
                "result_url": f"/v1/tasks/{self.task_id}/result",
            },
        }
        return snap


    def _plan_snapshot(self) -> dict[str, Any] | None:
        """把计划与逐节点运行状态合成快照里的 ``plan``。

        计划（静态形状）与运行状态（动态结果）分开存，快照里才合并——
        这样恢复时能直接拿静态计划复用，而运行状态可以独立更新。
        """
        if not self.plan:
            return None
        nodes = []
        for n in self.plan.get("nodes", []):
            sid = n.get("subtask_id")
            r = self.node_runs.get(sid, {})
            nodes.append({
                "subtask_id": sid,
                "name": n.get("name"),
                "status": r.get("status", "pending"),
                "progress": r.get("progress", 0.0),
                "attempts": r.get("attempts", 0),
                "tier": r.get("tier") or n.get("model_tier"),
                "started_at": r.get("started_at"),
                "ended_at": r.get("ended_at"),
                "cost": r.get("cost"),
                "error": r.get("error"),
            })
        return {
            "strategy": self.plan.get("strategy", "dag"),
            "revision": self.plan.get("revision", 1),
            "source": self.plan.get("source"),
            "max_parallelism": self.max_parallelism or self.plan.get("max_parallelism"),
            "nodes": nodes,
        }


TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "budget_exceeded", "rejected"}
)


def _redact_notes(notes: Any) -> list[str]:
    """把备注出快照前过一道密钥脱敏（见 ``settings.redact_secrets``）。

    非列表（历史记录形状不对）一律当作"没有备注"：快照是契约的一部分，
    一个形状不对的字段不该把渲染整条快照这件事带崩。
    """
    if not isinstance(notes, list):
        return []
    return [redact_secrets(str(n)) for n in notes]

# 子任务的终态。与 runner 里的同名集合一致——两边都表示"这一步已经有结论了"。
TERMINAL_NODE_STATUS = frozenset(
    {"succeeded", "failed", "skipped", "cancelled", "defaulted"}
)


def require_task(record: TaskRecord | None, task_id: str) -> TaskRecord:
    if record is None:
        raise DispatcherError("not_found", f"任务不存在：{task_id}", context={"task_id": task_id})
    return record


__all__ = ["TERMINAL_NODE_STATUS", "TERMINAL_STATUSES", "TaskRecord", "require_task"]
