"""内存状态存储。

给测试与单进程部署用。它也是 ``StateStorePort`` 抽象成立的证明：实现完全不含
持久化，而流水线、执行器与接口层一行都不用改。

**它不做的事**：不落盘、不跨进程、重启即失。因此它只适合两条路之外的场景：
测试、以及"单进程短生命周期"的部署。手机端本地模式用 SQLite（跨重启的
``Last-Event-ID`` 重放需要它），服务端用 Postgres。

并发安全：``append_event`` 的 seq 分配与写入必须在同一临界区内完成，
否则并发追加会分配出重复的 seq——而 seq 是重放游标，重复等于重放错位。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..core.events import EventRecord, new_event
from ..core.state import TERMINAL_STATUSES, TaskRecord


class InMemoryStateStore:
    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        # 幂等键作用域是 (key, tenant)——不同租户用同一个键不该互相干扰
        self._idem: dict[tuple[str, str], str] = {}
        self._events: dict[str, list[EventRecord]] = {}
        self._seq: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    async def create_task(self, record: TaskRecord) -> TaskRecord:
        self._tasks[record.task_id] = record
        return record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def put_task(self, record: TaskRecord) -> TaskRecord:
        self._tasks[record.task_id] = record
        return record

    async def list_tasks(
        self, *, tenant_id: str, user_id: str | None, limit: int
    ) -> list[TaskRecord]:
        rows = [
            t for t in self._tasks.values()
            if t.tenant_id == tenant_id and (user_id is None or t.user_id == user_id)
        ]
        rows.sort(key=lambda t: t.created_at, reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    async def append_event(
        self,
        task_id: str,
        type: str,
        data: dict | None = None,
        *,
        subtask_id: str | None = None,
    ) -> EventRecord:
        async with self._lock:
            seq = self._seq.get(task_id, 0) + 1
            self._seq[task_id] = seq
            rec = new_event(
                seq=seq, task_id=task_id, type=type, data=data, subtask_id=subtask_id
            )
            self._events.setdefault(task_id, []).append(rec)
            return rec

    async def read_events(
        self, task_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[EventRecord]:
        rows = [e for e in self._events.get(task_id, []) if e.seq > since_seq]
        return rows[:limit] if limit is not None else rows

    async def latest_seq(self, task_id: str) -> int:
        return self._seq.get(task_id, 0)

    async def prune_events(self, *, before: datetime, keep_terminal_days: int) -> int:
        """删旧事件。**终态任务走另一把尺子。**

        * 非终态 / 无任务记录 —— ``before`` 之前的删掉。
        * 已终态 —— ``now - keep_terminal_days`` 之前的删掉。

        两把尺子互相独立（不是取更宽者）：它们问的是两个不同问题。``before`` 问
        "这条事件有多旧"，``keep_terminal_days`` 问"这个任务结束多久了"。
        绑在一起会导致 ``before`` 落在未来时，本该被保护的终态事件一起被删。

        行为与 ``SqliteStateStore`` 严格一致，另有 parity 测试同时跑两者比对结果——
        否则"存储可替换"只是一个愿望。
        """
        now = datetime.now(UTC)
        terminal_cutoff = now - timedelta(days=keep_terminal_days)
        removed = 0
        for task_id, events in list(self._events.items()):
            task = self._tasks.get(task_id)
            is_terminal = task is not None and task.status in TERMINAL_STATUSES
            cutoff = terminal_cutoff if is_terminal else before
            keep = [e for e in events if e.ts >= cutoff]
            removed += len(events) - len(keep)
            if keep:
                self._events[task_id] = keep
            else:
                self._events.pop(task_id, None)
        return removed

    # ------------------------------------------------------------------
    # 幂等
    # ------------------------------------------------------------------
    async def get_idempotency(self, key: str, tenant_id: str) -> str | None:
        return self._idem.get((key, tenant_id))

    async def put_idempotency(self, key: str, tenant_id: str, task_id: str) -> None:
        self._idem[(key, tenant_id)] = task_id

    # -- 供测试与自省 ---------------------------------------------------
    def __len__(self) -> int:
        return len(self._tasks)

    async def event_count(self, task_id: str) -> int:
        return len(self._events.get(task_id, []))


__all__ = ["InMemoryStateStore"]
