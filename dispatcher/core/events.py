"""事件与事件日志。

事件日志是**状态的真源**，不只是给前端看的流水。理由有两条，都很具体：

1. **断线重放。** 手机端退到后台、网络切换、锁屏都会断连，而任务仍在服务端继续。
   客户端重连时用 ``Last-Event-ID`` 补齐缺失事件即可重建完整视图。
   这要求事件持久、带单调 ``seq``。现有 ``duowei-ai`` 的 SSE 是即发即弃、
   只喂活连接——断线即丢失整轮运行，那个做法不能复制。
2. **可观测与归因。** 04 自进化要回答"它当时为什么那样做"，
   而这个问题只有完整的过程记录能回答。

``seq`` 在**任务内**单调递增，由存储层保证。它是重放游标，因此不能有洞、不能回退。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# 事件类型是**结构性**词表：它对应实现里真实存在的分支，改它等于改契约。
# 与 schemas/event.json 的枚举一一对应。
EventType = Literal[
    "task.created",
    "profile.ready",
    "route.decided",
    "plan.ready",
    "subtask.started",
    "subtask.progress",
    "subtask.completed",
    "subtask.retrying",
    "subtask.failed",
    "agent.round",
    "agent.review",
    "task.escalated",
    "token",
    "budget.warning",
    "budget.exceeded",
    "clarification.needed",
    "task.completed",
    "task.failed",
    "task.cancelled",
    "error",
    "heartbeat",
]

TERMINAL_EVENTS: frozenset[str] = frozenset(
    {"task.completed", "task.failed", "task.cancelled"}
)


class EventRecord(BaseModel):
    """一条事件。对应 ``schemas/event.json``。"""

    model_config = ConfigDict(extra="forbid")

    seq: int
    task_id: str
    type: str
    ts: datetime
    subtask_id: str | None = None
    data: dict[str, Any]


def new_event(
    *,
    seq: int,
    task_id: str,
    type: EventType | str,
    data: dict[str, Any] | None = None,
    subtask_id: str | None = None,
    ts: datetime | None = None,
) -> EventRecord:
    return EventRecord(
        seq=seq,
        task_id=task_id,
        type=type,
        ts=ts or datetime.now(UTC),
        subtask_id=subtask_id,
        data=data or {},
    )


def to_sse(record: EventRecord) -> str:
    """渲染成一帧 SSE。

    格式遵循规范：``event:`` / ``id:`` / ``data:`` 三行加一个空行结尾。
    ``id`` 就是 ``seq`` —— 客户端把它作为 ``Last-Event-ID`` 回传即可重放。
    """
    import json

    payload = json.dumps(record.data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {record.type}\nid: {record.seq}\ndata: {payload}\n\n"


def to_sse_heartbeat(seq: int) -> str:
    """保活帧。

    没有它，手机过 NAT 之后连接会被静默回收，而客户端看不出区别——
    它只是"再也不收到事件了"。心跳让"链路断了"和"任务没进展"变得可区分。
    """
    return f"event: heartbeat\nid: {seq}\ndata: {{}}\n\n"


__all__ = ["EventRecord", "EventType", "TERMINAL_EVENTS", "new_event", "to_sse", "to_sse_heartbeat"]
