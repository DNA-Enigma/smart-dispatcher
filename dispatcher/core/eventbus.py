"""事件总线：持久化 + 实时扇出。

两件事必须同时成立，缺一不可：

* **持久化** —— 事件先落库，再谈推送。反过来（先推送再落库）会出现一个很尴尬的
  窗口：客户端收到了事件，但重连时从库里查不到它，于是重放把它"抹掉"了。
* **实时扇出** —— 订阅者不该靠轮询数据库。等待者在本进程内被唤醒，
  而重放仍然走存储，因此进程重启后一样能补齐。

这个分工让 SSE 端点的实现变得很短：先读库重放，再挂上实时队列，两段之间**无缝**
（重放结束时的 seq 就是队列的起点，不会漏也不会重）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from ..ports.state import StateStorePort
from .events import EventRecord

# 单个订阅者的队列上限。慢客户端不该拖垮生产者——
# 满了就丢掉最旧的：它重连时会用 Last-Event-ID 从库里补齐，不会真的丢数据。
_SUBSCRIBER_QUEUE = 256


class EventBus:
    def __init__(self, store: StateStorePort) -> None:
        self._store = store
        self._subs: dict[str, set[asyncio.Queue[EventRecord]]] = {}

    # ------------------------------------------------------------------
    async def emit(
        self,
        task_id: str,
        type: str,
        data: dict | None = None,
        *,
        subtask_id: str | None = None,
    ) -> EventRecord:
        """落库，然后扇出。顺序不能反——见模块 docstring。"""
        rec = await self._store.append_event(task_id, type, data, subtask_id=subtask_id)
        for q in list(self._subs.get(task_id, ())):
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                # 丢最旧的再放新的：重放能补齐，但"卡住不动"补不回来
                try:
                    q.get_nowait()
                    q.put_nowait(rec)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return rec

    # ------------------------------------------------------------------
    def subscribe(self, task_id: str) -> asyncio.Queue[EventRecord]:
        q: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE)
        self._subs.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue[EventRecord]) -> None:
        subs = self._subs.get(task_id)
        if not subs:
            return
        subs.discard(q)
        if not subs:
            self._subs.pop(task_id, None)

    def subscriber_count(self, task_id: str) -> int:
        return len(self._subs.get(task_id, ()))

    # ------------------------------------------------------------------
    async def replay(
        self, task_id: str, *, since_seq: int = 0, limit: int | None = None
    ) -> list[EventRecord]:
        return await self._store.read_events(task_id, since_seq=since_seq, limit=limit)

    async def stream(
        self,
        task_id: str,
        *,
        since_seq: int = 0,
        heartbeat_s: float | None = None,
        is_done: Callable[[], bool] | None = None,
    ) -> AsyncIterator[EventRecord | None]:
        """重放 + 实时跟进。

        产出 ``None`` 表示该发一次心跳——**这正是它与"任务没进展"的区别**。
        没有心跳，客户端无法分辨"链路被 NAT 悄悄回收了"和"任务确实还没到下一步"。

        ``is_done`` 给一个无参可调用，返回真时结束流（已经到终态且没有新事件）。
        """
        queue = self.subscribe(task_id)
        try:
            for rec in await self.replay(task_id, since_seq=since_seq):
                yield rec
                since_seq = rec.seq
            while True:
                try:
                    if heartbeat_s is None:
                        rec = await queue.get()
                        yield rec
                    else:
                        rec = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                        yield rec
                except TimeoutError:
                    yield None
                if is_done is not None and is_done() and queue.empty():
                    return
        finally:
            self.unsubscribe(task_id, queue)


__all__ = ["EventBus"]
