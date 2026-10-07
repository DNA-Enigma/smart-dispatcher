"""直接调 ASGI 的小工具：精确控制"读没读请求体"与"客户端什么时候断开"。

``httpx.ASGITransport`` 会把整个响应体收完才返回，因此它做不了两件事：

* **断言"超限的请求一个字节都没被读"**——需要自己数 ``receive`` 被调了几次，
  而那条正是"先读后判"与"先判后读"唯一可观测的差别；
* **让一个 SSE 连接真的挂着**——httpx 会一直等到流结束（终态任务要等一次心跳，
  活着的任务永远不结束）。

``call_app`` 把这两件事交回给调用方：``receive`` 的行为由参数决定，``send`` 收到的
消息原样留在结果里。注意 ``hang_after=True`` 时**永不给** ``http.disconnect``——
给了的话 Starlette 会立刻把响应任务取消，长连接就测不成了。
"""

from __future__ import annotations

import asyncio
from typing import Any


class AsgiResult:
    def __init__(self, messages: list[dict], receive_calls: int) -> None:
        self.messages = messages
        self.receive_calls = receive_calls

    @property
    def status(self) -> int:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return int(m["status"])
        raise AssertionError(f"没有 http.response.start：{[m['type'] for m in self.messages]}")

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )

    @property
    def headers(self) -> dict[str, str]:
        for m in self.messages:
            if m["type"] == "http.response.start":
                return {k.decode(): v.decode() for k, v in m.get("headers", [])}
        return {}


def make_scope(
    *,
    method: str = "POST",
    path: str = "/v1/tasks",
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": list(headers or []),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def call_app(
    app, scope: dict, *, chunks: list[bytes] | None = None, hang_after: bool = False
) -> AsgiResult:
    """把 ``chunks`` 依次作为 ``http.request`` 交给应用，返回它发出的消息。"""
    chunks = list(chunks or [])
    messages: list[dict] = []
    calls = {"n": 0}
    blocker = asyncio.Event()

    async def receive() -> dict:
        i = calls["n"]
        calls["n"] += 1
        if i < len(chunks):
            return {
                "type": "http.request",
                "body": chunks[i],
                "more_body": i < len(chunks) - 1,
            }
        if hang_after:
            await blocker.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)

    await app(scope, receive, send)
    return AsgiResult(messages, calls["n"])


__all__ = ["AsgiResult", "call_app", "make_scope"]
