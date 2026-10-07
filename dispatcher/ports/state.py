"""StateStorePort —— 任务记录与事件日志的持久化接口。

**明确不假定什么**（这几条是硬约束，因为消费端包含一部手机）：

* 不假定 Postgres。核心层没有 SQL，编排层的函数签名里没有 ``AsyncSession``。
  对照 ``ai-workmate`` 的 ``GraphState`` 节点函数直接带 ``db: AsyncSession``，
  那类耦合不能复制。
* 不假定分布式部署，因此读路径不依赖咨询锁。
* 不假定事件可按 JSON 路径查询——事件只按 ``seq`` 顺序读。
* 不假定有对象存储——媒体另走 ``MediaStorePort``。

M1 只需要任务记录的存取。事件日志（``append_event`` / ``read_events``）随 M2 的
SSE 与 ``Last-Event-ID`` 重放一起加，那时它才会被真正需要——现在加进来只会是一组
没有调用者的空方法。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..core.events import EventRecord
    from ..core.state import TaskRecord


@runtime_checkable
class StateStorePort(Protocol):
    """任务记录与事件日志的持久化。

    **事件日志与任务快照是一等公民的关系，不是主从。** 快照可以由事件折叠得出；
    反过来不行。这不是洁癖——它带来两个实际能力：断线重放（手机端必需），
    以及"只有事件管道、没有任务表"的部署形态（``EventLogOnlyStore``）。
    """

    # -- 任务 ------------------------------------------------------------
    async def create_task(self, record: TaskRecord) -> TaskRecord: ...

    async def get_task(self, task_id: str) -> TaskRecord | None: ...

    async def put_task(self, record: TaskRecord) -> TaskRecord: ...

    async def list_tasks(
        self, *, tenant_id: str, user_id: str | None, limit: int
    ) -> list[TaskRecord]: ...

    # -- 事件 ------------------------------------------------------------
    async def append_event(
        self,
        task_id: str,
        type: str,
        data: dict | None = None,
        *,
        subtask_id: str | None = None,
    ) -> EventRecord:
        """追加一条事件并返回它（含分配到的 ``seq``）。

        ``seq`` 必须**任务内单调递增且无洞**——它是重放游标。
        实现要保证并发追加时不会分配出重复或跳号的 seq。
        """
        ...

    async def read_events(
        self, task_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[EventRecord]:
        """读 ``seq > since_seq`` 的事件，按 seq 升序。

        这是 ``Last-Event-ID`` 重放的全部实现基础：客户端回传最后收到的 seq，
        服务端从这里接着给。
        """
        ...

    async def latest_seq(self, task_id: str) -> int:
        """当前最大 seq。没有事件时返回 0。"""
        ...

    async def prune_events(self, *, before: datetime, keep_terminal_days: int) -> int:
        """清理旧事件，返回删除条数。由定时任务驱动。

        ``keep_terminal_days`` 保护已终态任务的事件——那些是审计与 04 分析的依据，
        不能因为"旧"就被删掉。
        """
        ...

    # -- 幂等 ------------------------------------------------------------
    async def get_idempotency(self, key: str, tenant_id: str) -> str | None: ...

    async def put_idempotency(self, key: str, tenant_id: str, task_id: str) -> None:
        """登记 ``(key, tenant_id) → task_id``。同键重复登记时**覆盖**。

        覆盖是刻意的，而且**调用方必须先做过判定**（``Dispatcher.submit`` 在命中
        时比对请求内容，不同就 409，根本不走到这里）。会走到覆盖的只有一种情形：
        键上记着的那个任务已经不在库里了（记录被清理），此时把映射改指到新任务是
        正确行为——改成"不覆盖"反而会让这个键永远指向一个不存在的任务，于是每次
        重试都新建一个任务，幂等彻底失效。

        **租户是键空间的一部分，且租户来自 token（不再由客户端自报）**，因此
        "换个租户自报就能覆盖别人的键"这条路不存在。
        """
        ...


__all__ = ["StateStorePort"]
