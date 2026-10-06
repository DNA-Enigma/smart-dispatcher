"""记账领域的存储端口。

**它属于领域，不属于调度层**——所以它住在 ``handlers/bookkeeping/`` 里，
而不是 ``dispatcher/ports/``。调度层不知道"账本"是什么；把账本端口放进调度层，
等于让调度层知道了领域概念，接缝就漏了。

**端口只搬运不解释。** 条目是 ``dict``，schema 由消费端定——
你自己的应用实现这个端口、把真实数据库接进去即可，
调度层与这个参考实现都不对账本字段做任何假设。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LedgerPort(Protocol):
    async def append(self, entry: dict, *, idempotency_token: str) -> tuple[dict, bool]:
        """写入一条账目，返回 ``(落库后的条目, 是否命中幂等)``。

        ``idempotency_token`` 由调度层按 ``(task_id, subtask_id)`` 派生且**稳定**，
        因此重试拿到的是同一个值。实现据此做到"重试不会重复入账"——
        这件事不该由 handler 自己想办法。
        """
        ...

    async def find_by_token(self, token: str) -> dict | None: ...

    async def recent(self, *, limit: int) -> list[dict]:
        """最近的账目，用于查重与归类参考。"""
        ...


class InMemoryLedger:
    """参考实现：进程内存储。

    它存在的意义是让这个 handler **可以脱离你的应用单独跑起来**（测试、演示、
    本地验证）。生产环境请实现 ``LedgerPort`` 接上真实数据库——
    参考实现不落盘，重启即失，而账目显然不该如此。
    """

    def __init__(self) -> None:
        self._entries: list[dict] = []

    async def append(self, entry: dict, *, idempotency_token: str) -> tuple[dict, bool]:
        for e in self._entries:
            if e.get("_token") == idempotency_token:
                return e, True
        stored = {**entry, "id": f"e_{len(self._entries) + 1}", "_token": idempotency_token}
        self._entries.append(stored)
        return stored, False

    async def find_by_token(self, token: str) -> dict | None:
        return next((e for e in self._entries if e.get("_token") == token), None)

    async def recent(self, *, limit: int) -> list[dict]:
        return list(reversed(self._entries))[:limit]

    # -- 供参考实现自省 --------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["InMemoryLedger", "LedgerPort"]
