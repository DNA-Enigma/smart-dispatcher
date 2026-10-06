"""handler 的公共外壳。

放在领域目录里而不是调度层里：它只依赖 ``dispatcher`` 的**端口**（ToolResult、
DispatchContext 的形状），不依赖调度层的任何实现。这正是"插件"该有的依赖方向。
"""

from __future__ import annotations

from typing import Any

from dispatcher.core.execution import ToolResult
from dispatcher.core.registry import HandlerManifest


class HandlerBase:
    """声明与实现的公共部分。

    ``manifest`` 来自 ``handler.yaml``（由加载器读入后注入），**不在代码里重复写一遍**：
    ``HandlerRegistry.bind`` 会逐字段校验两者一致，复制一份只会多一个会漂移的地方。
    """

    def __init__(self, manifest: HandlerManifest) -> None:
        self.manifest = manifest

    async def health(self) -> str:
        return "closed"

    def _declared(self, tool: str) -> bool:
        return tool in {t.name for t in self.manifest.tools}

    async def execute_tool(self, tool_name: str, args: dict, ctx: Any) -> ToolResult:
        if not self._declared(tool_name):
            return ToolResult.fail(
                "tool_not_declared",
                f"{self.manifest.handler_id} 未声明 {tool_name}",
                retryable=False,
            )
        fn = getattr(self, f"tool_{tool_name}", None)
        if fn is None:
            return ToolResult.fail("handler_error", f"{tool_name} 未实现", retryable=False)
        try:
            return await fn(args, ctx)
        except Exception as e:
            # 未包装的异常一律变成有类型的失败：让它冒泡出去会变成一个
            # "任务挂了"的事件，而事件流里看不出哪一步、为什么。
            return ToolResult.fail("handler_error", str(e), retryable=False)
