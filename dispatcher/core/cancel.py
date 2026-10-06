"""取消传播。

**现有实现里完全没有这个东西。** ``duowei-ai`` 的 ``asyncio.gather`` 在客户端断连后
仍然继续跑——已经没人要的结果还在烧钱和占用并发槽位。对一个手机端应用尤其明显：
用户退出页面，任务照跑。

设计上只做一件事：一个可以被观察的布尔标志，加上一组等待它的回调。
不引入自定义异常层级——``asyncio.CancelledError`` 已经是标准词汇，
自己再造一个只会让调用方为难。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


class Cancelled(Exception):
    """任务被显式取消。

    与 ``asyncio.CancelledError`` 区分开：后者表示"这个协程被取消了"（含超时、
    父任务中止），前者表示"业务上决定不再继续"。两者在日志与指标里的含义不同，
    混为一谈会让"为什么停了"这个问题没法回答。
    """


class CancellationToken:
    """一次可观察、可等待的取消信号。

    典型用法::

        token = CancellationToken()
        # 某处：POST /tasks/{id}/cancel
        token.cancel("client_requested")
        # 执行器内部
        token.raise_if_cancelled()
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._waiters: list[asyncio.Future[None]] = []
        self._callbacks: list[Callable[[str], Any]] = []

    # ------------------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "unspecified") -> None:
        """幂等：重复取消只保留第一个原因。第一个原因通常最有信息量。"""
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()
        for cb in self._callbacks:
            cb(reason)
        self._callbacks.clear()

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled(f"任务已取消：{self._reason}")

    async def wait(self) -> None:
        """等到被取消。已经在执行中的节点用它配合 ``asyncio.wait`` 做竞速。"""
        if self._cancelled:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        await fut

    def on_cancel(self, cb: Callable[[str], Any]) -> None:
        """注册取消回调。已取消时立即调用——避免"注册晚了一步就永远收不到通知"。"""
        if self._cancelled:
            cb(self._reason or "unspecified")
        else:
            self._callbacks.append(cb)


__all__ = ["CancellationToken", "Cancelled"]
