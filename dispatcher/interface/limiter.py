"""SSE 连接闸：进程内计数 + 可重复释放的租约。

为什么需要它：``GET /v1/tasks/{id}/events`` 每个订阅占一个连接、一个生成器协程，
外加一个轮询 watcher。此前没有任何上限——一千个订阅就是一千条长连接加一千个轮询
任务，而它们全都对着同一个进程内存里的状态存储。

**为什么不是 ``asyncio.Semaphore``**：``await sem.acquire()`` 会把请求**挂住**
而不是拒绝它，于是"超限"表现为连接与协程持续堆积，客户端看到的是一次很慢的成功
（或者干脆超时）。这里的语义是"满了就让客户端稍后重试"，需要的是"试一次，拿不到
就返回 None"，那不是 Semaphore 的形状。

计数只在事件循环线程里增减，中间没有 ``await``，因此不需要锁。
"""

from __future__ import annotations


class ConnectionLease:
    """一次占用的句柄。

    ``release()`` **幂等**：释放点有两个——生成器的 ``finally``（正常结束、
    客户端断开、服务端取消都走它）与端点在构造响应失败时。两处都释放，
    谁先到谁生效；少一次幂等保护，计数就会被减两次。
    """

    __slots__ = ("_limiter", "_released")

    def __init__(self, limiter: ConnectionLimiter) -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter._active -= 1


class ConnectionLimiter:
    """按上限发放租约。``limit <= 0`` 表示不限制。"""

    def __init__(self, *, limit: int) -> None:
        self._limit = limit
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    def acquire(self) -> ConnectionLease | None:
        """占用一个名额；满了返回 ``None``（调用方据此回 429）。

        不限制时仍然发租约：调用方的释放路径只有一条，不该因为配置不同而分叉。
        """
        if self._limit > 0 and self._active >= self._limit:
            return None
        self._active += 1
        return ConnectionLease(self)


__all__ = ["ConnectionLease", "ConnectionLimiter"]
