"""Handler 端口：能力插件唯一需要实现的契约。

调度层与 handler 的分界一句话：

> **调度层拥有决策与编排；handler 拥有领域事实与领域动作。**

调度层永远不知道"票据""科目""账本"是什么。handler 永远不命名模型、
不读全局配置、不自定并发、不假设自己会被恰好调用一次。

唯一可执行面是 ``execute_tool``。重试、取消、超时、退避、档位升级、并发控制、
成本记账、事件上报——全部在调度层，handler 只实现"一次调用该做的工作"。

结果类型（``ToolResult`` 等）定义在 ``core/execution.py``，这里转发出来，
好让 handler 只 import 一个模块。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..core.execution import ConfirmationRequest, ToolFailure, ToolResult
from ..core.registry import HandlerManifest

if TYPE_CHECKING:
    from ..core.context import DispatchContext


@runtime_checkable
class CapabilityHandler(Protocol):
    """一个领域插件。

    ``manifest`` 是**声明**（能力、工具、流程、配置 schema），注册时校验并 fail-fast；
    ``execute_tool`` 是唯一可执行面。声明与实现分开，是因为调度层只依赖声明——
    它据此做集合校验、给评估器一份能力目录、给守卫一份工具白名单，
    而**一次都不需要知道实现长什么样**。
    """

    manifest: HandlerManifest

    async def execute_tool(self, tool_name: str, args: dict, ctx: DispatchContext) -> ToolResult: ...

    async def health(self) -> str:
        """``closed`` / ``half_open`` / ``open``。影响是否还把任务派给它。"""
        ...


def manifest_of(handler: CapabilityHandler) -> HandlerManifest:
    return handler.manifest


def as_dict(handler: CapabilityHandler) -> dict[str, Any]:
    """自省用的投影。"""
    m = handler.manifest
    return {"handler_id": m.handler_id, "version": m.version, "tools": [t.name for t in m.tools]}


__all__ = [
    "CapabilityHandler", "ConfirmationRequest", "ToolFailure", "ToolResult",
    "as_dict", "manifest_of",
]
