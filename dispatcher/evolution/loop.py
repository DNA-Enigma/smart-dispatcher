"""自进化循环：把检测器、分析、校验、审批、金丝雀串起来。

这个类**不在请求路径上**。它的输入是已经落库的运行日志，输出是待审批的建议；
它从不自动应用任何改动。整条链路上唯一能改变系统行为的事件是**使用者点批准**。

循环的形状：

    运行日志 ──检测器（确定性）──> 触发的指标
                    │
                    └──LLM 分析──> 建议草稿
                                     │
                                     └──确定性校验──> 通过 / 丢弃
                                                        │
                                          使用者审批 ─────┘
                                              │
                                    新 PolicyVersion（金丝雀）
                                              │
                                    护栏检查 ──> 转正 / 自动回滚
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..core.errors import DispatcherError
from ..core.policy import Policy
from ..core.runlog import HumanSignal
from .analyzer import Analyzer
from .policy_store import PolicyVersionManager


@dataclass
class LoopOutcome:
    analysis: dict[str, Any]
    accepted: list[dict]
    discarded: list[dict]

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.analysis,
            "accepted_suggestions": [
                {"suggestion_id": s["suggestion_id"], "kind": s["kind"],
                 "target": s["target"].get("path"), "proposed": s["target"].get("proposed"),
                 "confidence": s.get("confidence"), "risk": s.get("risk"),
                 "rationale": s.get("rationale")}
                for s in self.accepted
            ],
            "discarded_suggestions": [
                {"kind": s.get("kind"), "target": (s.get("target") or {}).get("path"),
                 "why": s.get("validation_notes")}
                for s in self.discarded
            ],
        }


class EvolutionLoop:
    def __init__(
        self,
        *,
        store,
        analyzer: Analyzer,
        policy: Policy,
        evolution_cfg: dict | None = None,
    ) -> None:
        self._store = store
        self._analyzer = analyzer
        self._policy = policy
        self._versions = PolicyVersionManager(
            store=store, policy=policy, evolution_cfg=evolution_cfg
        )
        self._cfg = evolution_cfg or {}

    # ------------------------------------------------------------------
    async def bootstrap(self) -> dict:
        """把当前策略登记为初始版本。没有父版本，任何改动都无处可挂。"""
        return await self._versions.ensure_initial_version(self._policy)

    # ------------------------------------------------------------------
    async def run_pass(
        self, *, tenant_id: str = "default", now: datetime | None = None
    ) -> LoopOutcome:
        """跑一次分析。**建议只入 ``proposed``，绝不自动应用。**"""
        now = now or datetime.now(UTC)
        if not self._cfg.get("enabled", True):
            return LoopOutcome({"notes": ["自进化在配置里被关掉了"]}, [], [])

        window_days = _days(self._cfg.get("analysis_window", "7d"))
        logs = await self._store.list_run_logs(
            tenant_id=tenant_id, since=now - timedelta(days=window_days)
        )
        outcome = await self._analyzer.analyze(logs, scope={"level": "user",
                                                          "tenant_id": tenant_id}, now=now)
        for s in outcome.suggestions:
            await self._store.put_suggestion(s)
        # 被丢弃的也存下来：丢弃率本身是一个信号，指向分析提示词的质量。
        for s in outcome.discarded:
            await self._store.put_suggestion(s)
        return LoopOutcome(outcome.to_wire(), outcome.suggestions, outcome.discarded)

    # ------------------------------------------------------------------
    async def list_suggestions(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        return await self._store.list_suggestions(status=status, limit=limit)

    async def approve(
        self,
        suggestion_id: str,
        *,
        approved_by: str,
        scope: dict | None = None,
        note: str | None = None,
    ) -> dict:
        """**使用者本人**批准。契约里没有 admin 角色——审批人就是策略范围的所有者。"""
        s = await self._store.get_suggestion(suggestion_id)
        if s is None:
            raise DispatcherError("not_found", f"建议不存在：{suggestion_id}")
        if s.get("status") not in {"proposed"}:
            raise DispatcherError(
                "idempotency_conflict",
                f"建议当前状态为 {s.get('status')}，不可批准（只有 proposed 可以）",
                context={"status": s.get("status")},
            )
        version = await self._versions.create_from_suggestion(
            suggestion=s, approved_by=approved_by, scope=scope, note=note
        )
        await self._store.update_suggestion(suggestion_id, {
            "status": "canarying" if version["status"] == "canary" else "applied",
            "decided_by": approved_by,
            "decided_at": datetime.now(UTC).isoformat(),
            "decision_note": note,
            "resulting_policy_version": version["policy_version"],
        })
        return version

    async def reject(self, suggestion_id: str, *, reason: str, decided_by: str = "user") -> dict:
        """拒绝。**理由本身是信号**——频繁被拒说明分析在提"看起来合理但没用"的东西。"""
        s = await self._store.get_suggestion(suggestion_id)
        if s is None:
            raise DispatcherError("not_found", f"建议不存在：{suggestion_id}")
        updated = await self._store.update_suggestion(suggestion_id, {
            "status": "rejected",
            "decided_by": decided_by,
            "decided_at": datetime.now(UTC).isoformat(),
            "decision_note": reason,
        })
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    async def list_versions(self, *, limit: int = 50) -> list[dict]:
        return await self._store.list_versions(limit=limit)

    async def rollback(self, *, to_version: str, note: str | None = None) -> dict:
        return await self._versions.rollback(to_version=to_version, note=note)

    async def check_canary(self, *, tenant_id: str = "default",
                           now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        window_days = max(1, _days(self._cfg.get("analysis_window", "7d")))
        logs = await self._store.list_run_logs(
            tenant_id=tenant_id, since=now - timedelta(days=window_days)
        )
        return await self._versions.check_canary(logs=logs, now=now)

    # ------------------------------------------------------------------
    async def record_feedback(
        self, task_id: str, signal: HumanSignal
    ) -> bool:
        """把人工信号挂到运行日志上。

        信号总是晚于运行到达（用户看完结果才改），所以这是**补挂**而不是新建。
        返回 False 说明对应的 run 已过保留期——那不是错误，是正常的时间差。
        """
        return await self._store.attach_human_signal(task_id, signal)

    # ------------------------------------------------------------------
    async def suggest_drop_rate(self, *, days: int = 30) -> float:
        """建议丢弃率。这是**机制自身的健康度**，不来自运行日志。

        ``detectors.yaml`` 里那两个 ``source_field: internal`` 的检测器算的就是这个。
        放在这里而不是检测器引擎里，是因为它读的是建议库，不是运行日志——
        引擎已经把这类显式标成"不在此处求值"，而不是安静跳过。
        """
        rows = await self._store.list_suggestions(limit=1000)
        if not rows:
            return 0.0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        recent = [s for s in rows if _after(s.get("created_at"), cutoff)]
        if not recent:
            return 0.0
        return sum(1 for s in recent if s.get("status") == "discarded") / len(recent)

    async def suggest_reject_rate(self, *, days: int = 30) -> float:
        rows = await self._store.list_suggestions(limit=1000)
        cutoff = datetime.now(UTC) - timedelta(days=days)
        recent = [s for s in rows if _after(s.get("created_at"), cutoff)]
        decided = [s for s in recent if s.get("status") in {"rejected", "applied", "canarying",
                                                           "active", "rolled_back"}]
        if not decided:
            return 0.0
        return sum(1 for s in decided if s.get("status") == "rejected") / len(decided)


def _days(window: str) -> int:
    w = str(window).strip().lower()
    if w.endswith("d"):
        return max(1, int(w[:-1]))
    if w.endswith("h"):
        return 1
    return 7


def _after(ts: Any, cutoff: datetime) -> bool:
    if not isinstance(ts, str):
        return False
    try:
        return datetime.fromisoformat(ts) >= cutoff
    except ValueError:
        return False


__all__ = ["EvolutionLoop", "LoopOutcome"]
