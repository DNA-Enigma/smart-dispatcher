"""内存版演化存储：运行日志、建议、策略版本。

给测试与单进程开发用。它也是 ``EvolutionStorePort`` 抽象成立的证明——把它换成
SQLite 时，检测器、分析器、审批流程一行都不用改。

**它不解决的问题**（写在 docstring 里而不是留给使用者踩）：不落盘、重启即失。
对自进化来说这尤其要紧：分析窗口是 7 天，而内存存储活不过一次重启——所以
生产部署必须用持久实现。这一点在 ``DispatcherConfig.state_backend`` 里
已经用同一个开关表达（``sqlite``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.runlog import HumanSignal, RunLog


class InMemoryEvolutionStore:
    def __init__(self) -> None:
        self._logs: dict[str, RunLog] = {}          # task_id → RunLog
        self._suggestions: dict[str, dict] = {}
        self._versions: dict[str, dict] = {}
        self._active: str | None = None

    # ------------------------------------------------------------------
    # 运行日志
    # ------------------------------------------------------------------
    async def append_run_log(self, log: RunLog) -> None:
        self._logs[log.task_id] = log

    async def list_run_logs(
        self,
        *,
        tenant_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
        policy_version: str | None = None,
        limit: int | None = None,
    ) -> list[RunLog]:
        rows = [x for x in self._logs.values() if x.tenant_id == tenant_id]
        if since is not None:
            rows = [x for x in rows if x.started_at >= since]
        if until is not None:
            rows = [x for x in rows if x.started_at <= until]
        if policy_version is not None:
            rows = [x for x in rows if x.policy_version == policy_version]
        rows.sort(key=lambda x: x.started_at)
        return rows[:limit] if limit is not None else rows

    async def attach_human_signal(self, task_id: str, signal: HumanSignal) -> bool:
        log = self._logs.get(task_id)
        if log is None:
            return False
        self._logs[task_id] = log.model_copy(update={"human_signal": signal})
        return True

    async def prune_run_logs(self, *, before: datetime) -> int:
        stale = [t for t, x in self._logs.items() if x.started_at < before]
        for t in stale:
            del self._logs[t]
        return len(stale)

    # ------------------------------------------------------------------
    # 建议
    # ------------------------------------------------------------------
    async def put_suggestion(self, suggestion: dict) -> None:
        self._suggestions[suggestion["suggestion_id"]] = suggestion

    async def get_suggestion(self, suggestion_id: str) -> dict | None:
        return self._suggestions.get(suggestion_id)

    async def list_suggestions(
        self, *, status: str | None = None, scope_level: str | None = None, limit: int = 50
    ) -> list[dict]:
        rows = list(self._suggestions.values())
        if status is not None:
            rows = [s for s in rows if s.get("status") == status]
        if scope_level is not None:
            rows = [s for s in rows if (s.get("scope") or {}).get("level") == scope_level]
        rows.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        return rows[:limit]

    async def update_suggestion(self, suggestion_id: str, patch: dict) -> dict | None:
        cur = self._suggestions.get(suggestion_id)
        if cur is None:
            return None
        merged = {**cur, **patch}
        self._suggestions[suggestion_id] = merged
        return merged

    # ------------------------------------------------------------------
    # 策略版本
    # ------------------------------------------------------------------
    async def put_version(self, version: dict) -> None:
        self._versions[version["policy_version"]] = version

    async def get_version(self, version_id: str) -> dict | None:
        return self._versions.get(version_id)

    async def list_versions(self, *, limit: int = 50) -> list[dict]:
        rows = sorted(
            self._versions.values(), key=lambda v: v.get("created_at") or "", reverse=True
        )
        return rows[:limit]

    async def active_version(self) -> dict | None:
        if self._active is None:
            return None
        return self._versions.get(self._active)

    async def set_active(self, version_id: str) -> None:
        self._active = version_id

    # -- 供测试与自省 ---------------------------------------------------
    async def count(self) -> dict[str, int]:
        return {
            "run_logs": len(self._logs),
            "suggestions": len(self._suggestions),
            "versions": len(self._versions),
        }

    def get(self, key: str) -> Any:  # pragma: no cover - 调试用
        return {"logs": self._logs, "suggestions": self._suggestions, "versions": self._versions}.get(key)


__all__ = ["InMemoryEvolutionStore"]
